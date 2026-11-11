"""Tolerant reader for a user-supplied expenses table (.xlsx / .xlsm / .csv).

Unlike ``excel.read_review_excel`` this accepts many header spellings, any column order, a title row
above the header, CSV input, free-form amounts ("THB 450", "1,234.50") and dates. Formulas are never
evaluated (openpyxl does not), so amounts must be plain values -- except in our own review layout,
where the amount is recomputed from Original Amount x Rate.

Dates: day-first is preferred (05/11/2026 is 5 November). ISO (2026-11-05) is unambiguous.
Rate means: 1 unit of the row currency = ``rate`` units of the target currency.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from expenser.currency import CurrencyError, convert, normalize_code
from expenser.models import Transaction
from expenser.participants import parse_participants_cell, resolve_name

DEFAULT_TARGET = "INR"
SHEET = "Transactions"
INFO_SHEET = "Info"
PARTICIPANTS_HEADING = "Participants"
TARGET_LABEL = "Target currency"


class DatatableError(ValueError):
    """The file cannot be read at all (missing, unsupported type, corrupt)."""


_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("transactionname", "transaction", "description", "name", "what", "expense"),
    "amount": ("amount",),
    "orig_amount": ("originalamount",),
    "paid_by": ("paidby", "payer", "paid", "whopaid"),
    "participants": ("participants", "splitbetween", "split", "for", "forwhom", "sharedwith"),
    "date": ("date", "transactiondate"),
    "currency": ("currency", "originalcurrency", "cur"),
    "rate": ("rate", "exchangerate", "fxrate"),
    "include": ("include", "push"),
    "skip": ("skip",),
}
_ALIAS_LOOKUP = {a: f for f, al in _ALIASES.items() for a in al}
_AMOUNT_HEADER_RE = re.compile(r"^\s*amount\s*[\(\[]\s*([A-Za-z]{3})\s*[\)\]]\s*$", re.IGNORECASE)
EXPECTED_HEADERS = (
    "Transaction Name (or Description/Name), Amount (or Amount (XXX)), Paid By (or Payer), "
    "Participants (or Split Between/For); optional: Date, Currency, Rate, Include"
)

_SYMBOLS = {"฿": "THB", "₹": "INR", "€": "EUR", "£": "GBP", "¥": "JPY"}
_IGNORED_SYMBOLS = "$"
AMBIGUOUS_DOLLAR = "$"  # marker returned by parse_amount_with_currency


@dataclass
class DatatableResult:
    transactions: list[Transaction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    currencies_needing_rates: dict[str, tuple[int, Decimal]] = field(default_factory=dict)
    target_currency: str = DEFAULT_TARGET
    members_source: str = "derived"  # "given" | "info" | "derived"
    pending: list[int] = field(default_factory=list)  # indexes into transactions still needing a rate


# --- low level parsing -----------------------------------------------------------------
def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, float) and v != v)


def _norm_header(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower()) if s is not None else ""


def parse_number(v: Any) -> Decimal:
    """Parse '1,234.50', '1234,50', '1.234,50', '1,23,456', 1200, 12.5 into a Decimal."""
    if isinstance(v, bool):
        raise ValueError("not a number")
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(str(v))
    else:
        s = re.sub(r"\s+", "", str(v)).replace(" ", "")
        if not s:
            raise ValueError("empty")
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
        elif "," in s:
            if re.fullmatch(r"\d{1,3}(,\d{2})*,\d{3}", s):  # 1,234 / 12,34,567 -> thousands
                s = s.replace(",", "")
            else:
                s = s.replace(",", ".")
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise ValueError("not a number") from None
    if not d.is_finite():
        raise ValueError("not a number")
    return d


def parse_amount_with_currency(v: Any) -> tuple[Decimal, str | None]:
    """Return (amount, embedded currency code or None) for cells such as 'THB 450' or '₹1,200'."""
    if not isinstance(v, str):
        return parse_number(v), None
    s = v.strip()
    code: str | None = None
    for sym, c in _SYMBOLS.items():
        if sym in s:
            code = c
            s = s.replace(sym, " ")
    dollar = any(sym in s for sym in _IGNORED_SYMBOLS)
    for sym in _IGNORED_SYMBOLS:
        s = s.replace(sym, " ")
    m = re.search(r"[A-Za-z]{3}", s)
    if m:
        code = m.group(0).upper()
        s = s[: m.start()] + " " + s[m.end():]
    if code is None and dollar:
        # "$" alone is ambiguous (USD/SGD/AUD/...): report it instead of guessing or
        # silently treating the amount as the target currency.
        code = AMBIGUOUS_DOLLAR
    return parse_number(s), code


_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y", "%d.%m.%y",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
)


def parse_date(v: Any) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = re.sub(r"\s+", " ", str(v).strip())
    s = s.replace("Sept", "Sep")
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", s):
        s = s[:10]
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {str(v).strip()!r} (use YYYY-MM-DD, dd-mm-yyyy or '5 Nov 2026')")


def _is_formula(v: Any) -> bool:
    return isinstance(v, str) and v.strip().startswith("=")


# --- file loading ----------------------------------------------------------------------
def _load_rows(path: Path) -> tuple[list[tuple], list[str] | None, str | None]:
    """All rows of the data sheet (index 0 = spreadsheet row 1), plus the Info-sheet participants
    and target currency, if any."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        try:
            wb = load_workbook(path, data_only=False, read_only=False)
        except Exception as e:  # noqa: BLE001 - corrupt / locked file
            raise DatatableError(f"Could not open {path}: {e}") from e
        ws = wb[SHEET] if SHEET in wb.sheetnames else wb.worksheets[0]
        rows = [tuple(c.value for c in r) for r in ws.iter_rows()]
        info: list[str] | None = None
        info_target: str | None = None
        if INFO_SHEET in wb.sheetnames:
            info = _info_participants(wb[INFO_SHEET])
            info_target = _info_target(wb[INFO_SHEET])
        return rows, info, info_target
    if suffix in (".csv", ".txt"):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            raise DatatableError(f"Could not read {path}: {e}") from e
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel  # type: ignore[assignment]
        return [tuple(r) for r in csv.reader(text.splitlines(), dialect)], None, None
    raise DatatableError(f"Unsupported file type {suffix!r}; use .xlsx, .xlsm or .csv")


