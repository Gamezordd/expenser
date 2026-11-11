"""``expenser flow ...`` sub-commands (mounted by the top-level CLI as ``flow_app``)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from expenser.automation.flow import FlowError, load_flow, resolve_flow

flow_app = typer.Typer(help="Define, record, validate and test web-app flows.", no_args_is_help=True)
console = Console()


def parse_vars(items: list[str] | None) -> dict[str, str]:
    """Parse ``key=value`` options."""
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise typer.BadParameter(f"'{item}' is not in key=value form", param_hint="--var")
        out[key.strip()] = value
    return out


def read_participants(path: Path) -> list[str]:
    """Names from a .txt (one per line) or .csv (first column); blanks and #comments ignored."""
    names: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split(",")[0].strip().strip('"')
        if name and name not in names:
            names.append(name)
    return names


def _resolve(flow: "str | Path") -> Path:
    try:
        return resolve_flow(flow)
    except FlowError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2)


def _load(flow: Path):
    try:
        return load_flow(flow)
    except FlowError as exc:
        console.print(f"[red]Invalid flow:[/] {exc}")
        raise typer.Exit(2)


def _participants(path: Optional[Path]) -> list[str] | None:
    if path is None:
        return None
    if not path.exists():
        raise typer.BadParameter(f"{path} not found", param_hint="--participants")
    return read_participants(path)


VarOpt = typer.Option(None, "--var", help="Flow variable, key=value (repeatable)")
ParticipantsOpt = typer.Option(None, "--participants", help="Participants file (txt/csv)")
BrowserOpt = typer.Option(None, "--browser", help="bundled (Playwright Chromium) or chrome (real Chrome via CDP)")
CdpOpt = typer.Option(None, "--cdp-url", help="Attach to an already running browser, e.g. http://127.0.0.1:9222")
ChromePathOpt = typer.Option(None, "--chrome-path", help="Path to the Chrome executable")


def _browser_kw(browser, cdp_url, chrome_path) -> dict:
    if browser is not None and browser not in ("bundled", "chrome"):
        raise typer.BadParameter("must be 'bundled' or 'chrome'", param_hint="--browser")
    return {"browser": browser, "cdp_url": cdp_url, "chrome_path": chrome_path}


@flow_app.command()
def new(
    name: str = typer.Argument(..., help="Flow name"),
    url: str = typer.Option(..., "--url", help="Start URL of the app"),
    out: Optional[Path] = typer.Option(None, "--out", help="Output path (default ~/.expenser/flows/NAME.yaml)"),
) -> None:
    """Scaffold a commented flow YAML."""
    from expenser.automation.recorder import new_flow

    from expenser.paths import user_flows_dir

    target = out or user_flows_dir() / f"{name}.yaml"
    try:
        new_flow(target, name, url)
    except (FileExistsError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"Created {target}. Next: expenser flow record {target}")


@flow_app.command()
def record(
    flow: str = typer.Argument(..., help="Flow name or YAML path"),
    key: Optional[list[str]] = typer.Option(None, "--key", help="Selector key (repeatable)"),
    url: Optional[str] = typer.Option(None, "--url"),
    var: Optional[list[str]] = VarOpt,
    participants: Optional[Path] = ParticipantsOpt,
    browser: Optional[str] = BrowserOpt,
    cdp_url: Optional[str] = CdpOpt,
    chrome_path: Optional[str] = ChromePathOpt,
) -> None:
    """Capture selectors by clicking elements in a browser."""
    from expenser.automation.recorder import record_selectors

    flow = _resolve(flow)
    _load(flow)
    try:
        saved = record_selectors(
            flow, keys=key, url=url, variables=parse_vars(var),
            participants=_participants(participants), console=console,
            **_browser_kw(browser, cdp_url, chrome_path),
        )
    except (ValueError, FlowError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"Saved {len(saved)} selector(s) to {flow}.")


