"""Tests for mcp-server-lauterbach-trace32.

All TRACE32 hardware interaction and the MCP stdio transport are replaced with
MagicMock objects so the tests run completely offline.

Coverage:
  - _ok / _require_connection helpers
  - Click CLI entry point (options, defaults, validation)
  - list_tools() completeness and schema correctness
  - Every tool handler via call_tool()
  - serve() auto-connect success and failure paths
  - Error handling (unknown tool, debugger exceptions)
"""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click import Command
from click.testing import CliRunner

import lauterbachdebugger_mcp.server as srv
from lauterbachdebugger_mcp import main
from lauterbachdebugger_mcp.server import (
    _build_instructions,
    _error,
    _load_hints,
    _ok,
    _require_connection,
    call_tool,
    list_tools,
    serve,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_connection():
    """Guarantee a clean (disconnected) state around every test."""
    srv._dbg = None
    srv._auto_connect_task = None
    srv._conn_defaults.update(host="localhost", port=20000,
                              protocol="TCP", timeout=60.0)
    srv._config.update(t32_dir="~/t32", hints=None)
    srv.server.instructions = srv.INSTRUCTIONS
    yield
    srv._dbg = None
    srv._auto_connect_task = None
    srv._conn_defaults.update(host="localhost", port=20000,
                              protocol="TCP", timeout=60.0)
    srv._config.update(t32_dir="~/t32", hints=None)
    srv.server.instructions = srv.INSTRUCTIONS


@pytest.fixture()
def mock_dbg():
    """Pre-connected mock debugger; address.from_string returns a mock."""
    dbg = MagicMock()
    dbg.address.from_string.return_value = MagicMock()
    srv._dbg = dbg
    return dbg


def run(coro):
    """Run an async coroutine from synchronous test code."""
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# _ok helper
# ─────────────────────────────────────────────────────────────────────────────

class TestOk:
    def test_string_passthrough(self):
        assert _ok("hello")[0].text == "hello"

    def test_dict_serialised_as_json(self):
        assert json.loads(_ok({"k": "v"})[0].text) == {"k": "v"}

    def test_list_serialised_as_json(self):
        assert json.loads(_ok([1, 2, 3])[0].text) == [1, 2, 3]

    def test_non_serialisable_uses_str_fallback(self):
        class _X:
            def __str__(self):
                return "CUSTOM"
        assert "CUSTOM" in _ok({"obj": _X()})[0].text

    def test_returns_single_text_content(self):
        result = _ok("x")
        assert len(result) == 1
        assert result[0].type == "text"


# ─────────────────────────────────────────────────────────────────────────────
# _require_connection
# ─────────────────────────────────────────────────────────────────────────────

class TestRequireConnection:
    def test_raises_when_not_connected(self):
        with pytest.raises(RuntimeError, match="Not connected"):
            _require_connection()

    def test_returns_debugger_when_connected(self, mock_dbg):
        assert _require_connection() is mock_dbg


# ─────────────────────────────────────────────────────────────────────────────
# CLI  (Click test runner — no real server is started)
# ─────────────────────────────────────────────────────────────────────────────

class TestCLI:
    runner = CliRunner()

    def test_main_is_click_command(self):
        assert isinstance(main, Command)

    def test_help_exits_zero(self):
        assert self.runner.invoke(main, ["--help"]).exit_code == 0

    def test_help_mentions_trace32(self):
        assert "TRACE32" in self.runner.invoke(main, ["--help"]).output

    def test_help_lists_all_options(self):
        output = self.runner.invoke(main, ["--help"]).output
        for opt in ("--host", "--port", "--protocol", "--timeout", "--verbose",
                     "--t32-dir", "--hints"):
            assert opt in output

    def test_invalid_protocol_rejected(self):
        result = self.runner.invoke(main, ["--protocol", "INVALID"])
        assert result.exit_code != 0

    def test_default_values_forwarded_to_serve(self):
        received = {}

        async def fake_serve(host, port, protocol, timeout, **kwargs):
            received.update(host=host, port=port, protocol=protocol,
                            timeout=timeout, **kwargs)

        # Clear env vars so Click uses coded defaults, not the shell environment
        with patch("lauterbachdebugger_mcp.serve", fake_serve):
            self.runner.invoke(main, [], env={
                "T32_HOST": "",
                "T32_PORT": "",
                "T32_PROTOCOL": "",
                "T32_TIMEOUT": "",
                "T32SYS": "",
                "T32_HINTS": "",
            })

        assert received == {"host": "localhost", "port": 20000,
                            "protocol": "TCP", "timeout": 60.0,
                            "t32_dir": "~/t32", "hints": None}

    def test_custom_host_and_port_forwarded(self):
        received = {}

        async def fake_serve(host, port, protocol, timeout, **kwargs):
            received.update(host=host, port=port)

        with patch("lauterbachdebugger_mcp.serve", fake_serve):
            self.runner.invoke(main, ["--host", "192.168.1.1", "--port", "9999"])

        assert received == {"host": "192.168.1.1", "port": 9999}

    def test_t32_dir_forwarded(self):
        received = {}

        async def fake_serve(host, port, protocol, timeout, **kwargs):
            received.update(**kwargs)

        with patch("lauterbachdebugger_mcp.serve", fake_serve):
            self.runner.invoke(main, ["--t32-dir", "/custom/t32"])

        assert received["t32_dir"] == "/custom/t32"

    def test_hints_file_forwarded(self):
        received = {}

        async def fake_serve(host, port, protocol, timeout, **kwargs):
            received.update(**kwargs)

        with patch("lauterbachdebugger_mcp.serve", fake_serve):
            self.runner.invoke(main, ["--hints", "/my/tips.md"])

        assert received["hints"] == "/my/tips.md"

    def test_hints_directory_forwarded(self):
        received = {}

        async def fake_serve(host, port, protocol, timeout, **kwargs):
            received.update(**kwargs)

        with patch("lauterbachdebugger_mcp.serve", fake_serve):
            self.runner.invoke(main, ["--hints", "/my/hints-dir"])

        assert received["hints"] == "/my/hints-dir"


# ─────────────────────────────────────────────────────────────────────────────
# list_tools
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Server instructions
# ─────────────────────────────────────────────────────────────────────────────

class TestServerInstructions:
    def test_instructions_are_non_empty(self):
        assert srv.INSTRUCTIONS
        assert len(srv.INSTRUCTIONS) > 100

    def test_server_has_instructions(self):
        assert srv.server.instructions is not None
        assert len(srv.server.instructions) > 0

    def test_instructions_mention_connect(self):
        assert "connect" in srv.INSTRUCTIONS.lower()

    def test_instructions_mention_target_states(self):
        assert "halted" in srv.INSTRUCTIONS
        assert "running" in srv.INSTRUCTIONS

    def test_instructions_mention_practice_functions(self):
        assert "Register(PC)" in srv.INSTRUCTIONS
        assert "sYmbol.FUNCTION" in srv.INSTRUCTIONS

    def test_instructions_mention_address_classes(self):
        assert "D:0x" in srv.INSTRUCTIONS
        assert "P:0x" in srv.INSTRUCTIONS

    def test_instructions_mention_enriched_responses(self):
        assert "Enriched Responses" in srv.INSTRUCTIONS
        assert "ascii" in srv.INSTRUCTIONS
        assert "symbol resolution" in srv.INSTRUCTIONS

    def test_load_hints_from_file(self, tmp_path):
        f = tmp_path / "tips.md"
        f.write_text("# My Tips\nuse breakpoints\n", encoding="utf-8")
        assert "My Tips" in _load_hints(str(f))

    def test_load_hints_from_directory(self, tmp_path):
        (tmp_path / "a.md").write_text("# Alpha\n", encoding="utf-8")
        (tmp_path / "b.md").write_text("# Beta\n", encoding="utf-8")
        (tmp_path / "readme.txt").write_text("ignored\n", encoding="utf-8")
        result = _load_hints(str(tmp_path))
        assert "Alpha" in result
        assert "Beta" in result
        assert "ignored" not in result

    def test_load_hints_nonexistent_returns_empty(self):
        assert _load_hints("/no/such/path") == ""

    def test_load_hints_empty_directory(self, tmp_path):
        assert _load_hints(str(tmp_path)) == ""

    def test_build_instructions_without_hints(self):
        result = _build_instructions()
        assert result == srv.INSTRUCTIONS

    def test_build_instructions_with_hints_file(self, tmp_path):
        f = tmp_path / "tips.md"
        f.write_text("# Custom Tips\n", encoding="utf-8")
        result = _build_instructions(str(f))
        assert result.startswith(srv.INSTRUCTIONS)
        assert "User Hints" in result
        assert "Custom Tips" in result

    def test_build_instructions_with_nonexistent_hints(self):
        result = _build_instructions("/no/such/path")
        assert result == srv.INSTRUCTIONS


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveSymbolAt:
    def test_returns_function_and_source_info(self):
        dbg = MagicMock()
        dbg.fnc.side_effect = lambda expr: {
            "sYmbol.FUNCTION(D:0x1000)": "main",
            "sYmbol.SOURCEFILE(D:0x1000)": "main.c",
            "sYmbol.SOURCELINE(D:0x1000)": "42",
        }[expr]
        info = srv._resolve_symbol_at(dbg, "0x1000")
        assert info == {"function": "main", "source_file": "main.c", "source_line": "42"}

    def test_graceful_on_partial_failure(self):
        dbg = MagicMock()
        call_count = [0]
        def _side_effect(expr):
            call_count[0] += 1
            if "FUNCTION" in expr:
                return "main"
            raise RuntimeError("no symbols")
        dbg.fnc.side_effect = _side_effect
        info = srv._resolve_symbol_at(dbg, "0x1000")
        assert info["function"] == "main"
        assert info["source_file"] is None
        assert info["source_line"] is None

    def test_graceful_on_total_failure(self):
        dbg = MagicMock()
        dbg.fnc.side_effect = RuntimeError("no symbols")
        info = srv._resolve_symbol_at(dbg, "0x1000")
        assert info == {"function": None, "source_file": None, "source_line": None}


class TestGetBriefContext:
    def test_returns_pc_and_symbol_info(self):
        dbg = MagicMock()
        dbg.fnc.side_effect = lambda expr: {
            "Register(PC)": "0x1000",
            "sYmbol.FUNCTION(D:0x1000)": "main",
            "sYmbol.SOURCEFILE(D:0x1000)": "main.c",
            "sYmbol.SOURCELINE(D:0x1000)": "42",
        }[expr]
        ctx = srv._get_brief_context(dbg)
        assert ctx["pc"] == "0x1000"
        assert ctx["function"] == "main"
        assert ctx["source_file"] == "main.c"
        assert ctx["source_line"] == "42"

    def test_graceful_when_pc_read_fails(self):
        dbg = MagicMock()
        dbg.fnc.side_effect = RuntimeError("not halted")
        ctx = srv._get_brief_context(dbg)
        assert ctx["pc"] is None
        assert ctx["function"] is None
        assert ctx["source_file"] is None
        assert ctx["source_line"] is None

    def test_graceful_when_symbol_fails_but_pc_works(self):
        dbg = MagicMock()
        def _side_effect(expr):
            if expr == "Register(PC)":
                return "0x2000"
            raise RuntimeError("no symbols")
        dbg.fnc.side_effect = _side_effect
        ctx = srv._get_brief_context(dbg)
        assert ctx["pc"] == "0x2000"
        assert ctx["function"] is None


class TestFormatHexDump:
    def test_single_line(self):
        data = bytes(range(16))
        dump = srv._format_hex_dump(data, 0x1000)
        assert "00000000" not in dump  # base is 0x1000
        assert "00001000" in dump
        assert "00 01 02" in dump
        # ASCII portion
        assert "|" in dump

    def test_multiple_lines(self):
        data = bytes(range(32))
        dump = srv._format_hex_dump(data, 0)
        lines = dump.strip().split("\n")
        assert len(lines) == 2

    def test_partial_last_line(self):
        data = bytes(range(20))
        dump = srv._format_hex_dump(data, 0)
        lines = dump.strip().split("\n")
        assert len(lines) == 2

    def test_non_printable_shown_as_dot(self):
        data = bytes([0x00, 0x7F, 0xFF, 0x41])  # non-printable + 'A'
        dump = srv._format_hex_dump(data, 0)
        assert "A" in dump
        assert "." in dump

    def test_empty_data(self):
        dump = srv._format_hex_dump(b"", 0)
        assert dump == ""


EXPECTED_TOOLS = {
    "connect", "disconnect", "ping", "get_state", "get_message",
    "go", "break_", "step", "step_asm", "step_hll",
    "step_over", "go_up", "go_return",
    "run_command", "evaluate_function", "run_practice_script",
    "read_memory", "read_memory_typed", "write_memory_typed",
    "read_register", "read_all_registers", "write_register",
    "set_breakpoint", "list_breakpoints", "delete_breakpoint",
    "read_variable", "write_variable",
    "query_symbol_by_name", "query_symbol_by_address",
    "get_practice_macro", "set_practice_macro",
}


class TestListTools:
    def _tools(self):
        return run(list_tools())

    def test_all_expected_tools_present(self):
        assert {t.name for t in self._tools()} == EXPECTED_TOOLS

    def test_no_extra_tools(self):
        extras = {t.name for t in self._tools()} - EXPECTED_TOOLS
        assert not extras

    def test_every_tool_has_description(self):
        for t in self._tools():
            assert t.description, f"'{t.name}' has no description"

    def test_every_tool_has_input_schema(self):
        for t in self._tools():
            assert t.inputSchema is not None, f"'{t.name}' is missing inputSchema"

    def test_required_fields_declared(self):
        tool_map = {t.name: t for t in self._tools()}
        assert "command" in tool_map["run_command"].inputSchema["required"]
        assert set(tool_map["read_memory"].inputSchema["required"]) == {"address", "length"}
        assert "address" in tool_map["set_breakpoint"].inputSchema["required"]


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — connection group
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectionTools:
    def test_connect_calls_t32_and_stores_dbg(self):
        mock_conn = MagicMock()
        with patch("lauterbachdebugger_mcp.server.t32") as mock_t32:
            mock_t32.connect.return_value = mock_conn
            result = run(call_tool("connect", {
                "node": "192.168.0.1", "port": 10000, "protocol": "UDP", "timeout": 5.0
            }))
        mock_t32.connect.assert_called_once_with(
            node="192.168.0.1", port="10000", protocol="UDP", timeout=5.0
        )
        assert "Connected" in result[0].text
        assert srv._dbg is mock_conn

    def test_connect_uses_defaults_when_no_args(self):
        # Reset to known defaults
        srv._conn_defaults.update(host="localhost", port=20000,
                                  protocol="TCP", timeout=60.0)
        with patch("lauterbachdebugger_mcp.server.t32") as mock_t32:
            mock_t32.connect.return_value = MagicMock()
            run(call_tool("connect", {}))
        mock_t32.connect.assert_called_once_with(
            node="localhost", port="20000", protocol="TCP", timeout=60.0
        )

    def test_connect_disconnects_existing_session(self, mock_dbg):
        with patch("lauterbachdebugger_mcp.server.t32") as mock_t32:
            mock_t32.connect.return_value = MagicMock()
            run(call_tool("connect", {}))
        mock_dbg.disconnect.assert_called_once()

    def test_disconnect_when_connected(self, mock_dbg):
        result = run(call_tool("disconnect", {}))
        mock_dbg.disconnect.assert_called_once()
        assert "Disconnected" in result[0].text
        assert srv._dbg is None

    def test_disconnect_when_not_connected(self):
        result = run(call_tool("disconnect", {}))
        assert "No active" in result[0].text

    def test_ping_success(self, mock_dbg):
        result = run(call_tool("ping", {}))
        mock_dbg.ping.assert_called_once()
        assert "Ping successful" in result[0].text

    def test_ping_not_connected_raises_structured_error(self):
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("ping", {}))
        data = json.loads(str(exc_info.value))
        assert data["error"] == "RuntimeError"
        assert "connect" in data["suggestion"].lower()

    def test_get_state_integer_value(self, mock_dbg):
        mock_dbg.get_state.return_value = 2
        data = json.loads(run(call_tool("get_state", {}))[0].text)
        assert data == {"state": 2, "state_name": "halted"}

    def test_get_state_bytearray_converted(self, mock_dbg):
        mock_dbg.get_state.return_value = bytearray([1, 0, 0, 0])
        data = json.loads(run(call_tool("get_state", {}))[0].text)
        assert data["state"] == 1
        assert data["state_name"] == "running"

    def test_get_state_unknown_code(self, mock_dbg):
        mock_dbg.get_state.return_value = 99
        data = json.loads(run(call_tool("get_state", {}))[0].text)
        assert "99" in data["state_name"]

    def test_get_message(self, mock_dbg):
        mock_dbg.get_message.return_value = MagicMock(text="TRACE32 ready", type="INFO")
        data = json.loads(run(call_tool("get_message", {}))[0].text)
        assert data == {"text": "TRACE32 ready", "type": "INFO"}


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — execution control group
# ─────────────────────────────────────────────────────────────────────────────