def _info_target(ws) -> str | None:
    """Target currency stored next to a 'Target currency' label (Info!C1:D1 in both the review
    Excel and the datatable template)."""
    for row in ws.iter_rows(min_row=1, max_row=10, max_col=6):
        for c in row[:-1]:
            if isinstance(c.value, str) and c.value.strip().casefold() == TARGET_LABEL.casefold():
                v = ws.cell(c.row, c.column + 1).value
                try:
                    return normalize_code(str(v)) if not _blank(v) else None
                except CurrencyError:
                    return None
    return None


def _info_participants(ws) -> list[str] | None:
    """Names listed under a 'Participants' heading in column A (layout of write_review_excel)."""
    heading = None
    for r in range(1, ws.max_row + 1):
        v = ws.cell(r, 1).value
        if isinstance(v, str) and v.strip().casefold() == PARTICIPANTS_HEADING.casefold():
            heading = r
            break
    if heading is None:
        return None
    names: list[str] = []
    for r in range(heading + 1, ws.max_row + 1):
        v = ws.cell(r, 1).value
        if _blank(v):
            if names:
                break
            continue  # write_review_excel leaves one blank row between heading and names
        names.append(str(v).strip())
    return _dedupe(names) or None


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out


def _find_header(rows: list[tuple]) -> tuple[int, dict[str, int], str | None] | None:
    for idx, row in enumerate(rows[:50]):
        cols: dict[str, int] = {}
        inferred: str | None = None
        for i, cell in enumerate(row):
            if _blank(cell):
                continue
            m = _AMOUNT_HEADER_RE.match(str(cell))
            if m:
                field_name = "amount"
                code = m.group(1).upper()
            else:
                field_name = _ALIAS_LOOKUP.get(_norm_header(cell))
                code = None
            if field_name and field_name not in cols:
                cols[field_name] = i
                if code and inferred is None:
                    inferred = code
        has_amount = "amount" in cols or "orig_amount" in cols
        if has_amount and "name" in cols:
            return idx, cols, inferred
    return None


