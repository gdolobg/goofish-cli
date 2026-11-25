"""Bounded, local-only evidence for search sort failures; never capture network or page text."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_START_JS = r"""() => {
  const key = '__goofishSortTraceV1';
  if (window[key]) window[key].stop();
  const start = performance.now(), events = [], cleanups = [];
  const allowed = new Set(['综合', '信用排序', '最近活跃', '距离最近', '新降价', '新发布', '价格']);
  const describe = e => {
    if (!(e instanceof Element)) return null;
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    const cls = typeof e.className === 'string' ? e.className : '';
    return {tag: e.tagName, classes: cls.split(/\s+/).filter(v => /^[\w-]+$/.test(v)).slice(0, 3).join(' ').slice(0, 120),
      x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height),
      display: s.display, visibility: s.visibility, opacity: s.opacity, zIndex: s.zIndex, pointerEvents: s.pointerEvents};
  };
  const controls = () => [...document.querySelectorAll('[class*="search-select-title"], [class*="search-select-item"]')]
    .filter(e => allowed.has((e.textContent || '').trim())).slice(0, 12)
    .map(e => ({label: e.textContent.trim(), ...describe(e)}));
  const add = (type, extra = {}) => {
    events.push({ms: Math.round(performance.now() - start), type, scrollY: Math.round(scrollY), ...extra});
    if (events.length > 160) events.shift();
  };
  const stack = () => String(new Error().stack || '').split('\n').slice(2, 7)
    .map(line => line.replace(/(?:https?|file|webpack):\S+/g, '[script]')).join('\n').slice(0, 600);
  const listen = (target, name, fn) => {
    target.addEventListener(name, fn, {capture: true, passive: true});
    cleanups.push(() => target.removeEventListener(name, fn, true));
  };
  let pointer = null, lastMove = 0, lastScroll = 0;
  for (const name of ['pointerdown', 'pointerup', 'click', 'mouseover', 'mouseout', 'focusin', 'focusout']) {
    listen(document, name, e => add(name, {target: describe(e.target)}));
  }
  listen(document, 'pointermove', e => {
    pointer = {x: Math.round(e.clientX), y: Math.round(e.clientY)};
    if (performance.now() - lastMove > 80) { lastMove = performance.now(); add('pointermove', {...pointer, target: describe(e.target)}); }
  });
  listen(document, 'scroll', () => {
    if (performance.now() - lastScroll > 40) { lastScroll = performance.now(); add('scroll', {controls: controls()}); }
  });
  listen(document, 'wheel', e => add('wheel', {deltaY: Math.round(e.deltaY)}));
  listen(document, 'keydown', e => add('keydown', {key: ['Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'].includes(e.key) ? e.key : 'other'}));
  for (const [object, name] of [[window, 'scrollTo'], [window, 'scrollBy'], [Element.prototype, 'scrollIntoView']]) {
    const original = object[name];
    const wrapped = function(...args) {
      add(name, {stack: stack(), target: describe(this)});
      return Reflect.apply(original, this, args);
    };
    object[name] = wrapped;
    cleanups.push(() => { if (object[name] === wrapped) object[name] = original; });
  }
  let signature = '';
  const timer = setInterval(() => {
    const value = controls(), next = JSON.stringify(value);
    if (next !== signature) { signature = next; add('menu-state', {controls: value}); }
  }, 100);
  cleanups.push(() => clearInterval(timer));
  const mark = stage => {
    const value = controls(), points = value.filter(e => e.label === '信用排序' || e.label === '最近活跃')
      .filter(e => e.width > 0 && e.height > 0).map(e => ({x: e.x + e.width / 2, y: e.y + e.height / 2}));
    if (pointer) points.unshift(pointer);
    add('stage', {stage, active: describe(document.activeElement), controls: value,
      hitTests: points.slice(0, 4).map(p => ({...p, elements: document.elementsFromPoint(p.x, p.y).slice(0, 4).map(describe)}))});
  };
  let stopped = false;
  const stop = () => {
    if (!stopped) { stopped = true; cleanups.reverse().forEach(fn => fn()); }
    return {events, viewport: {width: innerWidth, height: innerHeight}, expired};
  };
  let expired = false;
  const deadline = setTimeout(() => { expired = true; stop(); }, 25000);
  cleanups.push(() => clearTimeout(deadline));
  window[key] = {mark, stop};
  mark('start');
}"""
_MARK_JS = "stage => window.__goofishSortTraceV1?.mark(stage)"
_STOP_JS = "() => { const trace = window.__goofishSortTraceV1; if (!trace) return null; trace.mark('finish'); const result = trace.stop(); delete window.__goofishSortTraceV1; return result; }"
# Only the known sort controls remain visible; no account, product, price input or QR content.
_SCREENSHOT_STYLE = "* { visibility: hidden !important; } [class*=search-select-title], [class*=search-select-title] *, [class*=search-select-item], [class*=search-select-item] * { visibility: visible !important; }"
_BUNDLE_NAME = re.compile(r"sort-[0-9a-f]{32}\.json$")


def _action_log(error: Exception) -> list[dict]:
    # Retain action order and blocker identity, never arbitrary exception text/HTML.
    phrases = (
        "attempting click action",
        "retrying click action",
        "scrolling into view",
        "done scrolling",
        "element is not visible",
        "element is not stable",
        "element was detached",
        "intercepts pointer events",
    )
    entries = []
    for line in str(error).splitlines():
        action = next((phrase for phrase in phrases if phrase in line), None)
        if action:
            entry = {"action": action}
            if action == "intercepts pointer events":
                tags = re.findall(r"<([a-zA-Z][a-zA-Z0-9-]*)", line)
                classes = re.findall(r'class="([a-zA-Z0-9_ -]{1,160})"', line)
                entry["blocker_tags"] = tags[:3]
                entry["blocker_classes"] = classes[:3]
            entries.append(entry)
    return entries[-80:]


class SortDiagnostics:
    def __init__(self, page, root: Path | None = None):
        self.page = page
        self.root = root or Path.home() / ".goofish-cli" / "diagnostics" / "search-sort"
        self.active = False

    async def start(self):
        try:
            await asyncio.wait_for(self.page.evaluate(_START_JS), timeout=2)
            self.active = True
        except Exception:
            # Installation might have completed just before a transport timeout.
            await self.finish(cleanup_only=True)

    async def mark(self, stage: str):
        if self.active:
            with suppress(Exception):
                await asyncio.wait_for(self.page.evaluate(_MARK_JS, stage), timeout=1)

    async def finish(
        self,
        failure: dict | None = None,
        *,
        cleanup_only=False,
        source_error: Exception | None = None,
    ):
        if not self.active and not cleanup_only:
            return None
        self.active = False
        try:
            trace = await asyncio.wait_for(self.page.evaluate(_STOP_JS), timeout=2)
        except Exception as error:
            trace = {"capture_error": type(error).__name__}
        if failure is None:
            return None
        if source_error is not None:
            failure = {**failure, "action_log": _action_log(source_error)}
        token = "sort-" + uuid4().hex
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            image_error = None
            try:
                await asyncio.wait_for(
                    self.page.screenshot(
                        path=str(self.root / (token + ".png")),
                        style=_SCREENSHOT_STYLE,
                        timeout=2000,
                        caret="hide",
                        full_page=False,
                    ),
                    timeout=3,
                )
                (self.root / (token + ".png")).chmod(0o600)
            except Exception as error:
                image_error = type(error).__name__
                (self.root / (token + ".png")).unlink(missing_ok=True)
            value = {
                "id": token,
                "capturedAt": datetime.now(UTC).isoformat(),
                "failure": failure,
                "trace": trace,
                "screenshot_error": image_error,
            }
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            if len(data) > 512 * 1024:
                value["trace"] = {"capture_error": "size_limit"}
                data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            path = self.root / (token + ".json")
            with path.open("xb") as handle:
                handle.write(data)
            path.chmod(0o600)
            bundles = sorted(
                (p for p in self.root.glob("sort-*.json") if _BUNDLE_NAME.fullmatch(p.name)),
                key=lambda p: p.stat().st_mtime_ns,
                reverse=True,
            )
            for old in bundles[5:]:
                old.with_suffix(".png").unlink(missing_ok=True)
                old.unlink(missing_ok=True)
            return token
        except Exception:
            with suppress(OSError):
                (self.root / (token + ".png")).unlink(missing_ok=True)
                (self.root / (token + ".json")).unlink(missing_ok=True)
            return None