class TestExecutionTools:
    def test_go_returns_running_status(self, mock_dbg):
        result = run(call_tool("go", {}))
        mock_dbg.go.assert_called_once()
        data = json.loads(result[0].text)
        assert data["action"] == "go"
        assert data["status"] == "running"

    def test_break_returns_context_with_symbols(self, mock_dbg):
        mock_dbg.fnc.side_effect = lambda expr: {
            "Register(PC)": "0x1000",
            "sYmbol.FUNCTION(D:0x1000)": "main",
            "sYmbol.SOURCEFILE(D:0x1000)": "main.c",
            "sYmbol.SOURCELINE(D:0x1000)": "42",
        }.get(expr, "")
        result = run(call_tool("break_", {}))
        mock_dbg.break_.assert_called_once()
        data = json.loads(result[0].text)
        assert data["action"] == "break"
        assert data["status"] == "halted"
        assert data["pc"] == "0x1000"
        assert data["function"] == "main"

    def test_break_returns_context_without_symbols(self, mock_dbg):
        def _side_effect(expr):
            if expr == "Register(PC)":
                return "0x1000"
            raise RuntimeError("no symbols loaded")
        mock_dbg.fnc.side_effect = _side_effect
        result = run(call_tool("break_", {}))
        data = json.loads(result[0].text)
        assert data["action"] == "break"
        assert data["status"] == "halted"
        assert data["pc"] == "0x1000"
        assert data["function"] is None
        assert data["source_file"] is None
        assert data["source_line"] is None

    @pytest.mark.parametrize("tool", [
        "step", "step_asm", "step_hll", "step_over", "go_up", "go_return",
    ])
    def test_step_tools_return_context_with_symbols(self, tool, mock_dbg):
        mock_dbg.fnc.side_effect = lambda expr: {
            "Register(PC)": "0x2000",
            "sYmbol.FUNCTION(D:0x2000)": "handler",
            "sYmbol.SOURCEFILE(D:0x2000)": "irq.c",
            "sYmbol.SOURCELINE(D:0x2000)": "10",
        }.get(expr, "")
        result = run(call_tool(tool, {}))
        getattr(mock_dbg, tool).assert_called_once()
        data = json.loads(result[0].text)
        assert data["action"] == tool
        assert data["status"] == "completed"
        assert data["pc"] == "0x2000"
        assert data["function"] == "handler"

    @pytest.mark.parametrize("tool", [
        "step", "step_asm", "step_hll", "step_over", "go_up", "go_return",
    ])
    def test_step_tools_return_context_without_symbols(self, tool, mock_dbg):
        def _side_effect(expr):
            if expr == "Register(PC)":
                return "0x2000"
            raise RuntimeError("no symbols loaded")
        mock_dbg.fnc.side_effect = _side_effect
        result = run(call_tool(tool, {}))
        data = json.loads(result[0].text)
        assert data["action"] == tool
        assert data["status"] == "completed"
        assert data["pc"] == "0x2000"
        assert data["function"] is None

    def test_not_connected_raises_structured_error(self):
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("go", {}))
        data = json.loads(str(exc_info.value))
        assert data["error"] == "RuntimeError"


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — PRACTICE group
# ─────────────────────────────────────────────────────────────────────────────