@flow_app.command()
def validate(
    flow: str = typer.Argument(..., help="Flow name or YAML path"),
    var: Optional[list[str]] = VarOpt,
    url: Optional[str] = typer.Option(None, "--url"),
    headless: bool = typer.Option(False, "--headless"),
    sample_participant: Optional[str] = typer.Option(None, "--sample-participant"),
    browser: Optional[str] = BrowserOpt,
    cdp_url: Optional[str] = CdpOpt,
    chrome_path: Optional[str] = ChromePathOpt,
) -> None:
    """Schema-check the flow and count selector matches on the live page."""
    from expenser.automation.recorder import validate_flow

    flow = _resolve(flow)
    _load(flow)
    try:
        validate_flow(flow, variables=parse_vars(var), url=url, headless=headless,
                      sample_participant=sample_participant, console=console,
                      **_browser_kw(browser, cdp_url, chrome_path))
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)


@flow_app.command()
def test(
    flow: str = typer.Argument(..., help="Flow name or YAML path"),
    var: Optional[list[str]] = VarOpt,
    participants: Optional[Path] = ParticipantsOpt,
    step: bool = typer.Option(False, "--step", help="Pause before each step"),
    headless: bool = typer.Option(False, "--headless"),
    browser: Optional[str] = BrowserOpt,
    cdp_url: Optional[str] = CdpOpt,
    chrome_path: Optional[str] = ChromePathOpt,
) -> None:
    """Dry-run one sample transaction through per_transaction."""
    from expenser.automation.recorder import test_flow

    flow = _resolve(flow)
    _load(flow)
    people = _participants(participants)
    if not people:
        console.print("[red]--participants FILE is required (names to use for the sample).[/]")
        raise typer.Exit(2)
    try:
        result = test_flow(flow, variables=parse_vars(var), all_participants=people,
                           step=step, headless=headless, console=console,
                           **_browser_kw(browser, cdp_url, chrome_path))
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    raise typer.Exit(0 if result.ok else 1)


@flow_app.command()
def login(
    flow: str = typer.Argument(..., help="Flow name or YAML path"),
    var: Optional[list[str]] = VarOpt,
    browser: Optional[str] = BrowserOpt,
    chrome_path: Optional[str] = ChromePathOpt,
) -> None:
    """One-time manual login in the flow's browser profile (needed for Google sign-in)."""
    from expenser.automation.recorder import login_flow

    flow = _resolve(flow)
    _load(flow)
    try:
        ok = login_flow(flow, variables=parse_vars(var), console=console,
                        **_browser_kw(browser, None, chrome_path))
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    raise typer.Exit(0 if ok else 1)


@flow_app.command()
def show(flow: str = typer.Argument(..., help="Flow name or YAML path")) -> None:
    """Pretty-print selectors, steps and required variables."""
    flow = _resolve(flow)
    f = _load(flow)
    console.print(f"[bold]{f.name}[/] - {f.description}\nstart_url: {f.start_url}")
    req = f.required_variables()
    console.print("Required variables: " + (", ".join(f"--var {v}=..." for v in req) or "none"))
    if f.group_picker:
        console.print(f"group_picker: {f.group_picker.url or f.start_url} -> {{{f.group_picker.variable}}}")
    if f.members:
        console.print(f"members: {f.members.url or '(current page)'}")
    table = Table(title="Selectors")
    table.add_column("key")
    table.add_column("selector")
    for k, v in f.selectors.items():
        table.add_row(k, v.replace("[", "\\["))
    console.print(table)

    def dump(steps, depth=0):
        for s in steps:
            flags = "".join(
                f" \\[{c}]" for c, on in (("commit", s.commit), ("optional", s.optional)) if on
            )
            console.print("  " * (depth + 1) + s.summary().replace("[", "\\[") + flags, highlight=False)
            if s.steps:
                dump(s.steps, depth + 1)
            if s.else_steps:
                console.print("  " * (depth + 1) + "else:")
                dump(s.else_steps, depth + 1)

    for section, steps in f.sections().items():
        if steps:
            console.print(f"[bold]{section}[/]")
            dump(steps)
