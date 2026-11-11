"""Interactive tooling around flows: record selectors, validate, test, scaffold.

``record_selectors`` opens a headed browser with an in-page "picker": the user
navigates to where an element is visible, arms the picker from the terminal, and
clicks the element. The click is captured (not executed), candidate selectors are
generated in the page, validated here against the live DOM, and the chosen one is
written back into the flow YAML (comments and ordering preserved via ruamel).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Page
from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from expenser.automation.flow import Flow, FlowError, escape_braces, load_flow, render
from expenser.automation.runner import (
    FlowExecutor,
    StepEvent,
    StepFailure,
    UserAbort,
    open_session,
)
from expenser.models import Transaction

__all__ = [
    "PICKER_JS",
    "Candidate",
    "CandidateReport",
    "SelectorReport",
    "TestResult",
    "record_selectors",
    "validate_flow",
    "test_flow",
    "login_flow",
    "new_flow",
    "save_selector",
    "templatize",
    "find_participant",
    "evaluate_candidates",
    "choose_selector",
]

InputFn = Callable[[str], str]


# --------------------------------------------------------------------------- #
# In-page picker
# --------------------------------------------------------------------------- #

PICKER_JS = r"""
(() => {
  if (window.__expenserPicker) return;
  const INTERACTIVE = 'a,button,input,select,textarea,label,summary,[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch]';
  const TEST_ATTRS = ['data-testid', 'data-test', 'data-cy', 'data-qa', 'data-test-id'];
  const state = { armed: false, box: null, last: null };

  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const q = (s) => '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';

  function stableId(id) {
    if (!id || id.length > 40) return false;
    if (/:/.test(id) || /\d{3,}/.test(id) || /[0-9a-f]{8,}/i.test(id)) return false;
    const digits = (id.match(/\d/g) || []).length;
    return digits / id.length <= 0.3;
  }

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.split(/\s+/)[0];
    const t = el.tagName.toLowerCase();
    if (t === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (t === 'button') return 'button';
    if (t === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      const map = { checkbox: 'checkbox', radio: 'radio', button: 'button', submit: 'button',
        reset: 'button', image: 'button', range: 'slider', number: 'spinbutton',
        search: 'searchbox', text: 'textbox', email: 'textbox', tel: 'textbox', url: 'textbox' };
      return map[ty] || null;
    }
    if (/^h[1-6]$/.test(t)) return 'heading';
    const tagRoles = { img: 'img', li: 'listitem', ul: 'list', ol: 'list', nav: 'navigation',
      table: 'table', tr: 'row', td: 'cell', th: 'columnheader', dialog: 'dialog', option: 'option' };
    return tagRoles[t] || null;
  }

  function labelText(label, control) {
    const clone = label.cloneNode(true);
    clone.querySelectorAll('input,select,textarea,button').forEach((n) => n.remove());
    return norm(clone.textContent);
  }

  const FROM_CONTENT = ['button', 'link', 'option', 'menuitem', 'tab', 'heading', 'cell',
    'columnheader', 'listitem', 'row', 'checkbox', 'radio', 'switch'];

  function nameOf(el, role) {
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = norm(lb.split(/\s+/).map((i) => { const e = document.getElementById(i); return e ? e.textContent : ''; }).join(' '));
      if (t) return t;
    }
    const al = norm(el.getAttribute('aria-label'));
    if (al) return al;
    if (el.labels && el.labels.length) {
      const t = norm(Array.from(el.labels).map((l) => labelText(l, el)).join(' '));
      if (t) return t;
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'img') return norm(el.getAttribute('alt'));
    if (tag === 'input' && ['submit', 'button', 'reset'].includes(el.type)) return norm(el.value);
    if (FROM_CONTENT.includes(role) || tag === 'button' || tag === 'a') {
      const t = norm(el.textContent);
      if (t) return t;
    }
    return norm(el.getAttribute('placeholder')) || norm(el.getAttribute('title'));
  }

  function cssPath(el) {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body && parts.length < 5) {
      if (cur.id && stableId(cur.id)) { parts.unshift('#' + CSS.escape(cur.id)); break; }
      let part = cur.tagName.toLowerCase();
      const sibs = cur.parentElement ? Array.from(cur.parentElement.children).filter((c) => c.tagName === cur.tagName) : [];
      if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  function candidates(el) {
    const out = [];
    const add = (selector, kind) => { if (selector && !out.some((c) => c.selector === selector)) out.push({ selector, kind }); };
    const tag = el.tagName.toLowerCase();
    const isField = ['input', 'select', 'textarea'].includes(tag);
    const type = (el.getAttribute('type') || '').toLowerCase();

    for (const a of TEST_ATTRS) {
      const v = el.getAttribute(a);
      if (v) add('[' + a + '=' + q(v) + ']', 'test-id');
    }
    if (el.id && stableId(el.id)) add('#' + CSS.escape(el.id), 'id');

    const role = roleOf(el);
    const name = role ? nameOf(el, role) : '';
    if (role && name && name.length <= 60) add('role=' + role + '[name=' + q(name) + ']', 'role');

    if (isField && el.getAttribute('name')) {
      let s = tag + '[name=' + q(el.getAttribute('name')) + ']';
      if ((type === 'radio' || type === 'checkbox') && el.getAttribute('value')) s += '[value=' + q(el.getAttribute('value')) + ']';
      add(s, 'name');
    }
    if (el.getAttribute('placeholder')) add(tag + '[placeholder=' + q(el.getAttribute('placeholder')) + ']', 'placeholder');
    if (el.getAttribute('aria-label')) add('[aria-label=' + q(el.getAttribute('aria-label')) + ']', 'aria-label');

    const own = norm(el.innerText || el.textContent);
    if (!isField && own && own.length <= 40 && !own.includes('\n')) add('text=' + q(own), 'text');

    if (isField && el.labels && el.labels.length) {
      const lab = el.labels[0];
      const lt = labelText(lab, el);
      if (lt && lt.length <= 60) {
        const inner = tag + (type ? '[type=' + type + ']' : '');
        if (lab.contains(el)) add('label:has-text(' + q(lt) + ') >> ' + inner, 'label');
        else add('internal:label=' + q(lt) + 'i', 'label');
      }
    }
    add(cssPath(el), 'css');
    return out;
  }

  function resolveTarget(node) {
    if (!(node instanceof Element)) return null;
    return node.closest(INTERACTIVE) || node;
  }

  function mark(el) {
    document.querySelectorAll('[data-expenser-picked]').forEach((n) => n.removeAttribute('data-expenser-picked'));
    if (el) el.setAttribute('data-expenser-picked', '1');
  }

  function ensureBox() {
    if (state.box && state.box.isConnected) return state.box;
    const b = document.createElement('div');
    b.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;border:2px solid #e11d48;' +
      'background:rgba(225,29,72,.12);border-radius:3px;display:none;';
    document.documentElement.appendChild(b);
    state.box = b;
    return b;
  }

  function place(el) {
    const b = ensureBox();
    const r = el.getBoundingClientRect();
    b.style.display = 'block';
    b.style.left = r.left + 'px'; b.style.top = r.top + 'px';
    b.style.width = r.width + 'px'; b.style.height = r.height + 'px';
  }

  function disarm() {
    state.armed = false;
    state.last = null;
    if (state.box) state.box.style.display = 'none';
    document.documentElement.style.cursor = '';
  }

  function report(payload) {
    disarm();
    try { window.__expenserPicked(payload); } catch (e) { console.error('expenser: binding missing', e); }
  }

  const BLOCK = ['mousedown', 'mouseup', 'pointerdown', 'pointerup', 'dblclick', 'auxclick', 'contextmenu', 'submit'];
  BLOCK.forEach((type) => document.addEventListener(type, (e) => {
    if (!state.armed) return;
    e.preventDefault(); e.stopImmediatePropagation();
  }, true));

  document.addEventListener('mousemove', (e) => {
    if (!state.armed) return;
    const el = resolveTarget(e.target);
    if (el && el !== state.last) { state.last = el; place(el); }
  }, true);

  document.addEventListener('click', (e) => {
    if (!state.armed) return;
    e.preventDefault(); e.stopImmediatePropagation();
    const el = resolveTarget(e.target);
    if (!el) return;
    mark(el);
    report({ cancelled: false, tag: el.tagName.toLowerCase(), text: norm(el.innerText || el.textContent).slice(0, 80),
             candidates: candidates(el), url: location.href });
  }, true);

  document.addEventListener('keydown', (e) => {
    if (!state.armed || e.key !== 'Escape') return;
    e.preventDefault(); e.stopImmediatePropagation();
    report({ cancelled: true });
  }, true);

  window.__expenserPicker = {
    arm() { state.armed = true; document.documentElement.style.cursor = 'crosshair'; },
    disarm,
    isArmed: () => state.armed,
    candidates,
    mark,
  };
})();
"""


@dataclass
class Candidate:
    selector: str
    kind: str


@dataclass
class CandidateReport:
    selector: str
    kind: str
    matches: int
    hit_index: int  # position of the clicked element among the matches; -1 = not matched

    @property
    def is_unique_hit(self) -> bool:
        return self.matches == 1 and self.hit_index == 0


def evaluate_candidates(page: Page, candidates: list[Candidate]) -> list[CandidateReport]:
    """Count matches of each candidate and locate the (marked) clicked element among them."""
    reports: list[CandidateReport] = []
    for c in candidates:
        try:
            loc = page.locator(c.selector)
            n = loc.count()
            idx = (
                loc.evaluate_all("els => els.findIndex(e => e.hasAttribute('data-expenser-picked'))")
                if n else -1
            )
        except Exception:
            n, idx = 0, -1
        reports.append(CandidateReport(c.selector, c.kind, n, idx))
    return reports


def default_choice(reports: list[CandidateReport]) -> int:
    """Index of the best candidate: first unique hit, else first hit, else the first one."""
    for i, r in enumerate(reports):
        if r.is_unique_hit:
            return i
    for i, r in enumerate(reports):
        if r.hit_index >= 0:
            return i
    return 0


# --------------------------------------------------------------------------- #
# Selection UI
# --------------------------------------------------------------------------- #

def _render_reports(console: Console, key: str, reports: list[CandidateReport], default: int) -> None:
    table = Table(title=f"Candidates for '{key}'", show_lines=False)
    for col in ("#", "kind", "selector", "matches", "clicked element?"):
        table.add_column(col)
    for i, r in enumerate(reports):
        mark = "[green]yes[/]" if r.is_unique_hit else (
            f"[yellow]nth={r.hit_index}[/]" if r.hit_index >= 0 else "[red]no[/]"
        )
        count = f"[green]{r.matches}[/]" if r.matches == 1 else f"[yellow]{r.matches}[/]"
        num = f"{i + 1}{' *' if i == default else ''}"
        table.add_row(num, r.kind, r.selector.replace("[", "\\["), count, mark)
    console.print(table)


def choose_selector(
    console: Console,
    input_fn: InputFn,
    key: str,
    reports: list[CandidateReport],
    check: Callable[[str], int],
) -> tuple[str, str | None]:
    """Ask the user which candidate to use.

    Returns ``("pick", selector)``, ``("repick", None)`` or ``("skip", None)``.
    """
    default = default_choice(reports) if reports else -1
    while True:
        _render_reports(console, key, reports, default) if reports else None
        prompt = (
            f"Number to use [Enter = {default + 1}], custom selector, "
            "[r]e-pick, [s]kip: " if reports else "Custom selector, [r]e-pick, [s]kip: "
        )
        answer = input_fn(prompt).strip()
        if answer == "" and default >= 0:
            return "pick", reports[default].selector
        if answer.lower() == "r":
            return "repick", None
        if answer.lower() == "s":
            return "skip", None
        if answer.isdigit():
            n = int(answer)
            if 1 <= n <= len(reports):
                return "pick", reports[n - 1].selector
            console.print(f"[red]Pick a number between 1 and {len(reports)}[/]")
            continue
        if answer:
            console.print(f"Custom selector matches {check(answer)} element(s) on this page.")
            if input_fn("Use it? [Y/n]: ").strip().lower() in ("", "y", "yes"):
                return "pick", answer


# --------------------------------------------------------------------------- #
# Templating helpers
# --------------------------------------------------------------------------- #

def find_participant(selector: str, participants: list[str] | None) -> str | None:
    """The participant name (as written in ``selector``) occurring in it, if any."""
    low = selector.lower()
    for name in sorted(participants or [], key=len, reverse=True):
        i = low.find(name.lower())
        if name and i >= 0:
            return selector[i : i + len(name)]
    return None


def templatize(selector: str, text: str | None, var: str = "p") -> str:
    """Replace ``text`` (case-insensitive) in ``selector`` by ``{var}``; escape other braces."""
    if not text:
        return escape_braces(selector)
    pieces = re.split(re.escape(text), selector, flags=re.IGNORECASE)
    return ("{" + var + "}").join(escape_braces(p) for p in pieces)


def _ask_templatize(
    console: Console, input_fn: InputFn, selector: str, participants: list[str] | None
) -> str:
    found = find_participant(selector, participants)
    if found:
        ans = input_fn(
            f"Selector contains '{found}'. Replace it with {{p}} (per-participant selector)? "
            "[y/N or other text to replace]: "
        ).strip()
        if ans.lower() in ("y", "yes"):
            text: str | None = found
        elif ans.lower() in ("", "n", "no"):
            text = None
        else:
            text = ans
    else:
        ans = input_fn(
            "Make this a per-participant selector? Enter the text to replace with {p} "
            "(blank = no): "
        ).strip()
        text = ans or None
    if text and text.lower() not in selector.lower():
        console.print(f"[yellow]'{text}' not found in the selector; keeping it as is.[/]")
        text = None
    result = templatize(selector, text)
    if text:
        console.print(f"Saved as: [bold]{result}[/]")
    return result


# --------------------------------------------------------------------------- #
# YAML saving (comment-preserving)
# --------------------------------------------------------------------------- #

def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def save_selector(flow_path: Path, key: str, selector: str) -> None:
    """Set ``selectors.<key>`` in the flow YAML, keeping comments and key order."""
    y = _yaml()
    data = y.load(flow_path.read_text(encoding="utf-8"))
    if not isinstance(data, CommentedMap):
        raise FlowError(f"{flow_path}: not a YAML mapping")
    sel = data.get("selectors")
    if sel is None:
        sel = CommentedMap()
        data["selectors"] = sel
    if hasattr(sel, "fa") and sel.fa.flow_style():
        sel.fa.set_block_style()
    sel[key] = selector
    tmp = flow_path.with_suffix(flow_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        y.dump(data, fh)
    tmp.replace(flow_path)


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #

def _start_url(flow: Flow, url: str | None, variables: dict[str, str] | None) -> str:
    if url:
        return url
    resolved = flow.resolve_variables(variables)
    return render(flow.start_url, flow.base_context(resolved), "start_url")


def _wait_for_pick(page: Page, box: dict[str, Any]) -> dict[str, Any] | None:
    while box.get("payload") is None:
        if page.is_closed():
            return None
        page.wait_for_timeout(150)
    payload, box["payload"] = box["payload"], None
    return payload


def record_selectors(
    flow_path: Path,
    *,
    keys: list[str] | None = None,
    url: str | None = None,
    variables: dict[str, str] | None = None,
    participants: list[str] | None = None,
    profile_dir: Path | None = None,
    input_fn: InputFn = input,
    console: Console | None = None,
    browser: str | None = None,
    cdp_url: str | None = None,
    chrome_path: str | None = None,
) -> dict[str, str]:
    """Interactively capture selectors and save them into the flow. Returns what was saved."""
    console = console or Console()
    flow = load_flow(flow_path)
    start = _start_url(flow, url, variables)
    saved: dict[str, str] = {}

    todo = list(keys) if keys else list(flow.selectors)
    ask_new = not todo
    box: dict[str, Any] = {"payload": None, "page": None}

    def on_pick(source: Any, payload: dict[str, Any]) -> None:
        box["payload"] = payload
        box["page"] = source["page"]

    with open_session(
        flow, headless=False, profile_dir=profile_dir, browser=browser, cdp_url=cdp_url,
        chrome_path=chrome_path,
    ) as (context, page):
        context.add_init_script(PICKER_JS)
        context.expose_binding("__expenserPicked", on_pick)
        page.goto(start)
        console.print(f"[bold]Recording selectors for '{flow.name}'[/] at {start}")

        def next_key() -> str | None:
            if todo:
                return todo.pop(0)
            if ask_new:
                name = input_fn("New selector key name (blank to finish): ").strip()
                return name or None
            return None

        while (key := next_key()) is not None:
            current = flow.selectors.get(key)
            if current:
                console.print(f"Current value of '{key}': [dim]{current}[/]")
            action = "repick"
            while action == "repick":
                answer = input_fn(
                    f"Navigate to where `{key}` is visible, then press Enter to arm the picker "
                    "(s=skip, q=quit): "
                ).strip().lower()
                if answer == "q":
                    return saved
                if answer == "s":
                    break
                page = box["page"] or page
                if page.is_closed():
                    console.print("[red]Browser window closed; stopping.[/]")
                    return saved
                armed = page.evaluate(
                    "window.__expenserPicker ? (window.__expenserPicker.arm(), true) : false"
                )
                if not armed:
                    console.print("[red]Picker not available on this page (try reloading it).[/]")
                    continue
                console.print(f"[cyan]Picker armed:[/] click `{key}` in the browser (Esc in the page cancels).")
                payload = _wait_for_pick(box["page"] or page, box)
                if payload is None:
                    console.print("[red]Browser window closed; stopping.[/]")
                    return saved
                if payload.get("cancelled"):
                    console.print("Cancelled.")
                    action = "repick"
                    continue
                pick_page: Page = box["page"] or page
                page = pick_page
                cands = [Candidate(c["selector"], c["kind"]) for c in payload["candidates"]]
                reports = evaluate_candidates(pick_page, cands)
                console.print(f"Clicked <{payload['tag']}> {payload.get('text', '')!r}")
                action, selector = choose_selector(
                    console, input_fn, key, reports, lambda s: pick_page.locator(s).count()
                )
                if action == "pick" and selector is not None:
                    final = _ask_templatize(console, input_fn, selector, participants)
                    save_selector(flow_path, key, final)
                    saved[key] = final
                    console.print(f"[green]Saved[/] selectors.{key} = {final}")
                    try:
                        pick_page.evaluate("window.__expenserPicker.mark(null)")
                    except Exception:
                        pass
    return saved


# --------------------------------------------------------------------------- #
# validate / test
# --------------------------------------------------------------------------- #

@dataclass
class SelectorReport:
    key: str
    selector: str  # rendered
    matches: int | None  # None = could not evaluate
    note: str = ""


def _sample_transaction(participants: list[str]) -> Transaction:
    people = participants or ["Sample"]
    return Transaction(
        name="Test expense", amount=Decimal("1.23"), participants=people[:2],
        paid_by=people[0], date=None,
    )


def validate_flow(
    flow_path: Path,
    *,
    variables: dict[str, str],
    url: str | None = None,
    sample_participant: str | None = None,
    headless: bool = False,
    profile_dir: Path | None = None,
    input_fn: InputFn = input,
    console: Console | None = None,
    browser: str | None = None,
    cdp_url: str | None = None,
    chrome_path: str | None = None,
) -> list[SelectorReport]:
    """Schema-check the flow, open the page, run login + setup, count matches per selector."""
    console = console or Console()
    flow = load_flow(flow_path)  # raises FlowError with location on schema problems
    console.print(f"[green]Schema OK[/] for flow '{flow.name}'.")
    resolved = flow.resolve_variables(variables)
    sample = sample_participant or "Sample"
    reports: list[SelectorReport] = []
    with open_session(
        flow, headless=headless, profile_dir=profile_dir, browser=browser, cdp_url=cdp_url,
        chrome_path=chrome_path,
    ) as (_, page):
        ex = FlowExecutor(
            page, flow, variables=resolved, all_participants=[sample], input_fn=input_fn,
            console=console, verbose=False,
        )
        ex.goto_start(url)
        ex.ensure_logged_in(interactive=not headless)
        ex.run_setup()
        ctx = flow.context(resolved, _sample_transaction([sample]), [sample])
        for var in flow.loop_var_names():
            ctx = flow.bind_loop_var(ctx, var, sample, 0, "transaction.participants")
        for key in flow.selectors:
            try:
                rendered = ex.render_selector(key, ctx)
                reports.append(SelectorReport(key, rendered, page.locator(rendered).count()))
            except Exception as exc:
                reports.append(SelectorReport(key, flow.selectors[key], None, str(exc).splitlines()[0]))
    _print_selector_reports(console, reports, page_url=url or flow.start_url)
    return reports


def _print_selector_reports(console: Console, reports: list[SelectorReport], page_url: str) -> None:
    table = Table(title="Selector matches on the page after login/setup")
    for col in ("key", "selector", "matches", ""):
        table.add_column(col)
    for r in reports:
        if r.matches is None:
            status, note = "[red]error[/]", r.note
        elif r.matches == 0:
            status, note = "[red]0[/]", "not on this page (or wrong)"
        elif r.matches == 1:
            status, note = "[green]1[/]", "ok"
        else:
            status, note = f"[yellow]{r.matches}[/]", "many (fine for lists; otherwise use nth/narrow it)"
        table.add_row(r.key, r.selector.replace("[", "\\["), status, note)
    console.print(table)
    console.print(
        "[dim]Note: selectors that only appear later (e.g. inside the add-expense dialog) "
        "show 0 here. Check them with `expenser flow test <flow> --step`.[/]"
    )


@dataclass
class TestResult:
    __test__ = False  # not a pytest class

    ok: bool
    failed_step: str | None = None
    error: str | None = None


def test_flow(
    flow_path: Path,
    *,
    variables: dict[str, str],
    all_participants: list[str],
    transaction: Transaction | None = None,
    step: bool = False,
    headless: bool = False,
    profile_dir: Path | None = None,
    state_dir: Path | None = None,
    input_fn: InputFn = input,
    console: Console | None = None,
    browser: str | None = None,
    cdp_url: str | None = None,
    chrome_path: str | None = None,
) -> TestResult:
    """Run ``per_transaction`` once for a sample transaction in dry-run mode."""
    console = console or Console()
    flow = load_flow(flow_path)
    if not flow.has_commit_step():
        raise ValueError(
            "The flow has no `commit: true` step; refusing to test because it would really "
            "create the expense. Mark the final Save step with `commit: true`."
        )
    resolved = flow.resolve_variables(variables)
    tx = transaction or _sample_transaction(all_participants)

    def hook(ev: StepEvent) -> None:
        if ev.step.kind in ("for_each",) and not step:
            return
        console.print(f"[bold cyan]{'  ' * ev.depth}> {ev.step.where}[/]  {ev.text}")
        if step:
            ans = input_fn("  Enter = execute, q = quit: ").strip().lower()
            if ans == "q":
                raise UserAbort()

    with open_session(
        flow, headless=headless, profile_dir=profile_dir, browser=browser, cdp_url=cdp_url,
        chrome_path=chrome_path,
    ) as (_, page):
        ex = FlowExecutor(
            page, flow, variables=resolved, all_participants=all_participants, dry_run=True,
            input_fn=input_fn, console=console, state_dir=state_dir, step_hook=hook,
            verbose=False,
        )
        ex.goto_start()
        ex.ensure_logged_in(interactive=not headless)
        ex.run_setup()
        try:
            ex.run_transaction(tx)
        except StepFailure as exc:
            shot = ex.screenshot("test-failure")
            console.print(f"[red]FAILED at {exc.step.where}[/] ({exc.step.summary()}): {exc.message}")
            if shot:
                console.print(f"Screenshot: {shot}")
            return TestResult(False, exc.step.where, exc.message)
        except UserAbort:
            console.print("[yellow]Aborted.[/]")
            return TestResult(False, None, "aborted by user")
    console.print("[green]All per_transaction steps ran (dry run: commit steps were skipped).[/]")
    return TestResult(True)


def login_flow(
    flow_path: Path,
    *,
    variables: dict[str, str],
    profile_dir: Path | None = None,
    input_fn: InputFn = input,
    console: Console | None = None,
    browser: str | None = None,
    cdp_url: str | None = None,
    chrome_path: str | None = None,
) -> bool:
    """One-time manual login: open the flow's browser/profile, wait for Enter, check login."""
    console = console or Console()
    flow = load_flow(flow_path)
    resolved = flow.resolve_variables(variables)
    with open_session(
        flow, headless=False, profile_dir=profile_dir, browser=browser, cdp_url=cdp_url,
        chrome_path=chrome_path,
    ) as (_, page):
        ex = FlowExecutor(
            page, flow, variables=resolved, all_participants=[], input_fn=input_fn,
            console=console, verbose=False,
        )
        ex.goto_start()
        console.print("Log in in the browser window, then press Enter here.")
        input_fn("Press Enter when logged in... ")
        status = ex.is_logged_in(timeout_ms=flow.settings.default_timeout_ms)
    if status is None:
        console.print("[yellow]Flow has no login.logged_in_selector; cannot verify the login.[/]")
        return True
    if status:
        console.print("[green]Login detected.[/] It is stored in the browser profile.")
    else:
        console.print("[red]Login not detected[/] (logged_in_selector not visible).")
    return status