class TestPracticeTools:
    def test_run_command(self, mock_dbg):
        result = run(call_tool("run_command", {"command": "SYStem.Up"}))
        mock_dbg.cmd.assert_called_once_with("SYStem.Up")
        assert "SYStem.Up" in result[0].text

    def test_evaluate_function(self, mock_dbg):
        mock_dbg.fnc.return_value = "0x1234"
        data = json.loads(run(call_tool("evaluate_function", {"function": "Register(PC)"}))[0].text)
        mock_dbg.fnc.assert_called_once_with("Register(PC)")
        assert data == {"function": "Register(PC)", "result": "0x1234"}

    def test_run_practice_script_no_timeout(self, mock_dbg):
        run(call_tool("run_practice_script", {"script_path": "C:/init.cmm"}))
        mock_dbg.cmm.assert_called_once_with("C:/init.cmm", timeout=None)

    def test_run_practice_script_with_timeout(self, mock_dbg):
        run(call_tool("run_practice_script", {"script_path": "C:/init.cmm", "timeout": 30.0}))
        mock_dbg.cmm.assert_called_once_with("C:/init.cmm", timeout=30.0)


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — memory group
# ─────────────────────────────────────────────────────────────────────────────

class TestMemoryTools:
    def test_read_memory_returns_hex_bytes_and_ascii(self, mock_dbg):
        mock_dbg.memory.read.return_value = bytes([0xDE, 0xAD, 0x42, 0xEF])
        data = json.loads(run(call_tool("read_memory", {"address": "0x20000000", "length": 4}))[0].text)
        assert data["hex"] == "dead42ef"
        assert data["length"] == 4
        assert data["bytes"] == [0xDE, 0xAD, 0x42, 0xEF]
        assert data["ascii"] == "..B."  # 0xDE, 0xAD non-printable; 0x42='B'; 0xEF non-printable

    @pytest.mark.parametrize("dtype,method", [
        ("uint8",  "read_uint8"),
        ("int8",   "read_int8"),
        ("uint16", "read_uint16"),
        ("int16",  "read_int16"),
        ("uint32", "read_uint32"),
        ("int32",  "read_int32"),
        ("uint64", "read_uint64"),
        ("int64",  "read_int64"),
        ("float",  "read_float"),
        ("double", "read_double"),
    ])
    def test_read_memory_typed(self, dtype, method, mock_dbg):
        getattr(mock_dbg.memory, method).return_value = 42
        data = json.loads(run(call_tool("read_memory_typed", {
            "address": "0x20000000", "type": dtype
        }))[0].text)
        assert data["type"] == dtype
        assert data["value"] == 42

    @pytest.mark.parametrize("dtype,raw", [
        ("uint32", 0xFF),
        ("int32",  -1),
        ("float",  3.14),
        ("double", 2.718),
    ])
    def test_write_memory_typed(self, dtype, raw, mock_dbg):
        data = json.loads(run(call_tool("write_memory_typed", {
            "address": "0x20000000", "type": dtype, "value": raw
        }))[0].text)
        assert data["status"] == "written"
        assert data["type"] == dtype

    def test_read_memory_calls_address_from_string(self, mock_dbg):
        mock_dbg.memory.read.return_value = b"\x00"
        run(call_tool("read_memory", {"address": "D:0x1000", "length": 1}))
        mock_dbg.address.from_string.assert_called_with("D:0x1000")


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — register group
# ─────────────────────────────────────────────────────────────────────────────

