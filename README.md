# lauterbach-trace32-mcp

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that exposes
[Lauterbach TRACE32](https://www.lauterbach.com) debugger control as tools for AI
assistants (Claude, GPT-4, etc.).

Once connected, your AI assistant can control a live TRACE32 session via natural
language: set breakpoints, read registers, inspect memory, step through code, run
PRACTICE scripts, and more.

---

## Features

**51 tools across 12 categories:**

| Category | Tools |
|---|---|
| Connection | `connect`, `disconnect`, `ping`, `get_state`, `get_message` |
| Execution control | `go`, `break_`, `step`, `step_asm`, `step_hll`, `step_over`, `go_up`, `go_return` |
| PRACTICE | `run_command`, `evaluate_function`, `run_practice_script` |
| Memory | `read_memory`, `read_memory_typed`, `write_memory_typed` |
| Registers | `read_register`, `read_all_registers`, `write_register` |
| Breakpoints | `set_breakpoint`, `list_breakpoints`, `delete_breakpoint` |
| Variables | `read_variable`, `write_variable` |
| Symbols | `query_symbol_by_name`, `query_symbol_by_address` |
| PRACTICE Macros | `get_practice_macro`, `set_practice_macro` |
| Composite / high-level | `get_context`, `get_source_location`, `evaluate_expression`, `get_system_info`, `read_string`, `dump_memory_formatted`, `write_memory`, `backtrace`, `disassemble`, `set_breakpoint_at_symbol`, `run_until`, `snapshot`, `list_functions`, `list_global_variables`, `search_memory` |
| Documentation | `list_trace32_docs`, `search_trace32_docs` |
| Peripheral (PER) | `list_per_files`, `load_per_file`, `per_read_register` |

**MCP Resources:**
- `trace32://docs/<filename>` — TRACE32 PDF documentation with text extraction
- `trace32://hints` — user-provided debugging tips

**Structured error handling** with actionable suggestions for every TRACE32
exception type.

**Built-in server instructions** teach the LLM TRACE32 concepts: target states,
address classes, debug symbol requirements, common workflows, and useful PRACTICE
functions.

**Non-blocking Auto-connect startup** — the server registers with the MCP client immediately
and auto-connects to TRACE32 in the background. If TRACE32 is not running,
the server stays available and you can connect later via the `connect` tool.

---

## Prerequisites

- Python 3.10 or later
- [Lauterbach TRACE32](https://www.lauterbach.com) with the **Remote API** enabled
- `lauterbach-trace32-rcl` Python package (pyrcl) — install via pip:
  ```bash
  pip install lauterbach-trace32-rcl
  ```

### Enable the TRACE32 Remote API

Add the following to your TRACE32 startup script (`config.t32` or equivalent):

```
RCL=NETASSIST
PACKLEN=1024
PORT=20000
```

Restart TRACE32 after making this change.

---

## Installation

### From GitHub

```bash
pip install git+https://github.com/hsoffar/lauterbach-trace32-mcp.git
```

### From source (editable)

```bash
git clone https://github.com/hsoffar/lauterbach-trace32-mcp.git
cd lauterbach-trace32-mcp
pip install -e .
```

### Using uv

```bash
uv tool install git+https://github.com/hsoffar/lauterbach-trace32-mcp.git
```

---

## Usage

### Run directly

```bash
# Connect to TRACE32 on localhost:20000 (default)
lauterbachdebugger-mcp

# Custom host/port
lauterbachdebugger-mcp --host 192.168.1.100 --port 20000

# With TRACE32 installation path and user hints
lauterbachdebugger-mcp --t32-dir ~/t32 --hints ~/.trace32-hints.md

# Verbose logging
lauterbachdebugger-mcp -v

# Or via Python module
python -m lauterbachdebugger_mcp
```

### CLI options

| Option | Short | Env var | Default | Description |
|---|---|---|---|---|
| `--host` | `-H` | `T32_HOST` | `localhost` | TRACE32 hostname or IP |
| `--port` | `-p` | `T32_PORT` | `20000` | Remote API port |
| `--protocol` | | `T32_PROTOCOL` | `TCP` | `TCP` or `UDP` |
| `--timeout` | | `T32_TIMEOUT` | `60.0` | Connection timeout (seconds) |
| `--t32-dir` | | `T32SYS` | `~/t32` | TRACE32 installation directory (`C:\T32\` on Windows) |
| `--hints` | | `T32_HINTS` | | Hints file or directory (see [AGENTS.md](https://agents.md/) convention) |
| `--verbose` | `-v` | | off | Repeat for more detail (`-vv`) |

---

## Client Configuration

### Claude Code

```bash
claude mcp add --transport stdio lauterbach-trace32 \
  --env T32_HOST=localhost \
  --env T32_PORT=20000 \
  --env T32SYS=~/t32 \
  --env T32_HINTS=/path/to/hints.md \
  -- lauterbachdebugger-mcp
```

Or using `python -m` (no PATH required):

```bash
claude mcp add --transport stdio lauterbach-trace32 \
  --env T32_HOST=localhost \
  --env T32_PORT=20000 \
  -- python -m lauterbachdebugger_mcp
```

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "lauterbach-trace32": {
      "command": "lauterbachdebugger-mcp",
      "env": {
        "T32_HOST": "localhost",
        "T32_PORT": "20000",
        "T32_PROTOCOL": "TCP",
        "T32SYS": "~/t32",
        "T32_HINTS": "/path/to/hints.md"
      }
    }
  }
}
```

### VS Code / Cursor / Windsurf

Add to `.vscode/mcp.json` in your workspace (or user settings):

```json
{
  "servers": {
    "lauterbach-trace32": {
      "type": "stdio",
      "command": "lauterbachdebugger-mcp",
      "env": {
        "T32_HOST": "localhost",
        "T32_PORT": "20000",
        "T32SYS": "~/t32",
        "T32_HINTS": "/path/to/hints.md"
      }
    }
  }
}
```

### OpenAI Agents SDK (Codex)

```python
from agents import Agent, MCPServerStdio
import asyncio

async def main():
    async with MCPServerStdio(
        command="lauterbachdebugger-mcp",
        env={"T32_HOST": "localhost", "T32_PORT": "20000"},
    ) as server:
        agent = Agent(name="debugger", mcp_servers=[server])
        # agent is now aware of all TRACE32 tools

asyncio.run(main())
```

---

## Tool Reference

### Connection

#### `connect`
Connect to a TRACE32 debugger. If a background auto-connect is still
pending or has failed, cancels it and reconnects. Uses CLI defaults
when parameters are omitted.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `node` | string | `localhost` | Hostname or IP |
| `port` | integer | `20000` | Remote API port |
| `protocol` | `TCP`\|`UDP` | `TCP` | Transport protocol |
| `timeout` | number | `60.0` | Timeout in seconds |

#### `disconnect`
Disconnect from the debugger and release resources.

#### `ping`
Verify the connection is alive.

#### `get_state`
Returns the current debug state as `{ state: int, state_name: string }`.

| Value | Name | Meaning |
|---|---|---|
| 0 | `stopped` | Target stopped / not running |
| 1 | `running` | Target running |
| 2 | `halted` | Halted at breakpoint |
| 3 | `background_running` | Running in background mode |

#### `get_message`
Returns the last message shown in the TRACE32 message line as `{ text, type }`.

---

### Execution Control

All execution control tools return enriched responses with PC, function name,
source file, and source line after the operation completes.

| Tool | Description |
|---|---|
| `go` | Start / resume execution |
| `break_` | Halt execution |
| `step` | Single step (HLL or ASM, context-dependent) |
| `step_asm` | Single assembly instruction step |
| `step_hll` | Single source-level (HLL) step |
| `step_over` | Step over a function call |
| `go_up` | Run until return from current function |
| `go_return` | Immediately return from current function |

---

### PRACTICE Commands & Scripts

#### `run_command`
Execute any TRACE32 PRACTICE command string.

```
run_command("SYStem.Up")
run_command("Data.dump 0x20000000")
run_command("Register.Set PC 0x0")
```

#### `evaluate_function`
Evaluate a PRACTICE function expression and return the result.

```
evaluate_function("STATE.RUN()")
evaluate_function("Register(PC)")
evaluate_function("Var.VALUE(myVar)")
```

#### `run_practice_script`
Run a CMM script file (blocking).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `script_path` | string | yes | Path and optional arguments, e.g. `C:/init.cmm arg1` |
| `timeout` | number | no | Seconds to wait; omit to wait indefinitely |

---

### Memory

#### `read_memory`
Read raw bytes. Returns `{ address, length, hex, bytes[], ascii }`.

| Parameter | Type | Description |
|---|---|---|
| `address` | string | e.g. `0x20000000` or `D:0x1000` (with access class) |
| `length` | integer | Number of bytes |

#### `read_memory_typed`
Read a typed scalar value.

| Parameter | Type | Description |
|---|---|---|
| `address` | string | Target address |
| `type` | string | `int8` `uint8` `int16` `uint16` `int32` `uint32` `int64` `uint64` `float` `double` |
| `byteorder` | string | `little` (default) or `big` |

#### `write_memory_typed`
Write a typed scalar value. Same parameters as `read_memory_typed` plus `value`.

---

### Registers

#### `read_register`
Read a single register by name. Returns `{ name, unit, core, value }`.

| Parameter | Type | Description |
|---|---|---|
| `name` | string | e.g. `PC`, `SP`, `LR`, `R0` |
| `core` | integer | Core number (optional, for multi-core) |

#### `read_all_registers`
Read all registers, optionally filtered by `core` and/or `unit` (`CPU`, `FPU`, `VPU`).

#### `write_register`
Write a value to a register.

---

### Breakpoints

#### `set_breakpoint`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `address` | string | — | e.g. `0x08000100` |
| `type` | string | `PROGRAM` | `PROGRAM` `READ` `WRITE` `RW` |
| `impl` | string | `AUTO` | `AUTO` `SOFT` `ONCHIP` `HARD` `MARK` |
| `size` | integer | — | Size in bytes (optional) |
| `core` | integer | — | Core (optional) |
| `enabled` | boolean | `true` | Whether enabled |

Returns enriched response with symbol resolution at the breakpoint address.

#### `list_breakpoints`
Returns a list of all currently set breakpoints with symbol info.

#### `delete_breakpoint`
Delete the breakpoint at the given `address`.

---

### Variables

#### `read_variable`
Read a source-level variable by name. Requires debug symbols to be loaded.

```
read_variable("myGlobalVar")
read_variable("myModule\\myStaticVar")
```

#### `write_variable`
Write a value to a source-level variable. Accepts integer or float.

---

### Symbols

#### `query_symbol_by_name`
Look up a function, variable, or label by name. Returns `{ name, path, address, size }`.

#### `query_symbol_by_address`
Look up the symbol at a given target address.

---

### PRACTICE Macros

#### `get_practice_macro`
Get the current value of a global PRACTICE macro variable (without the leading `&`).

#### `set_practice_macro`
Set the value of a global PRACTICE macro variable.

---

### Composite / High-level Tools

These tools combine multiple TRACE32 operations into single, context-rich
responses. All use graceful degradation — partial failures in sub-operations
do not break the overall result.

#### `get_context`
Full CPU context snapshot: state, PC, SP, LR, current function, source location,
and CPU name. Optional `core` parameter for multi-core targets.

#### `get_source_location`
Source file and line for an address (defaults to current PC).

#### `evaluate_expression`
Evaluate a C/C++ expression. Returns value, type, and hex representation.
Supports `format` parameter: `decimal`, `hex`, or `string`.

#### `get_system_info`
Target system information: CPU name, family, endianness, power state, target state.

#### `read_string`
Read a null-terminated C string from target memory. Optional `max_length`
(default 256).

#### `dump_memory_formatted`
Hex + ASCII memory dump (like `hexdump`). Optional `length` (default 256).

#### `write_memory`
Write raw bytes (hex string) to target memory.

#### `backtrace`
Walk the call stack and return frame information with source resolution.
Optional `depth` (default 20).

#### `disassemble`
Disassemble instructions at an address (defaults to PC). Optional `count`
(default 10).

#### `set_breakpoint_at_symbol`
Set a breakpoint by function or label name (e.g. `main`).

#### `run_until`
Run to an address or symbol with timeout. Uses temporary breakpoint.
Optional `timeout` in seconds (default 10).

#### `snapshot`
Full state capture: context + backtrace + breakpoint list + system info.
Optional `include_registers` (default false).

#### `list_functions`
Browse function symbols. Optional `filter` (wildcard), `limit` (default 100).

#### `list_global_variables`
Browse global variable symbols. Same parameters as `list_functions`.

#### `search_memory`
Search for a byte pattern in a memory range.

| Parameter | Type | Description |
|---|---|---|
| `start_address` | string | Start of search range |
| `end_address` | string | End of search range |
| `pattern` | string | Hex byte pattern, e.g. `DEADBEEF` |

---

### Documentation Tools

#### `list_trace32_docs`
List available TRACE32 PDF documentation files from the T32 installation.
Optional `category` filter (e.g. `debugger`, `rtos`, `practice`).

#### `search_trace32_docs`
Search documentation filenames by keyword.

---

### Peripheral Register Tools

#### `list_per_files`
List available `.per` (peripheral description) files from the T32 installation.
Extracts title from `; @Title:` comment in file headers.
Optional `filter` for substring matching.

#### `load_per_file`
Load a PER file into TRACE32 via `PER.Program` command. Path can be absolute
or relative to the T32 installation directory.

#### `per_read_register`
Read and decode a peripheral register value. Requires a PER file to be loaded first.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `address` | string | — | Register address |
| `access_width` | string | `long` | `byte`, `word`, or `long` |

---

## MCP Resources

### Documentation (`trace32://docs/<filename>`)

Each PDF in the T32 installation's `pdf/` directory is exposed as an MCP
resource. When read, the server extracts text using `pdftotext` if available,
otherwise returns the file path.

### User Hints (`trace32://hints`)

User-provided debugging tips loaded from `--hints-dir` and/or `--hints-file`.
Hints are automatically embedded into the MCP server instructions at startup,
so the LLM sees them immediately without any extra fetch. They are also
available as an MCP resource (`trace32://hints`) for re-reading mid-session.

Configure hints via CLI flags or environment variables in your MCP client
config (see examples below).

#### Hints file format

Create markdown files with your debugging tips:

```markdown
# My TRACE32 Debugging Tips

## I2C Debugging
When debugging I2C on our custom board, always check the pull-up
resistor configuration first. Use PER.view with the I2C peripheral
file to verify SCL/SDA pin muxing.

## Flash Programming Workflow
1. Run SYStem.Up to connect
2. Load flash algorithm: FLASH.Create ...
3. Program: FLASH.Program ALL /Erase
4. Verify: Data.LOAD.ELF <file> /ComPare
```

Point the server at your hints with `--hints-file` or `--hints-dir`:

```bash
lauterbachdebugger-mcp --hints-file ~/.trace32-hints.md
lauterbachdebugger-mcp --hints-dir ~/.trace32-hints/
```

---

## Error Handling

The server provides structured error responses with actionable suggestions
for every TRACE32 exception type:

| Exception | Suggestion |
|---|---|
| Not connected | Call the `connect` tool first |
| `CommandError` | Check command syntax |
| `FunctionError` | Function may not exist or target not halted |
| `MemoryReadAccessError` | Halt target, check address/access class |
| `MemoryWriteAccessError` | Region may be read-only or protected |
| `VariableError` | Ensure debug symbols loaded and target halted |
| `SymbolError` | Ensure debug symbols loaded |
| `RegisterError` | Halt target, check register name |
| `ApiConnectionError` | Connection lost, try reconnecting |
| `BreakpointError` | Check address and breakpoint type |

---

## Development

```bash
git clone https://github.com/hsoffar/lauterbach-trace32-mcp.git
cd lauterbach-trace32-mcp

# Install with dev dependencies
pip install -e ".[dev]"
# or with uv
uv sync --group dev

# Run tests
pytest

# Lint & type-check
ruff check src/
pyright src/
```

---

## License

MIT — see [LICENSE](LICENSE).
