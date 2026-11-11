"""Review spreadsheet: write for human editing, read back with validation."""
from __future__ import annotations

import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from expenser.currency import CurrencyError, normalize_code
from expenser.extract import ExtractionResult
from expenser.models import Transaction
from expenser.participants import parse_participants_cell, resolve_name

BASE_COLUMNS = [
    "Date", "Transaction Name", "Original Amount", "Original Currency", "Rate",
    "Amount", "Paid By", "Participants", "Include", "Notes",
]
COLUMNS = BASE_COLUMNS  # header of the amount column is "Amount (<TARGET>)" in written files
LEGACY_COLUMNS = ["Date", "Transaction Name", "Amount", "Currency", "Paid By", "Participants", "Include", "Notes"]
_WIDTHS = [12, 34, 16, 18, 12, 16, 16, 36, 9, 50]
SHEET = "Transactions"
TOTAL_LABEL = "TOTAL"
DEFAULT_TARGET = "INR"
_AMOUNT_HEADER_RE = re.compile(r"^Amount\s*\(([A-Za-z]{3})\)$")


def amount_header(target: str) -> str:
    return f"Amount ({target})"


def _instructions(target: str) -> list[str]:
    return [
        "How to edit the Transactions sheet",
        "",
        "- Do not rename or reorder the column headers.",
        "- Participants: comma-separated names (Alice, Bob), or ALL for everyone.",
        "  Exclusions: 'ALL -Bob' or 'ALL except Bob, Carol'. Unique prefixes (ali) and any letter case work.",
        "- Paid By: required; pick a participant from the dropdown.",
        "- Include: set to N to skip a row (it will not be pushed).",
        f"- Original Amount / Original Currency: as shown on the screenshot. Amount ({target}) is what gets pushed.",
        f"- Rate: 1 unit of the Original Currency in {target}. Editing Rate (or Original Amount) recomputes",
        f"  Amount ({target}) = ROUND(Original Amount x Rate, 2). You can also overwrite Amount with a plain number to force a value.",
        "- The TOTAL row and Notes are ignored when reading.",
        "- Save the file, then go back to the terminal and approve.",
    ]


