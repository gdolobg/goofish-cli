from contextlib import asynccontextmanager

import pytest
from mcp import types

from goofish_cli.sandbox import mcp_proxy as proxy


@pytest.mark.asyncio
async def test_catalog_does_not_start_docker(monkeypatch):
    monkeypatch.setattr(proxy, "_prepare", lambda: pytest.fail("不应启动 Docker"))
    server = proxy._create_server()
    result = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
    tools = result.root.tools
    assert len(tools) == 19
    search = next(tool for tool in tools if tool.name == "search_items")
    assert "query" in search.inputSchema["properties"]


@pytest.mark.asyncio
async def test_call_starts_sandbox_and_preserves_result(monkeypatch):
    calls = []
    expected = types.CallToolResult(
        content=[types.TextContent(type="text", text="partial")],
        structuredContent={"ok": False, "data": {"items": []}, "error": {"phase": "extract"}},
        isError=False,
    )
    monkeypatch.setattr(proxy, "_prepare", lambda: calls.append("prepare"))

    @asynccontextmanager
    async def transport(parameters):
        assert parameters.command == "docker"
        assert parameters.args == ["exec", "-i", proxy.cli.CONTAINER, "goofish-cli"]
        calls.append("transport")
        yield None, None

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            calls.append("initialize")

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return expected

    monkeypatch.setattr(proxy, "stdio_client", transport)
    monkeypatch.setattr(proxy, "ClientSession", Session)
    server = proxy._create_server()
    request = types.CallToolRequest(params=types.CallToolRequestParams(
        name="search_items", arguments={"query": "机械键盘"},
    ))
    result = await server.request_handlers[types.CallToolRequest](request)
    assert result.root == expected
    assert calls == ["prepare", "transport", "initialize", ("search_items", {"query": "机械键盘"})]


@pytest.mark.asyncio
async def test_start_failure_does_not_execute_or_retry_tool(monkeypatch):
    calls = []

    def fail():
        calls.append("prepare")
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(proxy, "_prepare", fail)
    monkeypatch.setattr(proxy, "stdio_client", lambda *a: pytest.fail("不能转发请求"))
    server = proxy._create_server()
    request = types.CallToolRequest(params=types.CallToolRequestParams(
        name="search_items", arguments={"query": "test"},
    ))
    result = await server.request_handlers[types.CallToolRequest](request)
    assert result.root.isError
    assert calls == ["prepare"]
