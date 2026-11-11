"""Write a blank/example Excel template for ``expenser run --datatable``."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from expenser.datatable import INFO_SHEET, PARTICIPANTS_HEADING, SHEET, TARGET_LABEL

HEADERS = ["Date", "Transaction Name", "Amount", "Currency", "Rate", "Paid By", "Participants", "Include"]
_WIDTHS = [12, 30, 14, 10, 12, 16, 34, 9]


def _instructions(target: str) -> list[str]:
    return [
        "How to fill in the Transactions sheet",
        "",
        "- Keep one expense per row. The first blank row (or a row named TOTAL) ends the table.",
        f"- Amount: a positive number (1234.50) in the row's Currency (not necessarily {target}).",
        f"  Rows in another currency are converted to {target} with the Rate before pushing.",
        "  Text such as 'THB 450' or '₹1,200' also works: the embedded currency is used when the Currency cell is empty.",
        f"- Currency: 3-letter code; empty means {target}.",
        f"- Rate: for a foreign currency, 1 <currency> = <rate> {target}. Leave empty to be asked (or pass --rate THB=2.58).",
        "- Paid By: required, one name.",
        "- Participants: comma-separated names (Alice, Bob), or ALL for everyone.",
        "  Exclusions: 'ALL -Bob' or 'ALL except Bob, Carol'.",
        "- Date: optional, day-first (05/11/2026 is 5 November), or 2026-11-05, or '5 Nov 2026'.",
        "- Include: N skips a row.",
        "- Amounts must be plain values, not formulas.",
    ]


def write_datatable_template(path: Path, participants: list[str] | None, target: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    foreign, foreign_rate = ("THB", Decimal("2.58")) if target != "THB" else ("USD", Decimal("83.00"))
    if participants:
        a = participants[0]
        group = participants[:2]
        excl = participants[-1]
        row1_who = "ALL"
        row2_who = f"ALL -{excl}" if len(participants) > 2 else ", ".join(group)
        payer2 = participants[-1] if len(participants) > 1 else a
    else:
        a, payer2, row1_who, row2_who = "Alice", "Bob", "ALL", "Alice, Bob"

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    # Plain "Amount": values are in each row's Currency. (An "Amount (XXX)" header would read as
    # "already in XXX" and mislead whoever fills it in; the target comes from --target-currency.)
    ws.append(list(HEADERS))
    today = dt.date.today()
    ws.append([today, "Dinner", 1200, target, None, a, row1_who, "Y"])
    ws.append([today, "Taxi", 450, foreign, float(foreign_rate), payer2, row2_who, "Y"])
    for c, w in enumerate(_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="305496")
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    for r in range(2, 202):
        ws.cell(r, 1).number_format = "yyyy-mm-dd"
        ws.cell(r, 3).number_format = "0.00"
        ws.cell(r, 5).number_format = "0.000000"

    incl = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    incl.add("H2:H201")
    ws.add_data_validation(incl)

    info = wb.create_sheet(INFO_SHEET)
    lines = _instructions(target)
    for i, line in enumerate(lines, start=1):
        info.cell(i, 1, line)
    info.cell(1, 1).font = Font(bold=True, size=13)
    info.column_dimensions["A"].width = 110
    # Same spot as the review Excel; the reader takes the target currency from here.
    info.cell(1, 3, TARGET_LABEL).font = Font(bold=True)
    info.cell(1, 4, target)
    if participants:
        list_start = len(lines) + 4
        info.cell(list_start - 2, 1, PARTICIPANTS_HEADING).font = Font(bold=True)
        for i, name in enumerate(participants):
            info.cell(list_start + i, 1, name)
        pay = DataValidation(
            type="list",
            formula1=f"=Info!$A${list_start}:$A${list_start - 1 + len(participants)}",
            allow_blank=False,
        )
        pay.add("F2:F201")
        ws.add_data_validation(pay)
    wb.save(path)
    return path
