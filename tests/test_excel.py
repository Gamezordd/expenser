import datetime as dt
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from expenser.excel import COLUMNS, read_review_excel, write_review_excel
from expenser.extract import ExtractionResult
from expenser.models import Transaction

P = ["Alice", "Bob", "Carol"]


def tx(name, amt, parts=None, paid="Alice", date=None):
    return Transaction(name=name, amount=Decimal(amt), participants=parts or list(P), paid_by=paid, date=date)


@pytest.fixture
def xlsx(tmp_path):
    res = ExtractionResult(
        transactions=[tx("Albert Heijn", "12.50", date=dt.date(2026, 3, 4)), tx("Cafe", "8", ["Alice", "Bob"], "Bob")],
        notes={1: "blurry"},
        currency="EUR",
    )
    return write_review_excel(tmp_path / "sub" / "r.xlsx", res, P)


def edit(path, fn):
    wb = load_workbook(path)
    fn(wb["Transactions"])
    wb.save(path)


def test_layout(xlsx):
    wb = load_workbook(xlsx)
    ws = wb["Transactions"]
    assert [c.value for c in ws[1]] == [c.replace("Amount", "Amount (INR)") if c == "Amount" else c for c in COLUMNS]
    assert ws.freeze_panes == "A2"
    assert ws["H2"].value == "ALL" and ws["H3"].value == "Alice, Bob"
    assert ws["J3"].value == "blurry" and ws["D2"].value == "INR" and ws["E2"].value == 1
    assert ws["F2"].value == "=ROUND(C2*E2,2)"
    assert ws["F5"].value.startswith("=SUMIF")
    assert "Info" in wb.sheetnames
    assert len(ws.data_validations.dataValidation) == 2


def test_roundtrip(xlsx):
    txs, errs = read_review_excel(xlsx, P)
    assert errs == []
    assert [t.name for t in txs] == ["Albert Heijn", "Cafe"]  # TOTAL row skipped
    assert txs[0].date == dt.date(2026, 3, 4) and txs[0].participants == P
    assert txs[1].participants == ["Alice", "Bob"] and txs[1].paid_by == "Bob"
    assert txs[0].amount == Decimal("12.50")


def test_include_n(xlsx):
    edit(xlsx, lambda ws: ws.cell(3, 9).__setattr__("value", "N"))
    txs, errs = read_review_excel(xlsx, P)
    assert [t.name for t in txs] == ["Albert Heijn"] and not errs


def test_all_minus_and_comma_decimal_and_dates(xlsx):
    def f(ws):
        ws["H2"] = "ALL -Bob"
        ws["C2"] = "12,50"
        ws["A2"] = "04-03-2026"
        ws["A3"] = "2026-03-05"
    edit(xlsx, f)
    txs, errs = read_review_excel(xlsx, P)
    assert not errs
    assert txs[0].participants == ["Alice", "Carol"] and txs[0].amount == Decimal("12.50")
    assert txs[0].date == dt.date(2026, 3, 4) and txs[1].date == dt.date(2026, 3, 5)


def test_validation_errors(xlsx):
    def f(ws):
        ws["H2"] = "Alice, Zed"
        ws["C2"] = "abc"
        ws["G3"] = ""
        ws["C3"] = -4
        ws["A3"] = "garbage"
    edit(xlsx, f)
    txs, errs = read_review_excel(xlsx, P)
    assert txs == []
    text = "\n".join(errs)
    assert "Row 2" in text and "Zed" in text and "not a number" in text
    assert "Row 3" in text and "Paid By is required" in text and "greater than 0" in text and "date" in text


def test_unknown_payer(xlsx):
    edit(xlsx, lambda ws: ws.__setitem__("G2", "Mallory"))
    _, errs = read_review_excel(xlsx, P)
    assert any("Mallory" in e for e in errs)


def test_stops_at_blank_row(xlsx):
    def f(ws):
        ws["B5"] = None
        ws["C5"] = None
        ws["B7"] = "Ghost"
    edit(xlsx, f)
    txs, errs = read_review_excel(xlsx, P)
    assert len(txs) == 2 and not errs
