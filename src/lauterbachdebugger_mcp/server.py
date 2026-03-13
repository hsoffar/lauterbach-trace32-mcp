import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import mcp.types as types
import lauterbach.trace32.rcl as t32
from lauterbach.trace32.rcl import Breakpoint
from lauterbach.trace32.rcl import (
    ApiConnectionError as T32ApiConnectionError,
    BreakpointError as T32BreakpointError,
    CommandError as T32CommandError,
    FunctionError as T32FunctionError,
    MemoryReadAccessError as T32MemoryReadAccessError,
    MemoryWriteAccessError as T32MemoryWriteAccessError,
    RegisterError as T32RegisterError,
    SymbolError as T32SymbolError,
    VariableError as T32VariableError,
)
from mcp.server import Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global debugger connection (one per server process)
# ---------------------------------------------------------------------------
_dbg: Optional[t32.Debugger] = None

# Connection defaults (set by serve(), used by connect tool as fallbacks)
_conn_defaults: dict[str, Any] = {
    "host": "localhost",
    "port": 20000,
    "protocol": "TCP",
    "timeout": 60.0,
}

# Background auto-connect task (if running)
_auto_connect_task: Optional[asyncio.Task] = None

# Server configuration (set by serve())
_config: dict[str, Any] = {
    "t32_dir": "~/t32",
    "hints": None,
}

# ---------------------------------------------------------------------------
# MCP server instructions - teaches the LLM TRACE32 concepts
# ---------------------------------------------------------------------------
INSTRUCTIONS = """\
You are connected to a Lauterbach TRACE32 debugger via its Remote API.

## Connection
This server starts WITHOUT an active TRACE32 connection. You MUST call the
`connect` tool before using any other tool. The connect tool uses defaults
from the CLI (host/port) when called without arguments.

## Target States
The target CPU has four states (returned by `get_state`):
  0 = stopped/down - target is not accessible
  1 = running - target is executing code
  2 = halted - target is stopped at a breakpoint or after a step
  3 = background_running - target is running with background debug access

IMPORTANT: The target MUST be halted (state 2) to read registers, memory,
variables, or to step. If state is 1 (running), call `break_` first.

## Address Classes
TRACE32 uses access class prefixes for addresses:
  D:0x... = Data access
  P:0x... = Program access
  Plain 0x... = Default access class
Use the appropriate prefix when reading/writing memory.

## Debug Symbols
Debug symbols must be loaded in TRACE32 for variable/symbol queries to work.
Load symbols with: run_command("Data.LOAD.ELF <file>") or via a CMM script.

## Common Workflows

### Halt-Inspect-Resume
1. get_state - check if running
2. break_ - halt if running
3. Inspect: read registers, memory, variables, get_context, backtrace
4. go - resume execution

### After Any Step/Break
Execution control tools (step, break_, step_over, etc.) automatically
return PC and, when debug symbols are loaded, function name, source
file, and source line.  No separate call is needed for basic
situational awareness.  Use `get_context` only when you need the
full snapshot (SP, LR, CPU name, etc.).

### Enriched Responses
- Breakpoint tools include symbol resolution (function, source location)
  at the breakpoint address.
- `read_memory` includes an `ascii` field alongside hex and bytes.

### Set Breakpoint by Name
Use `set_breakpoint_at_symbol` with a function name like "main".

### Read C Strings
Use `read_string` tool with the string address.

## Useful PRACTICE Functions (for evaluate_function)
  Register(PC) - read PC register
  STATE.RUN() - check if target is running (TRUE/FALSE)
  sYmbol.FUNCTION(addr) - function name at address
  sYmbol.SOURCEFILE(addr) - source file at address
  sYmbol.SOURCELINE(addr) - source line at address
  Var.VALUE(expr) - evaluate C expression
  Data.STRing(D:addr) - read null-terminated string
  CPU() - current CPU name
  CPUFAMILY() - CPU family name
  SYSTEM.BIGENDIAN() - TRUE if big-endian target

"""


