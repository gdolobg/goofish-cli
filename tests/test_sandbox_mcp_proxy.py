import json
from contextlib import asynccontextmanager

import pytest
from mcp import types

from goofish_cli.sandbox import mcp_proxy as proxy


@pytest.fixture(autouse=True)
def isolated_timing_logs(monkeypatch, tmp_path):
    directory = tmp_path / "logs"
    monkeypatch.setattr(proxy, "TIMING_LOG_DIR", directory)
    return directory


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
@pytest.mark.parametrize("is_error", [False, True])
async def test_call_starts_sandbox_and_preserves_result(monkeypatch, caplog, is_error):
    calls = []
    expected = types.CallToolResult(
        content=[types.TextContent(type="text", text="partial")],
        structuredContent={"ok": False, "data": {"items": []}, "error": {"phase": "extract"}},
        isError=is_error,
    )
    monkeypatch.setattr(proxy, "_prepare", lambda: calls.append("prepare"))
    monkeypatch.setattr(proxy, "perf_counter", iter(range(8)).__next__)

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
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(
            name="search_items",
            arguments={"query": "机械键盘"},
        )
    )
    result = await server.request_handlers[types.CallToolRequest](request)
    assert result.root == expected
    timing = json.loads(
        next(
            r.getMessage().split("goofish_mcp_timing ", 1)[1]
            for r in caplog.records
            if "goofish_mcp_timing " in r.getMessage()
        )
    )
    assert timing["tool"] == "search_items"
    assert timing["outcome"] == ("tool_error" if is_error else "returned")
    assert len(timing["call_id"]) == 32
    for stage in ["lock_wait", "prepare", "transport", "initialize", "call_tool", "cleanup"]:
        assert timing[stage + "_ms"] == 1000
    assert timing["total_ms"] == 7000
    lines = next(proxy.TIMING_LOG_DIR.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == timing
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
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(
            name="search_items",
            arguments={"query": "test"},
        )
    )
    result = await server.request_handlers[types.CallToolRequest](request)
    assert result.root.isError
    assert calls == ["prepare"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "transport", "initialize", "call_tool"])
async def test_proxy_logs_traceback_and_preserves_error(monkeypatch, caplog, stage):
    calls = []

    def check(current):
        calls.append(current)
        if current == stage:
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr(proxy, "_prepare", lambda: check("prepare"))

    @asynccontextmanager
    async def transport(parameters):
        check("transport")
        yield None, None

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            check("initialize")

        async def call_tool(self, name, arguments):
            check("call_tool")

    monkeypatch.setattr(proxy, "stdio_client", transport)
    monkeypatch.setattr(proxy, "ClientSession", Session)
    server = proxy._create_server()
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(
            name="search_items",
            arguments={"query": "private-query"},
        )
    )
    result = await server.request_handlers[types.CallToolRequest](request)
    assert result.root.isError
    assert result.root.content[0].text == "[Errno 22] Invalid argument"
    assert calls.count(stage) == 1
    assert f"tool=search_items stage={stage}" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
    assert "OSError: [Errno 22] Invalid argument" in caplog.text
    assert "private-query" not in caplog.text
    timing = json.loads(
        next(
            r.getMessage().split("goofish_mcp_timing ", 1)[1]
            for r in caplog.records
            if "goofish_mcp_timing " in r.getMessage()
        )
    )
    assert timing["outcome"] == "exception"
    assert timing[stage + "_ms"] >= 0


def test_main_emits_utf8_on_both_pipes_under_gbk_default():
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = r"""
import asyncio
import sys
import typer
from goofish_cli.sandbox import mcp_proxy as proxy
assert sys.stdout.encoding.lower() == "gbk"
assert sys.stderr.encoding.lower() == "gbk"
async def run():
    typer.echo("正在启动 Docker Desktop，等待 Docker 服务就绪……", err=True)
    print("中文协议内容")
proxy._run = run
proxy.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        timeout=20,
        env={
            **os.environ,
            "PYTHONIOENCODING": "gbk",
            "PYTHONPATH": str(Path(proxy.__file__).parents[2]),
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode("utf-8").strip() == "中文协议内容"
    assert "正在启动 Docker Desktop，等待 Docker 服务就绪……" in result.stderr.decode("utf-8")


@pytest.mark.asyncio
async def test_timing_does_not_hide_result_when_log_sink_fails(monkeypatch):
    def prepare():
        raise OSError(22, "original failure")

    def broken_log(*args, **kwargs):
        raise OSError("stderr closed")

    monkeypatch.setattr(proxy, "_prepare", prepare)
    monkeypatch.setattr(proxy.logger, "exception", broken_log)
    monkeypatch.setattr(proxy.logger, "warning", broken_log)
    server = proxy._create_server()
    result = await server.request_handlers[types.CallToolRequest](
        types.CallToolRequest(
            params=types.CallToolRequestParams(name="search_items", arguments={"query": "test"}),
        )
    )
    assert result.root.isError
    assert result.root.content[0].text == "[Errno 22] original failure"
    timing = json.loads(next(proxy.TIMING_LOG_DIR.glob("*.jsonl")).read_text(encoding="utf-8"))
    assert timing["outcome"] == "exception"


def test_timing_file_appends_records_with_timestamp_and_pid():
    import os
    from datetime import datetime

    proxy._record_timing({"call_id": "first", "tool": "search_items", "total_ms": 12})
    proxy._record_timing({"call_id": "second", "tool": "item_view", "total_ms": 34})
    files = list(proxy.TIMING_LOG_DIR.glob("mcp-timing-*.jsonl"))
    assert len(files) == 1
    records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert [r["call_id"] for r in records] == ["first", "second"]
    assert all(r["pid"] == os.getpid() for r in records)
    assert all(
        datetime.fromisoformat(r["timestamp"]).utcoffset().total_seconds() == 0 for r in records
    )


def test_file_failure_preserves_tool_result_and_stderr(monkeypatch, tmp_path, caplog):
    invalid = tmp_path / "not-a-directory"
    invalid.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(proxy, "TIMING_LOG_DIR", invalid)
    proxy._record_timing({"call_id": "test", "tool": "search_items"})
    assert invalid.read_text(encoding="utf-8") == "keep"
    assert "timing file write failed" in caplog.text
    assert "goofish_mcp_timing" in caplog.text
