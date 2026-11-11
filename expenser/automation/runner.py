"""Execute a :class:`~expenser.automation.flow.Flow` in a browser.

Two layers:

* :class:`FlowExecutor` runs steps against an already-open Playwright ``Page``.
* :func:`push_transactions` manages the browser lifecycle, login, the dedup ledger,
  retries and result reporting.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page
from rich.console import Console

from expenser.automation.browser import default_profile_dir, open_session
from expenser.automation.flow import Flow, Step, load_flow, render
from expenser.models import Transaction
from expenser.paths import state_dir as default_state_dir

__all__ = [
    "PushResult",
    "push_transactions",
    "push_on_page",
    "wait_for_login",
    "ledger_scope",
    "ledger_path",
    "FlowExecutor",
    "StepFailure",
    "UserAbort",
    "UserSkip",
    "LoginRequired",
    "Ledger",
    "open_session",
    "default_profile_dir",
]

InputFn = Callable[[str], str]
StepHook = Callable[["StepEvent"], None]


@dataclass
class PushResult:
    transaction: Transaction
    status: str  # "pushed" | "dry_run" | "skipped_duplicate" | "skipped_by_user" | "failed"
    error: str | None = None
    screenshot: Path | None = None


@dataclass
class StepEvent:
    """Passed to ``step_hook`` right before a step runs."""

    step: Step
    depth: int
    text: str  # rendered one-line description


class StepFailure(Exception):
    def __init__(self, step: Step, message: str) -> None:
        self.step = step
        self.message = message
        super().__init__(f"{step.where} ({step.summary()}): {message}")


class UserAbort(Exception):
    """The user chose to stop everything."""


class UserSkip(Exception):
    """The user declined to commit the current transaction."""


class LoginRequired(RuntimeError):
    """Not logged in and no way to log in interactively."""


def _short_error(exc: BaseException) -> str:
    """First meaningful line(s) of a (Playwright) exception."""
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    return " | ".join(lines[:2]) if lines else exc.__class__.__name__


@dataclass
class _Scope:
    ctx: dict[str, str]
    lists: dict[str, list[str]]


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #

class FlowExecutor:
    """Runs flow steps on a page. Knows nothing about browser launch or the ledger."""

    def __init__(
        self,
        page: Page,
        flow: Flow,
        *,
        variables: dict[str, str],
        all_participants: list[str],
        dry_run: bool = False,
        confirm_commit: bool = False,
        input_fn: InputFn = input,
        console: Console | None = None,
        state_dir: Path | None = None,
        step_hook: StepHook | None = None,
        verbose: bool = True,
    ) -> None:
        self.page = page
        self.flow = flow
        self.variables = flow.resolve_variables(variables)
        self.all_participants = list(all_participants)
        self.dry_run = dry_run
        self.confirm_commit = confirm_commit
        self.input_fn = input_fn
        self.console = console or Console()
        self.state_dir = state_dir or default_state_dir()
        self.step_hook = step_hook
        self.verbose = verbose
        self._tx: Transaction | None = None
        self._commit_confirmed = False
        self.dry_run_screenshot: Path | None = None
        self._shot_counter = 0
        page.set_default_timeout(flow.settings.default_timeout_ms)

    # ---- public API ----------------------------------------------------- #
    def start_url(self, override: str | None = None) -> str:
        if override:
            return override
        return render(self.flow.start_url, self.flow.base_context(self.variables), "start_url")

    def goto_start(self, url: str | None = None) -> None:
        target = self.start_url(url)
        self._log(f"goto {target}")
        self.page.goto(target)

    def render_selector(self, key_or_selector: str, ctx: dict[str, str]) -> str:
        return render(self.flow.resolve_target(key_or_selector), ctx, f"selector '{key_or_selector}'")

    def is_logged_in(self, timeout_ms: int | None = None) -> bool | None:
        """True/False, or None when the flow defines no login check."""
        key = self.flow.login.logged_in_selector
        if key is None:
            return None
        sel = self.render_selector(key, self.flow.base_context(self.variables))
        try:
            self.page.locator(sel).first.wait_for(
                state="visible", timeout=timeout_ms or self.flow.settings.login_check_timeout_ms
            )
            return True
        except Exception:
            return False

    def ensure_logged_in(self, *, interactive: bool) -> None:
        """Check ``login.logged_in_selector``; let the user sign in in the browser if allowed."""
        wait_for_login(
            self.page, self.flow, console=self.console, variables=self.variables,
            interactive=interactive,
        )

    def run_setup(self) -> None:
        self._run_section("setup", self.flow.setup, _Scope(self.flow.base_context(self.variables), {}))

    def run_teardown(self) -> None:
        self._run_section(
            "teardown", self.flow.teardown, _Scope(self.flow.base_context(self.variables), {})
        )

    def run_transaction(self, tx: Transaction) -> str:
        """Run ``per_transaction``. Returns ``"pushed"`` or ``"dry_run"``."""
        self._tx = tx
        self._commit_confirmed = False
        self.dry_run_screenshot = None
        scope = _Scope(
            self.flow.context(self.variables, tx, self.all_participants),
            self.flow.item_lists(tx, self.all_participants),
        )
        self.run_steps(self.flow.per_transaction, scope)
        status = "dry_run" if self.dry_run else "pushed"
        if self.dry_run:
            self.cleanup_after_dry_run()
        return status

    def cleanup_after_dry_run(self) -> None:
        if self.flow.dry_run_cleanup:
            scope = _Scope(self.flow.base_context(self.variables), {})
            self._run_section("dry_run_cleanup", self.flow.dry_run_cleanup, scope)
        else:
            self.recover()

    def recover(self) -> None:
        """Reload the start page and re-run setup (used after failures / default dry-run cleanup)."""
        self.goto_start()
        self.run_setup()

    def screenshot(self, name: str | None = None) -> Path | None:
        self._shot_counter += 1
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.state_dir / "screenshots" / f"{stamp}-{name or self._shot_counter}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), full_page=True)
            return path
        except Exception as exc:  # screenshots must never mask the real error
            self.console.print(f"[dim]could not take screenshot: {_short_error(exc)}[/]")
            return None

    # ---- step execution ------------------------------------------------- #
    def run_steps(self, steps: list[Step], scope: _Scope, depth: int = 0) -> None:
        for step in steps:
            self._exec(step, scope, depth)

    def _run_section(self, name: str, steps: list[Step], scope: _Scope) -> None:
        if steps:
            self._log(f"[bold]{name}[/]")
        self.run_steps(steps, scope)

    def _log(self, text: str, depth: int = 0) -> None:
        if self.verbose:
            self.console.print(f"[dim]{'  ' * depth}· {text}[/]", highlight=False)

    def _exec(self, step: Step, scope: _Scope, depth: int) -> None:
        try:
            if step.when is not None:
                value = render(step.when, scope.ctx, f"{step.where}.when").strip().lower()
                if value in ("", "0", "false", "no", "none"):
                    self._log(f"skip {step.summary()} (when is empty)", depth)
                    return
            text = step.description or step.summary()
            if self.step_hook:
                self.step_hook(StepEvent(step, depth, text))
            if step.commit:
                if self.dry_run:
                    self._log(f"[yellow]DRY-RUN skipped commit step:[/] {text}", depth)
                    if self.dry_run_screenshot is None:
                        self.dry_run_screenshot = self.screenshot("dryrun")
                    return
                self._confirm_commit()
            self._log(text, depth)
            self._dispatch(step, scope, depth)
        except (UserAbort, UserSkip):
            raise
        except StepFailure as exc:
            if step.optional:
                self._log(f"[yellow]optional step failed, continuing:[/] {exc.message}", depth)
                return
            raise
        except Exception as exc:
            if step.optional:
                self._log(f"[yellow]optional step failed, continuing:[/] {_short_error(exc)}", depth)
                return
            raise StepFailure(step, _short_error(exc)) from exc

    def _confirm_commit(self) -> None:
        if not self.confirm_commit or self._commit_confirmed:
            return
        label = f"'{self._tx.name}' {self._tx.amount}" if self._tx else "this transaction"
        while True:
            answer = self.input_fn(
                f"Commit {label}? [y]es / [n]o, skip it / [a]bort all: "
            ).strip().lower()[:1]
            if answer == "y":
                self._commit_confirmed = True
                return
            if answer == "n":
                raise UserSkip()
            if answer == "a":
                raise UserAbort()

    def _locator(self, step: Step, scope: _Scope, key: str = "target"):
        sel = self.render_selector(step.params[key], scope.ctx)
        loc = self.page.locator(sel)
        if step.params.get("nth") is not None:
            loc = loc.nth(step.params["nth"])
        return loc

    def _r(self, step: Step, scope: _Scope, key: str) -> str:
        return render(step.params[key], scope.ctx, f"{step.where}.{key}")

    def _reached(self, sel: str) -> bool:
        loc = self.page.locator(sel)
        return loc.count() > 0 and loc.first.is_visible()

    def _click_until(self, step: Step, scope: _Scope) -> None:
        p = step.params
        until = self.render_selector(p["until"], scope.ctx)
        if self._reached(until):
            return
        target = self._locator(step, scope)
        for _ in range(p["max"]):
            target.click()
            self.page.wait_for_timeout(p["delay_ms"])
            if self._reached(until):
                return
        raise ValueError(
            f"'{p['until']}' not reached after {p['max']} clicks of '{p['target']}'"
        )

    def _dispatch(self, step: Step, scope: _Scope, depth: int) -> None:
        p = step.params
        kind = step.kind
        timeout = p.get("timeout_ms")
        if kind == "goto":
            self.page.goto(self._r(step, scope, "url"))
        elif kind == "click":
            self._locator(step, scope).click(force=p.get("force", False))
        elif kind == "fill":
            self._locator(step, scope).fill(self._r(step, scope, "value"))
        elif kind == "type":
            loc = self._locator(step, scope)
            if p.get("clear", True):
                loc.fill("")
            loc.press_sequentially(self._r(step, scope, "value"), delay=p.get("delay_ms", 0))
        elif kind == "select":
            loc = self._locator(step, scope)
            if "label" in p:
                loc.select_option(label=self._r(step, scope, "label"))
            else:
                loc.select_option(value=self._r(step, scope, "value"))
        elif kind == "check":
            self._locator(step, scope).check(force=p.get("force", False))
        elif kind == "uncheck":
            self._locator(step, scope).uncheck(force=p.get("force", False))
        elif kind == "press":
            key = self._r(step, scope, "key")
            if "target" in p:
                self._locator(step, scope).press(key)
            else:
                self.page.keyboard.press(key)
        elif kind == "wait_for":
            self._locator(step, scope).wait_for(state=p["state"], timeout=timeout)
        elif kind == "assert_visible":
            self._locator(step, scope).wait_for(state="visible", timeout=timeout)
        elif kind == "wait_for_url":
            self.page.wait_for_url(_url_matcher(self._r(step, scope, "url")), timeout=timeout)
        elif kind == "wait_ms":
            self.page.wait_for_timeout(p["ms"])
        elif kind == "pause":
            message = self._r(step, scope, "message") if "message" in p else "Manual step"
            self.input_fn(f"{message}\nPress Enter to continue... ")
        elif kind == "screenshot":
            path = Path(self._r(step, scope, "path"))
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), full_page=True)
        elif kind == "for_each":
            names = scope.lists.get(p["items"])
            if names is None:
                raise ValueError(
                    f"'{p['items']}' is only available inside per_transaction steps"
                )
            for i, name in enumerate(names):
                child = _Scope(
                    self.flow.bind_loop_var(scope.ctx, p["as"], name, i, p["items"]), scope.lists
                )
                self.run_steps(step.steps, child, depth + 1)
        elif kind == "group":
            self.run_steps(step.steps, scope, depth + 1)
        elif kind == "click_until":
            self._click_until(step, scope)
        elif kind == "if_visible":
            loc = self._locator(step, scope)
            try:
                loc.first.wait_for(state="visible", timeout=timeout if timeout is not None else 1500)
                visible = True
            except Exception:
                visible = False
            self.run_steps(step.steps if visible else step.else_steps, scope, depth + 1)
        else:  # pragma: no cover - guarded by the parser
            raise ValueError(f"unsupported step type {kind}")


def _url_matcher(text: str):
    """``/regex/`` -> compiled pattern, anything else is a Playwright glob."""
    if len(text) > 2 and text.startswith("/") and text.endswith("/"):
        return re.compile(text[1:-1])
    return text


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

def wait_for_login(
    page: Page,
    flow: Flow,
    *,
    console: Console | None = None,
    variables: dict[str, str] | None = None,
    timeout_s: float = 900,
    poll_s: float = 2,
    interactive: bool = True,
) -> None:
    """Return once ``login.logged_in_selector`` is visible, polling while the user signs in.

    No Enter key needed. Flows without a login check return immediately. When not
    ``interactive`` (headless) an absent login is an immediate :class:`LoginRequired`.
    Ctrl+C raises :class:`UserAbort`.
    """
    console = console or Console()
    key = flow.login.logged_in_selector
    if key is None:
        return
    ctx = flow.base_context(flow.resolve_variables(variables, partial=True))
    sel = render(flow.resolve_target(key), ctx, f"selector '{key}'")

    def visible(timeout_ms: int) -> bool:
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    if visible(flow.settings.login_check_timeout_ms):
        return
    app = flow.name
    if not interactive:
        raise LoginRequired(
            f"Not logged in to '{app}' (selector '{key}' not visible at {page.url}). "
            "Run once with a visible browser to sign in (the login is stored in the browser "
            "profile), e.g. run `expenser` without --headless."
        )
    console.print(f"[yellow]Sign in to {app} in the browser window...[/] (waiting; Ctrl+C to cancel)")
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if visible(1000):
                console.print("[green]Signed in.[/]")
                return
            page.wait_for_timeout(int(poll_s * 1000))
    except KeyboardInterrupt:
        raise UserAbort() from None
    raise LoginRequired(
        f"Gave up waiting for the sign-in to '{app}' after {timeout_s:.0f}s "
        f"(selector '{key}' not visible at {page.url})."
    )


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

def ledger_scope(flow: Flow, variables: dict[str, str]) -> str:
    """Short hash of the flow name + all resolved variables (identifies the target group)."""
    resolved = flow.resolve_variables(variables, partial=True)
    payload = json.dumps([flow.name, sorted(resolved.items())], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def ledger_path(state_dir: Path, flow: Flow, variables: dict[str, str]) -> Path:
    return Path(state_dir) / "pushed" / f"{flow.name}-{ledger_scope(flow, variables)}.jsonl"


class Ledger:
    """Append-only JSONL of successfully pushed transactions, for de-duplication.

    Lines carry a ``scope`` (see :func:`ledger_scope`); with a ``scope`` given, lines of other
    scopes and old lines without one are ignored.
    """

    def __init__(self, path: Path, scope: str | None = None) -> None:
        self.path = path
        self.scope = scope
        self._counts: Counter[str] = Counter()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                    fp = entry["fingerprint"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                if scope is not None and entry.get("scope") != scope:
                    continue
                self._counts[fp] += 1

    def __contains__(self, fingerprint: str) -> bool:
        return self._counts[fingerprint] > 0

    def count(self, fingerprint: str) -> int:
        """How many times this fingerprint was pushed (in this ledger's scope)."""
        return self._counts[fingerprint]

    def already_pushed_flags(self, transactions: list[Transaction]) -> list[bool]:
        """Per transaction: is it covered by the ledger? The k-th (1-based) occurrence of a
        fingerprint in the batch counts as pushed only if the ledger holds >= k entries for it,
        so identical rows in one batch are separate purchases."""
        seen: Counter[str] = Counter()
        flags: list[bool] = []
        for tx in transactions:
            fp = tx.fingerprint()
            seen[fp] += 1
            flags.append(self._counts[fp] >= seen[fp])
        return flags

    def add(self, tx: Transaction) -> None:
        fp = tx.fingerprint()
        self._counts[fp] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "fingerprint": fp,
            "scope": self.scope,
            "name": tx.name,
            "amount": f"{tx.amount:.2f}",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------- #
# Browser lifecycle
# --------------------------------------------------------------------------- #

def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (ValueError, AttributeError):
        return False


def _ask_choice(input_fn: InputFn, prompt: str, valid: str) -> str:
    while True:
        answer = input_fn(prompt).strip().lower()[:1]
        if answer in valid:
            return answer


def _check_participants(
    transactions: list[Transaction], all_participants: list[str]
) -> None:
    known = {p.lower() for p in all_participants}
    problems: list[str] = []
    for tx in transactions:
        for who in [*tx.participants, *([tx.paid_by] if tx.paid_by else [])]:
            if who.lower() not in known:
                problems.append(f"'{tx.name}': unknown participant '{who}'")
    if problems:
        raise ValueError(
            "Participants not in the participants list: " + "; ".join(problems)
            + f". Known: {', '.join(all_participants)}"
        )


def push_on_page(
    page: Page,
    flow: Flow,
    transactions: list[Transaction],
    *,
    all_participants: list[str],
    variables: dict[str, str],
    dry_run: bool = False,
    confirm_each: bool = False,
    force: bool = False,
    console: Console | None = None,
    state_dir: Path | None = None,
    input_fn: InputFn | None = None,
    interactive: bool | None = None,
    navigate: bool = True,
) -> list[PushResult]:
    """Push ``transactions`` on an ALREADY OPEN, logged-in ``page``: setup, each transaction, teardown.

    Dedupes against the ledger of this flow + variables (unless ``force``). ``interactive``
    (default: stdin is a terminal) enables the retry/skip/abort prompt after a failure.
    """
    console = console or Console()
    input_fn = input_fn or input
    state_dir = Path(state_dir) if state_dir else default_state_dir()
    resolved = flow.resolve_variables(variables)
    _check_participants(transactions, all_participants)
    if dry_run and not flow.has_commit_step():
        raise ValueError(
            f"Flow '{flow.name}' has no step marked `commit: true`, so a dry run could not tell "
            "where to stop and would really create the expense. Mark the final Save step(s)."
        )
    if interactive is None:
        interactive = _stdin_is_tty()

    ledger = Ledger(ledger_path(state_dir, flow, resolved), ledger_scope(flow, resolved))
    results: list[PushResult | None] = [None] * len(transactions)
    todo: list[int] = []
    flags = [False] * len(transactions) if force else ledger.already_pushed_flags(transactions)
    for i, tx in enumerate(transactions):
        if flags[i]:
            results[i] = PushResult(tx, "skipped_duplicate")
            console.print(f"[yellow]skip (already pushed):[/] {tx.name} {tx.amount}")
        else:
            todo.append(i)

    if todo:
        ex = FlowExecutor(
            page, flow, variables=resolved, all_participants=all_participants,
            dry_run=dry_run, confirm_commit=confirm_each and not dry_run,
            input_fn=input_fn, console=console, state_dir=state_dir,
        )
        if navigate:
            ex.goto_start()
        ex.run_setup()
        _process(ex, transactions, todo, results, ledger, dry_run, interactive, input_fn, console)
        try:
            ex.run_teardown()
        except Exception as exc:
            console.print(f"[yellow]teardown failed:[/] {_short_error(exc)}")

    final = [r for r in results if r is not None]
    assert len(final) == len(transactions)
    return final


def push_transactions(
    flow_path: Path,
    transactions: list[Transaction],
    *,
    all_participants: list[str],
    variables: dict[str, str],
    dry_run: bool = False,
    confirm_each: bool = False,
    headless: bool = False,
    force: bool = False,
    console: Console | None = None,
    state_dir: Path | None = None,
    profile_dir: Path | None = None,
    input_fn: InputFn | None = None,
    browser: str | None = None,
    cdp_url: str | None = None,
    chrome_path: str | None = None,
) -> list[PushResult]:
    """Add each transaction to the app described by ``flow_path`` (open browser -> login -> push).

    ``input_fn`` (optional, last) replaces ``input`` for prompts; used by tests.
    """
    console = console or Console()
    flow = load_flow(flow_path)
    resolved = flow.resolve_variables(variables)
    _check_participants(transactions, all_participants)
    if dry_run and not flow.has_commit_step():
        raise ValueError(
            f"Flow '{flow.name}' has no step marked `commit: true`, so a dry run could not tell "
            "where to stop and would really create the expense. Mark the final Save step(s)."
        )
    interactive = not headless and _stdin_is_tty()
    ledger = Ledger(
        ledger_path(state_dir or default_state_dir(), flow, resolved), ledger_scope(flow, resolved)
    )
    if not force and all(ledger.already_pushed_flags(transactions)):
        for t in transactions:  # nothing to do: don't even open a browser
            console.print(f"[yellow]skip (already pushed):[/] {t.name} {t.amount}")
        final = [PushResult(t, "skipped_duplicate") for t in transactions]
        _print_summary(console, final)
        return final
    with open_session(
        flow, headless=headless, profile_dir=profile_dir, browser=browser,
        cdp_url=cdp_url, chrome_path=chrome_path,
    ) as (_, page):
        page.set_default_timeout(flow.settings.default_timeout_ms)
        page.goto(render(flow.start_url, flow.base_context(resolved), "start_url"))
        wait_for_login(page, flow, console=console, variables=resolved, interactive=not headless)
        final = push_on_page(
            page, flow, transactions, all_participants=all_participants, variables=resolved,
            dry_run=dry_run, confirm_each=confirm_each, force=force, console=console,
            state_dir=state_dir, input_fn=input_fn, interactive=interactive, navigate=False,
        )
    _print_summary(console, final)
    return final


def _process(
    ex: FlowExecutor,
    transactions: list[Transaction],
    todo: list[int],
    results: list[PushResult | None],
    ledger: Ledger,
    dry_run: bool,
    interactive: bool,
    input_fn: InputFn,
    console: Console,
) -> None:
    aborted = False
    for n, i in enumerate(todo, 1):
        tx = transactions[i]
        if aborted:
            results[i] = PushResult(tx, "skipped_by_user")
            continue
        console.rule(f"[bold]{n}/{len(todo)}[/] {tx.name}  {tx.amount}  "
                     f"[dim]{', '.join(tx.participants)}[/]", align="left")
        while True:
            try:
                status = ex.run_transaction(tx)
                if status == "pushed":
                    ledger.add(tx)
                results[i] = PushResult(tx, status, screenshot=ex.dry_run_screenshot)
                console.print(f"[green]{status}[/]")
            except UserAbort:
                aborted = True
                results[i] = PushResult(tx, "skipped_by_user")
                _safe_recover(ex, console)
            except UserSkip:
                results[i] = PushResult(tx, "skipped_by_user")
                _safe_recover(ex, console)
            except StepFailure as exc:
                shot = ex.screenshot(f"{i}")
                console.print(f"[red]FAILED[/] {exc}" + (f"\n  screenshot: {shot}" if shot else ""))
                _safe_recover(ex, console)
                if interactive:
                    choice = _ask_choice(
                        input_fn, "[r]etry transaction / [s]kip / [a]bort: ", "rsa"
                    )
                    if choice == "r":
                        continue
                    aborted = choice == "a"
                results[i] = PushResult(tx, "failed", error=str(exc), screenshot=shot)
            break


def _safe_recover(ex: FlowExecutor, console: Console) -> None:
    try:
        ex.recover()
    except Exception as exc:
        console.print(f"[yellow]could not reset the page:[/] {_short_error(exc)}")


def _print_summary(console: Console, results: list[PushResult]) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    console.print("[bold]Summary:[/] " + (", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing to do"))
