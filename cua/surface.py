"""The Surface: how we perceive and act on a target application.

This is the seam between "the recorded flow" and "how a flow touches a UI".
The replay engine and the discovery agent both talk to this interface only;
swapping the browser implementation for a desktop (UIA/accessibility-API)
implementation would not change artifacts or the replay engine.

Perception is the accessibility tree (not the DOM): it survives non-semantic
legacy markup, and it is the one representation that also exists for desktop
apps. Action targeting is semantic-first (role/name, label) with structural
fallbacks (visible text, CSS), tried in ranked order.

Every action passes through the safety policy: origin allowlist, action
allowlist, and risky-control classification. Policy lives here -- below the
model -- so a prompt injection cannot bypass it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout, sync_playwright

from .policy import Policy, PolicyViolation, RiskyActionBlocked
from .schema import ElementTarget, LocatorStrategy


class ResolveError(Exception):
    """No locator strategy resolved to a visible element in time."""

    def __init__(self, target: ElementTarget, timeout_ms: int):
        self.target = target
        super().__init__(
            f"could not resolve element ({target.description or 'no description'}) "
            f"within {timeout_ms} ms; tried: "
            + "; ".join(s.describe() for s in target.strategies)
        )


@dataclass
class Observation:
    url: str
    title: str
    aria: str

    def for_llm(self, max_chars: int = 7000) -> str:
        aria = self.aria
        if len(aria) > max_chars:
            aria = aria[:max_chars] + "\n... (snapshot truncated)"
        return f"URL: {self.url}\nTITLE: {self.title}\nACCESSIBILITY TREE:\n{aria}"


@dataclass
class ElementDescriptor:
    """What we record about an element at action time, used by the compiler
    to build fallback strategies."""

    tag: str = ""
    text: str = ""
    name_attr: str = ""
    css_path: str = ""


_DESCRIBE_JS = """el => {
  const path = [];
  let node = el;
  while (node && node.nodeType === 1 && path.length < 6) {
    let seg = node.tagName.toLowerCase();
    const parent = node.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter(c => c.tagName === node.tagName);
      if (same.length > 1) seg += `:nth-of-type(${same.indexOf(node) + 1})`;
    }
    path.unshift(seg);
    if (node.tagName === 'BODY') break;
    node = parent;
  }
  return {
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.value || '').trim().slice(0, 60),
    name_attr: el.getAttribute('name') || '',
    css_path: path.join(' > '),
  };
}"""

# Injected so that manual human actions during a handoff are captured as
# structured events (who did what while the automation was paused).
_HUMAN_RECORDER_JS = """
(() => {
  const report = (detail) => {
    if (window.__cua_record) window.__cua_record(detail);
  };
  document.addEventListener('click', (e) => {
    const t = e.target.closest('a, button, input, select') || e.target;
    report({ kind: 'click', tag: t.tagName, text: (t.innerText || t.value || '').slice(0, 60) });
  }, true);
  document.addEventListener('change', (e) => {
    const t = e.target;
    const secret = t.type === 'password';
    report({ kind: 'change', tag: t.tagName, name: t.name || '',
             value: secret ? '***' : String(t.value || '').slice(0, 60) });
  }, true);
})();
"""


class BrowserSurface:
    def __init__(
        self,
        policy: Policy,
        headed: bool = False,
        on_human_action: Optional[Callable[[dict], None]] = None,
    ):
        self.policy = policy
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not headed)
        self._context = self._browser.new_context(viewport={"width": 1100, "height": 800})
        self.human_control = False
        self._on_human_action = on_human_action

        def _record(_source, detail):
            if self.human_control and self._on_human_action:
                self._on_human_action(dict(detail))

        self._context.expose_binding("__cua_record", _record)
        self._context.add_init_script(_HUMAN_RECORDER_JS)
        self.page: Page = self._context.new_page()
        self.page.set_default_timeout(policy.action_timeout_ms)
        self.page.set_default_navigation_timeout(policy.navigation_timeout_ms)

    # ---- perception ------------------------------------------------------
    def observe(self) -> Observation:
        try:
            aria = self.page.locator("body").aria_snapshot()
        except Exception:
            aria = "(no accessibility snapshot available)"
        return Observation(url=self.page.url, title=self.page.title(), aria=aria)

    def text_visible(self, text: str, timeout_ms: int = 1000) -> bool:
        try:
            loc = self.page.get_by_text(text, exact=False).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def screenshot(self, path: str) -> None:
        try:
            self.page.screenshot(path=path, full_page=True)
        except Exception:
            pass

    # ---- element resolution ---------------------------------------------
    def _candidates(self, strat: LocatorStrategy) -> Optional[Locator]:
        p = self.page
        if strat.kind == "role" and strat.role:
            return p.get_by_role(strat.role, name=strat.name) if strat.name else p.get_by_role(strat.role)
        if strat.kind == "label" and strat.label:
            return p.get_by_label(strat.label, exact=False)
        if strat.kind == "text" and strat.text:
            return p.get_by_text(strat.text, exact=False)
        if strat.kind == "css" and strat.selector:
            return p.locator(strat.selector)
        if strat.kind == "row_cell" and strat.row_text and strat.column_header:
            return self._resolve_row_cell(strat.row_text, strat.column_header)
        return None

    def _resolve_row_cell(self, row_text: str, column_header: str) -> Optional[Locator]:
        """Semantic table-cell addressing for legacy table-soup markup:
        find the column index by header text, the row by its text, return the
        intersecting cell. No ids, classes, or stable DOM shape required."""
        tables = self.page.locator("table")
        for ti in range(tables.count()):
            table = tables.nth(ti)
            try:
                rows = table.locator("tr")
                if rows.count() < 2:
                    continue
                # Header cell must EQUAL the header text (case-insensitive);
                # substring matching would latch onto outer layout tables that
                # merely contain the data table (classic legacy nesting trap).
                header_cells = rows.first.locator("th, td")
                col_idx = None
                for ci in range(header_cells.count()):
                    if header_cells.nth(ci).inner_text().strip().lower() == column_header.strip().lower():
                        col_idx = ci
                        break
                if col_idx is None:
                    continue
                for ri in range(1, rows.count()):  # data rows only
                    row = rows.nth(ri)
                    if row_text.lower() in row.inner_text().lower():
                        cells = row.locator("td, th")
                        if cells.count() > col_idx:
                            return cells.nth(col_idx)
            except Exception:
                continue
        return None

    def resolve(
        self, target: ElementTarget, timeout_ms: Optional[int] = None
    ) -> tuple[Locator, LocatorStrategy]:
        """Try strategies in ranked order until one yields a visible element.

        Returns the locator plus the strategy that won -- replay records this,
        and a capability that keeps resolving via fallbacks instead of its
        primary strategy is drifting and should be flagged for re-discovery.
        """
        timeout_ms = timeout_ms or self.policy.action_timeout_ms
        deadline = time.monotonic() + timeout_ms / 1000
        last_err: Optional[Exception] = None
        while True:
            for strat in target.strategies:
                try:
                    loc = self._candidates(strat)
                    if loc is None:
                        continue
                    loc = loc.first
                    if loc.is_visible():
                        return loc, strat
                except Exception as e:  # keep trying other strategies
                    last_err = e
            if time.monotonic() > deadline:
                raise ResolveError(target, timeout_ms) from last_err
            time.sleep(0.25)

    def describe(self, loc: Locator) -> ElementDescriptor:
        try:
            d = loc.evaluate(_DESCRIBE_JS)
            return ElementDescriptor(**d)
        except Exception:
            return ElementDescriptor()

    # ---- policy-checked actions -----------------------------------------
    def _pre_action(self, action: str) -> None:
        self.policy.check_action(action)
        if self.page.url and self.page.url != "about:blank":
            self.policy.check_origin(self.page.url)

    def navigate(self, url: str) -> None:
        self.policy.check_action("navigate")
        self.policy.check_origin(url)
        self.page.goto(url, wait_until="load")

    def click(self, target: ElementTarget, timeout_ms: Optional[int] = None) -> tuple[LocatorStrategy, ElementDescriptor]:
        self._pre_action("click")
        loc, strat = self.resolve(target, timeout_ms)
        desc = self.describe(loc)
        control_text = desc.text or strat.name or strat.text or strat.label or ""
        self.policy.check_click_allowed(control_text)
        loc.click(timeout=timeout_ms or self.policy.action_timeout_ms)
        return strat, desc

    def type(self, target: ElementTarget, value: str, timeout_ms: Optional[int] = None) -> tuple[LocatorStrategy, ElementDescriptor]:
        self._pre_action("type")
        loc, strat = self.resolve(target, timeout_ms)
        desc = self.describe(loc)
        loc.fill(value, timeout=timeout_ms or self.policy.action_timeout_ms)
        return strat, desc

    def select(self, target: ElementTarget, option: str, timeout_ms: Optional[int] = None) -> tuple[LocatorStrategy, ElementDescriptor]:
        self._pre_action("select")
        loc, strat = self.resolve(target, timeout_ms)
        desc = self.describe(loc)
        loc.select_option(label=option, timeout=timeout_ms or self.policy.action_timeout_ms)
        return strat, desc

    def press(self, key: str) -> None:
        self._pre_action("press")
        self.page.keyboard.press(key)

    def extract_text(self, target: ElementTarget, timeout_ms: Optional[int] = None) -> str:
        self.policy.check_action("extract")
        loc, _ = self.resolve(target, timeout_ms)
        return loc.inner_text().strip()

    def wait_load(self, state: str = "load", text_visible: Optional[str] = None) -> None:
        try:
            self.page.wait_for_load_state(state)
        except PWTimeout:
            pass
        if text_visible:
            self.page.get_by_text(text_visible, exact=False).first.wait_for(state="visible")

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()
