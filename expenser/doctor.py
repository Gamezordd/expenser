"""`expenser doctor`: offline environment checklist. Never opens a browser or uses the network."""
from __future__ import annotations

import platform
import sys
from pathlib import Path

OK, BAD, WARN = "ok", "fail", "warn"
_MARK = {OK: "[green]✓[/green]", BAD: "[red]✗[/red]", WARN: "[yellow]![/yellow]"}


def _os_kind() -> str:
    return {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")


def _chrome_hint() -> str:
    return "Install Google Chrome: https://www.google.com/chrome/ (or set EXPENSER_CHROME to its path)."


def _ant_hint() -> str:
    if _os_kind() == "macos":
        return "Optional, for 'sign in with Anthropic': brew install anthropics/tap/ant"
    return ("Optional, for 'sign in with Anthropic': download from "
            "https://github.com/anthropics/anthropic-cli/releases and put `ant` on PATH")


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("expenser")
    except Exception:  # noqa: BLE001
        return "unknown"


def _check_flows() -> tuple[str, str, str]:
    try:
        from importlib.resources import files

        folder = files("expenser") / "flows"
        if not folder.is_dir():
            return WARN, "Bundled flows folder not found", "Reinstall expenser (the flows folder should ship in the package)."
        names = sorted(p.name for p in folder.iterdir() if p.name.endswith(".yaml"))
        if not names:
            return BAD, "No bundled flow files (*.yaml) found", "Reinstall expenser."
        return OK, "Flows: " + ", ".join(names), ""
    except Exception as e:  # noqa: BLE001
        return WARN, f"Could not inspect bundled flows ({type(e).__name__}: {e})", "Reinstall expenser."


def _check_dir(path: Path) -> tuple[str, str, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".doctor-write-test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return OK, f"{path} is writable", ""
    except Exception as e:  # noqa: BLE001
        return BAD, f"{path} is not writable ({e})", "Check the folder's permissions or free disk space."


def collect_checks() -> list[tuple[str, str, str, str, bool]]:
    """Rows of (label, status, message, hint, required)."""
    rows: list[tuple[str, str, str, str, bool]] = []

    def add(label: str, res: tuple[str, str, str], required: bool) -> None:
        rows.append((label, res[0], res[1], res[2], required))

    ver = sys.version_info
    add("Python", (OK if ver >= (3, 10) else BAD,
                   f"Python {ver.major}.{ver.minor}.{ver.micro} on {platform.system()} {platform.release()} ({platform.machine()})",
                   "" if ver >= (3, 10) else "Python 3.10+ is required; reinstall with the install script."), True)
    add("expenser", (OK, f"version {_version()}", ""), False)

    try:
        from expenser.automation.browser import find_chrome

        add("Google Chrome", (OK, f"found at {find_chrome(None)}", ""), True)
    except Exception:  # noqa: BLE001
        add("Google Chrome", (BAD, "not found", _chrome_hint()), True)

    add("Bundled flows", _check_flows(), True)

    try:
        from expenser import auth

        st = auth.describe_credentials()
        if st.ok:
            add("Anthropic credentials", (OK, f"{st.detail} (optional: screenshot mode only)", ""), False)
        else:
            add("Anthropic credentials", (WARN, "not set up (optional: only needed for screenshot mode)",
                                          "Run `expenser auth login` or set ANTHROPIC_API_KEY. Excel mode needs no account."), False)
        for w in st.warnings:
            add("Anthropic credentials", (WARN, w, ""), False)
    except Exception as e:  # noqa: BLE001
        add("Anthropic credentials", (WARN, f"could not check ({type(e).__name__})", ""), False)

    try:
        from expenser import auth

        ant = auth.find_ant()
    except Exception:  # noqa: BLE001
        ant = None
    add("ant CLI", (OK, f"found at {ant} (optional)", "") if ant
        else (WARN, "not found (optional)", _ant_hint()), False)

    from expenser.paths import output_dir, state_dir

    add("Data folder", _check_dir(state_dir()), True)
    add("Output folder", _check_dir(output_dir()), True)

    enc = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
    if enc == "utf8":
        add("Terminal", (OK, "UTF-8 output", ""), False)
    elif _os_kind() == "windows":
        add("Terminal", (WARN, f"output encoding is {enc or 'unknown'}, not UTF-8",
                         "Use Windows Terminal (https://aka.ms/terminal) or run `chcp 65001`."), False)
    else:
        add("Terminal", (WARN, f"output encoding is {enc or 'unknown'}, not UTF-8", "Set LANG to a UTF-8 locale."), False)
    return rows


def run_doctor(console=None) -> int:
    from rich.console import Console

    console = console or Console()
    rows = collect_checks()
    console.print("[bold]expenser doctor[/bold]\n")
    failed = False
    for label, status, msg, hint, required in rows:
        console.print(f" {_MARK[status]} [bold]{label}[/bold]: {msg}", highlight=False)
        if hint and status != OK:
            console.print(f"     [dim]{hint}[/dim]", highlight=False)
        if status == BAD and required:
            failed = True
    console.print()
    if failed:
        console.print("[red]Some required checks failed.[/red] Fix the items marked ✗ above.")
    else:
        console.print("[green]All required checks passed.[/green] Items marked ! are optional or advisory.")
    return 1 if failed else 0
