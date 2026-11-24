"""清除沙箱登录态，不接触宿主机账号。"""

import asyncio
import os
from pathlib import Path

from goofish_cli.core import Strategy, command
from goofish_cli.core.errors import GoofishError


@command(
    namespace="auth",
    name="logout",
    description="清除沙箱登录态（浏览器 Cookie、站点存储及登录快照），需重新扫码登录",
    strategy=Strategy.PUBLIC,
    columns=["logged_out"],
    write=True,
)
def logout() -> dict[str, bool]:
    if os.environ.get("GOOFISH_DESKTOP_SANDBOX") != "1" or not Path("/.dockerenv").exists():
        raise GoofishError("仅允许在沙箱内退出登录；请执行 goofish sandbox logout")
    from goofish_cli.core.sandbox_browser import clear_login

    asyncio.run(clear_login())
    return {"logged_out": True}