def _load_hints(hints_path: str) -> str:
    """Load user hint content from a file or directory of .md files.

    If *hints_path* is a regular file, its content is returned directly.
    If it is a directory, all ``*.md`` files found inside (sorted by name)
    are concatenated and returned.  Returns an empty string when nothing
    could be loaded.
    """
    p = Path(os.path.expanduser(hints_path))
    parts: list[str] = []
    if p.is_file():
        try:
            parts.append(p.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("Failed to read hints file %s: %s", p, exc)
    elif p.is_dir():
        for md_file in sorted(p.glob("*.md")):
            try:
                parts.append(md_file.read_text(encoding="utf-8"))
            except OSError as exc:
                logger.warning("Failed to read hints file %s: %s",
                               md_file, exc)
    else:
        logger.warning("Hints path does not exist: %s", p)
    return "\n".join(parts)


def _build_instructions(hints: Optional[str] = None) -> str:
    """Return the full server instructions, optionally with user hints."""
    if not hints:
        return INSTRUCTIONS
    hints_content = _load_hints(hints)
    if not hints_content:
        return INSTRUCTIONS
    return INSTRUCTIONS + "\n## User Hints\n\n" + hints_content


server = Server("lauterbachdebugger-mcp", instructions=INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Shared helper functions
# ---------------------------------------------------------------------------
def _resolve_symbol_at(dbg: t32.Debugger, addr: str) -> dict[str, Any]:
    """Resolve function/source info at an address. Graceful on failure."""
    info: dict[str, Any] = {}
    for key, expr in (
        ("function", "sYmbol.FUNCTION(D:{addr})"),
        ("source_file", "sYmbol.SOURCEFILE(D:{addr})"),
        ("source_line", "sYmbol.SOURCELINE(D:{addr})"),
    ):
        try:
            info[key] = dbg.fnc(expr.format(addr=addr))
        except Exception:
            info[key] = None
    return info


def _get_brief_context(dbg: t32.Debugger) -> dict[str, Any]:
    """Read PC and resolve symbol info. Returns partial data on failure."""
    ctx: dict[str, Any] = {}
    try:
        pc_val = dbg.fnc("Register(PC)")
        ctx["pc"] = pc_val
    except Exception:
        ctx["pc"] = None
    if ctx["pc"] is not None:
        ctx.update(_resolve_symbol_at(dbg, str(ctx["pc"])))
    else:
        ctx.update(function=None, source_file=None, source_line=None)
    return ctx


def _format_hex_dump(data: bytes, base_addr: int) -> str:
    """Format bytes as a traditional hex+ASCII dump, 16 bytes per line."""
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base_addr + offset:08x}  {hex_part:<48s}  |{ascii_part}|")
    return "\n".join(lines)


def _require_connection() -> t32.Debugger:
    """Return the active Debugger or raise if not connected."""
    if _dbg is None:
        raise RuntimeError(
            "Not connected to a TRACE32 debugger. Call the 'connect' tool first."
        )
    return _dbg


def _ok(data: Any) -> list[types.TextContent]:
    """Wrap a result in a TextContent list (JSON-serialised if not a string)."""
    text = data if isinstance(data, str) else json.dumps(data, default=str)
    return [types.TextContent(type="text", text=text)]


# Mapping from pyrcl exception types to actionable suggestions.
# Used by _error() to provide context-appropriate guidance.
_EXCEPTION_SUGGESTIONS: dict[type, str] = {
    T32ApiConnectionError: "Connection lost. Try calling 'connect' again.",
    T32CommandError: "Check command syntax. Use 'run_command' help for examples.",
    T32FunctionError: (
        "Function may not exist or target not halted. "
        "Halt with 'break_' first."
    ),
    T32MemoryReadAccessError: (
        "Cannot read memory. Halt target with 'break_' "
        "and check address/access class."
    ),
    T32MemoryWriteAccessError: (
        "Cannot write memory. Region may be read-only or protected."
    ),
    T32VariableError: (
        "Variable access failed. Ensure debug symbols are loaded "
        "and target is halted."
    ),
    T32SymbolError: "Symbol not found. Ensure debug symbols are loaded.",
    T32RegisterError: (
        "Register access failed. Halt target with 'break_' "
        "and check register name."
    ),
    T32BreakpointError: (
        "Breakpoint operation failed. Check address and breakpoint type."
    ),
}


def _error(exc: Exception, suggestion: Optional[str] = None) -> list[types.TextContent]:
    """Return a structured error response with actionable suggestion.

    Looks up the exception type in _EXCEPTION_SUGGESTIONS for a known
    suggestion, falling back to the explicit *suggestion* parameter.
    For RuntimeError, checks if the message indicates a missing
    connection.

    To propagate isError=True to the MCP CallToolResult, we raise a
    ValueError whose message is the JSON-serialised error dict.  The
    MCP framework will catch it and set isError=True on the result.
    """
    if suggestion is None:
        suggestion = _EXCEPTION_SUGGESTIONS.get(type(exc))
    if suggestion is None and isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "not connected" in msg or "connect" in msg:
            suggestion = "Call the 'connect' tool first."
    error_data: dict[str, Any] = {
        "error": type(exc).__name__,
        "message": str(exc),
    }
    if suggestion:
        error_data["suggestion"] = suggestion
    raise ValueError(json.dumps(error_data))


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        # ── Connection ────────────────────────────────────────────────────
        types.Tool(
            name="connect",
            description=(
                "Connect to a Lauterbach TRACE32 debugger via its Remote API. "
                "Must be called before any other tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Hostname or IP address of the TRACE32 host. Default: localhost",
                        "default": "localhost",
                    },
                    "port": {
                        "type": "integer",
                        "description": "Remote API TCP/UDP port configured in TRACE32. Default: 20000",
                        "default": 20000,
                    },
                    "protocol": {
                        "type": "string",
                        "enum": ["TCP", "UDP"],
                        "description": "Transport protocol. Default: TCP",
                        "default": "TCP",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Connection timeout in seconds. Default: 60",
                        "default": 60.0,
                    },
                },
            },
        ),
        types.Tool(
            name="disconnect",
            description="Disconnect from the TRACE32 debugger and release resources.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="ping",
            description="Ping the TRACE32 debugger to verify the connection is alive.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_state",
            description=(
                "Get the current hardware/debug state from TRACE32. "
                "Returns an integer state code (0=stopped, 1=running, 2=halted, 3=background_running)."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_message",
            description="Get the last message text shown in the TRACE32 message line.",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── Execution control ─────────────────────────────────────────────
        types.Tool(
            name="go",
            description="Start or resume program execution on the target.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="break_",
            description="Halt (break) program execution on the target.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="step",
            description="Execute a single step (HLL or ASM depending on debug context).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="step_asm",
            description="Execute a single assembly instruction step.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="step_hll",
            description="Execute a single high-level language (source-level) step.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="step_over",
            description="Step over the current function call (Step.Over command).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="go_up",
            description="Run until return from the current function (Go.Up command).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="go_return",
            description="Immediately return from the current function (Go.Return command).",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── Commands & Functions ──────────────────────────────────────────
        types.Tool(
            name="run_command",
            description=(
                "Execute any TRACE32 PRACTICE command string "
                "(e.g. 'SYStem.Up', 'Data.dump 0x20000000', 'Register.Set PC 0x0')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "TRACE32 PRACTICE command string.",
                    },
                },
                "required": ["command"],
            },
        ),
        types.Tool(
            name="evaluate_function",
            description=(
                "Evaluate a TRACE32 PRACTICE function expression and return the result "
                "(e.g. 'STATE.RUN()', 'Register(PC)', 'Var.VALUE(myVar)')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "function": {
                        "type": "string",
                        "description": "TRACE32 PRACTICE function expression.",
                    },
                },
                "required": ["function"],
            },
        ),
        types.Tool(
            name="run_practice_script",
            description=(
                "Run a TRACE32 PRACTICE CMM script file (blocking). "
                "Pass the script path and any arguments as a single string."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "script_path": {
                        "type": "string",
                        "description": "CMM script path (and optional arguments), e.g. 'C:/scripts/init.cmm'.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds. Omit to wait indefinitely.",
                    },
                },
                "required": ["script_path"],
            },
        ),

        # ── Memory ────────────────────────────────────────────────────────
        types.Tool(
            name="read_memory",
            description=(
                "Read raw bytes from target memory. "
                "Returns the data as a hex string and a byte array."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Target address, e.g. '0x20000000' or 'D:0x1000' (with access class).",
                    },
                    "length": {
                        "type": "integer",
                        "description": "Number of bytes to read.",
                    },
                },
                "required": ["address", "length"],
            },
        ),
        types.Tool(
            name="read_memory_typed",
            description=(
                "Read a typed scalar value from target memory. "
                "Supported types: int8, uint8, int16, uint16, int32, uint32, int64, uint64, float, double."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Target address, e.g. '0x20000000'.",
                    },
                    "type": {
                        "type": "string",
                        "enum": [
                            "int8", "uint8",
                            "int16", "uint16",
                            "int32", "uint32",
                            "int64", "uint64",
                            "float", "double",
                        ],
                        "description": "Data type to read.",
                    },
                    "byteorder": {
                        "type": "string",
                        "enum": ["little", "big"],
                        "description": "Byte order. Default: little",
                        "default": "little",
                    },
                },
                "required": ["address", "type"],
            },
        ),
        types.Tool(
            name="write_memory_typed",
            description=(
                "Write a typed scalar value to target memory. "
                "Supported types: int8, uint8, int16, uint16, int32, uint32, int64, uint64, float, double."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Target address, e.g. '0x20000000'.",
                    },
                    "type": {
                        "type": "string",
                        "enum": [
                            "int8", "uint8",
                            "int16", "uint16",
                            "int32", "uint32",
                            "int64", "uint64",
                            "float", "double",
                        ],
                        "description": "Data type to write.",
                    },
                    "value": {
                        "description": "Numeric value to write (int for integer types, float for float/double).",
                    },
                    "byteorder": {
                        "type": "string",
                        "enum": ["little", "big"],
                        "description": "Byte order. Default: little",
                        "default": "little",
                    },
                },
                "required": ["address", "type", "value"],
            },
        ),

        # ── Registers ─────────────────────────────────────────────────────
        types.Tool(
            name="read_register",
            description="Read a single CPU/FPU/VPU register by name (e.g. 'PC', 'SP', 'R0').",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Register name, e.g. 'PC', 'SP', 'LR', 'R0'.",
                    },
                    "core": {
                        "type": "integer",
                        "description": "Target core number (optional, for multi-core targets).",
                    },
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="read_all_registers",
            description="Read all registers (optionally filtered by core and/or unit type: CPU, FPU, VPU).",
            inputSchema={
                "type": "object",
                "properties": {
                    "core": {
                        "type": "integer",
                        "description": "Target core number (optional).",
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["CPU", "FPU", "VPU"],
                        "description": "Register unit type filter (optional).",
                    },
                },
            },
        ),
        types.Tool(
            name="write_register",
            description="Write a value to a CPU/FPU/VPU register by name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Register name, e.g. 'PC', 'R0'.",
                    },
                    "value": {
                        "description": "Integer (for CPU/VPU) or float (for FPU) value to write.",
                    },
                    "core": {
                        "type": "integer",
                        "description": "Target core number (optional).",
                    },
                },
                "required": ["name", "value"],
            },
        ),

        # ── Breakpoints ───────────────────────────────────────────────────
        types.Tool(
            name="set_breakpoint",
            description="Set a breakpoint at a target address.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Target address, e.g. '0x08000100'.",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["PROGRAM", "READ", "WRITE", "RW"],
                        "description": "Breakpoint type. Default: PROGRAM",
                        "default": "PROGRAM",
                    },
                    "impl": {
                        "type": "string",
                        "enum": ["AUTO", "SOFT", "ONCHIP", "HARD", "MARK"],
                        "description": "Breakpoint implementation. Default: AUTO",
                        "default": "AUTO",
                    },
                    "size": {
                        "type": "integer",
                        "description": "Breakpoint size in bytes (optional).",
                    },
                    "core": {
                        "type": "integer",
                        "description": "Target core (optional).",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "Whether the breakpoint is enabled. Default: true",
                        "default": True,
                    },
                },
                "required": ["address"],
            },
        ),
        types.Tool(
            name="list_breakpoints",
            description="List all breakpoints currently set in TRACE32.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="delete_breakpoint",
            description="Delete a breakpoint at a given address.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Address of the breakpoint to delete.",
                    },
                },
                "required": ["address"],
            },
        ),

        # ── Variables ─────────────────────────────────────────────────────
        types.Tool(
            name="read_variable",
            description=(
                "Read a program variable value by its source-level name. "
                "Requires debug symbols to be loaded in TRACE32."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Variable name as it appears in source, e.g. 'myVar' or 'module\\\\myVar'.",
                    },
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="write_variable",
            description="Write a value to a program variable by its source-level name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Variable name.",
                    },
                    "value": {
                        "description": "Integer or float value to write.",
                    },
                },
                "required": ["name", "value"],
            },
        ),

        # ── Symbols ───────────────────────────────────────────────────────
        types.Tool(
            name="query_symbol_by_name",
            description=(
                "Look up a debug symbol (function, variable, label) by name. "
                "Returns address, size, and path information."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Symbol name, e.g. 'main' or 'myModule\\\\myFunction'.",
                    },
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="query_symbol_by_address",
            description="Look up the debug symbol located at a given target address.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Target address, e.g. '0x08000100'.",
                    },
                },
                "required": ["address"],
            },
        ),

        # ── PRACTICE Macros ───────────────────────────────────────────────
        types.Tool(
            name="get_practice_macro",
            description="Get the current value of a global PRACTICE macro variable.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Macro name (without the leading '&').",
                    },
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="set_practice_macro",
            description="Set the value of a global PRACTICE macro variable.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Macro name (without the leading '&').",
                    },
                    "value": {
                        "type": "string",
                        "description": "Macro value (always a string in PRACTICE).",
                    },
                },
                "required": ["name", "value"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    global _dbg

    try:
        # ── Connection ────────────────────────────────────────────────────
        if name == "connect":
            # Cancel pending auto-connect if still running
            if _auto_connect_task and not _auto_connect_task.done():
                _auto_connect_task.cancel()
                try:
                    await _auto_connect_task
                except (asyncio.CancelledError, Exception):
                    pass
            if _dbg is not None:
                _dbg.disconnect()
                _dbg = None
            _dbg = t32.connect(
                node=arguments.get("node", _conn_defaults["host"]),
                port=str(arguments.get("port", _conn_defaults["port"])),
                protocol=arguments.get("protocol", _conn_defaults["protocol"]),
                timeout=float(arguments.get("timeout", _conn_defaults["timeout"])),
            )
            return _ok("Connected to TRACE32 debugger.")

        elif name == "disconnect":
            if _dbg is not None:
                _dbg.disconnect()
                _dbg = None
                return _ok("Disconnected from TRACE32 debugger.")
            return _ok("No active connection.")

        elif name == "ping":
            _require_connection().ping()
            return _ok("Ping successful — debugger is reachable.")

        elif name == "get_state":
            raw_state = _require_connection().get_state()
            if isinstance(raw_state, (bytes, bytearray)):
                state = int.from_bytes(raw_state, "little")
            elif raw_state is not None:
                state = int(raw_state)
            else:
                state = 0
            STATE_NAMES = {0: "stopped", 1: "running", 2: "halted", 3: "background_running"}
            return _ok({"state": state, "state_name": STATE_NAMES.get(state, f"state_{state}")})

        elif name == "get_message":
            msg = _require_connection().get_message()
            return _ok({"text": msg.text, "type": msg.type})

        # ── Execution control ─────────────────────────────────────────────
        elif name == "go":
            _require_connection().go()
            return _ok({"action": "go", "status": "running"})

        elif name == "break_":
            dbg = _require_connection()
            dbg.break_()
            result: dict[str, Any] = {"action": "break", "status": "halted"}
            result.update(_get_brief_context(dbg))
            return _ok(result)

        elif name in ("step", "step_asm", "step_hll", "step_over",
                       "go_up", "go_return"):
            dbg = _require_connection()
            getattr(dbg, name)()
            result = {"action": name, "status": "completed"}
            result.update(_get_brief_context(dbg))
            return _ok(result)

        # ── Commands & Functions ──────────────────────────────────────────
        elif name == "run_command":
            _require_connection().cmd(arguments["command"])
            return _ok(f"Command executed: {arguments['command']}")

        elif name == "evaluate_function":
            result = _require_connection().fnc(arguments["function"])
            return _ok({"function": arguments["function"], "result": result})

        elif name == "run_practice_script":
            script_timeout = arguments.get("timeout")
            if script_timeout is not None:
                script_timeout = float(script_timeout)
            _require_connection().cmm(
                arguments["script_path"],
                timeout=script_timeout,  # type: ignore[arg-type]  # pyrcl accepts None
            )
            return _ok(f"Script completed: {arguments['script_path']}")

        # ── Memory ────────────────────────────────────────────────────────
        elif name == "read_memory":
            dbg = _require_connection()
            addr = dbg.address.from_string(arguments["address"])
            data = dbg.memory.read(addr, length=int(arguments["length"]))
            ascii_repr = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
            return _ok({
                "address": arguments["address"],
                "length": len(data),
                "hex": data.hex(),
                "bytes": list(data),
                "ascii": ascii_repr,
            })

        elif name == "read_memory_typed":
            dbg = _require_connection()
            addr = dbg.address.from_string(arguments["address"])
            dtype = arguments["type"]
            byteorder = arguments.get("byteorder", "little")
            readers = {
                "int8":   lambda: dbg.memory.read_int8(addr),
                "uint8":  lambda: dbg.memory.read_uint8(addr),
                "int16":  lambda: dbg.memory.read_int16(addr, byteorder=byteorder),
                "uint16": lambda: dbg.memory.read_uint16(addr, byteorder=byteorder),
                "int32":  lambda: dbg.memory.read_int32(addr, byteorder=byteorder),
                "uint32": lambda: dbg.memory.read_uint32(addr, byteorder=byteorder),
                "int64":  lambda: dbg.memory.read_int64(addr, byteorder=byteorder),
                "uint64": lambda: dbg.memory.read_uint64(addr, byteorder=byteorder),
                "float":  lambda: dbg.memory.read_float(addr, byteorder=byteorder),
                "double": lambda: dbg.memory.read_double(addr, byteorder=byteorder),
            }
            value = readers[dtype]()
            return _ok({"address": arguments["address"], "type": dtype, "value": value})

        elif name == "write_memory_typed":
            dbg = _require_connection()
            addr = dbg.address.from_string(arguments["address"])
            dtype = arguments["type"]
            raw = arguments["value"]
            byteorder = arguments.get("byteorder", "little")
            writers = {
                "int8":   lambda: dbg.memory.write_int8(addr, int(raw)),
                "uint8":  lambda: dbg.memory.write_uint8(addr, int(raw)),
                "int16":  lambda: dbg.memory.write_int16(addr, int(raw), byteorder=byteorder),
                "uint16": lambda: dbg.memory.write_uint16(addr, int(raw), byteorder=byteorder),
                "int32":  lambda: dbg.memory.write_int32(addr, int(raw), byteorder=byteorder),
                "uint32": lambda: dbg.memory.write_uint32(addr, int(raw), byteorder=byteorder),
                "int64":  lambda: dbg.memory.write_int64(addr, int(raw), byteorder=byteorder),
                "uint64": lambda: dbg.memory.write_uint64(addr, int(raw), byteorder=byteorder),
                "float":  lambda: dbg.memory.write_float(addr, float(raw), byteorder=byteorder),
                "double": lambda: dbg.memory.write_double(addr, float(raw), byteorder=byteorder),
            }
            writers[dtype]()
            return _ok({"address": arguments["address"], "type": dtype, "value": raw, "status": "written"})

        # ── Registers ─────────────────────────────────────────────────────
        elif name == "read_register":
            dbg = _require_connection()
            kwargs: dict[str, Any] = {}
            if "core" in arguments:
                kwargs["core"] = int(arguments["core"])
            reg = dbg.register.read(arguments["name"], **kwargs)
            return _ok(reg.to_dict())

        elif name == "read_all_registers":
            dbg = _require_connection()
            kwargs = {}
            if "core" in arguments:
                kwargs["core"] = int(arguments["core"])
            if "unit" in arguments:
                kwargs["unit"] = arguments["unit"]
            regs = dbg.register.read_all(**kwargs)
            return _ok([r.to_dict() for r in regs])

        elif name == "write_register":
            dbg = _require_connection()
            raw = arguments["value"]
            kwargs = {}
            if "core" in arguments:
                kwargs["core"] = int(arguments["core"])
            value = float(raw) if isinstance(raw, float) else int(raw)
            reg = dbg.register.write(arguments["name"], value, **kwargs)
            return _ok(reg.to_dict())

        # ── Breakpoints ───────────────────────────────────────────────────
        elif name == "set_breakpoint":
            dbg = _require_connection()
            addr = dbg.address.from_string(arguments["address"])
            bp = dbg.breakpoint.set(
                address=addr,
                type_=Breakpoint.Type[arguments.get("type", "PROGRAM")],
                impl=Breakpoint.Impl[arguments.get("impl", "AUTO")],
                size=arguments.get("size"),
                core=arguments.get("core"),
                enabled=arguments.get("enabled", True),
            )
            bp_info: dict[str, Any] = {
                "breakpoint": str(bp),
                "address": arguments["address"],
                "type": arguments.get("type", "PROGRAM"),
                "impl": arguments.get("impl", "AUTO"),
            }
            bp_info.update(_resolve_symbol_at(dbg, arguments["address"]))
            return _ok(bp_info)

        elif name == "list_breakpoints":
            dbg = _require_connection()
            bps = dbg.breakpoint.list()
            bp_list = []
            for bp in bps:
                entry: dict[str, Any] = {"breakpoint": str(bp)}
                try:
                    addr_str = str(bp.address) if hasattr(bp, "address") and bp.address else None
                    if addr_str:
                        entry.update(_resolve_symbol_at(dbg, addr_str))
                except Exception:
                    pass
                bp_list.append(entry)
            return _ok(bp_list)

        elif name == "delete_breakpoint":
            dbg = _require_connection()
            addr = dbg.address.from_string(arguments["address"])
            bp = dbg.breakpoint(address=addr)
            bp.delete()
            return _ok(f"Breakpoint at {arguments['address']} deleted.")

        # ── Variables ─────────────────────────────────────────────────────
        elif name == "read_variable":
            var = _require_connection().variable.read(arguments["name"])
            return _ok(var.to_dict())

        elif name == "write_variable":
            dbg = _require_connection()
            raw = arguments["value"]
            if isinstance(raw, str):
                try:
                    raw = int(raw, 0)
                except ValueError:
                    raw = float(raw)
            var = dbg.variable.write(arguments["name"], raw)
            return _ok(var.to_dict())

        # ── Symbols ───────────────────────────────────────────────────────
        elif name == "query_symbol_by_name":
            sym = _require_connection().symbol.query_by_name(arguments["name"])
            return _ok({
                "name": sym.name,
                "path": sym.path,
                "address": str(sym.address) if sym.address else None,
                "size": sym.size,
            })

        elif name == "query_symbol_by_address":
            dbg = _require_connection()
            addr = dbg.address.from_string(arguments["address"])
            sym = dbg.symbol.query_by_address(addr)
            return _ok({
                "name": sym.name,
                "path": sym.path,
                "address": str(sym.address) if sym.address else None,
                "size": sym.size,
            })

        # ── PRACTICE Macros ───────────────────────────────────────────────
        elif name == "get_practice_macro":
            macro = _require_connection().practice.get_macro(arguments["name"])
            return _ok(macro.to_dict())

        elif name == "set_practice_macro":
            macro = _require_connection().practice.set_macro(
                arguments["name"], arguments["value"]
            )
            return _ok(macro.to_dict())

        else:
            return _ok(f"Unknown tool: '{name}'")

    except Exception as exc:
        return _error(exc)


# ---------------------------------------------------------------------------
# Serve
# ---------------------------------------------------------------------------
async def _try_auto_connect(host: str, port: int, protocol: str,
                            timeout: float) -> None:
    """Background auto-connect attempt.  Sets _dbg on success."""
    global _dbg
    try:
        conn = await asyncio.to_thread(
            t32.connect, node=host, port=str(port), protocol=protocol,
            timeout=timeout,
        )
        # Only set if no explicit connect happened while we were waiting
        if _dbg is None:
            _dbg = conn
            logger.info("Auto-connected to TRACE32 at %s:%s", host, port)
        else:
            # User already connected via 'connect' tool; discard ours
            try:
                conn.disconnect()
            except Exception:
                pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Auto-connect to TRACE32 at %s:%s failed (%s) "
            "-- call the 'connect' tool manually.",
            host, port, exc,
        )


