"""测 search 的参数归一化、URL 构造与 _walk_pages 翻页状态机（fake page 驱动）。"""
from __future__ import annotations

import asyncio

import pytest

from goofish_cli.commands.search.search import __test__ as t
from goofish_cli.commands.search.search import _item_id_from_url


def test_normalize_limit_clamps_and_defaults():
    normalize = t["_normalize_limit"]
    assert normalize(10) == 10
    assert normalize("5") == 5
    assert normalize(0) == 1
    assert normalize(-3) == 1
    assert normalize(999) == t["MAX_LIMIT"]
    assert normalize(None) == 20
    assert normalize("abc") == 20


def test_normalize_pages_clamps_and_defaults():
    normalize = t["_normalize_pages"]
    assert normalize(1) == 1
    assert normalize("3") == 3
    assert normalize(0) == 1
    assert normalize(-2) == 1
    assert normalize(999) == t["MAX_PAGES"]
    assert normalize(None) == 1
    assert normalize("abc") == 1


def test_build_search_url_encodes_query():
    build = t["_build_search_url"]
    assert build("iPhone 15") == "https://www.goofish.com/search?q=iPhone%2015"
    assert build("中文") == "https://www.goofish.com/search?q=%E4%B8%AD%E6%96%87"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.goofish.com/item?id=123456&spm=foo", "123456"),
        ("https://www.goofish.com/item?spm=a&id=999", "999"),
        ("https://example.com/no-id", ""),
        ("", ""),
    ],
)
def test_item_id_from_url(url, expected):
    assert t["_item_id_from_url"](url) == expected


def test_auth_wall_attempts_is_two():
    """瞬时登录墙重试：总尝试 2 次（首次 + 1 次退避重试）。"""
    assert t["AUTH_WALL_ATTEMPTS"] == 2


# ---------------------------------------------------------------------------
# _walk_pages 翻页状态机（fake page 驱动，确定性、无网络）
# ---------------------------------------------------------------------------


class FakeLocator:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    def nth(self, i: int) -> FakeLocator:
        return self

    async def click(self, timeout: int = 0) -> None:
        self._page.clicks += 1


