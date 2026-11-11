"""Interactive approval loop around the review spreadsheet."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table

from expenser.excel import read_review_excel
from expenser.models import Transaction
from expenser.opener import open_in_default_app  # noqa: F401  (re-exported; tests patch it here)


def approval_path(excel_path: Path) -> Path:
    excel_path = Path(excel_path)
    return excel_path.with_name(excel_path.name + ".approved.json")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def total_of(transactions: list[Transaction]) -> Decimal:
    return sum((t.amount for t in transactions), Decimal("0.00"))


def render_transactions(transactions: list[Transaction], all_participants: list[str] | None = None,
                        title: str = "Transactions", target_currency: str | None = None) -> Table:
    table = Table(title=title, show_footer=True)
    converted = any(t.original_currency and t.rate is not None for t in transactions)
    table.add_column("#", justify="right")
    table.add_column("Date")
    table.add_column("Name", footer="TOTAL")
    if converted:
        table.add_column("Original", justify="right")
        table.add_column("Rate", justify="right")
    amt_title = f"Amount ({target_currency})" if target_currency else "Amount"
    table.add_column(amt_title, justify="right", footer=f"{total_of(transactions):.2f}")
    table.add_column("Paid by")
    table.add_column("Participants")
    for i, t in enumerate(transactions, 1):
        everyone = all_participants is not None and set(t.participants) == set(all_participants)
        extra: list[str] = []
        if converted:
            if t.original_currency and t.rate is not None and t.original_amount is not None:
                extra = [f"{t.original_amount:.2f} {t.original_currency}", f"{t.rate.normalize():f}"]
            else:
                extra = ["-", "-"]
        table.add_row(
            str(i),
            t.date.isoformat() if t.date else "-",
            t.name,
            *extra,
            f"{t.amount:.2f}",
            t.paid_by or "-",
            "ALL" if everyone else ", ".join(t.participants),
        )
    return table


def write_approval(excel_path: Path, transactions: list[Transaction]) -> Path:
    out = approval_path(excel_path)
    payload = {
        "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sha256": file_sha256(excel_path),
        "transactions": [t.model_dump(mode="json") for t in transactions],
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def is_approved(excel_path: Path) -> bool:
    """True if a sidecar exists whose hash matches the current xlsx bytes."""
    p = approval_path(excel_path)
    if not p.exists() or not Path(excel_path).exists():
        return False
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("sha256") == file_sha256(excel_path)
    except (OSError, ValueError):
        return False


def review_loop(
    excel_path: Path,
    participants: list[str],
    *,
    open_file: bool = True,
    console: Console | None = None,
    input_fn: Callable[[str], str] = input,
    target_currency: str | None = None,
) -> list[Transaction] | None:
    """Let the user edit the Excel and approve it. Returns transactions, or None on quit."""
    console = console or Console()
    excel_path = Path(excel_path)
    console.print(f"Review file: [bold]{excel_path}[/]")
    if open_file:
        open_in_default_app(excel_path, console)

    def show() -> tuple[list[Transaction], list[str]]:
        try:
            txs, errs = read_review_excel(excel_path, participants, target_currency=target_currency)
        except Exception as e:  # noqa: BLE001 - e.g. file locked / corrupt
            console.print(f"[red]Could not read {excel_path}: {e}[/]")
            return [], [str(e)]
        if errs:
            console.print("[red]Problems found:[/]")
            for e in errs:
                console.print(f"  [red]-[/] {e}")
        console.print(render_transactions(txs, participants, title=f"{len(txs)} transaction(s) to push",
                                   target_currency=target_currency))
        return txs, errs

    show()
    while True:
        try:
            choice = input_fn("[a]pprove  [r]eload  [o]pen again  [q]uit > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Aborted.[/]")
            return None
        if choice in ("q", "quit"):
            return None
        if choice in ("o", "open"):
            open_in_default_app(excel_path, console)
        elif choice in ("r", "reload"):
            show()
        elif choice in ("a", "approve"):
            txs, errs = show()
            if errs:
                console.print("[red]Fix the problems above, save the file, then reload/approve again.[/]")
                continue
            if not txs:
                console.print("[yellow]Nothing to approve (no included transactions).[/]")
                continue
            cur = f" {target_currency}" if target_currency else ""
            try:
                ans = input_fn(f"Approve {len(txs)} transactions totalling {total_of(txs):.2f}{cur}? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                return None
            if ans.strip().lower() in ("y", "yes"):
                sidecar = write_approval(excel_path, txs)
                console.print(f"[green]Approved.[/] Saved {sidecar.name}")
                return txs
            console.print("Not approved.")
        else:
            console.print("Please enter a, r, o or q.")
