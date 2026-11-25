from __future__ import annotations

import asyncio
import json

import pytest

from goofish_cli.core.sort_diagnostics import (
    _MARK_JS,
    _SCREENSHOT_STYLE,
    _START_JS,
    _STOP_JS,
    SortDiagnostics,
)


class Page:
    def __init__(self, *, screenshot_error=False, capture_error=False):
        self.calls = []
        self.screenshot_error = screenshot_error
        self.capture_error = capture_error

    async def evaluate(self, script, arg=None):
        self.calls.append((script, arg))
        if script == _STOP_JS:
            if self.capture_error:
                raise RuntimeError("closed")
            return {"events": [{"ms": 1, "type": "stage", "stage": "click_start"}]}

    async def screenshot(self, **options):
        from pathlib import Path

        assert options["style"] == _SCREENSHOT_STYLE
        assert "visibility: hidden" in options["style"]
        assert options["caret"] == "hide"
        if self.screenshot_error:
            raise RuntimeError("closed")
        Path(options["path"]).write_bytes(b"fake image")


def test_success_discards_evidence_and_restores_page(tmp_path):
    page = Page()
    trace = SortDiagnostics(page, root=tmp_path / "unused")

    async def run():
        await trace.start()
        await trace.mark("click_start")
        assert await trace.finish() is None
        await trace.finish()

    asyncio.run(run())
    assert page.calls == [(_START_JS, None), (_MARK_JS, "click_start"), (_STOP_JS, None)]
    assert not trace.root.exists()


@pytest.mark.parametrize(
    "screenshot_error,capture_error", [(False, False), (True, False), (False, True)]
)
def test_failure_saves_sanitized_bundle_and_id_without_masking_capture_errors(
    tmp_path, screenshot_error, capture_error
):
    page = Page(screenshot_error=screenshot_error, capture_error=capture_error)
    trace = SortDiagnostics(page, root=tmp_path)

    async def run():
        await trace.start()
        return await trace.finish({"stage": "click", "reason": "intercepts pointer events"})

    token = asyncio.run(run())
    value = json.loads((tmp_path / (token + ".json")).read_text())
    assert value["id"] == token
    assert value["failure"]["stage"] == "click"
    assert "screenshot_error" in value
    assert (tmp_path / (token + ".png")).exists() is not screenshot_error
    if capture_error:
        assert value["trace"]["capture_error"] == "RuntimeError"
    if screenshot_error:
        assert value["screenshot_error"] == "RuntimeError"


def test_retention_keeps_five_bundles_and_does_not_delete_unrelated_files(tmp_path):
    (tmp_path / "sort-user-notes.json").write_text("keep")

    async def run():
        for _ in range(7):
            trace = SortDiagnostics(Page(), root=tmp_path)
            await trace.start()
            assert await trace.finish({"stage": "click"})

    asyncio.run(run())
    assert len(list(tmp_path.glob("sort-*.png"))) == 5
    assert len(list(tmp_path.glob("sort-*.json"))) == 6
    assert (tmp_path / "sort-user-notes.json").read_text() == "keep"


def test_start_failure_attempts_cleanup_and_never_changes_business_failure(tmp_path):
    class BrokenPage(Page):
        async def evaluate(self, script, arg=None):
            self.calls.append((script, arg))
            raise RuntimeError("connection lost")

    page = BrokenPage()
    trace = SortDiagnostics(page, root=tmp_path)

    async def run():
        await trace.start()
        assert await trace.finish({"stage": "click"}) is None

    asyncio.run(run())
    assert page.calls == [(_START_JS, None), (_STOP_JS, None)]
    assert not list(tmp_path.iterdir())


def test_trace_script_has_event_and_duration_bounds_without_collecting_page_text_or_credentials():
    for fragment in [
        "events.length > 160",
        "25000",
        "'scrollTo'",
        "'scrollBy'",
        "'scrollIntoView'",
        "elementsFromPoint",
        "focusin",
        "menu-state",
    ]:
        assert fragment in _START_JS
    for forbidden in [
        "document.cookie",
        "localStorage",
        "sessionStorage",
        ".href",
        "outerHTML",
        "innerHTML",
        "e.keyCode",
    ]:
        assert forbidden not in _START_JS
    assert "window.__goofishSortTraceV1" in _STOP_JS


def test_action_log_preserves_scroll_and_blocker_order_without_raw_html_or_secrets():
    from goofish_cli.core.sort_diagnostics import _action_log

    error = RuntimeError(
        'Cookie: secret-cookie\nURL https://example.test/?token=secret-token\nattempting click action\nscrolling into view if needed\ndone scrolling\n<div class="search-header--abc">private account name</div> intercepts pointer events\nretrying click action'
    )
    result = _action_log(error)
    assert [entry["action"] for entry in result] == [
        "attempting click action",
        "scrolling into view",
        "done scrolling",
        "intercepts pointer events",
        "retrying click action",
    ]
    assert result[3]["blocker_classes"] == ["search-header--abc"]
    assert result[3]["blocker_tags"] == ["div"]
    encoded = json.dumps(result)
    for forbidden in ["secret-cookie", "secret-token", "private account name", "https://", "<div"]:
        assert forbidden not in encoded