async def serve(
    host: str,
    port: int,
    protocol: str,
    timeout: float,
    *,
    t32_dir: str = "~/t32",
    hints: Optional[str] = None,
    hints_file: Optional[str] = None,
    hints_dir: Optional[str] = None,
) -> None:
    global _auto_connect_task

    # Store connection parameters so the connect tool can use them as defaults
    _conn_defaults.update(host=host, port=port, protocol=protocol, timeout=timeout)

    # Store configuration paths
    _config.update(t32_dir=t32_dir, hints=hints)

    # hints_file / hints_dir kwargs let tests (and future callers) pass a
    # path directly without going through the unified --hints option.
    effective_hints: Optional[str] = hints
    if hints_file is not None:
        effective_hints = hints_file
    elif hints_dir is not None:
        effective_hints = hints_dir

    # Embed user hints into server instructions so the LLM sees them
    # automatically without needing to fetch the trace32://hints resource.
    server.instructions = _build_instructions(effective_hints)

    logger.info(
        "MCP server starting (auto-connecting to TRACE32 at %s:%s)",
        host, port,
    )

    async with stdio_server() as (read_stream, write_stream):
        _auto_connect_task = asyncio.create_task(
            _try_auto_connect(host, port, protocol, timeout)
        )
        try:
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
        finally:
            # Clean up the background task on shutdown
            if _auto_connect_task and not _auto_connect_task.done():
                _auto_connect_task.cancel()
                try:
                    await _auto_connect_task
                except (asyncio.CancelledError, Exception):
                    pass
