"""串行使用沙箱内常驻浏览器，保存真实会话而不重建已验证窗口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from filelock import FileLock, Timeout
from loguru import logger
from playwright.async_api import async_playwright

from .browser_cookie import ALLOWED_HOSTS
from .crypto import decrypt_cookies, encrypt_cookies
from .errors import AuthRequiredError, GoofishError
from .local_state import atomic_write_bytes
from .session import (
    _coerce_records,
    _records_to_flat,
    read_cookie_snapshot,
    resolve_cookie_path,
    save_cookies_if_unchanged,
)

ENDPOINT = "http://127.0.0.1:9222"
ROOT = Path.home() / ".goofish-cli"
STATE_PATH = ROOT / "desktop-session.enc"


async def clear_login() -> None:
    """与浏览器任务互斥，防止旧会话在清除后回写。"""
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(ROOT / "desktop-browser.lock", timeout=0, mode=0o600):
            source = resolve_cookie_path().resolve()
            source.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(source) + ".lock", timeout=5, mode=0o600):
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.connect_over_cdp(ENDPOINT)
                    context = browser.contexts[0]
                    # 先停止页面请求，避免清除后页面立即重新写入旧会话。
                    for page in list(context.pages):
                        await page.close()
                    page = await context.new_page()
                    cdp = await context.new_cdp_session(page)
                    try:
                        # 清除相关站点的持久存储；Cookie 在整个沙箱上下文中清除。
                        for host in (*ALLOWED_HOSTS, "www.goofish.com", "passport.goofish.com", "login.taobao.com"):
                            for scheme in ("http", "https"):
                                await cdp.send("Storage.clearDataForOrigin", {
                                    "origin": f"{scheme}://{host}", "storageTypes": "all",
                                })
                        await context.clear_cookies()
                    finally:
                        await cdp.detach()
                source.unlink(missing_ok=True)
                STATE_PATH.unlink(missing_ok=True)
                (ROOT / "device.json").unlink(missing_ok=True)
    except Timeout as exc:
        raise GoofishError("沙箱正在处理任务或登录态被占用，请完成后再退出登录") from exc


def _allowed(cookie: dict) -> bool:
    host = cookie.get("domain", "").lstrip(".")
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def _missing_cookies(saved: list[dict], current: list[dict]) -> list[dict]:
    # Chrome 自己已持久化的 cookie 优先，不能用旧快照回滚新 token。
    keys = {(c["name"], c["domain"], c["path"]) for c in current}
    return [c for c in saved if _allowed(c) and (c["name"], c["domain"], c["path"]) not in keys]


def _read_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        state = decrypt_cookies(STATE_PATH.read_bytes())
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
            raise ValueError("invalid session state")
        for cookie in state["cookies"]:
            if not isinstance(cookie, dict) or not all(
                isinstance(cookie.get(k), str) for k in ("name", "value", "domain", "path")
            ):
                raise ValueError("invalid cookie record")
        return state
    except (OSError, ValueError, TypeError) as exc:
        raise GoofishError("沙箱会话快照不可读取；原文件保留，请检查后再继续") from exc


def _generation() -> str:
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(ENDPOINT + "/json/version", timeout=3) as response:
            value = json.load(response)["webSocketDebuggerUrl"]
        return hashlib.sha256(value.encode()).hexdigest()
    except (OSError, ValueError, KeyError) as exc:
        raise GoofishError("沙箱浏览器未就绪；请检查沙箱状态，不会启动本机浏览器") from exc


async def close_browser_connection():
    loop = asyncio.get_running_loop()
    cached = getattr(loop, "_goofish_browser_connection", None)
    loop._goofish_browser_connection = None
    if cached is not None:
        await cached[1].__aexit__(None, None, None)


async def _connect_browser(playwright):
    try:
        return await playwright.chromium.connect_over_cdp(ENDPOINT)
    except Exception as exc:
        raise GoofishError("无法连接沙箱浏览器；不会回退到本机浏览器") from exc


@asynccontextmanager
async def _browser_connection(generation):
    from .browser_runtime import browser_connection_reuse_enabled

    if not browser_connection_reuse_enabled():
        async with async_playwright() as playwright:
            yield await _connect_browser(playwright)
        return
    loop = asyncio.get_running_loop()
    cached = getattr(loop, "_goofish_browser_connection", None)
    if cached is not None and (cached[0] != generation or not cached[2].is_connected()):
        await close_browser_connection()
        cached = None
    if cached is None:
        manager = async_playwright()
        playwright = await manager.__aenter__()
        try:
            browser = await _connect_browser(playwright)
        except BaseException:
            with suppress(Exception):
                await manager.__aexit__(None, None, None)
            raise
        cached = (generation, manager, browser)
        loop._goofish_browser_connection = cached
    try:
        yield cached[2]
    except BaseException:
        # Discard a failed connection, but never replay the uncertain operation.
        with suppress(Exception):
            await close_browser_connection()
        raise


@asynccontextmanager
async def sandbox_page(*, cookies=None, persist_session: bool = True):
    from .browser import _cookies_to_playwright

    source = resolve_cookie_path()
    if cookies is None:
        records, original_digest = read_cookie_snapshot(source)
    else:
        # 外部传入的记录没有磁盘版本来源，不能据此回写 CLI 登录文件。
        records, original_digest = _coerce_records(cookies), None
    expected_user = _records_to_flat(records).get("unb")
    ROOT.mkdir(parents=True, exist_ok=True)
    lock = FileLock(ROOT / "desktop-browser.lock", timeout=0, mode=0o600)
    try:
        lock.acquire()
    except Timeout as exc:
        raise GoofishError("沙箱浏览器正在处理另一个任务或等待人工验证；请完成后再试") from exc
    try:
        generation = _generation()
        state = _read_state()
        async with _browser_connection(generation) as browser:
            context = browser.contexts[0]
            current_cookies = await context.cookies()
            current = _records_to_flat(current_cookies)
            if expected_user and current.get("unb") not in (None, expected_user):
                raise AuthRequiredError("沙箱浏览器账号与 CLI 登录态不同，请在沙箱内重新登录")
            if state and state.get("generation") != generation:
                saved = _records_to_flat(state["cookies"])
                if expected_user and saved.get("unb") == expected_user:
                    await context.add_cookies(_missing_cookies(state["cookies"], current_cookies))
            elif state is None and records and not current.get("unb"):
                await context.add_cookies(_cookies_to_playwright(records))
            page = context.pages[0] if context.pages else await context.new_page()
            await page.bring_to_front()
            failed = False
            try:
                yield page
            except BaseException:
                failed = True
                raise
            finally:
                try:
                    native = [c for c in await context.cookies() if _allowed(c)]
                    flat = _records_to_flat(native)
                    if (
                        persist_session
                        and original_digest
                        and expected_user == flat.get("unb")
                        and all(flat.get(k) for k in ("unb", "_m_h5_tk", "cookie2"))
                    ):
                        save_cookies_if_unchanged(source, _coerce_records(native), original_digest)
                    atomic_write_bytes(
                        STATE_PATH, encrypt_cookies({"generation": generation, "cookies": native})
                    )
                except Exception as exc:
                    if failed:
                        logger.warning("沙箱会话保存失败，已保留原登录态；请检查沙箱浏览器状态")
                    else:
                        raise GoofishError("沙箱会话保存失败；请保留窗口并检查状态目录") from exc
            # 只断开本次 Playwright 连接；浏览器、页面和 sessionStorage 保持运行。
    finally:
        lock.release()
