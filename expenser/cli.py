"""Command line interface."""
from __future__ import annotations

import datetime as dt
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, List, Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from expenser import auth as auth_mod
from expenser.currency import (
    CurrencyError, collect_rates, convert, normalize_code, parse_rate_args, summarize_currencies,
)
from expenser.datatable import DatatableError, apply_rates, read_datatable
from expenser.excel import read_review_excel, write_review_excel
from expenser.extract import ExtractionError, ExtractionResult, extract_transactions
from expenser.models import Transaction
from expenser.participants import load_participants
from expenser.paths import output_dir, state_dir
from expenser.pipeline import ask_dedupe, convert_extraction, find_duplicates, drop_duplicates, print_push_summary, resolve_unknown_currencies
from expenser.review import is_approved, render_transactions, review_loop

load_dotenv()  # ./.env first (wins) ...
load_dotenv(state_dir() / ".env")  # ... then ~/.expenser/.env

app = typer.Typer(
    name="expenser",
    help=(
        "Add expenses to Splitser (or any app via flows) from an Excel file or screenshots. "
        "Run `expenser` with no arguments for the interactive wizard."
    ),
    invoke_without_command=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


@app.callback()
def _main(ctx: typer.Context) -> None:
    """Interactive wizard when called without a command."""
    if ctx.invoked_subcommand is None:
        _start_wizard("splitser", None, None, None, False, {})

try:  # automation half may not exist yet
    from expenser.automation.cli import flow_app

    app.add_typer(flow_app, name="flow", help="Define, record and validate site flows")
except ImportError:  # pragma: no cover
    pass

auth_app = typer.Typer(help="Sign in with your Anthropic account (via the `ant` CLI)", no_args_is_help=True)
app.add_typer(auth_app, name="auth")

ParticipantsOpt = typer.Option(..., "--participants", "-p", help="Participants file (.txt or .csv)", exists=True, dir_okay=False)
OptionalParticipantsOpt = typer.Option(
    None, "--participants", "-p", exists=True, dir_okay=False,
    help="Participants file (.txt or .csv); optional in datatable mode / push (then taken from the Info sheet or the table)",
)
NoOpenOpt = typer.Option(False, "--no-open", help="Do not open Excel automatically")


def _fail(msg: str, code: int = 1) -> "typer.Exit":
    err_console.print(f"[red]Error:[/] {msg}")
    return typer.Exit(code)


def _load_people(path: Path) -> list[str]:
    try:
        return load_participants(path)
    except (ValueError, OSError) as e:
        raise _fail(str(e), 2) from e


def _parse_vars(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise _fail(f"Malformed --var {item!r}; expected key=value", 2)
        out[key.strip()] = value
    return out


def _resolve_flow_path(flow: "str | Path") -> Path:
    from expenser.automation.flow import FlowError, resolve_flow

    try:
        return resolve_flow(flow)
    except FlowError as e:
        raise _fail(str(e), 2) from e


def _check_group_variable(flow: Path, variables: dict[str, str]) -> None:
    """Flows with a group picker need the group on the command line (non-interactive mode)."""
    from expenser.automation.flow import FlowError, load_flow

    try:
        f = load_flow(flow)
    except FlowError:
        return  # reported properly when the flow is actually used
    var = f.picker_variable
    if var and var in f.missing_variables(variables):
        raise _fail(
            f"Which group? Run `expenser` for the interactive mode, or pass --var {var}=... "
            f"({f.variables[var].description or 'the group id'}).", 2
        )


TargetOpt = typer.Option(
    "INR", "--target-currency", envvar="EXPENSER_TARGET_CURRENCY",
    help="Currency the expenses are pushed in (default INR / $EXPENSER_TARGET_CURRENCY)",
)
RateOpt = typer.Option(None, "--rate", help="Conversion rate CODE=RATE, e.g. THB=2.58 = INR per 1 THB (repeatable)")
SourceOpt = typer.Option(None, "--source-currency", help="Force the currency of every screenshot row (e.g. THB)")


def _is_interactive() -> bool:
    return sys.stdin.isatty()


DedupeOpt = typer.Option(False, "--dedupe", help="Remove identical rows (keep one of each) without asking")
KeepDupOpt = typer.Option(False, "--keep-duplicates", help="Keep identical rows without asking")


def _dedupe_choice(dedupe: bool, keep_duplicates: bool) -> bool | None:
    """True = remove, False = keep, None = ask / default. Usage error if both flags."""
    if dedupe and keep_duplicates:
        raise typer.BadParameter("Use either --dedupe or --keep-duplicates, not both.")
    return True if dedupe else False if keep_duplicates else None


def _apply_dedupe(txs: list[Transaction], choice: bool | None) -> list[Transaction]:
    dups = find_duplicates(txs)
    if not dups:
        return txs
    if choice is True:
        out = drop_duplicates(txs)
        console.print(f"Removed {len(txs) - len(out)} duplicate row(s)")
        return out
    if choice is False:
        return txs
    if _is_interactive():
        return ask_dedupe(
            txs, confirm=lambda p, default=False: typer.confirm(p, default=default), console=console
        )
    n = sum(len(idx) - 1 for _, idx in dups)
    console.print(
        f"Note: {n} duplicate row(s) are kept as separate expenses (use --dedupe to drop them)."
    )
    return txs


def _default_out() -> Path:
    return output_dir() / f"expenses-{dt.datetime.now():%Y%m%d-%H%M%S}.xlsx"


def _print_credentials(status: "auth_mod.CredentialStatus") -> None:
    mark = "[green]OK[/]" if status.ok else "[red]NOT SIGNED IN[/]"
    console.print(f"{mark}  source: [bold]{status.source}[/]  {status.detail}", markup=True, highlight=False)
    for w in status.warnings:
        console.print(f"[yellow]Warning:[/] {w}")


def _preflight() -> None:
    status = auth_mod.describe_credentials()
    if status.ok:
        for w in status.warnings:
            console.print(f"[yellow]Warning:[/] {w}")
        return
    err_console.print(
        "[red]Not signed in to Anthropic.[/] Run `expenser auth login` (or set ANTHROPIC_API_KEY in .env)."
    )
    if status.source == "error":
        err_console.print(f"  {status.detail}", highlight=False)
    for w in status.warnings:
        err_console.print(f"[yellow]Warning:[/] {w}")
    raise typer.Exit(1)


@auth_app.command("login")
def auth_login(
    profile: Optional[str] = typer.Option(None, "--profile", help="Profile name to log in to"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print a URL instead of opening a browser (SSH)"),
) -> None:
    """Sign in with your Anthropic account (runs `ant auth login`)."""
    if auth_mod.find_ant() is None:
        err_console.print(auth_mod.ANT_INSTALL_HELP, highlight=False)
        raise typer.Exit(1)
    args = ["auth", "login"]
    if profile:
        args += ["--profile", profile]
    if no_browser:
        args.append("--no-browser")
    code = auth_mod.run_ant(args)
    if code != 0:
        raise _fail(f"`ant auth login` exited with code {code}", code if code > 0 else 1)
    status = auth_mod.describe_credentials()
    _print_credentials(status)
    if os.environ.get("ANTHROPIC_API_KEY") is not None:
        err_console.print(
            "[bold red]WARNING:[/] ANTHROPIC_API_KEY is set (environment or .env) and will OVERRIDE your login. "
            "Remove or comment out that line in .env (and `unset ANTHROPIC_API_KEY` in your shell) to use the login."
        )


@auth_app.command("status")
def auth_status() -> None:
    """Show which credentials expenser will use."""
    status = auth_mod.describe_credentials()
    _print_credentials(status)
    if not status.ok:
        console.print("Run `expenser auth login` (or set ANTHROPIC_API_KEY in .env).")
    if auth_mod.find_ant():
        console.print("For more detail run: ant auth status")
    if not status.ok:
        raise typer.Exit(1)


@auth_app.command("logout")
def auth_logout(
    profile: Optional[str] = typer.Option(None, "--profile", help="Profile to log out of"),
    all_profiles: bool = typer.Option(False, "--all", help="Log out of all profiles"),
) -> None:
    """Sign out (runs `ant auth logout`)."""
    if auth_mod.find_ant() is None:
        err_console.print(auth_mod.ANT_INSTALL_HELP, highlight=False)
        raise typer.Exit(1)
    args = ["auth", "logout"]
    if profile:
        args += ["--profile", profile]
    if all_profiles:
        args.append("--all")
    code = auth_mod.run_ant(args)
    if code != 0:
        raise typer.Exit(code if code > 0 else 1)


def _resolve_unknown_currencies(result: ExtractionResult, target: str, input_fn=input) -> list:
    try:
        return resolve_unknown_currencies(
            result, target, interactive=_is_interactive(), input_fn=input_fn, console=console
        )
    except CurrencyError as e:
        raise _fail(str(e), 2) from e


def _do_extract(
    images: list[Path], participants_file: Path, payer: str | None, hint: str | None,
    model: str | None, out: Path | None,
    target_currency: str = "INR", rate: list[str] | None = None, source_currency: str | None = None,
) -> tuple[Path, list[str], str]:
    people = _load_people(participants_file)
    _preflight()
    try:
        target = normalize_code(target_currency)
        given = parse_rate_args(rate)
        source = normalize_code(source_currency) if source_currency else None
    except CurrencyError as e:
        raise _fail(str(e), 2) from e
    if payer is None:
        payer = people[0]
        console.print(f"No --payer given; defaulting to first participant: [bold]{payer}[/]")
    else:
        from expenser.participants import resolve_name

        resolved = resolve_name(payer, people)
        if resolved is None:
            raise _fail(f"--payer {payer!r} is not a known participant ({', '.join(people)})", 2)
        payer = resolved
    with console.status(f"Asking Claude to read {len(images)} screenshot(s)..."):
        try:
            result: ExtractionResult = extract_transactions(images, people, payer=payer, hint=hint, model=model, source_currency=source)
        except ExtractionError as e:
            raise _fail(str(e)) from e
    u = result.usage
    console.print(
        f"Extracted [bold]{len(result.transactions)}[/] transaction(s) "
        f"({u.input_tokens} input / {u.output_tokens} output tokens)."
    )
    for s in result.skipped:
        console.print(f"  [yellow]skipped:[/] {s}")
    if not result.transactions:
        raise _fail("No transactions were found in the screenshots.")
    try:
        txs, rates = convert_extraction(
            result, target, given, interactive=_is_interactive(), console=console
        )
    except CurrencyError as e:
        raise _fail(str(e), 2) from e
    result.transactions = txs
    out = out or _default_out()
    write_review_excel(out, result, people, target_currency=target, rates=rates)
    console.print(f"Wrote [bold]{out}[/]")
    return out, people, target


def _do_push(
    excel: Path, people: list[str], flow: Path, variables: dict[str, str], *, dry_run: bool,
    confirm_each: bool, headless: bool, force: bool, yes: bool, no_open: bool,
    pre_approved: list[Transaction] | None = None,
    confirm_prompt: "str | Callable[[list[Transaction]], str] | None" = None,
    target_currency: str | None = None, dedupe: bool | None = None,
    browser: str | None = None, cdp_url: str | None = None, chrome_path: str | None = None,
) -> None:
    if not flow.exists():
        raise _fail(f"Flow file not found: {flow}", 2)
    if pre_approved is not None:
        txs = pre_approved
    elif is_approved(excel):
        txs, errs = read_review_excel(excel, people)
        if errs:
            raise _fail("Excel has problems:\n" + "\n".join(errs))
        console.print("Found matching approval for this file.")
    else:
        console.print("[yellow]No valid approval for this file (missing or edited); starting review.[/]")
        reviewed = review_loop(excel, people, open_file=not no_open, console=console)
        if reviewed is None:
            console.print("Not approved; nothing pushed.")
            raise typer.Exit(1)
        txs = reviewed
    if not txs:
        raise _fail("No transactions to push.")
    txs = _apply_dedupe(txs, dedupe)
    console.print(render_transactions(txs, people, title="About to push", target_currency=target_currency))
    if callable(confirm_prompt):
        confirm_prompt = confirm_prompt(txs)
    prompt = confirm_prompt or f"Push {len(txs)} transaction(s) using {flow}?"
    if not yes and not dry_run and not typer.confirm(prompt, default=False):
        console.print("Cancelled.")
        raise typer.Exit(1)

    from expenser.automation.runner import push_transactions  # lazy: heavy + parallel module

    try:
        results = push_transactions(
            flow, txs, all_participants=people, variables=variables, dry_run=dry_run,
            confirm_each=confirm_each, headless=headless, force=force, console=console,
            browser=browser, cdp_url=cdp_url, chrome_path=chrome_path,
        )
    except Exception as e:  # noqa: BLE001 - automation errors are user-facing
        raise _fail(f"Push failed: {e}") from e
    _print_summary(results)
    if any(r.status == "failed" for r in results):
        raise typer.Exit(1)


_SOURCE_LABEL = {"given": "participants file", "info": "Info sheet", "derived": "derived from table"}


def _read_table(path: Path, **kw: Any):
    try:
        return read_datatable(path, **kw)
    except (DatatableError, CurrencyError) as e:
        raise _fail(str(e), 2) from e


def _members_from_table(path: Path) -> list[str]:
    """Participants for `push` without -p: the Info sheet of the file, else names used in the table."""
    res = _read_table(path)
    if not res.members:
        raise _fail(f"Could not work out the participants from {path}; pass --participants.", 2)
    console.print(f"Participants ({_SOURCE_LABEL[res.members_source]}): {', '.join(res.members)}")
    return res.members


def _flow_name(flow: Path) -> str:
    try:
        import yaml

        data = yaml.safe_load(flow.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("name"):
            return str(data["name"])
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
    return flow.stem


def _run_datatable(
    datatable: Path, participants: Path | None, flow: Path, variables: dict[str, str], *,
    dry_run: bool, confirm_each: bool, headless: bool, yes: bool, browser: str | None,
    cdp_url: str | None, chrome_path: str | None, target_currency: str | None,
    rate: list[str] | None, source_currency: str | None, dedupe: bool | None = None,
) -> None:
    if not flow.exists():
        raise _fail(f"Flow file not found: {flow}", 2)
    people = _load_people(participants) if participants is not None else None
    try:
        given = parse_rate_args(rate)
    except CurrencyError as e:
        raise _fail(str(e), 2) from e
    res = _read_table(
        datatable, participants=people, target_currency=target_currency, rates=given,
        force_currency=source_currency,
    )
    target = res.target_currency
    if res.errors:
        err_console.print(f"[red]{len(res.errors)} problem(s) in {datatable}; nothing pushed:[/]")
        for e in res.errors:
            err_console.print(f"  [red]-[/] {e}", highlight=False)
        raise typer.Exit(1)
    if not res.transactions:
        raise _fail(f"No transactions found in {datatable}.")
    txs = res.transactions
    if res.currencies_needing_rates:
        needed = list(res.currencies_needing_rates)
        console.print(
            "Currencies needing a rate: "
            + ", ".join(f"{c} ({n} row{'s' if n != 1 else ''}, {tot:,.2f})" for c, (n, tot) in res.currencies_needing_rates.items())
        )
        try:
            rates = collect_rates(
                res.currencies_needing_rates, target, given, console=console,
                interactive=_is_interactive() or all(c in given for c in needed),
            )
            txs = apply_rates(res, rates)
        except CurrencyError as e:
            raise _fail(str(e), 2) from e
        for c, r in rates.items():
            console.print(f"Rate applied: 1 {c} = {r.normalize():f} {target}")
    console.print(f"Participants ({_SOURCE_LABEL[res.members_source]}): {', '.join(res.members)}")
    def prompt(kept: list[Transaction]) -> str:
        total = sum((t.amount for t in kept), Decimal("0.00"))
        return f"Push {len(kept)} transactions totalling {total:.2f} {target} to {_flow_name(flow)}?"

    _do_push(datatable, res.members, flow, variables, dry_run=dry_run, confirm_each=confirm_each,
             headless=headless, force=False, yes=yes, no_open=True, pre_approved=txs,
             confirm_prompt=prompt, target_currency=target, dedupe=dedupe,
             browser=browser, cdp_url=cdp_url, chrome_path=chrome_path)


def _print_summary(results: list[Any]) -> None:
    print_push_summary(console, results)


@app.command()
def extract(
    images: List[Path] = typer.Argument(..., exists=True, dir_okay=False, help="Screenshot image(s)"),
    participants: Path = ParticipantsOpt,
    payer: Optional[str] = typer.Option(None, "--payer", help="Who paid (default: first participant)"),
    hint: Optional[str] = typer.Option(None, "--hint", help="Free-text hint for Claude"),
    model: Optional[str] = typer.Option(None, "--model", help="Claude model (default claude-opus-5-5 / $EXPENSER_MODEL)"),
    out: Optional[Path] = typer.Option(None, "--out", help="Output .xlsx path"),
    no_review: bool = typer.Option(False, "--no-review", help="Skip the approval loop"),
    no_open: bool = NoOpenOpt,
    target_currency: str = TargetOpt,
    rate: Optional[List[str]] = RateOpt,
    source_currency: Optional[str] = SourceOpt,
) -> None:
    """Extract transactions from screenshots into a review Excel file."""
    path, people, target = _do_extract(
        images, participants, payer, hint, model, out, target_currency, rate, source_currency
    )
    if no_review:
        console.print(f"Review it with: expenser review {path} -p {participants}")
        return
    if review_loop(path, people, open_file=not no_open, console=console, target_currency=target) is None:
        console.print("Not approved.")
        raise typer.Exit(1)


@app.command()
def review(
    excel: Path = typer.Argument(..., exists=True, dir_okay=False, help="Review .xlsx file"),
    participants: Path = ParticipantsOpt,
    no_open: bool = NoOpenOpt,
) -> None:
    """Review and approve an existing Excel file."""
    people = _load_people(participants)
    if review_loop(excel, people, open_file=not no_open, console=console) is None:
        console.print("Not approved.")
        raise typer.Exit(1)


@app.command()
def push(
    excel: Path = typer.Argument(..., exists=True, dir_okay=False, help="Approved review .xlsx"),
    participants: Optional[Path] = OptionalParticipantsOpt,
    flow: str = typer.Option("splitser", "--flow", help="Flow name (bundled or in ~/.expenser/flows) or YAML path"),
    var: Optional[List[str]] = typer.Option(None, "--var", help="Flow variable key=value (repeatable)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fill forms but do not submit"),
    confirm_each: bool = typer.Option(False, "--confirm-each", help="Ask before each submission"),
    headless: bool = typer.Option(False, "--headless", help="Run the browser headless"),
    force: bool = typer.Option(False, "--force", help="Push even if already pushed before"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the final confirmation"),
    no_open: bool = NoOpenOpt,
    browser: Optional[str] = typer.Option(None, "--browser", help="bundled or chrome (overrides the flow setting)"),
    cdp_url: Optional[str] = typer.Option(None, "--cdp-url", help="Attach to a running browser, e.g. http://127.0.0.1:9222"),
    chrome_path: Optional[str] = typer.Option(None, "--chrome-path", help="Chrome executable path"),
    dedupe: bool = DedupeOpt,
    keep_duplicates: bool = KeepDupOpt,
) -> None:
    """Push an approved Excel file into the web app."""
    dedupe_choice = _dedupe_choice(dedupe, keep_duplicates)
    variables = _parse_vars(var)
    flow = _resolve_flow_path(flow)
    _check_group_variable(flow, variables)
    if participants is not None:
        people = _load_people(participants)
    else:
        people = _members_from_table(excel)
    _do_push(excel, people, flow, variables, dry_run=dry_run, confirm_each=confirm_each,
             headless=headless, force=force, yes=yes, no_open=no_open, dedupe=dedupe_choice,
             browser=browser, cdp_url=cdp_url, chrome_path=chrome_path)


@app.command()
def run(
    images: Optional[List[Path]] = typer.Argument(None, exists=True, dir_okay=False, help="Screenshot image(s) (omit when using --datatable)"),
    participants: Optional[Path] = OptionalParticipantsOpt,
    flow: str = typer.Option("splitser", "--flow", help="Flow name (bundled or in ~/.expenser/flows) or YAML path"),
    datatable: Optional[Path] = typer.Option(
        None, "--datatable", exists=True, dir_okay=False,
        help="Push straight from your own .xlsx/.csv table (no screenshots, no Claude, no review step)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the final confirmation (datatable mode)"),
    payer: Optional[str] = typer.Option(None, "--payer"),
    hint: Optional[str] = typer.Option(None, "--hint"),
    model: Optional[str] = typer.Option(None, "--model"),
    var: Optional[List[str]] = typer.Option(None, "--var", help="Flow variable key=value (repeatable)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm_each: bool = typer.Option(False, "--confirm-each"),
    headless: bool = typer.Option(False, "--headless"),
    out: Optional[Path] = typer.Option(None, "--out"),
    no_open: bool = NoOpenOpt,
    browser: Optional[str] = typer.Option(None, "--browser", help="bundled or chrome (overrides the flow setting)"),
    cdp_url: Optional[str] = typer.Option(None, "--cdp-url", help="Attach to a running browser, e.g. http://127.0.0.1:9222"),
    chrome_path: Optional[str] = typer.Option(None, "--chrome-path", help="Chrome executable path"),
    target_currency: Optional[str] = typer.Option(
        None, "--target-currency",
        help="Currency the expenses are pushed in (default INR / $EXPENSER_TARGET_CURRENCY; datatable mode also reads 'Amount (XXX)')",
    ),
    rate: Optional[List[str]] = RateOpt,
    source_currency: Optional[str] = SourceOpt,
    dedupe: bool = DedupeOpt,
    keep_duplicates: bool = KeepDupOpt,
) -> None:
    """Full pipeline: extract -> convert -> Excel -> review -> push. With --datatable: push your own table directly."""
    if datatable is not None and images:
        raise typer.BadParameter("Give either IMAGES or --datatable, not both.")
    if datatable is None and not images:
        raise typer.BadParameter("Give screenshot IMAGES, or --datatable FILE.")
    dedupe_choice = _dedupe_choice(dedupe, keep_duplicates)
    variables = _parse_vars(var)
    flow = _resolve_flow_path(flow)
    _check_group_variable(flow, variables)
    if datatable is not None:
        _run_datatable(
            datatable, participants, flow, variables, dry_run=dry_run, confirm_each=confirm_each,
            headless=headless, yes=yes, browser=browser, cdp_url=cdp_url, chrome_path=chrome_path,
            target_currency=target_currency, rate=rate, source_currency=source_currency, dedupe=dedupe_choice,
        )
        return
    if participants is None:
        raise typer.BadParameter("--participants/-p is required when extracting from screenshots.")
    target_currency = target_currency or os.environ.get("EXPENSER_TARGET_CURRENCY") or "INR"
    path, people, target = _do_extract(
        images, participants, payer, hint, model, out, target_currency, rate, source_currency
    )
    txs = review_loop(path, people, open_file=not no_open, console=console, target_currency=target)
    if txs is None:
        console.print("Not approved; nothing pushed.")
        raise typer.Exit(1)
    _do_push(path, people, flow, variables, dry_run=dry_run, confirm_each=confirm_each,
             headless=headless, force=False, yes=True, no_open=no_open, pre_approved=txs, dedupe=dedupe_choice,
             browser=browser, cdp_url=cdp_url, chrome_path=chrome_path)


def _start_wizard(
    flow: str, browser: str | None, chrome_path: str | None, cdp_url: str | None,
    no_dialog: bool, variables: dict[str, str], headless: bool = False,
) -> None:
    from expenser import wizard
    from expenser.automation.flow import FlowError, load_flow

    try:
        loaded = load_flow(_resolve_flow_path(flow))
        wizard.run(
            flow=loaded, variables=variables, headless=headless, browser=browser,
            chrome_path=chrome_path, cdp_url=cdp_url, no_dialog=no_dialog, console=console,
        )
    except FlowError as e:
        raise _fail(str(e), 2) from e
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")
        raise typer.Exit(130)
    except Exception as e:  # noqa: BLE001 - browser/login problems are user-facing
        raise _fail(str(e)) from e


@app.command()
def start(
    flow: str = typer.Option("splitser", "--flow", help="Flow name (bundled or in ~/.expenser/flows) or YAML path"),
    browser: Optional[str] = typer.Option(None, "--browser", help="bundled or chrome (overrides the flow setting)"),
    chrome_path: Optional[str] = typer.Option(None, "--chrome-path", help="Chrome executable path"),
    cdp_url: Optional[str] = typer.Option(None, "--cdp-url", help="Attach to a running browser, e.g. http://127.0.0.1:9222"),
    no_dialog: bool = typer.Option(False, "--no-dialog", help="Never show a file chooser window; paste paths instead"),
    var: Optional[List[str]] = typer.Option(None, "--var", help="Flow variable key=value (repeatable)"),
) -> None:
    """Interactive wizard: sign in, pick the group, load expenses, push them."""
    _start_wizard(flow, browser, chrome_path, cdp_url, no_dialog, _parse_vars(var))


@app.command()
def doctor() -> None:
    """Check this computer is ready (Chrome, data folders, optional Claude sign-in)."""
    from expenser.doctor import run_doctor

    raise typer.Exit(run_doctor(console))


@app.command("datatable-template")
def datatable_template(
    path: Path = typer.Argument(..., dir_okay=False, help="Where to write the .xlsx template"),
    participants: Optional[Path] = OptionalParticipantsOpt,
    target_currency: str = typer.Option("INR", "--target-currency", envvar="EXPENSER_TARGET_CURRENCY"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file"),
) -> None:
    """Write a blank Excel template for `expenser run --datatable`."""
    from expenser.datatable_template import write_datatable_template

    if path.exists() and not force:
        raise _fail(f"{path} already exists; use --force to overwrite.", 2)
    people = _load_people(participants) if participants is not None else None
    try:
        target = normalize_code(target_currency)
    except CurrencyError as e:
        raise _fail(str(e), 2) from e
    write_datatable_template(path, people, target)
    console.print(f"Wrote [bold]{path}[/]. Fill it in, then: expenser run --datatable {path} --flow splitser --var list_id=... --dry-run")


if __name__ == "__main__":  # pragma: no cover
    app()
