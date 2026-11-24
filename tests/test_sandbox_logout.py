from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from filelock import FileLock
from typer.testing import CliRunner

from goofish_cli.cli import app
from goofish_cli.core import sandbox_browser as sb
from goofish_cli.core.errors import GoofishError
from goofish_cli.sandbox import cli


@pytest.fixture
def state(tmp_path, monkeypatch):
    source = tmp_path / "cookies.json"
    snapshot = tmp_path / "desktop-session.enc"
    device = tmp_path / "device.json"
    for path in (source, snapshot, device):
        path.write_text("synthetic-login")
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    monkeypatch.setattr(sb, "STATE_PATH", snapshot)
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(source))
    page = SimpleNamespace(close=AsyncMock())
    cdp = SimpleNamespace(send=AsyncMock(), detach=AsyncMock())
    context = SimpleNamespace(
        pages=[page], new_page=AsyncMock(return_value=page),
        new_cdp_session=AsyncMock(return_value=cdp), clear_cookies=AsyncMock(),
    )
    connect = AsyncMock(return_value=SimpleNamespace(contexts=[context]))

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

    monkeypatch.setattr(sb, "async_playwright", playwright)
    return (source, snapshot, device), context, cdp, connect


async def test_logout_clears_browser_and_files_and_is_repeatable(state):
    paths, context, cdp, _ = state
    await sb.clear_login()
    assert all(not path.exists() for path in paths)
    context.pages[0].close.assert_awaited_once()
    context.clear_cookies.assert_awaited_once()
    assert cdp.send.await_count == (len(sb.ALLOWED_HOSTS) + 3) * 2
    await sb.clear_login()


async def test_logout_busy_preserves_login(state):
    paths, _, _, connect = state
    with FileLock(sb.ROOT / "desktop-browser.lock"):
        with pytest.raises(GoofishError, match="正在处理"):
            await sb.clear_login()
    assert all(path.exists() for path in paths)
    connect.assert_not_awaited()


async def test_logout_browser_failure_does_not_claim_success(state):
    paths, _, _, connect = state
    connect.side_effect = RuntimeError("browser unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        await sb.clear_login()
    assert all(path.exists() for path in paths)


def test_logout_rejects_host(monkeypatch):
    from goofish_cli.commands.auth.logout import logout

    monkeypatch.delenv("GOOFISH_DESKTOP_SANDBOX", raising=False)
    with pytest.raises(GoofishError, match="仅允许"):
        logout()


def test_sandbox_logout_forwards_to_container(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_host_only", lambda: None)
    monkeypatch.setattr(cli, "_require_running", lambda: None)
    monkeypatch.setattr(cli, "_docker", lambda *args, **kwargs: (
        calls.append(args) or SimpleNamespace(returncode=0)
    ))
    result = CliRunner().invoke(app, ["sandbox", "logout"])
    assert result.exit_code == 0
    assert calls == [("exec", cli.CONTAINER, "goofish", "auth", "logout", "--format", "json")]


async def test_logout_exposed_to_mcp():
    from mcp.server.fastmcp import FastMCP
    from goofish_cli.core.registry import discover, registry
    from goofish_cli.mcp_server import _register_one

    discover()
    server = FastMCP("test")
    _register_one(registry()["auth.logout"], server)
    assert [tool.name for tool in await server.list_tools()] == ["auth_logout"]
