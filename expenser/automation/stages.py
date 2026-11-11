"""Generic "stages" that read information from the app: which groups exist, who the members are.

Everything app-specific (URLs, selectors, regexes) lives in the flow's ``group_picker`` and
``members`` sections; this module only executes them on an open page.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page

from expenser.automation.flow import Flow, FlowError, render

__all__ = ["StageError", "discover_groups", "discover_members", "extract_group_value", "clean_label"]

_LABEL_MAX = 80

_READ_ITEMS_JS = """(els, args) => els.map(el => {
  const attr = args.attr === 'text' ? (el.innerText || el.textContent || '') : (el.getAttribute(args.attr) || '');
  let label = '';
  if (args.label) {
    const sub = el.querySelector(args.label);
    if (sub) label = sub.innerText || sub.textContent || '';
  }
  return {value: attr, label: label, text: el.innerText || el.textContent || ''};
})"""


class StageError(RuntimeError):
    """A stage could not read what it needs from the page."""


def clean_label(text: str | None) -> str:
    """Collapse whitespace and cap at 80 characters."""
    out = re.sub(r"\s+", " ", text or "").strip()
    return out if len(out) <= _LABEL_MAX else out[: _LABEL_MAX - 1].rstrip() + "…"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line
    return ""


def extract_group_value(flow: Flow, text: str) -> str | None:
    """Value from a pasted URL/id using the picker's ``value_regex``; a bare id is used as is."""
    gp = flow.group_picker
    text = text.strip().strip("\"'")
    if not text:
        return None
    if gp is not None and gp.value_regex:
        m = re.search(gp.value_regex, text)
        if m:
            return m.group(1)
        if re.fullmatch(r"[\w.-]+", text):
            return text
        return None
    return text


def _selector(flow: Flow, key: str, ctx: dict[str, str], where: str) -> str:
    return render(flow.resolve_target(key), ctx, where)


def discover_groups(page: Page, flow: Flow) -> list[tuple[str, str]]:
    """Read ``(label, value)`` for each group on the current page (deduped by value, order kept)."""
    gp = flow.group_picker
    if gp is None:
        raise FlowError(f"Flow '{flow.name}' has no group_picker section")
    ctx = flow.base_context(flow.resolve_variables(None, partial=True))
    timeout = flow.settings.default_timeout_ms
    if gp.wait_for:
        try:
            page.locator(_selector(flow, gp.wait_for, ctx, "group_picker.wait_for")).first.wait_for(
                state="attached", timeout=timeout
            )
        except Exception:
            return []
    items = _selector(flow, gp.items, ctx, "group_picker.items")
    label_sel = _selector(flow, gp.label, ctx, "group_picker.label") if gp.label else None
    raw = page.eval_on_selector_all(items, _READ_ITEMS_JS, {"attr": gp.value_attribute, "label": label_sel})
    rx = re.compile(gp.value_regex) if gp.value_regex else None
    order: list[str] = []
    best: dict[str, tuple[int, str]] = {}  # value -> (label quality, label); 2 = label selector, 1 = text
    for item in raw:
        value = (item["value"] or "").strip()
        if rx is not None:
            m = rx.search(value)
            if not m:
                continue
            value = m.group(1)
        if not value:
            continue
        label = clean_label(item["label"])
        quality = 2
        if not label:
            label, quality = clean_label(_first_line(item["text"] or "")), 1
        if not label:
            quality = 0
        if value not in best:
            order.append(value)
            best[value] = (quality, label)
        elif quality > best[value][0]:  # e.g. an icon-only "edit" link came first
            best[value] = (quality, label)
    return [(best[v][1] or v, v) for v in order]


def discover_members(
    page: Page, flow: Flow, variables: dict[str, str]
) -> tuple[list[str], str | None]:
    """Open the members page and read ``(members, me)``. Raises :class:`StageError` when none found."""
    spec = flow.members
    if spec is None:
        raise FlowError(f"Flow '{flow.name}' has no members section")
    ctx = flow.base_context(flow.resolve_variables(variables))
    timeout = flow.settings.default_timeout_ms
    if spec.url:
        page.goto(render(spec.url, ctx, "members.url"))
    items = _selector(flow, spec.items, ctx, "members.items")
    try:
        page.locator(items).first.wait_for(state="attached", timeout=timeout)
    except Exception as exc:
        raise StageError(f"No members found on {page.url} (selector {items!r} did not appear)") from exc
    names: list[str] = []
    seen: set[str] = set()
    for text in page.eval_on_selector_all(items, "els => els.map(e => e.textContent || '')"):
        name = re.sub(r"\s+", " ", text).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        raise StageError(f"The members list on {page.url} is empty")
    me: str | None = None
    if spec.me:
        sel = _selector(flow, spec.me, ctx, "members.me")
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="attached", timeout=1500)
            candidate = re.sub(r"\s+", " ", loc.text_content() or "").strip()
            me = candidate or None
        except Exception:
            me = None
    return names, me
