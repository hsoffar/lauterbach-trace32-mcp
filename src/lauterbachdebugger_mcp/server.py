import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import mcp.types as types
import lauterbach.trace32.rcl as t32
from pydantic import AnyUrl
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
    "pdf_cache_dir": "~/.cache/lauterbach-t32-mcp",
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

## Composite / High-Level Tools

These tools combine multiple TRACE32 operations into a single call, reducing
round-trips and applying graceful degradation (partial data is returned even
when a sub-operation fails).  Prefer them over assembling equivalent results
from several primitive tools.

### Situational Awareness
- `get_context` - Full CPU snapshot in one call: state, PC, SP, LR, current
  function name, source file, source line, and CPU name.  Use at any halt to
  understand where execution stopped without issuing four separate register and
  symbol queries.
- `get_source_location` - Source file and line for a given address (defaults
  to current PC).  Faster than manually chaining sYmbol.SOURCEFILE and
  sYmbol.SOURCELINE via evaluate_function.
- `backtrace` - Walk the entire call stack with function name and source
  resolution per frame.  Use after a crash or unexpected halt to trace how
  execution arrived at the current point.
- `snapshot` - One-shot: context + backtrace + breakpoint list + system_info.
  Use as the first action when investigating an unknown failure; a single call
  captures everything needed to describe the program state.

### Execution Flow
- `run_until` - Run to an address or symbol, then halt.  Sets a temporary
  breakpoint, resumes execution, and polls until halted or timeout expires.
  Avoids the manual sequence of set_breakpoint + go + polling loop.
- `set_breakpoint_at_symbol` - Set a breakpoint by function or label name
  without a preceding address lookup.  One call instead of
  query_symbol_by_name + set_breakpoint.

### Memory and Data
- `read_string` - Read a null-terminated C string from a memory address.
  Use for char* variables without manually issuing repeated read_memory calls
  to find the null terminator.
- `dump_memory_formatted` - Hex + ASCII dump (like hexdump -C).  Output is
  immediately readable without post-processing raw byte arrays.
- `write_memory` - Write raw bytes expressed as a hex string to target memory.
- `search_memory` - Scan a memory range for a byte pattern in chunks.
  Useful for locating magic constants, stack canaries, or signs of corruption.

### Code Inspection
- `disassemble` - Read raw instruction bytes at an address (defaults to PC).
  Returns hex bytes per instruction-sized chunk.  TRACE32 PRACTICE does not
  expose a disassembly text function via its remote API; use the CPU type
  returned to decode the hex bytes.
- `evaluate_expression` - Evaluate a C/C++ expression and return value, type,
  and hex representation.  Understands struct field access, pointer
  dereferences, and type casts without requiring PRACTICE syntax knowledge.

### System and Symbol Browsing
- `get_system_info` - CPU name, family, and endianness in one call.
  Use at session start to orient the LLM to the target.
- `list_functions` - Count symbols matching a wildcard filter via
  sYmbol.COUNT().  TRACE32 PRACTICE has no indexed symbol iterator; this
  tool reports the match count and advises using query_symbol_by_name for
  individual lookups.
- `list_global_variables` - Same as list_functions but documents intent as
  variable lookup.  Use query_symbol_by_name to fetch a specific variable.

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


def _build_instructions() -> str:
    """Return the full server instructions, optionally with user hints."""
    hints_content = _load_hints()
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
        ctx["pc"] = hex(int(str(pc_val), 0))
    except Exception:
        ctx["pc"] = None
    if ctx["pc"] is not None:
        ctx.update(_resolve_symbol_at(dbg, ctx["pc"]))
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


