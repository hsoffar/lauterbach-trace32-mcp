import asyncio
import json
import logging
from typing import Any, Optional

import mcp.types as types
import lauterbach.trace32.rcl as t32
from lauterbach.trace32.rcl import Breakpoint
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

server = Server("lauterbachdebugger-mcp")


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
            return _ok("Execution started (Go).")

        elif name == "break_":
            _require_connection().break_()
            return _ok("Execution halted (Break).")

        elif name == "step":
            _require_connection().step()
            return _ok("Single step executed.")

        elif name == "step_asm":
            _require_connection().step_asm()
            return _ok("Assembly step executed.")

        elif name == "step_hll":
            _require_connection().step_hll()
            return _ok("HLL (source-level) step executed.")

        elif name == "step_over":
            _require_connection().step_over()
            return _ok("Step.Over executed.")

        elif name == "go_up":
            _require_connection().go_up()
            return _ok("Go.Up executed — running to function return.")

        elif name == "go_return":
            _require_connection().go_return()
            return _ok("Go.Return executed.")

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
            return _ok({
                "address": arguments["address"],
                "length": len(data),
                "hex": data.hex(),
                "bytes": list(data),
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
            return _ok(str(bp))

        elif name == "list_breakpoints":
            bps = _require_connection().breakpoint.list()
            return _ok([str(bp) for bp in bps])

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
        return [
            types.TextContent(
                type="text",
                text=f"Error [{type(exc).__name__}]: {exc}",
            )
        ]


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


async def serve(host: str, port: int, protocol: str, timeout: float) -> None:
    global _auto_connect_task

    # Store connection parameters so the connect tool can use them as defaults
    _conn_defaults.update(host=host, port=port, protocol=protocol, timeout=timeout)

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