class TestRegisterTools:
    def test_read_register(self, mock_dbg):
        mock_dbg.register.read.return_value = MagicMock(
            to_dict=lambda: {"name": "PC", "value": 0x1000}
        )
        data = json.loads(run(call_tool("read_register", {"name": "PC"}))[0].text)
        mock_dbg.register.read.assert_called_once_with("PC")
        assert data["name"] == "PC"

    def test_read_register_passes_core(self, mock_dbg):
        mock_dbg.register.read.return_value = MagicMock(to_dict=lambda: {})
        run(call_tool("read_register", {"name": "R0", "core": 1}))
        mock_dbg.register.read.assert_called_once_with("R0", core=1)

    def test_read_all_registers(self, mock_dbg):
        mock_dbg.register.read_all.return_value = [
            MagicMock(to_dict=lambda: {"name": f"R{i}"}) for i in range(4)
        ]
        data = json.loads(run(call_tool("read_all_registers", {}))[0].text)
        assert len(data) == 4

    def test_read_all_registers_filtered_by_core_and_unit(self, mock_dbg):
        mock_dbg.register.read_all.return_value = []
        run(call_tool("read_all_registers", {"core": 0, "unit": "FPU"}))
        mock_dbg.register.read_all.assert_called_once_with(core=0, unit="FPU")

    def test_write_register_integer(self, mock_dbg):
        mock_dbg.register.write.return_value = MagicMock(to_dict=lambda: {})
        run(call_tool("write_register", {"name": "PC", "value": 0x1000}))
        mock_dbg.register.write.assert_called_once_with("PC", 0x1000)

    def test_write_register_float(self, mock_dbg):
        mock_dbg.register.write.return_value = MagicMock(to_dict=lambda: {})
        run(call_tool("write_register", {"name": "S0", "value": 3.14}))
        mock_dbg.register.write.assert_called_once_with("S0", 3.14)


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — breakpoint group
# ─────────────────────────────────────────────────────────────────────────────

