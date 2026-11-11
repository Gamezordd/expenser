import datetime as dt
from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook

from expenser.currency import (
    CurrencyError, collect_rates, convert, normalize_code, parse_rate_args, summarize_currencies,
)
from expenser.excel import read_review_excel, write_review_excel
from expenser.models import Transaction

P = ["Alice", "Bob"]
D = Decimal


def tx(name, amt, cur=None):
    return Transaction(
        name=name, amount=D(amt), participants=P, paid_by="Alice",
        original_amount=D(amt) if cur else None, original_currency=cur,
    )


def test_normalize():
    assert normalize_code(" thb ") == "THB"
    assert normalize_code("฿") == "THB" and normalize_code("₹") == "INR"
    for bad in ("", "US", "EURO", "12$", "$"):
        with pytest.raises(CurrencyError):
            normalize_code(bad)


def test_parse_rate_args():
    assert parse_rate_args(["thb=2.58", "EUR:90,5"]) == {"THB": D("2.58"), "EUR": D("90.5")}
    assert parse_rate_args(None) == {}
    for bad in ("THB", "THB=abc", "THB=0", "THB=-1", "TH=2", "THB=1.1234567"):
        with pytest.raises(CurrencyError):
            parse_rate_args([bad])


def test_collect_rates_prompts_and_reprompts():
    prompts = []
    answers = iter(["abc", "0", "2,583"])

    def inp(p):
        prompts.append(p)
        return next(answers)

    cur = {"THB": (5, D("3450")), "INR": (2, D("10")), "EUR": (1, D("5"))}
    rates = collect_rates(cur, "INR", {"EUR": D("90")}, input_fn=inp, interactive=True)
    assert rates == {"THB": D("2.583"), "EUR": D("90")}
    assert len(prompts) == 3
    assert prompts[0] == "THB detected in 5 transactions (total 3,450.00 THB). Rate: 1 THB = ? INR: "


def test_collect_rates_non_interactive():
    with pytest.raises(CurrencyError, match=r"--rate THB=<rate>"):
        collect_rates({"THB": (1, D("1"))}, "INR", {}, interactive=False)
    assert collect_rates({"INR": (1, D("1"))}, "INR", {}, interactive=False) == {}


def test_convert_rounding():
    out = convert([tx("a", "450", "THB"), tx("b", "7", "INR"), tx("c", "1")], {"THB": D("2.583")}, "INR")
    assert out[0].amount == D("1162.35") and out[0].original_amount == D("450")
    assert out[0].original_currency == "THB" and out[0].rate == D("2.583")
    assert out[1].amount == D("7.00") and out[1].original_currency is None
    assert out[2].amount == D("1.00")
    assert convert([tx("h", "0.5", "THB")], {"THB": D("0.01")}, "INR")[0].amount == D("0.01")  # half up
    with pytest.raises(CurrencyError):
        convert([tx("a", "1", "THB")], {}, "INR")


def test_summarize():
    s = summarize_currencies([tx("a", "10", "THB"), tx("b", "5", "THB"), tx("c", "1")], "INR")
    assert s == {"THB": (2, D("15")), "INR": (1, D("1.00"))}


def _converted(tmp_path):
    txs = convert([tx("Noodles", "450", "THB"), tx("Taxi", "100")], {"THB": D("2.583")}, "INR")
    return write_review_excel(tmp_path / "c.xlsx", txs, P, target_currency="INR", rates={"THB": D("2.583")})


def test_excel_converted_roundtrip(tmp_path):
    x = _converted(tmp_path)
    wb = load_workbook(x)
    ws = wb["Transactions"]
    assert ws["F1"].value == "Amount (INR)" and ws["F2"].value == "=ROUND(C2*E2,2)"
    assert (ws["C2"].value, ws["D2"].value, ws["E2"].value) == (450, "THB", 2.583)
    assert ws["F5"].value.startswith("=SUMIF")
    assert wb["Info"]["D1"].value == "INR"
    txs, errs = read_review_excel(x, P)
    assert not errs
    assert txs[0].amount == D("1162.35") and txs[0].rate == D("2.583") and txs[0].original_currency == "THB"
    assert txs[1].amount == D("100.00") and txs[1].rate == D(1)


def test_excel_rate_edit_and_override(tmp_path):
    x = _converted(tmp_path)
    wb = load_workbook(x)
    wb["Transactions"]["E2"] = 3
    wb["Transactions"]["F3"] = 99.5  # plain number wins
    wb.save(x)
    txs, errs = read_review_excel(x, P)
    assert not errs and txs[0].amount == D("1350.00") and txs[1].amount == D("99.50")


def test_excel_validation(tmp_path):
    x = _converted(tmp_path)
    wb = load_workbook(x)
    ws = wb["Transactions"]
    ws["E2"] = None
    ws["D3"] = "RUPEES"
    wb.save(x)
    _, errs = read_review_excel(x, P)
    text = "\n".join(errs)
    assert "Row 2" in text and "Rate is required" in text and "Row 3" in text and "3-letter" in text
    wb = load_workbook(x)
    wb["Transactions"]["E2"] = -1
    wb.save(x)
    assert any("Rate must be greater than 0" in e for e in read_review_excel(x, P)[1])


def test_old_layout_still_reads(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Date", "Transaction Name", "Amount", "Currency", "Paid By", "Participants", "Include", "Notes"])
    ws.append([dt.date(2026, 3, 4), "Old", 12.5, "EUR", "Alice", "ALL", "Y", ""])
    ws.append([None, "TOTAL", "=SUM(C2)"])
    wb.save(tmp_path / "old.xlsx")
    txs, errs = read_review_excel(tmp_path / "old.xlsx", P)
    assert not errs and txs[0].amount == D("12.50") and txs[0].rate is None
