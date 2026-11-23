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


def _create_server() -> Server:
    catalog = FastMCP("goofish")
    discover()
    for command in iter_commands():
        _register_one(command, catalog)
    server = Server("goofish")
    lock = asyncio.Lock()

    @server.list_tools()
    async def list_tools():
        return await catalog.list_tools()

    @server.call_tool()
    async def call_tool(name, arguments):
        # 每次确认运行状态，支持用户在两次工具调用之间关闭 Docker。
        stage = "lock_wait"
        started = checkpoint = perf_counter()
        call_id = uuid4().hex
        timings = {}
        outcome = "exception"

        def advance(next_stage):
            nonlocal checkpoint, stage
            now = perf_counter()
            timings[stage + "_ms"] = round((now - checkpoint) * 1000, 3)
            checkpoint, stage = now, next_stage

        try:
            async with lock:
                advance("prepare")
                await asyncio.to_thread(_prepare)
                parameters = StdioServerParameters(
                    command="docker",
                    args=["exec", "-i", cli.CONTAINER, "goofish-cli"],
                )
                # 不重试工具调用，避免结果不确定时重复执行写操作。
                advance("transport")
                async with (
                    stdio_client(parameters) as (read, write),
                    ClientSession(
                        read, write, read_timeout_seconds=timedelta(minutes=10)
                    ) as session,
                ):
                    advance("initialize")
                    await session.initialize()
                    advance("call_tool")
                    result = await session.call_tool(name, arguments)
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
                    "total_ms": round((perf_counter() - started) * 1000, 3),
                    **timings,
                }
            )

    return server


async def _run() -> None:
    server = _create_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    # Codex 按 UTF-8 读取 MCP 管道，Windows 默认 GBK 会破坏中文启动日志。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    asyncio.run(_run())
