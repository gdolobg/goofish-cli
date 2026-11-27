"""Keep sandbox MCP browser commands on one thread and event loop."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

_local = threading.local()
_executor = None


def run_browser_command(coroutine):
    runner = getattr(_local, "runner", None)
    return runner.run(coroutine) if runner is not None else asyncio.run(coroutine)


def browser_connection_reuse_enabled():
    return getattr(_local, "runner", None) is not None


def _execute(function, kwargs):
    if getattr(_local, "runner", None) is None:
        _local.runner = asyncio.Runner()
    return function(**kwargs)


async def execute_browser_command(function, kwargs):
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="goofish-browser")
    return await asyncio.get_running_loop().run_in_executor(_executor, _execute, function, kwargs)


def _close():
    runner = getattr(_local, "runner", None)
    if runner is not None:
        from .sandbox_browser import close_browser_connection

        try:
            runner.run(close_browser_connection())
        finally:
            runner.close()
            _local.runner = None


def close_browser_runtime():
    global _executor
    executor, _executor = _executor, None
    if executor is not None:
        try:
            executor.submit(_close).result()
        finally:
            executor.shutdown(wait=True)
