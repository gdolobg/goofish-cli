"""浏览器回写必须保留另一个进程已经保存的新登录。"""

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from goofish_cli.core import sandbox_browser as sb
from goofish_cli.core import session as sm
from goofish_cli.core.crypto import decrypt_cookies


def records(user):
    return [
        {"name": key, "value": value, "domain": ".goofish.com", "path": "/"}
        for key, value in {
            "unb": user,
            "cookie2": "synthetic-" + user,
            "_m_h5_tk": "synthetic_token",
        }.items()
    ]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    source = tmp_path / "cookies.json"
    old, new = records("synthetic-A"), records("synthetic-B")
    sm.write_cookies_json(source, old)
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(source))
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    monkeypatch.setattr(sb, "STATE_PATH", tmp_path / "desktop-session.enc")
    monkeypatch.setattr(sb, "_generation", lambda: "synthetic-daemon")
    monkeypatch.setattr(sm, "_load_or_mint_device_id", lambda _: "synthetic-device")

    async def noop():
        pass

    async def cookies():
        return old

    context = SimpleNamespace(cookies=cookies, pages=[SimpleNamespace(bring_to_front=noop)])

    async def connect(_):
        return SimpleNamespace(contexts=[context])

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

    monkeypatch.setattr(sb, "async_playwright", playwright)
    return source, old, new


def saved_user(source):
    return sm._records_to_flat(decrypt_cookies(source.read_bytes()))["unb"]


def concurrent_login(source, new):
    # 独立进程通过生产写入函数和真实文件锁提交合成登录，不操作真实凭证。
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; from pathlib import Path; "
            "from goofish_cli.core.session import write_cookies_json; "
            "write_cookies_json(Path(sys.argv[1]), json.loads(sys.argv[2]))",
            str(source),
            json.dumps(new),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


async def test_login_during_snapshot_decode_survives_browser_exit(sandbox, monkeypatch):
    source, _, new = sandbox
    original_decode = sm.decrypt_cookies
    injected = False

    def decode_then_login(raw):
        nonlocal injected
        decoded = original_decode(raw)
        if not injected:
            injected = True
            concurrent_login(source, new)
        return decoded

    monkeypatch.setattr(sm, "decrypt_cookies", decode_then_login)
    async with sb.sandbox_page():
        assert injected
        assert saved_user(source) == "synthetic-B"
    assert saved_user(source) == "synthetic-B"


async def test_explicit_cookies_cannot_overwrite_an_unrelated_saved_login(sandbox):
    source, old, new = sandbox
    concurrent_login(source, new)
    async with sb.sandbox_page(cookies=old):
        assert saved_user(source) == "synthetic-B"
    assert saved_user(source) == "synthetic-B"


async def test_login_after_browser_entry_survives_exit(sandbox):
    source, _, new = sandbox
    async with sb.sandbox_page():
        concurrent_login(source, new)
    assert saved_user(source) == "synthetic-B"


async def test_unchanged_login_accepts_browser_cookie_refresh(sandbox):
    source, old, _ = sandbox
    async with sb.sandbox_page():
        old.append(
            {
                "name": "refreshed",
                "value": "synthetic-refresh",
                "domain": ".goofish.com",
                "path": "/",
            }
        )
    assert (
        sm._records_to_flat(decrypt_cookies(source.read_bytes()))["refreshed"]
        == "synthetic-refresh"
    )


@pytest.mark.parametrize("invalidate", [None, "generation", "disconnect", "failure"])
async def test_browser_connection_reuse_and_recovery(monkeypatch, invalidate):
    from goofish_cli.core import browser_runtime
    monkeypatch.setattr(browser_runtime, "browser_connection_reuse_enabled", lambda: True)
    opened, closed = [], []

    @asynccontextmanager
    async def playwright():
        connected = [True]
        browser = SimpleNamespace(is_connected=lambda: connected[0])
        async def connect(_):
            opened.append(connected)
            return browser
        try:
            yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
        finally:
            closed.append(browser)

    monkeypatch.setattr(sb, "async_playwright", playwright)
    try:
        async with sb._browser_connection("generation-1") as first:
            pass
        assert len(opened) == 1 and len(closed) == 0
        if invalidate == "disconnect":
            opened[0][0] = False
        if invalidate == "failure":
            with pytest.raises(RuntimeError, match="uncertain"):
                async with sb._browser_connection("generation-1"):
                    raise RuntimeError("uncertain")
            assert len(opened) == 1  # Never retry the failed operation.
        generation = "generation-2" if invalidate == "generation" else "generation-1"
        async with sb._browser_connection(generation) as second:
            assert (first is second) == (invalidate is None)
        assert len(opened) == (1 if invalidate is None else 2)
    finally:
        await sb.close_browser_connection()
    assert len(closed) == len(opened)


async def test_mcp_browser_commands_share_loop_and_thread():
    import asyncio
    import threading

    from goofish_cli.core import browser_runtime as runtime

    async def identity():
        return asyncio.get_running_loop(), threading.get_ident()

    def command():
        return runtime.run_browser_command(identity())

    try:
        first = await runtime.execute_browser_command(command, {})
        second = await runtime.execute_browser_command(command, {})
        assert first == second
        assert first[0] is not asyncio.get_running_loop()
    finally:
        runtime.close_browser_runtime()

@pytest.mark.parametrize('connect_fails', [False, True])
async def test_connection_cleanup_error_preserves_original_failure(monkeypatch, connect_fails):
    from goofish_cli.core import browser_runtime
    from goofish_cli.core.errors import GoofishError

    monkeypatch.setattr(browser_runtime, 'browser_connection_reuse_enabled', lambda: True)

    @asynccontextmanager
    async def playwright():
        async def connect(_):
            if connect_fails:
                raise RuntimeError('connect failed')
            return SimpleNamespace(is_connected=lambda: True)
        try:
            yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
        finally:
            raise RuntimeError('cleanup failed')

    monkeypatch.setattr(sb, 'async_playwright', playwright)
    error = GoofishError if connect_fails else RuntimeError
    message = '无法连接沙箱浏览器' if connect_fails else 'operation failed'
    with pytest.raises(error, match=message):
        async with sb._browser_connection('test-generation'):
            raise RuntimeError('operation failed')
    assert getattr(asyncio.get_running_loop(), '_goofish_browser_connection', None) is None
