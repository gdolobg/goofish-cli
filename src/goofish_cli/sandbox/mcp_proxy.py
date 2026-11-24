"""本机只提供工具目录；实际调用始终在 Docker 沙箱内执行。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import redirect_stdout, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from goofish_cli.core import iter_commands
from goofish_cli.core.registry import discover
from goofish_cli.mcp_server import _register_one
from goofish_cli.sandbox import cli

logger = logging.getLogger(__name__)
TIMING_LOG_DIR = Path.home() / ".goofish-cli" / "logs"


def _record_timing(values: dict) -> None:
    now = datetime.now(UTC)
    line = json.dumps(
        {"timestamp": now.isoformat(timespec="milliseconds"), "pid": os.getpid(), **values}
    )
    # 按日期和进程分文件，避免多个 MCP 进程同时写同一个文件。
    try:
        TIMING_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = TIMING_LOG_DIR / f"mcp-timing-{now.date()}-{os.getpid()}.jsonl"
        with path.open("a", encoding="utf-8") as output:
            output.write(line + "\n")
    except OSError as error:
        with suppress(Exception):
            logger.warning("goofish MCP timing file write failed: %s", error)
    # 两路输出独立：stderr 失效也不能阻止本机文件落盘。
    with suppress(Exception):
        logger.warning("goofish_mcp_timing %s", line)


def _prepare() -> None:
    cli._ensure_docker()
    with redirect_stdout(sys.stderr):
        cli.start(port=16080, rebuild=False)


class _SandboxConnection:
    """One owner task enters, uses and exits SDK contexts (AnyIO task-affinity)."""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.task = None

    async def call(self, name, arguments, advance):
        future = asyncio.get_running_loop().create_future()
        reused = self.task is not None and not self.task.done()
        if not reused:
            if self.task is not None:
                with suppress(BaseException):
                    await self.task
            advance("prepare")
            self.task = asyncio.create_task(self._serve())
        await self.queue.put((name, arguments, advance, future))
        try:
            return await future, reused
        except BaseException:
            # Never replay an uncertain call, including write operations.
            await self.close()
            raise

    async def _serve(self):
        job = await self.queue.get()
        try:
            await asyncio.to_thread(_prepare)
            job[2]("transport")
            parameters = StdioServerParameters(
                command="docker", args=["exec", "-i", cli.CONTAINER, "goofish-cli"]
            )
            async with (
                stdio_client(parameters) as (read, write),
                ClientSession(read, write, read_timeout_seconds=timedelta(minutes=10)) as session,
            ):
                job[2]("initialize")
                await session.initialize()
                while True:
                    name, arguments, advance, future = job
                    advance("call_tool")
                    result = await session.call_tool(name, arguments)
                    if not future.done():
                        future.set_result(result)
                    job = await self.queue.get()
        except BaseException as error:
            future = job[3]
            if not future.done():
                if isinstance(error, asyncio.CancelledError):
                    future.cancel()
                else:
                    future.set_exception(error)
            raise

    async def close(self):
        task, self.task = self.task, None
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(BaseException):
                await task


def _create_server() -> Server:
    catalog = FastMCP("goofish")
    discover()
    for command in iter_commands():
        _register_one(command, catalog)
    server = Server("goofish")
    lock = asyncio.Lock()
    connection = _SandboxConnection()
    server.sandbox_connection = connection

    @server.list_tools()
    async def list_tools():
        return await catalog.list_tools()

    @server.call_tool()
    async def call_tool(name, arguments):
        # First call prepares Docker; later calls reuse the live session. A broken
        # connection fails this call; only a subsequent explicit call reconnects.
        stage = "lock_wait"
        started = checkpoint = perf_counter()
        call_id = uuid4().hex
        timings = {"prepare_ms": 0, "transport_ms": 0, "initialize_ms": 0}
        reused = connection.task is not None and not connection.task.done()
        outcome = "exception"

        def advance(next_stage):
            nonlocal checkpoint, stage
            now = perf_counter()
            timings[stage + "_ms"] = round((now - checkpoint) * 1000, 3)
            checkpoint, stage = now, next_stage

        try:
            async with lock:
                result, reused = await connection.call(name, arguments, advance)
                advance("cleanup")
                outcome = "tool_error" if result.isError else "returned"
                return result
        except Exception:
            # stderr 留下完整堆栈；不记录可能包含消息或凭据的工具参数。
            # 日志流本身失效时仍保留原始异常。
            with suppress(Exception):
                logger.exception(
                    "goofish MCP proxy failed: tool=%s stage=%s call_id=%s", name, stage, call_id
                )
            outcome = "exception"
            raise
        finally:
            advance("finished")
            _record_timing(
                {
                    "call_id": call_id,
                    "tool": name,
                    "outcome": outcome,
                    "session_reused": reused,
                    "total_ms": round((perf_counter() - started) * 1000, 3),
                    **timings,
                }
            )

    return server


async def _run() -> None:
    server = _create_server()
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await server.sandbox_connection.close()


def main() -> None:
    # Codex 按 UTF-8 读取 MCP 管道，Windows 默认 GBK 会破坏中文启动日志。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    asyncio.run(_run())