# --------------------------------------------------------------------------- #
# scaffold
# --------------------------------------------------------------------------- #

_FALLBACK_TEMPLATE = """\
name: my_app
description: Add expenses to my_app
start_url: "https://example.com/app"
variables: {}
settings:
  default_timeout_ms: 10000
  slow_mo_ms: 100
  amount_decimal_separator: "."
  date_format: "%d-%m-%Y"
participant_aliases: {}
login:
  logged_in_selector: logged_in_marker
selectors:
  logged_in_marker: "body"
setup: []
per_transaction: []
dry_run_cleanup: []
teardown: []
"""


def _template_text() -> str:
    from expenser.paths import bundled_flows_dir

    candidate = bundled_flows_dir() / "_template.yaml"
    return candidate.read_text(encoding="utf-8") if candidate.exists() else _FALLBACK_TEMPLATE


def new_flow(path: Path, name: str, start_url: str) -> Path:
    """Write a commented template flow to ``path`` with ``name``/``start_url`` filled in."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    if not re.match(r"^[A-Za-z0-9_.-]+$", name):
        raise ValueError("flow name may only contain letters, digits, '_', '-' and '.'")
    text = _template_text()
    text, n1 = re.subn(r"(?m)^name:.*$", lambda _m: f"name: {name}", text, count=1)
    quoted = '"' + start_url.replace("\\", "\\\\").replace('"', '\\"') + '"'
    text, n2 = re.subn(r"(?m)^start_url:.*$", lambda _m: f"start_url: {quoted}", text, count=1)
    if not (n1 and n2):
        raise RuntimeError("template is missing the name/start_url lines")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    load_flow(path)  # make sure the scaffold is valid
    return path