# --- participants helpers ----------------------------------------------------------------
def _names_in_cell(cell: str) -> list[str]:
    """Names mentioned in a Participants cell (ALL / exclusions keywords removed)."""
    text = re.sub(r"\b(?:except|excluding|minus|without)\b", ",-", cell, flags=re.IGNORECASE)
    out: list[str] = []
    for tok in re.split(r"[,;]", text):
        for part in re.split(r"(?:^|\s)-\s*", tok.strip()):
            part = part.strip()
            if part and part.casefold() != "all":
                out.append(part)
    return out


# --- main entry --------------------------------------------------------------------------
def read_datatable(
    path: Path | str,
    *,
    participants: list[str] | None = None,
    target_currency: str | None = None,
    rates: dict[str, Decimal] | None = None,
    force_currency: str | None = None,
) -> DatatableResult:
    path = Path(path)
    if not path.exists():
        raise DatatableError(f"File not found: {path}")
    rows, info_names, info_target = _load_rows(path)
    res = DatatableResult()
    rates = dict(rates or {})

    found = _find_header(rows)
    if found is None:
        res.errors.append(
            "Could not find a header row. Expected columns: " + EXPECTED_HEADERS
        )
        return res
    hdr_idx, cols, inferred = found

    # Amount / Original Amount handling
    if "orig_amount" in cols and "amount" not in cols and "rate" not in cols:
        cols["amount"] = cols.pop("orig_amount")  # "Original Amount" alone is just the amount
    review_style = "orig_amount" in cols
    missing = [h for f, h in (("paid_by", "Paid By"), ("participants", "Participants")) if f not in cols]
    if missing:
        res.errors.append(
            f"Missing required column(s): {', '.join(missing)}. Expected columns: " + EXPECTED_HEADERS
        )
        return res

    try:
        target = normalize_code(target_currency or inferred or info_target or os.environ.get("EXPENSER_TARGET_CURRENCY") or DEFAULT_TARGET)
        forced = normalize_code(force_currency) if force_currency else None
    except CurrencyError as e:
        raise DatatableError(str(e)) from e
    res.target_currency = target

    def get(row: tuple, name: str) -> Any:
        i = cols.get(name)
        return row[i] if i is not None and i < len(row) else None

    # collect data rows
    data: list[tuple[int, tuple]] = []
    for off, row in enumerate(rows[hdr_idx + 1:], start=hdr_idx + 2):
        name_v = get(row, "name")
        if isinstance(name_v, str) and name_v.strip().upper() == "TOTAL":
            break
        if all(_blank(c) for c in row):
            break
        data.append((off, row))

    # members
    if participants is not None:
        members, res.members_source = list(participants), "given"
    elif info_names:
        members, res.members_source = list(info_names), "info"
    else:
        names: list[str] = []
        for _, row in data:
            pb = get(row, "paid_by")
            if not _blank(pb):
                names.append(str(pb).strip())
            pc = get(row, "participants")
            if not _blank(pc):
                names.extend(_names_in_cell(str(pc)))
        members, res.members_source = _dedupe(names), "derived"
    res.members = members

    txs: list[Transaction] = []
    pending: list[int] = []
    needing: dict[str, tuple[int, Decimal]] = {}
    for rnum, row in data:
        inc = get(row, "include")
        if not _blank(inc):
            val = str(inc).strip().upper()
            yes, no = ("Y", "YES", "TRUE", "1"), ("N", "NO", "FALSE", "0")
            if val in no:
                continue
            if val not in yes:
                res.errors.append(f"Row {rnum}: Include must be Y or N (got {inc!r})")
                continue
        if "skip" in cols:
            sk = get(row, "skip")
            if not _blank(sk):
                sval = str(sk).strip().upper()
                if sval in ("Y", "YES", "TRUE", "1"):
                    continue
                if sval not in ("N", "NO", "FALSE", "0"):
                    res.errors.append(f"Row {rnum}: Skip must be Y or N (got {sk!r})")
                    continue

        errs: list[str] = []
        name_v = get(row, "name")
        name = "" if _blank(name_v) else str(name_v).strip()
        if not name:
            errs.append("Transaction Name is empty")

        # currency
        code: str | None = forced
        cur_v = get(row, "currency")
        if code is None and not _blank(cur_v):
            try:
                code = normalize_code(str(cur_v))
            except CurrencyError:
                errs.append(f"Currency {str(cur_v).strip()!r} is not a 3-letter code")

        # amount
        orig: Decimal | None = None
        override: Decimal | None = None
        amt_field = "orig_amount" if review_style else "amount"
        amt_v = get(row, amt_field)
        embedded: str | None = None
        if _blank(amt_v):
            errs.append("Amount is empty")
        elif _is_formula(amt_v) and not review_style:
            errs.append("Amount is a formula, which cannot be evaluated; paste it as a plain value")
        else:
            try:
                orig, embedded = parse_amount_with_currency(amt_v)
                if orig <= 0:
                    errs.append(f"Amount must be greater than 0 (got {amt_v!r})")
                    orig = None
            except ValueError:
                errs.append(f"Amount {amt_v!r} is not a number")
        if review_style:
            a_v = get(row, "amount")
            if isinstance(a_v, (int, float)) and not isinstance(a_v, bool):
                override = parse_number(a_v)
                if override <= 0:
                    errs.append(f"Amount must be greater than 0 (got {a_v!r})")
                    override = None
            elif not _blank(a_v) and not _is_formula(a_v):
                try:
                    override = parse_number(a_v)
                    if override <= 0:
                        errs.append(f"Amount must be greater than 0 (got {a_v!r})")
                        override = None
                except ValueError:
                    errs.append(f"Amount {a_v!r} is not a number")
        if code is None and embedded == AMBIGUOUS_DOLLAR:
            errs.append(
                f"Amount {amt_v!r} uses '$', which is ambiguous; fill in the Currency column (USD, SGD, AUD, ...)"
            )
            code = target  # placeholder; the row is rejected by the error above
        if code is None:
            code = embedded or target

        # rate
        rate: Decimal | None = None
        rate_v = get(row, "rate")
        if code != target and not _blank(rate_v):
            try:
                rate = parse_number(rate_v)
                if rate <= 0:
                    errs.append(f"Rate must be greater than 0 (got {rate_v!r})")
                    rate = None
            except ValueError:
                errs.append(f"Rate {rate_v!r} is not a number")
        if code != target and rate is None and not _blank(rate_v):
            pass  # error already recorded
        elif code != target and rate is None:
            rate = rates.get(code)

        # names
        paid_by = None
        pb_v = get(row, "paid_by")
        if _blank(pb_v):
            errs.append("Paid By is required")
        else:
            paid_by = resolve_name(str(pb_v), members) if res.members_source != "derived" else str(pb_v).strip()
            if paid_by is None:
                errs.append(f"Paid By {str(pb_v).strip()!r} is not a known participant")
        who: list[str] = []
        pc_v = get(row, "participants")
        if _blank(pc_v):
            errs.append("Participants is required (names, or ALL)")
        else:
            try:
                who = parse_participants_cell(str(pc_v), members)
                if not who:
                    errs.append("Participants is empty after exclusions")
            except ValueError as e:
                errs.append(str(e))

        date = None
        d_v = get(row, "date")
        if not _blank(d_v):
            try:
                date = parse_date(d_v)
            except ValueError as e:
                errs.append(str(e))

        if errs:
            res.errors.extend(f"Row {rnum}: {m}" for m in errs)
            continue
        assert orig is not None
        base: dict[str, Any] = dict(name=name, participants=who, paid_by=paid_by, date=date)
        if code == target:
            if review_style:
                amount = override if override is not None else orig
                txs.append(Transaction(amount=amount, original_amount=orig, original_currency=code,
                                       rate=Decimal(1), **base))
            else:
                txs.append(Transaction(amount=orig, **base))
        elif override is not None:  # review layout: explicit Amount wins
            txs.append(Transaction(amount=override, original_amount=orig, original_currency=code, rate=rate, **base))
        elif rate is not None:
            t = Transaction(amount=orig, original_amount=orig, original_currency=code, **base)
            txs.append(convert([t], {code: rate}, target)[0])
        else:
            txs.append(Transaction(amount=orig, original_amount=orig, original_currency=code, **base))
            pending.append(len(txs) - 1)
            n, tot = needing.get(code, (0, Decimal("0")))
            needing[code] = (n + 1, tot + orig)
    res.transactions, res.pending, res.currencies_needing_rates = txs, pending, needing
    return res


def apply_rates(result: DatatableResult, rates: dict[str, Decimal]) -> list[Transaction]:
    """Convert the rows that were waiting for a rate; other rows are returned unchanged."""
    out = list(result.transactions)
    for i in result.pending:
        out[i] = convert([out[i]], rates, result.target_currency)[0]
    return out
