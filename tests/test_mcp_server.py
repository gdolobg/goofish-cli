from types import SimpleNamespace

import pytest

from goofish_cli import mcp_server


def test_main_rejects_interactive_terminal(monkeypatch, capsys):
    monkeypatch.setattr(mcp_server.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(mcp_server, "_register_all", lambda: pytest.fail("registered tools"))
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: pytest.fail("started MCP server"))

    with pytest.raises(SystemExit) as exc_info:
        mcp_server.main()

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "MCP stdio server" in stderr
    assert "goofish --help" in stderr


def test_main_starts_mcp_server_for_piped_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(mcp_server, "_register_all", lambda: calls.append("register"))
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: calls.append("run"))

    mcp_server.main()

    assert calls == ["register", "run"]


def test_all_commands_register_as_mcp_tools():
    mcp_server._register_all()


@pytest.mark.parametrize("sandbox, name, reused", [(True, "view", True), (True, "get", False), (False, "view", False)])
async def test_only_sandbox_browser_reads_use_persistent_runtime(monkeypatch, sandbox, name, reused):
    from goofish_cli.core import browser_runtime
    from goofish_cli.core.registry import Command
    calls, registered = [], []
    def function(item_id: str):
        return {"item_id": item_id}
    def tool(**kwargs):
        return lambda handler: registered.append(handler)
    async def execute(fn, kwargs):
        calls.append("persistent")
        return fn(**kwargs)
    monkeypatch.setenv("GOOFISH_DESKTOP_SANDBOX", "1" if sandbox else "0")
    monkeypatch.setattr(browser_runtime, "execute_browser_command", execute)
    mcp_server._register_one(Command("item", name, "read", function), SimpleNamespace(tool=tool))
    result = await registered[0](item_id="123")
    assert result == {"ok": True, "data": {"item_id": "123"}}
    assert bool(calls) == reused
