"""Logic shared by the CLI commands and the interactive wizard."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from rich.console import Console
from rich.table import Table

from expenser.currency import (
    CurrencyError, collect_rates, convert, normalize_code, summarize_currencies,
)
from expenser.extract import ExtractionResult
from expenser.models import Transaction

__all__ = ["find_duplicates", "drop_duplicates", "ask_dedupe", "resolve_unknown_currencies", "convert_extraction", "print_push_summary", "safe_name"]


def safe_name(text: str, fallback: str = "group") -> str:
    """File-name friendly version of ``text``."""
    import re

    out = re.sub(r"[^\w.-]+", "-", text.strip(), flags=re.UNICODE).strip("-._")
    return out[:60] or fallback


def resolve_unknown_currencies(
    result: ExtractionResult,
    target: str,
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    console: Console | None = None,
) -> list[Transaction]:
    """Rows whose currency Claude reported in an unparseable form must NOT silently be treated
    as the target currency (450 THB would become 450 INR). Ask for each one, or raise
    :class:`CurrencyError`."""
    console = console or Console()
    txs = list(result.transactions)
    unknown = [i for i, t in enumerate(txs) if not t.original_currency]
    if not unknown:
        return txs
    if not interactive:
        names = ", ".join(repr(txs[i].name) for i in unknown)
        raise CurrencyError(
            f"Could not determine the currency of: {names}. "
            "Re-run with --source-currency CODE (or in an interactive terminal to be asked)."
        )
    console.print(f"[yellow]Currency not recognised for {len(unknown)} row(s).[/]")
    for i in unknown:
        t = txs[i]
        note = result.notes.get(i, "")
        while True:
            raw = input_fn(f"  Currency for {t.name!r} ({t.amount}) [{note}] — ISO code, Enter = {target}: ").strip()
            try:
                code = normalize_code(raw) if raw else target
                break
            except CurrencyError as e:
                console.print(f"  [red]{e}[/]")
        txs[i] = t.model_copy(update={"original_currency": code, "original_amount": t.amount})
    return txs


def convert_extraction(
    result: ExtractionResult,
    target: str,
    given_rates: dict[str, Decimal],
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    console: Console | None = None,
) -> tuple[list[Transaction], dict[str, Decimal]]:
    """Currency resolution + rates + conversion for freshly extracted rows. Raises CurrencyError."""
    console = console or Console()
    result.transactions = resolve_unknown_currencies(
        result, target, interactive=interactive, input_fn=input_fn, console=console
    )
    detected = summarize_currencies(result.transactions, target)
    console.print(
        "Detected currencies: "
        + ", ".join(f"{c} ({n} row{'s' if n != 1 else ''}, {tot:,.2f})" for c, (n, tot) in detected.items())
    )
    needed = [c for c in detected if c != target]
    rates = collect_rates(
        detected, target, given_rates, console=console, input_fn=input_fn,
        interactive=interactive or all(c in given_rates for c in needed),
    )
    txs = convert(result.transactions, rates, target)
    for c, r in rates.items():
        console.print(f"Rate applied: 1 {c} = {r.normalize():f} {target}")
    return txs, rates


_STATUS_STYLE = {
    "pushed": "green", "dry_run": "cyan", "skipped_duplicate": "yellow",
    "skipped_by_user": "yellow", "failed": "red",
}


def print_push_summary(console: Console, results: list[Any]) -> None:
    table = Table(title="Push summary")
    for c in ("#", "Transaction", "Amount", "Status", "Details"):
        table.add_column(c)
    for i, r in enumerate(results, 1):
        style = _STATUS_STYLE.get(r.status, "white")
        detail = r.error or ""
        if r.screenshot:
            detail = f"{detail} (screenshot: {r.screenshot})".strip()
        table.add_row(str(i), r.transaction.name, f"{r.transaction.amount:.2f}", f"[{style}]{r.status}[/]", detail)
    console.print(table)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    console.print(", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing to do")


def find_duplicates(txs: list[Transaction]) -> list[tuple[Transaction, list[int]]]:
    """Fingerprints occurring more than once: (first occurrence, 0-based indexes), in order."""
    groups: dict[str, list[int]] = {}
    for i, t in enumerate(txs):
        groups.setdefault(t.fingerprint(), []).append(i)
    return [(txs[idx[0]], idx) for idx in groups.values() if len(idx) > 1]


def drop_duplicates(txs: list[Transaction]) -> list[Transaction]:
    """Keep the first row of each fingerprint, preserving order."""
    seen: set[str] = set()
    out: list[Transaction] = []
    for t in txs:
        fp = t.fingerprint()
        if fp not in seen:
            seen.add(fp)
            out.append(t)
    return out


def ask_dedupe(
    txs: list[Transaction], *, confirm: Callable[..., bool], console: Console
) -> list[Transaction]:
    """If rows repeat exactly, show them and ask whether to keep one of each (default: No)."""
    dups = find_duplicates(txs)
    if not dups:
        return txs
    table = Table(title="Identical rows")
    for c in ("Name", "Amount", "Date", "Paid by", "Participants", "Found"):
        table.add_column(c)
    for t, idx in dups:
        amount = f"{t.amount:.2f}"
        if t.original_currency and t.original_amount is not None:
            amount += f" ({t.original_amount:.2f} {t.original_currency})"
        table.add_row(
            t.name, amount, t.date.isoformat() if t.date else "no date", t.paid_by or "-",
            ", ".join(t.participants),
            f"x{len(idx)} (rows {', '.join(str(i + 1) for i in idx)})",
        )
    console.print(table)
    if confirm("Remove the duplicates (keep one of each)?", default=False):
        out = drop_duplicates(txs)
        console.print(f"Removed {len(txs) - len(out)} duplicate row(s)")
        return out
    console.print("Keeping all rows as they are")
    return txs