def write_review_excel(
    path: Path,
    result_or_transactions: ExtractionResult | list[Transaction],
    participants: list[str],
    *,
    currency: str | None = None,
    target_currency: str = DEFAULT_TARGET,
    rates: dict[str, Decimal] | None = None,
) -> Path:
    """Write the review sheet. Rows without ``original_currency`` are taken to be in the target currency.

    ``currency`` is accepted for backwards compatibility and ignored.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = target_currency.strip().upper()
    rates = dict(rates or {})
    if isinstance(result_or_transactions, ExtractionResult):
        txs = result_or_transactions.transactions
        notes = result_or_transactions.notes
    else:
        txs, notes = list(result_or_transactions), {}

    amt_col = amount_header(target)
    columns = [amt_col if c == "Amount" else c for c in BASE_COLUMNS]
    rows = []
    for i, t in enumerate(txs):
        r = i + 2
        code = t.original_currency or target
        rate = t.rate if t.rate is not None else (Decimal(1) if code == target else rates.get(code))
        orig = t.original_amount if t.original_amount is not None else t.amount
        everyone = set(t.participants) == set(participants)
        rows.append(
            {
                "Date": t.date,
                "Transaction Name": t.name,
                "Original Amount": float(orig),
                "Original Currency": code,
                "Rate": float(rate) if rate is not None else None,
                amt_col: f"=ROUND(C{r}*E{r},2)" if rate is not None else float(t.amount),
                "Paid By": t.paid_by or "",
                "Participants": "ALL" if everyone else ", ".join(t.participants),
                "Include": "Y",
                "Notes": notes.get(i, ""),
            }
        )
    df = pd.DataFrame(rows, columns=columns)
    instructions = _instructions(target)
    list_start = len(instructions) + 4
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name=SHEET, index=False)
        info = xw.book.create_sheet("Info")
        ws = xw.sheets[SHEET]
        n = len(df)
        last = n + 1  # header is row 1

        for c, w in enumerate(_WIDTHS, start=1):
            ws.column_dimensions[get_column_letter(c)].width = w
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="305496")
            cell.alignment = Alignment(vertical="center")
        ws.freeze_panes = "A2"
        for r in range(2, last + 1):
            ws.cell(r, 1).number_format = "yyyy-mm-dd"
            ws.cell(r, 3).number_format = "0.00"
            ws.cell(r, 5).number_format = "0.000000"
            ws.cell(r, 6).number_format = "0.00"
            ws.cell(r, 10).alignment = Alignment(wrap_text=True, vertical="top")

        total_row = last + 2
        ws.cell(total_row, 2, TOTAL_LABEL).font = Font(bold=True)
        e = max(last, 2)
        tc = ws.cell(total_row, 6, f'=SUMIF(I2:I{e},"Y",F2:F{e})')
        tc.number_format = "0.00"
        tc.font = Font(bold=True)

        # Dropdowns (extend a bit beyond the data so users can add rows).
        end = max(last + 1, 2)
        incl = DataValidation(type="list", formula1='"Y,N"', allow_blank=False)
        incl.add(f"I2:I{end}")
        ws.add_data_validation(incl)
        if participants:
            # Reference the Info sheet list; a literal list is limited to 255 chars.
            pay = DataValidation(
                type="list",
                formula1=f"=Info!$A${list_start}:$A${list_start - 1 + len(participants)}",
                allow_blank=False,
            )
            pay.add(f"G2:G{end}")
            ws.add_data_validation(pay)

        info.column_dimensions["A"].width = 100
        info.column_dimensions["C"].width = 22
        info.column_dimensions["D"].width = 14
        for i, line in enumerate(instructions, start=1):
            info.cell(i, 1, line)
        info.cell(1, 1).font = Font(bold=True, size=13)
        info.cell(list_start - 2, 1, "Participants").font = Font(bold=True)
        for i, name in enumerate(participants):
            info.cell(list_start + i, 1, name)
        info.cell(1, 3, "Target currency").font = Font(bold=True)
        info.cell(1, 4, target)
        info.cell(3, 3, "Rates used").font = Font(bold=True)
        info.cell(3, 4, f"1 unit = ? {target}").font = Font(bold=True)
        for i, (code, rate) in enumerate(sorted(rates.items()), start=4):
            info.cell(i, 3, code)
            c = info.cell(i, 4, float(rate))
            c.number_format = "0.000000"
    return path


# --- Reading -------------------------------------------------------------------
def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip())


def _parse_amount(v: Any) -> Decimal:
    if isinstance(v, bool):
        raise ValueError("not a number")
    if isinstance(v, (int, float)):
        d = Decimal(str(v))
    else:
        s = str(v).strip().replace(" ", "").replace("€", "").replace("$", "").replace("£", "")
        if "," in s and "." in s:
            # the last separator is the decimal one
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
        d = Decimal(s)
    if not d.is_finite():
        raise ValueError("not a number")
    return d


def _parse_date(v: Any) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, pd.Timestamp):
        return v.date()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {s!r} (use YYYY-MM-DD or dd-mm-yyyy)")


def _is_formula(v: Any) -> bool:
    return isinstance(v, str) and v.strip().startswith("=")


def read_review_excel(
    path: Path, participants: list[str], *, target_currency: str | None = None
) -> tuple[list[Transaction], list[str]]:
    """Return (included transactions, human-readable validation errors).

    Understands the current layout (Original Amount/Currency/Rate + ``Amount (XXX)``) and the legacy
    layout (``Amount | Currency``). Formula cells are not evaluated by openpyxl, so the amount is
    recomputed as round(Original Amount x Rate, 2) unless the Amount cell holds a plain number.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Excel file not found: {path}")
    wb = load_workbook(path, data_only=False)
    ws = wb[SHEET] if SHEET in wb.sheetnames else wb.worksheets[0]
    header = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
    col: dict[str, int] = {}
    inferred: str | None = None
    for i, h in enumerate(header):
        m = _AMOUNT_HEADER_RE.match(h)
        if m:
            inferred = m.group(1).upper()
            col.setdefault("Amount", i)
        elif h and h not in col:
            col[h] = i
    legacy = "Original Amount" not in col and "Rate" not in col
    target = (target_currency or inferred or "").strip().upper() or None
    required = (LEGACY_COLUMNS if legacy else BASE_COLUMNS)[:-1]
    missing = [c for c in required if c not in col]
    if missing:
        return [], [f"Missing column header(s): {', '.join(missing)}. Do not rename headers."]

    def get(row: tuple, name: str) -> Any:
        i = col.get(name)
        return row[i].value if i is not None and i < len(row) else None

    txs: list[Transaction] = []
    errors: list[str] = []
    for rnum, row in enumerate(ws.iter_rows(min_row=2), start=2):
        name_v = get(row, "Transaction Name")
        if isinstance(name_v, str) and name_v.strip().upper() == TOTAL_LABEL:
            break
        if all(_blank(c.value) for c in row):
            break
        include = get(row, "Include")
        if not _blank(include):
            inc = str(include).strip().upper()
            if inc in ("N", "NO", "FALSE", "0"):
                continue
            if inc not in ("Y", "YES", "TRUE", "1"):
                errors.append(f"Row {rnum}: Include must be Y or N (got {include!r})")
                continue
        row_errors: list[str] = []

        name = "" if _blank(name_v) else str(name_v).strip()
        if not name:
            row_errors.append("Transaction Name is empty")

        amount = None
        orig_amount = orig_code = rate = None
        amt_v = get(row, "Amount")
        if legacy:
            if _blank(amt_v):
                row_errors.append("Amount is empty")
            else:
                try:
                    amount = _parse_amount(amt_v)
                    if amount <= 0:
                        row_errors.append(f"Amount must be greater than 0 (got {amt_v!r})")
                        amount = None
                except (InvalidOperation, ValueError):
                    row_errors.append(f"Amount {amt_v!r} is not a number")
        else:
            oa_v, oc_v, rate_v = get(row, "Original Amount"), get(row, "Original Currency"), get(row, "Rate")
            if not _blank(oa_v):
                try:
                    orig_amount = _parse_amount(oa_v)
                    if orig_amount <= 0:
                        row_errors.append(f"Original Amount must be greater than 0 (got {oa_v!r})")
                        orig_amount = None
                except (InvalidOperation, ValueError):
                    row_errors.append(f"Original Amount {oa_v!r} is not a number")
            if _blank(oc_v):
                row_errors.append("Original Currency is required (3-letter code such as THB)")
            else:
                try:
                    orig_code = normalize_code(str(oc_v))
                except CurrencyError:
                    row_errors.append(f"Original Currency {str(oc_v).strip()!r} is not a 3-letter code")
            same = orig_code is not None and target is not None and orig_code == target
            if not _blank(rate_v):
                try:
                    rate = _parse_amount(rate_v)
                    if rate <= 0:
                        row_errors.append(f"Rate must be greater than 0 (got {rate_v!r})")
                        rate = None
                except (InvalidOperation, ValueError):
                    row_errors.append(f"Rate {rate_v!r} is not a number")
            elif same:
                rate = Decimal(1)
            elif orig_code is not None:
                row_errors.append(f"Rate is required for {orig_code} (1 {orig_code} = ? {target or 'target'})")

            if isinstance(amt_v, (int, float)) and not isinstance(amt_v, bool):
                amount = _parse_amount(amt_v)
            elif not _blank(amt_v) and not _is_formula(amt_v):
                try:
                    amount = _parse_amount(amt_v)
                except (InvalidOperation, ValueError):
                    row_errors.append(f"Amount {amt_v!r} is not a number")
            elif orig_amount is not None and rate is not None:
                amount = (orig_amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            elif not any("Original Amount" in m or "Rate" in m for m in row_errors):
                row_errors.append("Amount cannot be determined (fill Original Amount and Rate, or Amount)")
            if amount is not None and amount <= 0:
                row_errors.append(f"Amount must be greater than 0 (got {amount})")
                amount = None
            if orig_amount is None and amount is not None and orig_code is not None:
                orig_amount = amount  # amount-only override

        paid_by = None
        pb_v = get(row, "Paid By")
        if _blank(pb_v):
            row_errors.append("Paid By is required")
        else:
            paid_by = resolve_name(str(pb_v), participants)
            if paid_by is None:
                row_errors.append(f"Paid By {str(pb_v).strip()!r} is not a known participant")

        people: list[str] = []
        try:
            people = parse_participants_cell("" if _blank(get(row, "Participants")) else str(get(row, "Participants")), participants)
            if not people:
                row_errors.append("Participants is empty after exclusions")
        except ValueError as e:
            row_errors.append(str(e))

        date = None
        d_v = get(row, "Date")
        if not _blank(d_v):
            try:
                date = _parse_date(d_v)
            except ValueError as e:
                row_errors.append(str(e))

        if row_errors:
            errors.extend(f"Row {rnum}: {m}" for m in row_errors)
            continue
        txs.append(
            Transaction(
                name=name, amount=amount, participants=people, paid_by=paid_by, date=date,
                original_amount=orig_amount, original_currency=orig_code, rate=rate,
            )
        )
    return txs, errors