class TestBreakpointTools:
    def test_set_breakpoint_returns_enriched_info(self, mock_dbg):
        mock_dbg.breakpoint.set.return_value = MagicMock(__str__=lambda s: "BP@0x1000")
        mock_dbg.fnc.return_value = "main"
        result = run(call_tool("set_breakpoint", {"address": "0x08000100"}))
        assert mock_dbg.breakpoint.set.called
        data = json.loads(result[0].text)
        assert data["breakpoint"] == "BP@0x1000"
        assert data["address"] == "0x08000100"
        assert data["type"] == "PROGRAM"
        assert data["impl"] == "AUTO"

    def test_list_breakpoints_returns_enriched_list(self, mock_dbg):
        bp_mocks = []
        for i in range(3):
            bp = MagicMock(__str__=lambda s, n=i: f"BP{n}")
            bp.address = MagicMock(__str__=lambda s, n=i: f"0x{n}000")
            bp_mocks.append(bp)
        mock_dbg.breakpoint.list.return_value = bp_mocks
        mock_dbg.fnc.return_value = None
        data = json.loads(run(call_tool("list_breakpoints", {}))[0].text)
        assert len(data) == 3
        assert "breakpoint" in data[0]

    def test_delete_breakpoint(self, mock_dbg):
        mock_bp = MagicMock()
        mock_dbg.breakpoint.return_value = mock_bp
        result = run(call_tool("delete_breakpoint", {"address": "0x08000100"}))
        mock_bp.delete.assert_called_once()
        assert "deleted" in result[0].text


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — variable group
# ─────────────────────────────────────────────────────────────────────────────