class FakePage:
    """按脚本依次返回 evaluate 结果；脚本耗尽后执行 on_exhausted。"""

    def __init__(self, script: list, on_exhausted=None) -> None:
        self.script = list(script)
        self.on_exhausted = on_exhausted
        self.clicks = 0

    def locator(self, sel: str) -> FakeLocator:
        return FakeLocator(self)

    async def evaluate(self, js: str, arg=None):
        # auto_scroll 的滚动指令不占脚本拍，直接短路
        if isinstance(js, str) and js.startswith("window.scrollTo"):
            return None
        if not self.script:
            if self.on_exhausted is not None:
                return self.on_exhausted()
            raise AssertionError("fake page script exhausted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(arg) if callable(step) else step

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def goto(self, url: str, wait_until: str = "") -> None:
        return None


def _item(iid: str) -> dict:
    return {"url": f"https://www.goofish.com/item?id={iid}", "title": f"item-{iid}"}


def _page_payload(ids: list[str], **kw) -> dict:
    base = {"items": [_item(i) for i in ids], "requiresAuth": False, "blocked": False,
            "empty": False, "bodyPreview": ""}
    base.update(kw)
    return base


def _pagination(has_next: bool = True, total: int | None = 50) -> dict:
    return {"hasNext": has_next, "totalPages": total}


# 翻页状态机入口（fake page 驱动用）
_walk = t["_walk_pages"]


def test_should_retry_truth_table():
    """瞬时登录墙重试谓词（#28 契约）：只有 items=[] 且 requiresAuth 才重试。

    注意 `_EXTRACT_JS` 恒返回 `items` 键：真实登录墙载荷是
    `{"items": [], "requiresAuth": True, ...}`，必须重试；
    未知零卡片形态（requiresAuth=False）不重试，避免无谓的二次导航。
    """
    should_retry = t["_should_retry"]
    assert should_retry({"items": [{"id": 1}]}) is False
    assert should_retry({}) is False
    assert should_retry({"requiresAuth": True}) is True
    assert should_retry({"requiresAuth": False}) is False
    assert should_retry({"items": [], "requiresAuth": True}) is True
    assert should_retry({"items": [], "requiresAuth": False}) is False


def test_walk_pages_accumulates_and_dedupes_across_pages():
    """跨页去重：第 2 页重复第 1 页的 id 只计一次，pages_fetched 正确递增。"""
    script = [
        _pagination(True, 50),
        True,  # page-change 等待
        _page_payload(["201", "100", "202"]),  # 100 与第 1 页重复
    ]
    items, seen = [_item("100"), _item("101")], {"100", "101"}
    fetched, total, reason = asyncio.run(
        _walk(FakePage(script), items, seen, 1, pages=2, limit=200)
    )
    assert fetched == 2
    assert total == 50
    assert reason == "pages_reached"
    ids = [_item_id_from_url(it["url"]) for it in items]
    assert ids == ["100", "101", "201", "202"]  # 去重后顺序保持


def test_walk_pages_anchor_uses_unfiltered_first_card():
    """审核 P1-2：第 2 页首卡与第 1 页重复时，锚点仍取未过滤首卡，
    第 3 页可正常到达（不再因锚点失真而提前终止）。"""
    script = [
        _pagination(True, 50),
        True,  # 等待：page2 首卡 100 与 page1 重复（changed 依 DOM 而定，这里视为已变）
        _page_payload(["100", "201", "202"]),
        _pagination(True, 50),
        True,
        _page_payload(["301", "302"]),
    ]
    items, seen = [_item("100")], {"100"}
    fetched, _, reason = asyncio.run(
        _walk(FakePage(script), items, seen, 1, pages=3, limit=200)
    )
    assert fetched == 3
    assert reason == "pages_reached"
    ids = [_item_id_from_url(it["url"]) for it in items]
    assert ids == ["100", "201", "202", "301", "302"]


def test_walk_pages_stops_on_disabled_arrow():
    """右箭头 disabled（末页）→ last_page，保留已有结果。"""
    script = [_pagination(False, 1)]
    fetched, total, reason = asyncio.run(
        _walk(FakePage(script), [_item("100")], {"100"}, 1, pages=5, limit=200)
    )
    assert (fetched, total, reason) == (1, 1, "last_page")


def test_walk_pages_stops_at_cross_page_limit():
    """跨页 limit：累积到 limit 即停，reason=limit。"""
    script = [
        _pagination(True, 50),
        True,
        _page_payload(["201", "202"]),
    ]
    items, seen = [_item(f"{i}") for i in range(1, 44)], {str(i) for i in range(1, 44)}
    fetched, _, reason = asyncio.run(
        _walk(FakePage(script), items, seen, 1, pages=5, limit=45)
    )
    assert len(items) == 45
    assert reason == "limit"


def test_walk_pages_preserves_results_on_mid_walk_exception():
    """审核 P1-1：翻页途中 evaluate 抛（SPA 销毁执行上下文）→
    不抛出，返回第 1 页已累积结果 + reason=error。"""
    boom = RuntimeError("Execution context was destroyed")
    script = [
        _pagination(True, 50),
        boom,  # page-change 等待抛出
    ]
    items, seen = [_item("100"), _item("101")], {"100", "101"}
    fetched, _, reason = asyncio.run(
        _walk(FakePage(script), items, seen, 1, pages=3, limit=200)
    )
    assert fetched == 1
    assert reason == "error"
    assert len(items) == 2


def test_walk_pages_pagination_state_exception_preserves_results():
    """翻页第一拍（pagination 状态读取）抛出同样优雅终止。"""
    boom = RuntimeError("Execution context was destroyed")
    fetched, _, reason = asyncio.run(
        _walk(FakePage([boom]), [_item("100")], {"100"}, 1, pages=3, limit=200)
    )
    assert (fetched, reason) == (1, "error")


def test_walk_pages_wait_timeout_with_no_new_items_is_stale():
    """等待超时（changed=False）且提取无新数据 → reason=stale，保留结果。"""
    # wait JS resolve(False)；extract 返回与 seen 全重复的页
    script = [
        _pagination(True, 50),
        False,
        _page_payload(["100"]),  # 全是重复 → 无 fresh
    ]
    fetched, _, reason = asyncio.run(
        _walk(FakePage(script), [_item("100")], {"100"}, 1, pages=3, limit=200)
    )
    assert (fetched, reason) == (1, "stale")


def test_walk_pages_wait_timeout_but_fresh_items_continues():
    """等待超时但提取到新数据（渲染慢而非失败）→ 照常累积继续。"""
    script = [
        _pagination(True, 50),
        False,  # 等待超时
        _page_payload(["201"]),
        _pagination(False, 50),
    ]
    fetched, _, reason = asyncio.run(
        _walk(FakePage(script), [_item("100")], {"100"}, 1, pages=5, limit=200)
    )
    assert fetched == 2
    assert reason == "last_page"


def test_walk_pages_repeated_page_is_no_new():
    """重渲染成功但整页重复 → reason=no_new，防御性终止。"""
    script = [
        _pagination(True, 50),
        True,  # changed
        _page_payload(["100", "101"]),  # 全重复
    ]
    fetched, _, reason = asyncio.run(
        _walk(FakePage(script), [_item("100"), _item("101")], {"100", "101"}, 1,
              pages=3, limit=200)
    )
    assert (fetched, reason) == (1, "no_new")


def test_walk_pages_blocked_mid_walk():
    """翻页途中撞风控页 → reason=blocked，保留部分结果。"""
    script = [
        _pagination(True, 50),
        True,
        _page_payload([], blocked=True),
    ]
    fetched, _, reason = asyncio.run(
        _walk(FakePage(script), [_item("100")], {"100"}, 1, pages=3, limit=200)
    )
    assert (fetched, reason) == (1, "blocked")


def test_walk_pages_non_dict_extract_is_error():
    """extract 返回非 dict（结构突变）→ reason=error，保留结果。"""
    script = [
        _pagination(True, 50),
        True,
        "not-a-dict",
    ]
    fetched, _, reason = asyncio.run(
        _walk(FakePage(script), [_item("100")], {"100"}, 1, pages=3, limit=200)
    )
    assert (fetched, reason) == (1, "error")


def test_first_card_id_returns_first_with_id():
    """锚点函数：跳过无 id 条目，取首个可解析 id。"""
    f = t["_first_card_id"]
    assert f([{"url": "https://x/no-id"}, {"url": "https://www.goofish.com/item?id=42"}]) == "42"
    assert f([]) == ""
    assert f([{"url": ""}]) == ""


def test_walk_pages_skips_cards_without_numeric_id():
    """审核 P2：?id=abc 这类解析不出数字 id 的卡片不能跨页重复追加。"""
    bad = {"url": "https://www.goofish.com/item?id=abc", "title": "bad-card"}
    script = [
        _pagination(True, 50),
        True,
        # 第 2 页：新卡 101 + 坏卡（坏卡跳过，不影响继续翻页）
        {**_page_payload(["101"]), "items": [_item("101"), dict(bad)]},
        _pagination(True, 50),
        True,
        # 第 3 页：坏卡再次出现 + 新卡 102（坏卡不重复追加）
        {**_page_payload(["102"]), "items": [dict(bad), _item("102")]},
    ]
    items, seen = [_item("100")], {"100"}
    fetched, _, reason = asyncio.run(
        _walk(FakePage(script), items, seen, 1, pages=3, limit=200)
    )
    ids = [_item_id_from_url(it["url"]) for it in items]
    assert ids == ["100", "101", "102"]  # 坏卡被跳过，不重复出现
    assert fetched == 3
    assert reason == "pages_reached"


def test_walk_pages_non_dict_pagination_state_is_error():
    """审核 P2：pagination 状态返回 None/非 dict 时优雅终止，不 AttributeError。"""
    for bad in (None, [], "unexpected", 123):
        fetched, _, reason = asyncio.run(
            _walk(FakePage([bad]), [_item("100")], {"100"}, 1, pages=3, limit=200)
        )
        assert (fetched, reason) == (1, "error"), f"bad={bad!r}"


def test_search_sort_validation_before_browser():
    from goofish_cli.commands.search.search import search
    from goofish_cli.core.errors import GoofishError
    with pytest.raises(GoofishError, match="sort"):
        search("phone", sort="unknown")


def test_search_passes_sort_and_keeps_default(monkeypatch):
    import importlib
    module = importlib.import_module("goofish_cli.commands.search.search")
    calls = []
    async def run(query, limit, pages, sort, min_price, max_price):
        calls.append((query, limit, pages, sort, min_price, max_price))
        return {}
    monkeypatch.setattr(module, "_run", run)
    module.search("phone")
    module.search("phone", limit=150, pages=5, sort="credit")
    module.search("phone", sort="active")
    assert calls == [("phone", 20, 1, "default", None, None), ("phone", 150, 5, "credit", None, None), ("phone", 20, 1, "active", None, None)]


@pytest.mark.parametrize("number", [1, 2, 5])
@pytest.mark.parametrize("sort,field,value", [("credit", "credit", "credit_desc"), ("active", "modify", "desc")])
def test_sorted_capture_rejects_unsorted_requests_on_every_page(number, sort, field, value):
    import json
    from urllib.parse import urlencode

    from goofish_cli.commands.search.search import _SearchPage
    page = object.__new__(_SearchPage)
    page.sort, page.query, page.number, page.generation = sort, "phone", number, 7
    page.requests = {}
    class Request:
        url = "https://www.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
        def __init__(self, **fields):
            self.post_data = urlencode({"data": json.dumps({"keyword": "phone", "pageNumber": number, **fields})})
    bad = Request(sortField="", sortValue="")
    page._request(bad)
    assert not page.requests
    good = Request(sortField=field, sortValue=value)
    page._request(good)
    assert page.requests == {good: 7}


def test_mcp_existing_search_tool_exposes_sort():
    from mcp.server.fastmcp import FastMCP

    from goofish_cli.core.registry import registry
    from goofish_cli.mcp_server import _register_one
    server = FastMCP("search-sort-test")
    _register_one(registry()["search.items"], server)
    tools = asyncio.run(server.list_tools())
    schema = next(tool.inputSchema for tool in tools if tool.name == "search_items")
    assert schema["properties"]["sort"]["default"] == "default"
    for name in ("min_price", "max_price"):
        assert {"type": "integer"} in schema["properties"][name]["anyOf"]
        assert schema["properties"][name]["default"] is None


@pytest.mark.parametrize("bounds", [{"min_price": -1}, {"max_price": -1}, {"min_price": 1.5}, {"min_price": 500, "max_price": 100}])
def test_price_validation_before_browser(bounds):
    from goofish_cli.commands.search.search import search
    from goofish_cli.core.errors import GoofishError
    with pytest.raises(GoofishError, match="price"):
        search("phone", **bounds)


@pytest.mark.parametrize("number", [1, 2, 5])
@pytest.mark.parametrize("low,high", [(100, 500), (0, 500), (100, None), (None, 500)])
def test_price_capture_requires_requested_range_on_every_page(number, low, high):
    import json
    from urllib.parse import urlencode

    from goofish_cli.commands.search.search import _SearchPage
    page = object.__new__(_SearchPage)
    page.query, page.number, page.generation, page.requests = "phone", number, 7, {}
    low = str(low) if low is not None else "0"
    high = str(high) if high is not None else "undefined"
    page.price_range = f"priceRange:{low},{high}"
    class Request:
        url = "https://www.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
        def __init__(self, filters):
            self.post_data = urlencode({"data": json.dumps({"keyword": "phone", "pageNumber": number, "propValueStr": {"searchFilter": filters}})})
    page._request(Request(""))
    page._request(Request("priceRange:1,2;"))
    page._request(Request("priceRange:undefined,500;"))
    assert not page.requests
    good = Request(page.price_range + ";")
    page._request(good)
    assert page.requests == {good: 7}


def test_search_passes_price_bounds(monkeypatch):
    import importlib
    module = importlib.import_module("goofish_cli.commands.search.search")
    calls = []
    async def run(*args):
        calls.append(args)
        return {}
    monkeypatch.setattr(module, "_run", run)
    module.search("phone", sort="active", min_price=100, max_price=500)
    assert calls == [("phone", 20, 1, "active", 100, 500)]


@pytest.mark.parametrize("sort,label", [("credit", "信用排序"), ("active", "最近活跃")])
def test_sort_uses_visible_text_without_build_specific_classes(sort, label):
    from goofish_cli.commands.search.search import _select_sort
    events = []

    class Locator:
        def and_(self, other):
            assert other == ":visible"
            events.append("visible")
            return self

        @property
        def first(self):
            return self

        @property
        def last(self):
            return self

        async def hover(self, timeout):
            events.append(("hover", timeout))

        async def element_handle(self, timeout):
            events.append(("element", timeout))
            return self

        async def wait_for_element_state(self, state, timeout):
            events.append((state, timeout))

        async def dispose(self):
            events.append("dispose")

        async def click(self, timeout):
            events.append(("click", timeout))

    class Page:
        async def evaluate(self, script):
            assert script == "window.scrollTo(0, 0)"

        def locator(self, selector):
            assert selector == ":visible"
            return selector

        def get_by_text(self, text, exact=False):
            if exact:
                assert text == label
            else:
                assert text.fullmatch("综合")
                assert not text.fullmatch("综合评价")
            return Locator()

    asyncio.run(_select_sort(Page(), sort))
    assert events == ["visible", ("hover", 8000), "visible", ("element", 5000), ("stable", 5000), ("click", 5000), "dispose"]


@pytest.mark.parametrize("phase", ["hover", "stable", "click", "diagnostic"])
def test_sort_timeout_reports_manual_recovery_without_silent_fallback(phase, monkeypatch, tmp_path):
    import importlib

    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    from goofish_cli.commands.search.search import _select_sort
    from goofish_cli.core.errors import GoofishError
    module = importlib.import_module("goofish_cli.commands.search.search")
    diagnostics = module.SortDiagnostics
    monkeypatch.setattr(module, "SortDiagnostics", lambda page: diagnostics(page, root=tmp_path))

    class Page:
        async def evaluate(self, script):
            if phase == "diagnostic" and script != "window.scrollTo(0, 0)":
                raise RuntimeError("page closed")
            return {"menuItems": [{"text": "信用排序", "display": "none"}]}

        def locator(self, selector):
            return self

        def get_by_text(self, text, exact=False):
            return self

        def and_(self, other):
            return self

        @property
        def first(self):
            return self

        @property
        def last(self):
            return self

        async def hover(self, timeout):
            if phase in ("hover", "diagnostic"):
                raise PlaywrightTimeoutError("missing control")

        async def element_handle(self, timeout):
            return self

        async def wait_for_element_state(self, state, timeout):
            assert state == "stable"
            if phase == "stable":
                raise PlaywrightTimeoutError("element is not stable")

        async def dispose(self):
            pass

        async def click(self, timeout):
            assert phase != "stable", "unstable options must never be clicked"
            raise PlaywrightTimeoutError("element is not visible")

    with pytest.raises(GoofishError, match="无法确认搜索排序.*沙箱观察页面") as captured:
        asyncio.run(_select_sort(Page(), "credit"))

    message = str(captured.value)
    assert ("点击信用排序选项" if phase == "click" else "等待信用排序选项稳定" if phase == "stable" else "悬停排序入口") in message
    assert ('"diagnostic_error":"RuntimeError"' if phase == 'diagnostic' else '"display":"none"') in message
    assert captured.value.__cause__ is not None


@pytest.mark.parametrize("sort", ["credit", "active", "default"])
def test_confirmed_empty_search_needs_no_sort_or_price_controls(monkeypatch, sort):
    import importlib
    from contextlib import asynccontextmanager
    module = importlib.import_module("goofish_cli.commands.search.search")

    class Page:
        def begin(self, query, number):
            self.query, self.number = query, number

        async def goto(self, *args, **kwargs):
            pass

        async def wait_for_timeout(self, ms):
            pass

        async def extract(self):
            return {"items": [], "empty": True, "result_kind": "empty", "reported_match_count": 0}

        async def evaluate(self, script, arg=None):
            return {"hasNext": False, "totalPages": 0}

        def locator(self, selector):
            pytest.fail("confirmed empty results must not require filter controls")

    @asynccontextmanager
    async def browser():
        yield Page()

    async def scroll(*args, **kwargs):
        pass

    monkeypatch.setattr(module, "goofish_page", browser)
    # Preserve isinstance-based dispatch while avoiding real browser listeners.
    class SearchPage(Page):
        def __init__(self, raw):
            pass
    monkeypatch.setattr(module, "_SearchPage", SearchPage)
    monkeypatch.setattr(module, "auto_scroll", scroll)
    result = asyncio.run(module._run("no matches", 30, 5, sort, 100, 500))
    assert result["items"] == []
    assert result["complete"] is True
    assert result["result_kind"] == "empty"
    assert result["sort"] == sort
    assert result["stopped_reason"] == "last_page"


@pytest.mark.parametrize("bounds,expected", [((None, 500), "priceRange:0,500"), ((0, 500), "priceRange:0,500"), ((100, 500), "priceRange:100,500"), ((100, None), "priceRange:100,undefined")])
def test_run_uses_platform_price_range_format(monkeypatch, bounds, expected):
    import importlib
    from contextlib import asynccontextmanager
    module = importlib.import_module("goofish_cli.commands.search.search")
    pages = []

    class Page:
        def __init__(self, raw=None):
            pages.append(self)

        def begin(self, query, number):
            self.query, self.number = query, number

        async def goto(self, *args, **kwargs):
            pass

        async def wait_for_timeout(self, ms):
            pass

        async def extract(self):
            return _page_payload(["100"])

        async def evaluate(self, script, arg=None):
            return {"hasNext": False, "totalPages": 1}

        def locator(self, selector):
            assert selector == 'input[class*="search-price-input"]'
            return self

        def nth(self, index):
            return self

        async def fill(self, value):
            pass

        async def press(self, key):
            assert key == "Tab"
            assert self.price_range == expected

    @asynccontextmanager
    async def browser():
        yield None

    async def scroll(*args, **kwargs):
        pass

    monkeypatch.setattr(module, "goofish_page", browser)
    monkeypatch.setattr(module, "_SearchPage", Page)
    monkeypatch.setattr(module, "auto_scroll", scroll)
    result = asyncio.run(module._run("phone", 5, 1, "default", *bounds))
    assert result["complete"] is True
    assert result["min_price"] == bounds[0]
    assert result["max_price"] == bounds[1]
    assert pages[0].price_range == expected
