"""本机只提供工具目录；实际调用始终在 Docker 沙箱内执行。"""
from __future__ import annotations

import asyncio
import sys
from contextlib import redirect_stdout
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from goofish_cli.core import iter_commands
from goofish_cli.core.registry import discover
from goofish_cli.mcp_server import _register_one
from goofish_cli.sandbox import cli


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
        async with lock:
            await asyncio.to_thread(_prepare)
            parameters = StdioServerParameters(
                command="docker",
                args=["exec", "-i", cli.CONTAINER, "goofish-cli"],
            )
            # 不重试工具调用，避免结果不确定时重复执行写操作。
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(minutes=10)
                ) as session:
                    await session.initialize()
                    return await session.call_tool(name, arguments)

    return server


async def _run() -> None:
    server = _create_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())
