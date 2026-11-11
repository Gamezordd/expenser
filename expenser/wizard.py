"""Interactive, staged wizard: sign in -> pick group -> members -> expenses -> push.

All prompting goes through :class:`UI`, so the whole wizard can be driven by scripted answers
in tests. The browser session stays open for the whole wizard.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.markup import escape

from expenser import auth
from expenser.automation.flow import Flow, FlowError, load_flow, render, resolve_flow
from expenser.automation.runner import PushResult, UserAbort, push_on_page, wait_for_login
from expenser.automation.browser import open_session
from expenser.automation.stages import StageError, discover_groups, discover_members, extract_group_value
from expenser.currency import CurrencyError, collect_rates, normalize_code
from expenser.datatable import DatatableError, apply_rates, read_datatable
from expenser.datatable_template import write_datatable_template
from expenser.excel import write_review_excel
from expenser.extract import ExtractionError, extract_transactions
from expenser.models import Transaction
from expenser.opener import open_in_default_app
from expenser.paths import output_dir as default_output_dir
from expenser.paths import state_dir as default_state_dir
from expenser.pipeline import ask_dedupe, convert_extraction, print_push_summary, safe_name
from expenser.review import render_transactions, review_loop

__all__ = ["UI", "WizardQuit", "WizardError", "WizardResult", "run"]

InputFn = Callable[[str], str]


class WizardQuit(Exception):
    """The user asked to leave the wizard."""


class WizardError(RuntimeError):
    """The wizard cannot continue (e.g. a required flow variable is missing)."""


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

class UI:
    """Everything the wizard says or asks. Replace ``input_fn``/``choose_files_fn`` in tests."""

    def __init__(
        self,
        console: Console | None = None,
        input_fn: InputFn = input,
        choose_files_fn: Callable[[str, bool], list[Path]] | None = None,
        open_fn: Callable[[Path], object] | None = None,
        use_dialog: bool = True,
    ) -> None:
        self.console = console or Console()
        self.input_fn = input_fn
        self._choose_files_fn = choose_files_fn
        self._open_fn = open_fn
        self.use_dialog = use_dialog

    def say(self, text: str = "", **kw) -> None:
        self.console.print(text, **kw)

    def ask_raw(self, prompt: str) -> str:
        """Free text. Ctrl+C / end of input leaves the wizard."""
        try:
            return self.input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            raise WizardQuit() from None

    def confirm(self, prompt: str, default: bool = False) -> bool:
        while True:
            raw = self.ask_raw(f"{prompt} [{'Y/n' if default else 'y/N'}] ").strip().lower()
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            self.say("Please answer y or n.")

    def menu(self, title: str, options: list[str], *, quit_ok: bool = True) -> int:
        """Numbered menu; returns the 0-based index. ``q`` quits the wizard."""
        self.say(f"\n[bold]{title}[/]")
        for i, opt in enumerate(options, 1):
            self.say(f"  {i}) {escape(opt)}", highlight=False)
        hint = f"Choose 1-{len(options)}" + (" (q to quit)" if quit_ok else "") + ": "
        while True:
            raw = self.ask_raw(hint).strip().lower()
            if quit_ok and raw in ("q", "quit", "exit"):
                raise WizardQuit()
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            self.say(f"[red]Please type a number from 1 to {len(options)}.[/]")

    def choose_files(self, kind: str, multiple: bool = False) -> list[Path]:
        if self._choose_files_fn is not None:
            return self._choose_files_fn(kind, multiple)
        from expenser.filechooser import choose_files

        return choose_files(
            kind, multiple, input_fn=self.input_fn, console=self.console, use_dialog=self.use_dialog
        )

    def open_path(self, path: Path) -> None:
        (self._open_fn or (lambda p: open_in_default_app(p, self.console)))(path)


# --------------------------------------------------------------------------- #
# Wizard
# --------------------------------------------------------------------------- #

@dataclass
class WizardResult:
    results: list[PushResult] = field(default_factory=list)  # every push (dry runs included)
    groups: list[str] = field(default_factory=list)  # group labels worked on


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("expenser")
    except Exception:  # noqa: BLE001
        return "dev"


class _Wizard:
    def __init__(
        self, ui: UI, flow: Flow, variables: dict[str, str], *, headless: bool,
        state_dir: Path, output_dir: Path,
    ) -> None:
        self.ui = ui
        self.flow = flow
        self.variables = variables
        self.headless = headless
        self.state_dir = state_dir
        self.output_dir = output_dir
        self.result = WizardResult()
        self.group_label = flow.name
        self.group_value: str | None = None

    # ---- helpers ---------------------------------------------------------- #
    def _vars(self) -> dict[str, str]:
        out = dict(self.variables)
        if self.flow.picker_variable and self.group_value is not None:
            out[self.flow.picker_variable] = self.group_value
        return out

    def _ctx(self) -> dict[str, str]:
        return self.flow.base_context(self.flow.resolve_variables(self._vars(), partial=True))

    def _picker_url(self) -> str:
        gp = self.flow.group_picker
        return render((gp.url if gp and gp.url else self.flow.start_url), self._ctx(), "group_picker.url")

    def _target_currency(self) -> str | None:
        env = os.environ.get("EXPENSER_TARGET_CURRENCY")
        try:
            return normalize_code(env) if env else None
        except CurrencyError:
            return None

    # ---- stages ----------------------------------------------------------- #
    def main(self, page) -> None:
        ui = self.ui
        self.login(page)
        while True:
            if not self.choose_group(page):
                return
            members, me = self.get_members(page)
            while True:
                self.work_on_group(page, members, me)
                if self.flow.group_picker:
                    choice = ui.menu(
                        "What next?",
                        ["Add more to this group", "Choose another group", "Quit"], quit_ok=False,
                    )
                    if choice == 0:
                        continue
                    if choice == 1:
                        break
                    raise WizardQuit()
                choice = ui.menu("What next?", ["Add more", "Quit"], quit_ok=False)
                if choice == 0:
                    continue
                raise WizardQuit()

    def login(self, page) -> None:
        self.ui.say(f"\nOpening [bold]{escape(self.flow.name)}[/] in the browser...")
        url = self._picker_url()
        page.goto(url)
        wait_for_login(
            page, self.flow, console=self.ui.console, variables=self._vars(),
            interactive=not self.headless,
        )
        if page.url.rstrip("/") != url.rstrip("/"):
            page.goto(url)  # the site redirected after sign-in; go back to the group list

    def choose_group(self, page) -> bool:
        """Let the user pick the group; sets ``group_label`` / ``group_value``. Always True (q quits)."""
        ui, gp = self.ui, self.flow.group_picker
        if gp is None:
            self.group_label, self.group_value = self.flow.name, None
            self.result.groups.append(self.group_label)
            return True
        url = self._picker_url()
        first = True
        while True:
            if not first or page.url.rstrip("/") != url.rstrip("/"):
                page.goto(url)
            first = False
            try:
                groups = discover_groups(page, self.flow)
            except Exception as exc:  # noqa: BLE001 - page oddities must not crash the wizard
                ui.say(f"[yellow]Could not read the group list: {escape(str(exc))}[/]")
                groups = []
            if groups:
                options = [f"{label}  (id {value[:8]})" for label, value in groups]
                options.append("Not listed? Enter a group id or URL myself")
                idx = ui.menu("Which group do you want to add expenses to?", options)
                if idx < len(groups):
                    self.group_label, self.group_value = groups[idx]
                    break
                picked = self._ask_group_manually()
                if picked:
                    self.group_label, self.group_value = picked, picked
                    break
                continue
            ui.say(
                "\n[yellow]I could not find any groups on this page.[/] "
                "Make sure the browser shows your list of groups (you can navigate there yourself)."
            )
            idx = ui.menu(
                "What now?",
                ["Try again", "Enter the group id or URL myself"],
            )
            if idx == 1:
                picked = self._ask_group_manually()
                if picked:
                    self.group_label, self.group_value = picked, picked
                    break
        self.result.groups.append(self.group_label)
        ui.say(f"Group: [bold]{escape(self.group_label)}[/]")
        return True

    def _ask_group_manually(self) -> str | None:
        while True:
            text = self.ui.ask_raw("Paste the group's URL or id (Enter to go back): ").strip()
            if not text:
                return None
            value = extract_group_value(self.flow, text)
            if value:
                return value
            self.ui.say("[red]That does not look like a group URL or id.[/]")

    def get_members(self, page) -> tuple[list[str], str | None]:
        ui = self.ui
        if self.flow.members is not None:
            while True:
                try:
                    with ui.console.status("Reading the group's members..."):
                        members, me = discover_members(page, self.flow, self._vars())
                    break
                except Exception as exc:  # noqa: BLE001
                    ui.say(f"[yellow]Could not read the members: {escape(str(exc))}[/]")
                    if ui.menu("What now?", ["Try again", "Type the names myself"]) == 1:
                        members, me = self._ask_members_manually(), None
                        break
        else:
            members, me = self._ask_members_manually(), None
        you = f"  (you: {escape(me)})" if me else ""
        ui.say(f"\nMembers of [bold]{escape(self.group_label)}[/]: {escape(', '.join(members))}{you}")
        return members, me

    def _ask_members_manually(self) -> list[str]:
        while True:
            text = self.ui.ask_raw("Names of the people in this group, separated by commas: ")
            names: list[str] = []
            for n in text.split(","):
                n = n.strip()
                if n and n not in names:
                    names.append(n)
            if names:
                return names
            self.ui.say("[red]Please type at least one name.[/]")

    # ---- expenses --------------------------------------------------------- #
    def work_on_group(self, page, members: list[str], me: str | None) -> None:
        got = self.get_transactions(members, me)
        if got is None:
            return
        txs, target = got
        txs = ask_dedupe(txs, confirm=self.ui.confirm, console=self.ui.console)
        self.push_stage(page, txs, members, target)

    def get_transactions(self, members: list[str], me: str | None) -> tuple[list[Transaction], str] | None:
        ui = self.ui
        while True:
            idx = ui.menu(
                "Where do the expenses come from?",
                [
                    "Excel / CSV file (no Claude account needed)",
                    "Screenshots (uses Claude - needs an Anthropic account)",
                    "Create an Excel template for this group, fill it in, then continue",
                    "Back",
                ],
            )
            if idx == 3:
                return None
            got = (
                self.from_excel(members) if idx == 0
                else self.from_screenshots(members, me) if idx == 1
                else self.from_template(members, me)
            )
            if got is not None:
                return got

    def from_excel(self, members: list[str], path: Path | None = None) -> tuple[list[Transaction], str] | None:
        ui = self.ui
        while True:
            if path is None:
                files = ui.choose_files("table", False)
                if not files:
                    return None
                path = files[0]
            errors: list[str] = []
            res = None
            try:
                res = read_datatable(path, participants=members, target_currency=self._target_currency())
                errors = list(res.errors)
                if not errors and not res.transactions:
                    errors = [f"No transactions found in {path.name}."]
            except (DatatableError, CurrencyError) as exc:
                errors = [str(exc)]
            if errors:
                ui.say(f"[red]{len(errors)} problem(s) in {escape(str(path))}:[/]")
                for e in errors:
                    ui.say(f"  [red]-[/] {escape(e)}", highlight=False)
                idx = ui.menu(
                    "What now?",
                    ["I fixed the file - read it again", "Choose another file", "Back"],
                )
                if idx == 0:
                    continue
                if idx == 1:
                    path = None
                    continue
                return None
            assert res is not None
            target = res.target_currency
            txs = res.transactions
            if res.currencies_needing_rates:
                ui.say(
                    "Currencies needing a rate: "
                    + ", ".join(f"{c} ({n} row{'s' if n != 1 else ''}, {t:,.2f})"
                                for c, (n, t) in res.currencies_needing_rates.items())
                )
                try:
                    rates = collect_rates(
                        res.currencies_needing_rates, target, {}, input_fn=ui.input_fn,
                        console=ui.console, interactive=True,
                    )
                    txs = apply_rates(res, rates)
                except CurrencyError as exc:
                    ui.say(f"[red]{escape(str(exc))}[/]")
                    return None
            return txs, target

    def from_screenshots(self, members: list[str], me: str | None) -> tuple[list[Transaction], str] | None:
        ui = self.ui
        while True:
            status = auth.describe_credentials()
            if status.ok:
                for w in status.warnings:
                    ui.say(f"[yellow]Warning:[/] {escape(w)}")
                break
            ui.say(
                "\nReading screenshots uses the Anthropic API (Claude). That needs an Anthropic "
                "Console account that is billed per use - a claude.ai chat subscription does not work."
            )
            idx = ui.menu(
                "Not signed in to Anthropic. What now?",
                ["Sign in now", "Use an Excel file instead", "Back"],
            )
            if idx == 0:
                if auth.find_ant():
                    code = auth.run_ant(["auth", "login"])
                    if code != 0:
                        ui.say(f"[red]Sign-in did not finish (exit code {code}).[/]")
                else:
                    ui.say(auth.ANT_INSTALL_HELP, highlight=False)
                continue
            if idx == 1:
                return self.from_excel(members)
            return None
        images = ui.choose_files("images", True)
        if not images:
            return None
        hint = ui.ask_raw("Anything Claude should know? (Enter to skip) ").strip() or None
        payer = me or members[0]
        target = self._target_currency() or "INR"
        try:
            with ui.console.status(f"Asking Claude to read {len(images)} screenshot(s)..."):
                result = extract_transactions(images, members, payer=payer, hint=hint)
        except ExtractionError as exc:
            ui.say(f"[red]{escape(str(exc))}[/]")
            return None
        u = result.usage
        ui.say(f"Extracted {len(result.transactions)} transaction(s) ({u.input_tokens} input / {u.output_tokens} output tokens).")
        for s in result.skipped:
            ui.say(f"  [yellow]skipped:[/] {escape(s)}")
        if not result.transactions:
            ui.say("[yellow]No transactions were found in the screenshots.[/]")
            return None
        try:
            txs, rates = convert_extraction(
                result, target, {}, interactive=True, input_fn=ui.input_fn, console=ui.console
            )
        except CurrencyError as exc:
            ui.say(f"[red]{escape(str(exc))}[/]")
            return None
        path = self.output_dir / f"{safe_name(self.group_label)}-{dt.datetime.now():%Y%m%d-%H%M%S}.xlsx"
        write_review_excel(path, result, members, target_currency=target, rates=rates)
        ui.say(f"Saved the extracted expenses to [bold]{escape(str(path))}[/]")
        ui.open_path(path)
        approved = review_loop(
            path, members, open_file=False, console=ui.console, input_fn=ui.input_fn,
            target_currency=target,
        )
        if approved is None:
            return None
        return approved, target

    def from_template(self, members: list[str], me: str | None) -> tuple[list[Transaction], str] | None:
        ui = self.ui
        base = safe_name(self.group_label)
        path = self.output_dir / f"{base}-template.xlsx"
        while path.exists():
            idx = ui.menu(
                f"{path.name} already exists in {path.parent}.",
                ["Overwrite it", "Save under a new name", "Back"],
            )
            if idx == 0:
                break
            if idx == 2:
                return None
            name = safe_name(ui.ask_raw("New file name (without .xlsx): ").strip(), fallback=base)
            path = self.output_dir / f"{name}.xlsx"
        ordered = ([me] + [m for m in members if m != me]) if me in members else list(members)
        target = self._target_currency() or "INR"
        write_datatable_template(path, ordered, target)
        ui.say(f"Created [bold]{escape(str(path))}[/] (people: {escape(', '.join(ordered))}).")
        ui.open_path(path)
        answer = ui.ask_raw("Fill it in, SAVE it, then press Enter (q to cancel): ").strip().lower()
        if answer in ("q", "quit"):
            return None
        return self.from_excel(members, path)

    # ---- push ------------------------------------------------------------- #
    def push_stage(self, page, txs: list[Transaction], members: list[str], target: str) -> None:
        ui = self.ui
        total = sum((t.amount for t in txs), Decimal("0.00"))
        ui.console.print(render_transactions(
            txs, members, title=f"{len(txs)} expense(s) for {self.group_label}", target_currency=target,
        ))
        ui.say(f"Total: [bold]{total:.2f} {target}[/]")
        idx = ui.menu(
            "How should I enter them?",
            [
                "Ask before each save (recommended)",
                "Save all",
                "Dry run (fill forms, save nothing)",
                "Cancel",
            ],
        )
        if idx == 3:
            return
        dry = idx == 2
        if self._push(page, txs, members, dry_run=dry, confirm_each=idx == 0) and dry:
            if ui.confirm("Push these for real now?"):
                idx = ui.menu(
                    "How should I enter them?",
                    ["Ask before each save (recommended)", "Save all", "Cancel"],
                )
                if idx < 2:
                    self._push(page, txs, members, dry_run=False, confirm_each=idx == 0)

    def _push(self, page, txs, members, *, dry_run: bool, confirm_each: bool) -> bool:
        ui = self.ui
        try:
            results = push_on_page(
                page, self.flow, txs, all_participants=members, variables=self._vars(),
                dry_run=dry_run, confirm_each=confirm_each, force=False, console=ui.console,
                state_dir=self.state_dir, input_fn=ui.input_fn, interactive=True,
            )
        except (UserAbort, WizardQuit, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - show it, stay in the wizard
            ui.say(f"[red]Pushing failed:[/] {escape(str(exc))}")
            return False
        self.result.results.extend(results)
        print_push_summary(ui.console, results)
        return True


def run(
    *,
    ui: UI | None = None,
    flow: Flow | str | Path = "splitser",
    variables: dict[str, str] | None = None,
    headless: bool = False,
    browser: str | None = None,
    cdp_url: str | None = None,
    chrome_path: str | None = None,
    no_dialog: bool = False,
    profile_dir: Path | None = None,
    state_dir: Path | None = None,
    output_dir: Path | None = None,
    console: Console | None = None,
    before_close: Callable[[object], None] | None = None,
) -> WizardResult:
    """Run the wizard. ``before_close(page)`` is called with the still-open page at the very end."""
    ui = ui or UI(console=console, use_dialog=not no_dialog)
    if not isinstance(flow, Flow):
        flow = load_flow(resolve_flow(flow))
    variables = dict(variables or {})
    missing = flow.missing_variables(variables, picker_provides=True)
    if missing:
        raise WizardError(
            "Missing flow variable(s): " + ", ".join(f"--var {m}=..." for m in missing)
        )
    wiz = _Wizard(
        ui, flow, variables, headless=headless,
        state_dir=Path(state_dir) if state_dir else default_state_dir(),
        output_dir=Path(output_dir) if output_dir else default_output_dir(),
    )
    ui.say(f"[bold]Expenser {_version()}[/]  -  flow: [bold]{escape(flow.name)}[/]")
    if flow.description:
        ui.say(escape(flow.description), style="dim")
    with open_session(
        flow, headless=headless, profile_dir=profile_dir, browser=browser,
        cdp_url=cdp_url, chrome_path=chrome_path,
    ) as (_, page):
        page.set_default_timeout(flow.settings.default_timeout_ms)
        try:
            wiz.main(page)
        except (WizardQuit, UserAbort, KeyboardInterrupt):
            ui.say("\nGoodbye!")
        finally:
            if before_close is not None:
                before_close(page)
    return wiz.result
