"""搜索响应分类及复用页面上的监听器、任务清理。"""

import asyncio
import importlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from goofish_cli.core.errors import GoofishError

search = importlib.import_module("goofish_cli.commands.search.search")


class Page:
    def __init__(self):
        self.listeners = {"request": [], "response": []}
        self.navigation_error = None

    def on(self, event, callback):
        self.listeners[event].append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, value):
        for callback in tuple(self.listeners[event]):
            callback(value)

    async def goto(self, *args, **kwargs):
        if self.navigation_error:
            raise self.navigation_error
        request = SimpleNamespace(
            url="https://example.test/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/",
            post_data=urlencode({"data": json.dumps({"keyword": "test", "pageNumber": 1})}),
        )
        # Request identity must be hashable, just like a Playwright Request.
        request = type("Request", (), vars(request))()

        async def body():
            return {
                "ret": ["SUCCESS"],
                "data": {
                    "resultInfo": {"searchResControlFields": {"hasItems": False, "numFound": 0}},
                    "resultList": [],
                },
            }

        self.emit("request", request)
        self.emit("response", SimpleNamespace(request=request, json=body))

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)

    async def evaluate(self, script, arg=None):
        if script == search._EXTRACT_JS:
            return {"items": []}
        return {"hasNext": False, "totalPages": 0}


@pytest.mark.parametrize(
    "failure", [None, RuntimeError("navigation failed"), asyncio.CancelledError()]
)
async def test_run_always_cleans_observer_and_repeated_page_is_reusable(monkeypatch, failure):
    page = Page()
    observers = []

    class Observer(search._SearchPage):
        def __init__(self, raw):
            super().__init__(raw)
            observers.append(self)

    @asynccontextmanager
    async def browser():
        yield page

    async def scroll(*args, **kwargs):
        pass

    monkeypatch.setattr(search, "_SearchPage", Observer)
    monkeypatch.setattr(search, "goofish_page", browser)
    monkeypatch.setattr(search, "auto_scroll", scroll)
    for _ in range(3):
        page.navigation_error = failure
        if failure:
            with pytest.raises(type(failure)):
                await search._run("test", 2, 1)
        else:
            result = await search._run("test", 2, 1)
            assert result["complete"] and result["total"] == 0
        assert page.listeners == {"request": [], "response": []}
        assert not observers[-1].tasks
        assert not observers[-1].requests


async def test_close_cancels_and_awaits_pending_response_body():
    page = Page()
    observer = search._SearchPage(page)
    observer.begin("test", 1)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    request = object()

    async def body():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    observer.requests[request] = observer.generation
    observer._schedule(SimpleNamespace(request=request, json=body))
    await started.wait()
    tasks = tuple(observer.tasks)
    await observer.aclose()
    assert cancelled.is_set()
    assert all(task.done() for task in tasks)
    assert not observer.tasks and not observer.requests
    assert observer.payload is None and not observer.ready.is_set()
    assert page.listeners == {"request": [], "response": []}


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"ret": ["SUCCESS"], "data": {}},
        {
            "ret": ["SUCCESS"],
            "data": {"resultList": [], "resultInfo": {}},
        },
    ],
)
async def test_schema_mismatch_is_distinct_from_json_read_failure(raw):
    observer = search._SearchPage(Page())

    async def body():
        return raw

    try:
        await observer._capture(SimpleNamespace(json=body), observer.generation)
        assert observer.payload["response_error"] == "search_response_schema_mismatch"
        with pytest.raises(GoofishError, match=r"\[search_response_schema_mismatch\].*结构不匹配"):
            search._raise_for_failed_page(observer.payload)
    finally:
        await observer.aclose()


async def test_json_read_failure_does_not_expose_raw_error_or_claim_schema_changed():
    observer = search._SearchPage(Page())

    async def body():
        raise ValueError("secret response contents")

    try:
        await observer._capture(SimpleNamespace(json=body), observer.generation)
        assert observer.payload == {
            "items": [],
            "response_error": "search_response_json_error",
            "response_exception_type": "ValueError",
        }
        with pytest.raises(GoofishError, match=r"\[search_response_json_error\].*JSON 读取失败"):
            search._raise_for_failed_page(observer.payload)
    finally:
        await observer.aclose()


async def test_response_timeout_is_not_reported_as_schema_mismatch():
    observer = search._SearchPage(Page())

    class Ready:
        async def wait(self):
            raise TimeoutError

        def clear(self):
            pass

    observer.ready = Ready()
    try:
        payload = await observer.extract()
        assert payload == {"items": [], "response_error": "search_response_timeout"}
        with pytest.raises(GoofishError, match=r"\[search_response_timeout\].*响应超时"):
            search._raise_for_failed_page(payload)
    finally:
        await observer.aclose()
