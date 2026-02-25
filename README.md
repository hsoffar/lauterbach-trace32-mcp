# lauterbach-trace32-mcp

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that exposes
[Lauterbach TRACE32](https://www.lauterbach.com) debugger control as tools for AI
assistants (Claude, GPT-4, etc.).

Once connected, your AI assistant can control a live TRACE32 session via natural
language: set breakpoints, read registers, inspect memory, step through code, run
PRACTICE scripts, and more.

---

## Features

**30+ tools across 8 categories:**

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

**Auto-connect on startup** via environment variables — no need to call `connect` manually.

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
| `--verbose` | `-v` | | off | Repeat for more detail (`-vv`) |

---

## Client Configuration

### Claude Code

```bash
claude mcp add --transport stdio lauterbach-trace32 \
  --env T32_HOST=localhost \
  --env T32_PORT=20000 \
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
        "T32_PROTOCOL": "TCP"
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
        "T32_PORT": "20000"
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
Connect to a TRACE32 debugger manually (use when auto-connect is not configured).

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
Read raw bytes. Returns `{ address, length, hex, bytes[] }`.

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

#### `list_breakpoints`
Returns a list of all currently set breakpoints.

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