class TestVariableTools:
    def test_read_variable(self, mock_dbg):
        mock_dbg.variable.read.return_value = MagicMock(
            to_dict=lambda: {"name": "myVar", "value": 42}
        )
        data = json.loads(run(call_tool("read_variable", {"name": "myVar"}))[0].text)
        mock_dbg.variable.read.assert_called_once_with("myVar")
        assert data["value"] == 42

    def test_write_variable_integer(self, mock_dbg):
        mock_dbg.variable.write.return_value = MagicMock(to_dict=lambda: {})
        run(call_tool("write_variable", {"name": "x", "value": 7}))
        mock_dbg.variable.write.assert_called_once_with("x", 7)

    def test_write_variable_float(self, mock_dbg):
        mock_dbg.variable.write.return_value = MagicMock(to_dict=lambda: {})
        run(call_tool("write_variable", {"name": "f", "value": 1.5}))
        mock_dbg.variable.write.assert_called_once_with("f", 1.5)

    def test_write_variable_string_parsed_as_int(self, mock_dbg):
        mock_dbg.variable.write.return_value = MagicMock(to_dict=lambda: {})
        run(call_tool("write_variable", {"name": "x", "value": "0xFF"}))
        mock_dbg.variable.write.assert_called_once_with("x", 255)

    def test_write_variable_string_parsed_as_float(self, mock_dbg):
        mock_dbg.variable.write.return_value = MagicMock(to_dict=lambda: {})
        run(call_tool("write_variable", {"name": "f", "value": "3.14"}))
        args = mock_dbg.variable.write.call_args[0]
        assert args[0] == "f"
        assert args[1] == pytest.approx(3.14)


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — symbol group
# ─────────────────────────────────────────────────────────────────────────────

class TestSymbolTools:
    def _sym(self, name="main", path="mod\\main", addr="P:0x1000", size=100):
        s = MagicMock()
        s.name, s.path, s.size = name, path, size
        s.address = MagicMock(__str__=lambda _: addr)
        return s

    def test_query_by_name(self, mock_dbg):
        mock_dbg.symbol.query_by_name.return_value = self._sym()
        data = json.loads(run(call_tool("query_symbol_by_name", {"name": "main"}))[0].text)
        mock_dbg.symbol.query_by_name.assert_called_once_with("main")
        assert data["name"] == "main"
        assert data["size"] == 100

    def test_query_by_address(self, mock_dbg):
        mock_dbg.symbol.query_by_address.return_value = self._sym()
        data = json.loads(run(call_tool("query_symbol_by_address", {"address": "0x1000"}))[0].text)
        assert data["name"] == "main"

    def test_null_address_serialised_as_none(self, mock_dbg):
        sym = self._sym()
        sym.address = None
        mock_dbg.symbol.query_by_name.return_value = sym
        data = json.loads(run(call_tool("query_symbol_by_name", {"name": "main"}))[0].text)
        assert data["address"] is None


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — PRACTICE macro group
# ─────────────────────────────────────────────────────────────────────────────

class TestMacroTools:
    def test_get_practice_macro(self, mock_dbg):
        mock_dbg.practice.get_macro.return_value = MagicMock(
            to_dict=lambda: {"name": "FOO", "value": "bar"}
        )
        data = json.loads(run(call_tool("get_practice_macro", {"name": "FOO"}))[0].text)
        mock_dbg.practice.get_macro.assert_called_once_with("FOO")
        assert data["value"] == "bar"

    def test_set_practice_macro(self, mock_dbg):
        mock_dbg.practice.set_macro.return_value = MagicMock(
            to_dict=lambda: {"name": "FOO", "value": "baz"}
        )
        data = json.loads(run(call_tool("set_practice_macro", {"name": "FOO", "value": "baz"}))[0].text)
        mock_dbg.practice.set_macro.assert_called_once_with("FOO", "baz")
        assert data["value"] == "baz"


# ─────────────────────────────────────────────────────────────────────────────
# call_tool — error handling
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorHelper:
    """Tests for the _error() helper function."""

    def test_error_raises_value_error(self):
        with pytest.raises(ValueError):
            _error(RuntimeError("something failed"))

    def test_error_message_is_json(self):
        with pytest.raises(ValueError) as exc_info:
            _error(RuntimeError("something failed"))
        data = json.loads(str(exc_info.value))
        assert data["error"] == "RuntimeError"
        assert data["message"] == "something failed"

    def test_error_includes_suggestion_when_given(self):
        with pytest.raises(ValueError) as exc_info:
            _error(RuntimeError("fail"), suggestion="Try this instead.")
        data = json.loads(str(exc_info.value))
        assert data["suggestion"] == "Try this instead."

    def test_error_no_suggestion_when_omitted(self):
        with pytest.raises(ValueError) as exc_info:
            _error(RuntimeError("fail"))
        data = json.loads(str(exc_info.value))
        assert "suggestion" not in data


class TestErrorHandling:
    def test_unknown_tool_name(self, mock_dbg):
        result = run(call_tool("nonexistent_tool", {}))
        assert "Unknown tool" in result[0].text

    def test_debugger_exception_raises_with_structured_json(self, mock_dbg):
        mock_dbg.go.side_effect = RuntimeError("hardware fault")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("go", {}))
        data = json.loads(str(exc_info.value))
        assert data["error"] == "RuntimeError"
        assert "hardware fault" in data["message"]

    def test_generic_exception_raises_with_error_type(self, mock_dbg):
        mock_dbg.step.side_effect = Exception("boom")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("step", {}))
        data = json.loads(str(exc_info.value))
        assert data["error"] == "Exception"
        assert "boom" in data["message"]

    def test_not_connected_error_suggests_connect(self):
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("read_register", {"name": "PC"}))
        data = json.loads(str(exc_info.value))
        assert data["error"] == "RuntimeError"
        assert "connect" in data["suggestion"].lower()