def _text_of(result: Any) -> str:
    """Extract the text from the first element of a tool result.

    The MCP decorator types call_tool's return as
    Iterable[TextContent | ImageContent | EmbeddedResource], but our tools
    always return list[TextContent].  This helper safely extracts the text
    so callers avoid repeated type narrowing.
    """
    items = list(result)
    first = items[0]
    assert isinstance(first, types.TextContent)
    return first.text


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
        # Message-specific patterns checked first so they override generic
        # type-based suggestions (e.g. CommandError "symbol not found" needs
        # a more actionable hint than the generic syntax suggestion).
        msg = str(exc).lower()
        if "symbol not found" in msg:
            suggestion = (
                "Symbol not found. Load debug symbols first with "
                "run_command(\"Data.LOAD.ELF <path/to/file.elf> /nocode\")."
            )
    if suggestion is None and isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "not connected" in msg or "connect" in msg:
            suggestion = "Call the 'connect' tool first."
    if suggestion is None:
        suggestion = _EXCEPTION_SUGGESTIONS.get(type(exc))
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

        # ── Composite / higher-level tools ───────────────────────────────
        types.Tool(
            name="get_context",
            description=(
                "Get a full CPU context snapshot: state, PC, SP, LR, "
                "current function, source location, and CPU name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "core": {
                        "type": "integer",
                        "description": "Core number (optional, for multi-core).",
                    },
                },
            },
        ),
        types.Tool(
            name="get_source_location",
            description=(
                "Get source file and line for an address (defaults to current PC)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Target address. Omit to use current PC.",
                    },
                },
            },
        ),
        types.Tool(
            name="evaluate_expression",
            description=(
                "Evaluate a C/C++ expression and return value, type, and hex. "
                "Uses Var.VALUE(), Var.STRing(), Var.TYPEOF()."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "C/C++ expression, e.g. 'myVar', 'myStruct.field', '*(int*)0x1000'.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["decimal", "hex", "string"],
                        "description": "Output format. Default: decimal",
                        "default": "decimal",
                    },
                },
                "required": ["expression"],
            },
        ),
        types.Tool(
            name="get_system_info",
            description=(
                "Get target system information: CPU name, family, and endianness."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="read_string",
            description="Read a null-terminated C string from target memory.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Address of the string in target memory.",
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "Maximum characters to read. Default: 256",
                        "default": 256,
                    },
                },
                "required": ["address"],
            },
        ),
        types.Tool(
            name="dump_memory_formatted",
            description="Hex + ASCII dump of target memory (like hexdump).",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Start address, e.g. '0x20000000'.",
                    },
                    "length": {
                        "type": "integer",
                        "description": "Number of bytes. Default: 256",
                        "default": 256,
                    },
                },
                "required": ["address"],
            },
        ),
        types.Tool(
            name="write_memory",
            description="Write raw bytes (hex string) to target memory.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Target address, e.g. '0x20000000'.",
                    },
                    "data": {
                        "type": "string",
                        "description": "Hex string of bytes to write, e.g. 'DEADBEEF'.",
                    },
                },
                "required": ["address", "data"],
            },
        ),
        types.Tool(
            name="backtrace",
            description="Walk the call stack and return frame information.",
            inputSchema={
                "type": "object",
                "properties": {
                    "depth": {
                        "type": "integer",
                        "description": "Maximum number of frames. Default: 20",
                        "default": 20,
                    },
                },
            },
        ),
        types.Tool(
            name="disassemble",
            description="Read raw instruction bytes at an address (defaults to PC). Returns hex bytes; TRACE32 PRACTICE does not expose a disassembly text function via its remote API.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Start address. Omit to use current PC.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of instructions. Default: 10",
                        "default": 10,
                    },
                },
            },
        ),
        types.Tool(
            name="set_breakpoint_at_symbol",
            description="Set a breakpoint at a function or label by name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Function or label name, e.g. 'main'.",
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
                },
                "required": ["symbol"],
            },
        ),
        types.Tool(
            name="run_until",
            description=(
                "Run until a target address/symbol is reached (temporary breakpoint). "
                "Blocks until halted or timeout."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Target address or symbol name.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds. Default: 10",
                        "default": 10.0,
                    },
                },
                "required": ["target"],
            },
        ),
        types.Tool(
            name="snapshot",
            description=(
                "Full state capture: context, backtrace, breakpoints, "
                "and system info in one call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_registers": {
                        "type": "boolean",
                        "description": "Include all registers. Default: false",
                        "default": False,
                    },
                },
            },
        ),
        types.Tool(
            name="list_functions",
            description="Count symbols matching a wildcard filter via sYmbol.COUNT(). TRACE32 PRACTICE has no indexed symbol iterator. Use query_symbol_by_name to fetch individual symbols.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Wildcard filter, e.g. 'main*'. Optional.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results. Default: 100",
                        "default": 100,
                    },
                },
            },
        ),
        types.Tool(
            name="list_global_variables",
            description="Count variables matching a wildcard filter via sYmbol.COUNT(). TRACE32 PRACTICE has no indexed symbol iterator. Use query_symbol_by_name to fetch individual symbols.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Wildcard filter, e.g. 'g_*'. Optional.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results. Default: 100",
                        "default": 100,
                    },
                },
            },
        ),
        types.Tool(
            name="search_memory",
            description="Search for a byte pattern in a memory range.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_address": {
                        "type": "string",
                        "description": "Start of search range.",
                    },
                    "end_address": {
                        "type": "string",
                        "description": "End of search range.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Hex byte pattern to search for, e.g. 'DEADBEEF'.",
                    },
                },
                "required": ["start_address", "end_address", "pattern"],
            },
        ),

        # ── Documentation ────────────────────────────────────────────────
        types.Tool(
            name="list_trace32_docs",
            description="List available TRACE32 PDF documentation files with categories.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": (
                            "Filter by category prefix "
                            "(e.g. 'debugger', 'rtos', 'app', 'trace', 'flash', 'practice')."
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="search_trace32_docs",
            description="Search TRACE32 documentation filenames and descriptions by keyword.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword (matched against filename).",
                    },
                },
                "required": ["query"],
            },
        ),

        # ── PER files ────────────────────────────────────────────────────
        types.Tool(
            name="list_per_files",
            description="List available TRACE32 PER (peripheral description) files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Substring filter on filename (case-insensitive).",
                    },
                },
            },
        ),
        types.Tool(
            name="load_per_file",
            description=(
                "Load a PER (peripheral description) file into TRACE32. "
                "Uses PER.Program PRACTICE command."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Path to .per file. Absolute or relative to T32 install dir."
                        ),
                    },
                },
                "required": ["file_path"],
            },
        ),
        types.Tool(
            name="per_read_register",
            description=(
                "Read and decode a peripheral register using TRACE32 PER system. "
                "Requires a PER file to be loaded first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Register address, e.g. '0x40021000'.",
                    },
                    "access_width": {
                        "type": "string",
                        "enum": ["byte", "word", "long"],
                        "description": "Access width. Default: long (32-bit).",
                        "default": "long",
                    },
                },
                "required": ["address"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Helper: scan for documentation PDFs
# ---------------------------------------------------------------------------
def _scan_pdf_docs(category: Optional[str] = None) -> list[dict[str, Any]]:
    """Scan the T32 installation pdf/ directory for documentation files."""
    pdf_dir = Path(_config["t32_dir"]) / "pdf"
    if not pdf_dir.is_dir():
        return []
    docs = []
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        name = pdf.name
        cat = name.split("_")[0] if "_" in name else "other"
        if category and not cat.lower().startswith(category.lower()):
            continue
        txt_path = _pdf_cache_txt_path(pdf)
        entry: dict[str, Any] = {
            "name": name,
            "category": cat,
            "size_kb": pdf.stat().st_size // 1024,
            "pdf_path": str(pdf),
        }
        if txt_path is not None:
            entry["txt_path"] = str(txt_path) if txt_path.is_file() else None
        docs.append(entry)
    return docs


# ---------------------------------------------------------------------------
# Helper: PDF-to-text cache
#
# When pdftotext is available, extracted text is stored under
# ~/.cache/lauterbach-t32-mcp/<stem>.txt alongside an MD5 file
# ~/.cache/lauterbach-t32-mcp/<stem>.md5 that records the hash of the
# source PDF.  If the PDF is replaced (e.g., a T32 upgrade) the MD5
# changes and the cache entry is regenerated automatically.
#
# Design notes:
#   - MD5 is checked at most once per session per PDF (session cache).
#   - A per-file lock prevents two concurrent agent processes from
#     running pdftotext on the same file simultaneously.  The lock
#     uses fcntl on POSIX and a best-effort msvcrt approach on Windows.
#   - Reads are not locked; the .txt file is written atomically via a
#     temp file so readers never see partial content.
# ---------------------------------------------------------------------------
# Set of resolved PDF paths whose MD5 has been verified this session.
_pdf_cache_verified: set[str] = set()


def _get_pdf_cache_dir() -> Optional[Path]:
    """Return the PDF text cache directory, creating it if needed.

    Returns None when pdftotext is not installed or the directory
    cannot be created.
    """
    if shutil.which("pdftotext") is None:
        return None
    cache_dir = Path(os.path.expanduser(_config["pdf_cache_dir"]))
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir
    except OSError:
        return None


def _pdf_cache_txt_path(pdf_path: Path) -> Optional[Path]:
    """Return the cache .txt path for a PDF, or None if pdftotext unavailable."""
    cache_dir = _get_pdf_cache_dir()
    if cache_dir is None:
        return None
    return cache_dir / f"{pdf_path.stem}.txt"


def _pdf_md5(pdf_path: Path) -> str:
    """Compute the MD5 hex digest of a PDF file."""
    h = hashlib.md5()
    with open(pdf_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _acquire_file_lock(fh: Any) -> None:
    """Acquire an exclusive write lock on an open file (best-effort)."""
    try:
        import fcntl  # POSIX
        fcntl.flock(fh, fcntl.LOCK_EX)
    except ImportError:
        try:
            import msvcrt  # Windows
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except (ImportError, OSError):
            pass  # Fall through; writes are idempotent so races are harmless.


def _release_file_lock(fh: Any) -> None:
    """Release a lock previously acquired with _acquire_file_lock."""
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)
    except ImportError:
        try:
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        except (ImportError, OSError):
            pass


def _get_or_cache_pdf_text(pdf_path: Path) -> Optional[str]:
    """Return the text of a PDF, building or reusing a persistent disk cache.

    Cache layout (in _PDF_CACHE_DIR):
      <stem>.txt   - pdftotext output (written atomically via temp file)
      <stem>.md5   - MD5 of the source PDF (invalidated on upgrade)
      <stem>.lock  - lock file used during cache regeneration

    Session optimisation: once a PDF's MD5 is verified in this process,
    subsequent calls return the cached text without re-hashing the PDF.

    Returns None if pdftotext is unavailable or conversion fails.
    This function is intentionally synchronous; callers in async contexts
    should use asyncio.to_thread(_get_or_cache_pdf_text, path).
    """
    cache_dir = _get_pdf_cache_dir()
    if cache_dir is None:
        return None

    pdf_key = str(pdf_path.resolve())
    stem = pdf_path.stem
    txt_path = cache_dir / f"{stem}.txt"
    md5_path = cache_dir / f"{stem}.md5"
    lock_path = cache_dir / f"{stem}.lock"

    # Fast path: already verified this session -- skip MD5 recheck.
    if pdf_key in _pdf_cache_verified and txt_path.is_file():
        try:
            return txt_path.read_text(errors="replace")
        except OSError:
            pass

    # Acquire exclusive lock before touching the cache files.
    try:
        lock_fh = open(lock_path, "w")  # noqa: WPS515
    except OSError:
        return None

    try:
        _acquire_file_lock(lock_fh)

        # Compute current MD5 (done inside the lock so only one process
        # pays this cost at a time per PDF).
        try:
            current_md5 = _pdf_md5(pdf_path)
        except OSError:
            return None

        # Cache hit: MD5 matches stored value.
        if txt_path.is_file() and md5_path.is_file():
            try:
                if md5_path.read_text().strip() == current_md5:
                    _pdf_cache_verified.add(pdf_key)
                    return txt_path.read_text(errors="replace")
            except OSError:
                pass

        # Cache miss or stale: run pdftotext into a temp file then rename
        # for an atomic replace so concurrent readers are never interrupted.
        tmp_path = txt_path.with_suffix(".tmp")
        try:
            result = subprocess.run(
                ["pdftotext", str(pdf_path), str(tmp_path)],
                capture_output=True,
                timeout=120,
            )
            if result.returncode == 0:
                tmp_path.replace(txt_path)
                md5_path.write_text(current_md5 + "\n")
                _pdf_cache_verified.add(pdf_key)
                try:
                    return txt_path.read_text(errors="replace")
                except OSError:
                    pass
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    finally:
        _release_file_lock(lock_fh)
        lock_fh.close()
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    return None


# ---------------------------------------------------------------------------
# Helper: scan for PER files
# ---------------------------------------------------------------------------
def _scan_per_files(name_filter: Optional[str] = None) -> list[dict[str, Any]]:
    """Scan the T32 installation directory for .per files."""
    t32_dir = Path(_config["t32_dir"])
    if not t32_dir.is_dir():
        return []
    results = []
    for per in sorted(t32_dir.rglob("*.per")):
        if name_filter and name_filter.lower() not in per.name.lower():
            continue
        title = ""
        try:
            with open(per, "r", errors="replace") as fh:
                for line in fh:
                    if line.strip().startswith("; @Title:"):
                        title = line.split("; @Title:", 1)[1].strip()
                        break
                    if not line.startswith(";"):
                        break
        except OSError:
            pass
        results.append({
            "name": per.name,
            "path": str(per),
            "size_kb": per.stat().st_size // 1024,
            "title": title,
        })
    return results


# ---------------------------------------------------------------------------
# Helper: load user hints
# ---------------------------------------------------------------------------
def _load_hints() -> str:
    """Load user-provided hints from configured file or directory."""
    hints = _config.get("hints")
    if not hints:
        return ""
    p = Path(os.path.expanduser(hints))
    parts: list[str] = []
    if p.is_dir():
        for md_file in sorted(p.glob("*.md")):
            try:
                parts.append(md_file.read_text(errors="replace"))
            except OSError:
                pass
    elif p.is_file():
        try:
            parts.append(p.read_text(errors="replace"))
        except OSError:
            pass
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# MCP Resources
# ---------------------------------------------------------------------------
@server.list_resources()
async def list_resources() -> list[types.Resource]:
    resources: list[types.Resource] = []

    # Documentation PDFs
    for doc in _scan_pdf_docs():
        resources.append(types.Resource(
            uri=AnyUrl(f"trace32://docs/{doc['name']}"),
            name=doc["name"],
            description=f"TRACE32 {doc['category']} documentation ({doc['size_kb']} KB)",
            mimeType="application/pdf",
        ))

    # User hints
    hints_text = _load_hints()
    if hints_text:
        resources.append(types.Resource(
            uri=AnyUrl("trace32://hints"),
            name="User debugging hints",
            description="User-provided TRACE32 debugging tips and notes.",
            mimeType="text/markdown",
        ))

    return resources


@server.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    uri_str = str(uri)

    if uri_str.startswith("trace32://docs/"):
        filename = uri_str.split("trace32://docs/", 1)[1]
        pdf_path = Path(_config["t32_dir"]) / "pdf" / filename
        if not pdf_path.is_file():
            return f"Document not found: {filename}"
        # Use cached text (pdftotext run in a thread to avoid blocking the
        # event loop during the first conversion of a large PDF).
        text = await asyncio.to_thread(_get_or_cache_pdf_text, pdf_path)
        if text:
            return text
        return (
            f"PDF file available at: {pdf_path}"
            " (install pdftotext for text extraction)"
        )

    if uri_str == "trace32://hints":
        text = _load_hints()
        return text if text else "No hints configured."

    return f"Unknown resource: {uri_str}"


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

        # ── Composite / higher-level tools ────────────────────────────────
        elif name == "get_context":
            dbg = _require_connection()
            ctx: dict[str, Any] = {}
            try:
                state = dbg.get_state()
                if isinstance(state, (bytes, bytearray)):
                    state = int.from_bytes(state, "little")
                state_names = {0: "stopped", 1: "running", 2: "halted",
                               3: "background_running"}
                ctx["state"] = state
                ctx["state_name"] = (
                    state_names.get(int(state), f"state_{state}")
                    if state is not None else None
                )
            except Exception:
                ctx["state"] = None
                ctx["state_name"] = None
            for key, expr in (("pc", "Register(PC)"), ("sp", "Register(SP)"),
                              ("lr", "Register(LR)")):
                try:
                    raw = dbg.fnc(expr)
                    ctx[key] = hex(int(str(raw), 0))
                except Exception:
                    ctx[key] = None
            if ctx["pc"] is not None:
                ctx.update(_resolve_symbol_at(dbg, ctx["pc"]))
            else:
                ctx.update(function=None, source_file=None, source_line=None)
            try:
                ctx["cpu"] = dbg.fnc("CPU()")
            except Exception:
                ctx["cpu"] = None
            return _ok(ctx)

        elif name == "get_source_location":
            dbg = _require_connection()
            addr = arguments.get("address")
            if addr is None:
                try:
                    pc_raw = dbg.fnc("Register(PC)")
                    addr = hex(int(str(pc_raw), 0))
                except Exception:
                    addr = "0x0"
            info: dict[str, Any] = {"address": addr}
            info.update(_resolve_symbol_at(dbg, addr))
            return _ok(info)

        elif name == "evaluate_expression":
            dbg = _require_connection()
            expr = arguments["expression"]
            fmt = arguments.get("format", "decimal")
            result: dict[str, Any] = {"expression": expr}
            if fmt == "string":
                try:
                    result["value"] = dbg.fnc(f'Var.STRing({expr})')
                except Exception:
                    result["value"] = None
            else:
                try:
                    result["value"] = dbg.fnc(f"Var.VALUE({expr})")
                except Exception:
                    result["value"] = None
            try:
                result["type"] = dbg.fnc(f"Var.TYPEOF({expr})")
            except Exception:
                result["type"] = None
            if fmt == "hex" and result["value"] is not None:
                try:
                    result["hex"] = hex(int(result["value"]))
                except (ValueError, TypeError):
                    result["hex"] = None
            return _ok(result)

        elif name == "get_system_info":
            dbg = _require_connection()
            info = {}
            for key, expr in (
                ("cpu", "CPU()"),
                ("cpu_family", "CPUFAMILY()"),
                ("big_endian", "SYSTEM.BIGENDIAN()"),
            ):
                try:
                    info[key] = dbg.fnc(expr)
                except Exception:
                    info[key] = None
            return _ok(info)

        elif name == "read_string":
            dbg = _require_connection()
            addr = arguments["address"]
            max_len = int(arguments.get("max_length", 256))
            try:
                s = dbg.fnc(f"Data.STRing(D:{addr})")
            except UnicodeDecodeError:
                return _ok({
                    "address": addr,
                    "string": None,
                    "length": 0,
                    "error": "Memory at this address does not contain valid UTF-8 text.",
                })
            if s and len(s) > max_len:
                s = s[:max_len]
            return _ok({"address": addr, "string": s, "length": len(s) if s else 0})

        elif name == "dump_memory_formatted":
            dbg = _require_connection()
            addr_str = arguments["address"]
            length = int(arguments.get("length", 256))
            addr = dbg.address.from_string(addr_str)
            data = dbg.memory.read(addr, length=length)
            try:
                base = int(addr_str, 0)
            except ValueError:
                base = 0
            dump = _format_hex_dump(data, base)
            return _ok({
                "address": addr_str,
                "length": len(data),
                "dump": dump,
                "raw_hex": data.hex(),
            })

        elif name == "write_memory":
            dbg = _require_connection()
            addr_str = arguments["address"]
            hex_data = arguments["data"]
            raw_bytes = bytes.fromhex(hex_data)
            addr = dbg.address.from_string(addr_str)
            dbg.memory.write(addr, raw_bytes)
            return _ok({
                "address": addr_str,
                "length": len(raw_bytes),
                "data_written": hex_data,
            })

        elif name == "backtrace":
            dbg = _require_connection()
            depth = int(arguments.get("depth", 20))
            frames: list[dict[str, Any]] = []
            frames_walked = 0
            for i in range(depth):
                frame: dict[str, Any] = {"frame": i}
                try:
                    pc_raw = dbg.fnc("Register(PC)")
                    addr_str = hex(int(str(pc_raw), 0))
                    frame["address"] = addr_str
                except Exception:
                    break  # Cannot read PC; target likely not halted or top of stack
                if addr_str:
                    for key, expr in (
                        ("function", f"sYmbol.FUNCTION(D:{addr_str})"),
                        ("source_file", f"sYmbol.SOURCEFILE(D:{addr_str})"),
                        ("source_line", f"sYmbol.SOURCELINE(D:{addr_str})"),
                    ):
                        try:
                            frame[key] = dbg.fnc(expr)
                        except Exception:
                            frame[key] = None
                frames.append(frame)
                # Navigate up one frame; stop when we reach the top
                if i < depth - 1:
                    try:
                        dbg.cmd("Frame.Up")
                        frames_walked += 1
                    except Exception:
                        break  # At top of call stack
            # Restore original frame position
            for _ in range(frames_walked):
                try:
                    dbg.cmd("Frame.Down")
                except Exception:
                    break
            return _ok({
                "frames": frames,
                "depth": len(frames),
                "truncated": len(frames) >= depth,
            })

        elif name == "disassemble":
            dbg = _require_connection()
            addr = arguments.get("address")
            addr_warning: Optional[str] = None
            if addr is None:
                try:
                    pc_raw = dbg.fnc("Register(PC)")
                    addr = hex(int(str(pc_raw), 0))
                except Exception:
                    addr = "0x0"
                    addr_warning = (
                        "Could not read PC register; address defaulted to 0x0. "
                        "Halt the target before disassembling."
                    )
            count = int(arguments.get("count", 10))
            # TRACE32 PRACTICE exposes no disassembly text function via
            # its remote API.  Read raw bytes (4 bytes per instruction slot)
            # and return them.  The caller can decode them using CPU type.
            byte_count = count * 4
            mem_addr = dbg.address.from_string(addr)
            raw = dbg.memory.read(mem_addr, length=byte_count)
            cpu_name: Optional[str] = None
            try:
                cpu_name = str(dbg.fnc("CPU()"))
            except Exception:
                pass
            base = int(addr, 0)
            instrs = []
            for i in range(0, min(len(raw), byte_count), 4):
                chunk = raw[i:i + 4]
                instrs.append({
                    "address": hex(base + i),
                    "hex": chunk.hex(),
                })
            disasm_result: dict[str, Any] = {
                "start_address": addr,
                "cpu": cpu_name,
                "instructions": instrs,
                "count": len(instrs),
                "note": (
                    "Raw instruction bytes. TRACE32 PRACTICE does not expose "
                    "a disassembly text function. Use the cpu field to decode."
                ),
            }
            if addr_warning is not None:
                disasm_result["pc_fallback"] = True
                disasm_result["warning"] = addr_warning
            return _ok(disasm_result)

        elif name == "set_breakpoint_at_symbol":
            dbg = _require_connection()
            symbol = arguments["symbol"]
            bp_type = arguments.get("type", "PROGRAM")
            bp_impl = arguments.get("impl", "AUTO")
            dbg.cmd(f"Break.Set {symbol} /{bp_type} /{bp_impl}")
            result = {
                "symbol": symbol,
                "type": bp_type,
                "impl": bp_impl,
                "enabled": True,
            }
            try:
                sym = dbg.symbol.query_by_name(symbol)
                result["address"] = str(sym.address) if sym.address else None
            except Exception:
                result["address"] = None
            return _ok(result)

        elif name == "run_until":
            dbg = _require_connection()
            target = arguments["target"]
            timeout_s = float(arguments.get("timeout", 10.0))
            try:
                dbg.cmd(f"Go.direct {target}")
            except Exception:
                dbg.cmd("Go")
            elapsed = 0.0
            poll_interval = 0.1
            reached = False
            while elapsed < timeout_s:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                try:
                    state = dbg.get_state()
                    if isinstance(state, (bytes, bytearray)):
                        state = int.from_bytes(state, "little")
                    if state == 2:  # halted
                        reached = True
                        break
                except Exception:
                    pass
            result = {"reached": reached, "target": target}
            if reached:
                result.update(_get_brief_context(dbg))
            else:
                try:
                    dbg.break_()
                except Exception:
                    pass
                result["status"] = "timeout"
            return _ok(result)

        elif name == "snapshot":
            dbg = _require_connection()
            snap: dict[str, Any] = {}
            # Context
            try:
                snap["context"] = json.loads(
                    _text_of(await call_tool("get_context", {})))
            except Exception:
                snap["context"] = None
            # Backtrace
            try:
                snap["backtrace"] = json.loads(
                    _text_of(await call_tool("backtrace", {"depth": 20})))
            except Exception:
                snap["backtrace"] = None
            # Breakpoints
            try:
                snap["breakpoints"] = json.loads(
                    _text_of(await call_tool("list_breakpoints", {})))
            except Exception:
                snap["breakpoints"] = None
            # System info
            try:
                snap["system_info"] = json.loads(
                    _text_of(await call_tool("get_system_info", {})))
            except Exception:
                snap["system_info"] = None
            # Optional registers
            if arguments.get("include_registers", False):
                try:
                    snap["registers"] = json.loads(
                        _text_of(await call_tool("read_all_registers", {})))
                except Exception:
                    snap["registers"] = None
            return _ok(snap)

        elif name == "list_functions":
            dbg = _require_connection()
            filt = arguments.get("filter", "*")
            count = 0
            try:
                count = int(dbg.fnc(f"sYmbol.COUNT({filt})"))
            except Exception:
                pass
            return _ok({
                "filter": filt,
                "count": count,
                "items": [],
                "note": (
                    "TRACE32 PRACTICE does not provide indexed symbol iteration. "
                    f"sYmbol.COUNT('{filt}') reports {count} matching symbol(s). "
                    "Use query_symbol_by_name to look up individual symbols by name."
                ),
            })

        elif name == "list_global_variables":
            dbg = _require_connection()
            filt = arguments.get("filter", "*")
            count = 0
            try:
                count = int(dbg.fnc(f"sYmbol.COUNT({filt})"))
            except Exception:
                pass
            return _ok({
                "filter": filt,
                "count": count,
                "items": [],
                "note": (
                    "TRACE32 PRACTICE does not provide indexed symbol iteration. "
                    f"sYmbol.COUNT('{filt}') reports {count} matching symbol(s). "
                    "Use query_symbol_by_name to look up individual symbols by name."
                ),
            })

        elif name == "search_memory":
            dbg = _require_connection()
            start = arguments["start_address"]
            end = arguments["end_address"]
            pattern = arguments["pattern"]
            pattern_bytes = bytes.fromhex(pattern)
            try:
                start_int = int(start, 0)
                end_int = int(end, 0)
            except ValueError:
                start_int = 0
                end_int = 0
            chunk_size = 4096
            found_addr = None
            offset = 0
            total_len = end_int - start_int
            while offset < total_len:
                read_len = min(chunk_size, total_len - offset)
                addr = dbg.address.from_string(hex(start_int + offset))
                data = dbg.memory.read(addr, length=read_len)
                idx = data.find(pattern_bytes)
                if idx >= 0:
                    found_addr = hex(start_int + offset + idx)
                    break
                # Overlap to catch patterns spanning chunks
                overlap = len(pattern_bytes) - 1
                offset += max(read_len - overlap, 1)
            return _ok({
                "found": found_addr is not None,
                "address": found_addr,
                "pattern": pattern,
                "search_range": f"{start}--{end}",
            })

        # ── Documentation ────────────────────────────────────────────────
        elif name == "list_trace32_docs":
            category = arguments.get("category")
            docs = _scan_pdf_docs(category=category)
            return _ok({"docs": docs, "total": len(docs)})

        elif name == "search_trace32_docs":
            query = arguments["query"]
            ql = query.lower()
            docs = _scan_pdf_docs()
            results = []
            for doc in docs:
                name_match = ql in doc["name"].lower()
                snippets: list[str] = []
                # Get text: from cache if available, otherwise extract now.
                txt_path_str = doc.get("txt_path")
                text_content: Optional[str] = None
                if txt_path_str:
                    try:
                        text_content = Path(txt_path_str).read_text(
                            errors="replace"
                        )
                    except OSError:
                        pass
                elif doc.get("pdf_path"):
                    # Not yet cached - extract on demand.  First-time cost is
                    # acceptable; subsequent searches use the cached .txt file.
                    text_content = _get_or_cache_pdf_text(
                        Path(doc["pdf_path"])
                    )
                if text_content is not None:
                    lines = text_content.splitlines()
                    for idx, line in enumerate(lines):
                        if ql in line.lower():
                            start = max(0, idx - 1)
                            end = min(len(lines), idx + 3)
                            snippet = " | ".join(
                                ln.strip() for ln in lines[start:end] if ln.strip()
                            )
                            if snippet:
                                snippets.append(snippet)
                            if len(snippets) >= 5:
                                break
                if name_match or snippets:
                    entry = dict(doc)
                    if snippets:
                        entry["snippets"] = snippets
                    results.append(entry)
            return _ok({"results": results, "query": query,
                        "total": len(results)})

        # ── PER files ────────────────────────────────────────────────────
        elif name == "list_per_files":
            name_filter = arguments.get("filter")
            files = _scan_per_files(name_filter=name_filter)
            return _ok({"files": files, "total": len(files)})

        elif name == "load_per_file":
            dbg = _require_connection()
            file_path = arguments["file_path"]
            # Resolve relative paths against t32_dir
            p = Path(file_path)
            if not p.is_absolute():
                p = Path(_config["t32_dir"]) / p
            if not p.is_file():
                return _ok({"loaded": False, "error": f"File not found: {p}"})
            dbg.cmd(f"PER.Program {p}")
            return _ok({"loaded": True, "file": str(p)})

        elif name == "per_read_register":
            dbg = _require_connection()
            address = arguments["address"]
            width = arguments.get("access_width", "long")
            width_map = {"byte": 1, "word": 2, "long": 4}
            nbytes = width_map.get(width, 4)
            addr = dbg.address.from_string(address)
            data = dbg.memory.read(addr, length=nbytes)
            value = int.from_bytes(data, "little")
            return _ok({
                "address": address,
                "access_width": width,
                "value": value,
                "hex": hex(value),
                "raw_bytes": data.hex(),
            })

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
    pdf_cache_dir: str = "~/.cache/lauterbach-t32-mcp",
) -> None:
    global _auto_connect_task

    # Store connection parameters so the connect tool can use them as defaults
    _conn_defaults.update(host=host, port=port, protocol=protocol, timeout=timeout)

    # Store configuration paths
    _config.update(t32_dir=t32_dir, hints=hints, pdf_cache_dir=pdf_cache_dir)

    # Load user hints and update server instructions
    server.instructions = _build_instructions()

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