class TestStructuredErrors:
    """Tests for typed pyrcl exception handling with suggestions."""

    def _assert_error(self, exc_info, expected_error, suggestion_fragment):
        data = json.loads(str(exc_info.value))
        assert data["error"] == expected_error
        assert "suggestion" in data
        assert suggestion_fragment.lower() in data["suggestion"].lower()

    def test_api_connection_error(self, mock_dbg):
        from lauterbachdebugger_mcp.server import T32ApiConnectionError
        mock_dbg.go.side_effect = T32ApiConnectionError("connection lost")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("go", {}))
        self._assert_error(exc_info, "ApiConnectionError", "connect")

    def test_command_error(self, mock_dbg):
        from lauterbachdebugger_mcp.server import T32CommandError
        mock_dbg.cmd.side_effect = T32CommandError("bad command")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("run_command", {"command": "BAD"}))
        self._assert_error(exc_info, "CommandError", "syntax")

    def test_function_error(self, mock_dbg):
        from lauterbachdebugger_mcp.server import T32FunctionError
        mock_dbg.fnc.side_effect = T32FunctionError("no such function")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("evaluate_function", {"function": "BAD()"}))
        self._assert_error(exc_info, "FunctionError", "halted")

    def test_memory_read_access_error(self, mock_dbg):
        from lauterbachdebugger_mcp.server import T32MemoryReadAccessError
        mock_dbg.memory.read.side_effect = T32MemoryReadAccessError("access denied")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("read_memory", {"address": "0x0", "length": 4}))
        self._assert_error(exc_info, "MemoryReadAccessError", "halt")

    def test_memory_write_access_error(self, mock_dbg):
        from lauterbachdebugger_mcp.server import T32MemoryWriteAccessError
        mock_dbg.memory.write_uint32.side_effect = T32MemoryWriteAccessError("read-only")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("write_memory_typed", {
                "address": "0x0", "type": "uint32", "value": 0
            }))
        self._assert_error(exc_info, "MemoryWriteAccessError", "read-only")

    def test_variable_error(self, mock_dbg):
        from lauterbachdebugger_mcp.server import T32VariableError
        mock_dbg.variable.read.side_effect = T32VariableError("no symbols")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("read_variable", {"name": "myVar"}))
        self._assert_error(exc_info, "VariableError", "debug symbols")

    def test_symbol_error(self, mock_dbg):
        from lauterbachdebugger_mcp.server import T32SymbolError
        mock_dbg.symbol.query_by_name.side_effect = T32SymbolError("unknown symbol")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("query_symbol_by_name", {"name": "missing"}))
        self._assert_error(exc_info, "SymbolError", "debug symbols")

    def test_register_error(self, mock_dbg):
        from lauterbachdebugger_mcp.server import T32RegisterError
        mock_dbg.register.read.side_effect = T32RegisterError("invalid register")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("read_register", {"name": "BADREG"}))
        self._assert_error(exc_info, "RegisterError", "halt")

    def test_breakpoint_error(self, mock_dbg):
        from lauterbachdebugger_mcp.server import T32BreakpointError
        mock_dbg.breakpoint.set.side_effect = T32BreakpointError("bp failed")
        with pytest.raises(ValueError) as exc_info:
            run(call_tool("set_breakpoint", {"address": "0x1000"}))
        self._assert_error(exc_info, "BreakpointError", "breakpoint")


# ─────────────────────────────────────────────────────────────────────────────
# serve() — auto-connect behaviour
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _mock_stdio():
    """Async context manager that stands in for stdio_server()."""
    yield MagicMock(), MagicMock()


class TestServe:
    def _run_serve(self, host="localhost", port=20000, protocol="TCP",
                   timeout=60.0):
        async def _inner():
            with patch("lauterbachdebugger_mcp.server.stdio_server", _mock_stdio), \
                 patch("lauterbachdebugger_mcp.server.server") as mock_srv:
                mock_srv.run = AsyncMock()
                mock_srv.create_initialization_options.return_value = {}
                await serve(host, port, protocol, timeout)
        asyncio.run(_inner())

    def _run_serve_real_server(self, **kwargs):
        """Run serve() without replacing the server object.

        Patches only server.run and stdio_server so that serve() can
        modify server.instructions on the real Server instance.
        """
        async def _inner():
            with patch("lauterbachdebugger_mcp.server.stdio_server", _mock_stdio), \
                 patch.object(srv.server, "run", new=AsyncMock()), \
                 patch.object(srv.server, "create_initialization_options",
                              return_value={}):
                await serve("localhost", 20000, "TCP", 60.0, **kwargs)
        asyncio.run(_inner())

    def test_auto_connect_task_is_created(self):
        """serve() creates a background auto-connect task."""
        with patch("lauterbachdebugger_mcp.server.t32"):
            self._run_serve("192.168.0.1", 9000, "UDP", 5.0)
        # The task was created (may be done or cancelled by now)
        assert srv._auto_connect_task is not None

    def test_auto_connect_success_sets_dbg(self):
        """Successful auto-connect sets the global debugger handle."""
        mock_conn = MagicMock()
        with patch("lauterbachdebugger_mcp.server.t32") as mock_t32:
            mock_t32.connect.return_value = mock_conn
            # Run _try_auto_connect directly to test the logic
            asyncio.run(srv._try_auto_connect(
                "localhost", 20000, "TCP", 60.0))
        assert srv._dbg is mock_conn

    def test_auto_connect_failure_does_not_abort_server(self):
        """Failed auto-connect logs warning; server keeps running."""
        with patch("lauterbachdebugger_mcp.server.t32") as mock_t32:
            mock_t32.connect.side_effect = ConnectionRefusedError("no T32")
            self._run_serve()  # must not raise
        assert srv._dbg is None

    def test_auto_connect_skips_if_already_connected(self):
        """If user connected via tool before auto-connect finishes, discard."""
        existing = MagicMock()
        srv._dbg = existing
        new_conn = MagicMock()
        with patch("lauterbachdebugger_mcp.server.t32") as mock_t32:
            mock_t32.connect.return_value = new_conn
            asyncio.run(srv._try_auto_connect(
                "localhost", 20000, "TCP", 60.0))
        # Existing connection preserved, new one disconnected
        assert srv._dbg is existing
        new_conn.disconnect.assert_called_once()

    def test_stores_connection_defaults(self):
        """serve() must store CLI params so the connect tool can use them."""
        self._run_serve("10.0.0.1", 9999, "UDP", 5.0)
        assert srv._conn_defaults["host"] == "10.0.0.1"
        assert srv._conn_defaults["port"] == 9999
        assert srv._conn_defaults["protocol"] == "UDP"
        assert srv._conn_defaults["timeout"] == 5.0

    def test_stdio_server_run_is_called(self):
        with patch("lauterbachdebugger_mcp.server.stdio_server", _mock_stdio), \
             patch("lauterbachdebugger_mcp.server.server") as mock_srv:
            mock_srv.run = AsyncMock()
            mock_srv.create_initialization_options.return_value = {}
            asyncio.run(serve("localhost", 20000, "TCP", 60.0))
        mock_srv.run.assert_awaited_once()

    def test_stores_t32_dir_config(self):
        """serve() must store t32_dir in _config."""
        self._run_serve()
        assert srv._config["t32_dir"] == "~/t32"

    def test_stores_custom_t32_dir_config(self):
        async def _inner():
            with patch("lauterbachdebugger_mcp.server.stdio_server", _mock_stdio), \
                 patch("lauterbachdebugger_mcp.server.server") as mock_srv:
                mock_srv.run = AsyncMock()
                mock_srv.create_initialization_options.return_value = {}
                await serve("localhost", 20000, "TCP", 60.0,
                             t32_dir="/custom/t32")
        asyncio.run(_inner())
        assert srv._config["t32_dir"] == "/custom/t32"

    def test_stores_hints_config(self):
        """serve() must store hints in _config."""
        self._run_serve()
        assert srv._config["hints"] is None

    def test_stores_hints_file_config(self):
        async def _inner():
            with patch("lauterbachdebugger_mcp.server.stdio_server", _mock_stdio), \
                 patch("lauterbachdebugger_mcp.server.server") as mock_srv:
                mock_srv.run = AsyncMock()
                mock_srv.create_initialization_options.return_value = {}
                await serve("localhost", 20000, "TCP", 60.0,
                             hints="/my/tips.md")
        asyncio.run(_inner())
        assert srv._config["hints"] == "/my/tips.md"

    def test_stores_hints_directory_config(self):
        async def _inner():
            with patch("lauterbachdebugger_mcp.server.stdio_server", _mock_stdio), \
                 patch("lauterbachdebugger_mcp.server.server") as mock_srv:
                mock_srv.run = AsyncMock()
                mock_srv.create_initialization_options.return_value = {}
                await serve("localhost", 20000, "TCP", 60.0,
                             hints="/my/hints-dir")
        asyncio.run(_inner())
        assert srv._config["hints"] == "/my/hints-dir"

    def test_hints_embedded_in_instructions(self, tmp_path):
        hints = tmp_path / "tips.md"
        hints.write_text("# My Board Tips\nAlways reset before flash.\n")
        self._run_serve_real_server(hints_file=str(hints))
        assert "My Board Tips" in srv.server.instructions
        assert "Always reset before flash" in srv.server.instructions

    def test_instructions_reset_without_hints(self):
        """Without hints, serve() resets instructions to the static default."""
        srv.server.instructions = "modified"
        self._run_serve_real_server()
        assert srv.server.instructions == srv.INSTRUCTIONS

    def test_connect_tool_uses_stored_defaults(self):
        """When connect tool is called with no args, it uses serve() defaults."""
        srv._conn_defaults.update(host="10.0.0.1", port=9999,
                                  protocol="UDP", timeout=5.0)
        with patch("lauterbachdebugger_mcp.server.t32") as mock_t32:
            mock_t32.connect.return_value = MagicMock()
            run(call_tool("connect", {}))
        mock_t32.connect.assert_called_once_with(
            node="10.0.0.1", port="9999", protocol="UDP", timeout=5.0
        )

    def test_connect_tool_cancels_pending_auto_connect(self):
        """Explicit connect cancels a pending auto-connect task."""
        # Simulate a pending (not yet done) auto-connect task
        pending_task = AsyncMock(spec=asyncio.Task)
        pending_task.done.return_value = False
        pending_task.cancel.return_value = True
        srv._auto_connect_task = pending_task

        with patch("lauterbachdebugger_mcp.server.t32") as mock_t32:
            mock_t32.connect.return_value = MagicMock()
            run(call_tool("connect", {}))

        pending_task.cancel.assert_called_once()
