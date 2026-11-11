import datetime as dt
from decimal import Decimal

import pytest
from openpyxl import Workbook

from expenser.datatable import DatatableError, apply_rates, parse_amount_with_currency, parse_date, read_datatable
from expenser.excel import read_review_excel, write_review_excel
from expenser.models import Transaction

D = Decimal


def make(path, rows, sheet="Transactions", extra=None):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    if extra:
        extra(wb)
    wb.save(path)
    return path


H = ["Transaction Name", "Amount", "Paid By", "Participants"]


def test_aliases_order_and_title_row(tmp_path):
    p = make(tmp_path / "a.xlsx", [
        ["My trip expenses"], [],
        ["Who Paid", "For Whom", "Expense", "Amount (THB)", "Transaction Date"],
        ["Alice", "ALL", "Dinner", 450, "05/11/2026"],
        ["Bob", "Alice", "Taxi", "1,200.50", dt.datetime(2026, 11, 6)],
    ])
    r = read_datatable(p)
    assert r.errors == [] and r.target_currency == "THB"
    assert [t.name for t in r.transactions] == ["Dinner", "Taxi"]
    assert r.transactions[0].date == dt.date(2026, 11, 5)  # day-first
    assert r.transactions[1].amount == D("1200.50") and r.transactions[1].participants == ["Alice"]
    assert r.transactions[0].original_currency is None


def test_missing_columns_single_error(tmp_path):
    p = make(tmp_path / "a.xlsx", [["Name", "Amount"], ["x", 1]])
    r = read_datatable(p)
    assert len(r.errors) == 1 and "Paid By" in r.errors[0] and "Participants" in r.errors[0]
    p = make(tmp_path / "b.xlsx", [["foo", "bar"], [1, 2]])
    r = read_datatable(p)
    assert len(r.errors) == 1 and "header" in r.errors[0]


def test_csv_and_total_stop(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("Description,Amount,Payer,Split\nLunch,10,Al,ALL\nTOTAL,10,,\nAfter,5,Al,ALL\n")
    r = read_datatable(p, target_currency="eur")
    assert r.errors == [] and len(r.transactions) == 1 and r.target_currency == "EUR"
    p.write_text("Description;Amount;Payer;Split\nLunch;10,50;Al;ALL\n;;;\nAfter;5;Al;ALL\n")
    r = read_datatable(p)
    assert len(r.transactions) == 1 and r.transactions[0].amount == D("10.50")


def test_unsupported_and_missing(tmp_path):
    with pytest.raises(DatatableError):
        read_datatable(tmp_path / "nope.xlsx")
    (tmp_path / "x.json").write_text("{}")
    with pytest.raises(DatatableError):
        read_datatable(tmp_path / "x.json")


@pytest.mark.parametrize("raw,amt,code", [
    ("1,234.50", "1234.50", None), ("1234,50", "1234.50", None), ("1.234,50", "1234.50", None),
    ("₹1,200", "1200", "INR"), ("THB 450", "450", "THB"), ("450 thb", "450", "THB"),
    ("1,234", "1234", None), (12.5, "12.5", None),
])
def test_amount_variants(raw, amt, code):
    assert parse_amount_with_currency(raw) == (D(amt), code)


@pytest.mark.parametrize("raw,exp", [
    ("2026-11-05", dt.date(2026, 11, 5)), ("05-11-2026", dt.date(2026, 11, 5)), ("5/11/2026", dt.date(2026, 11, 5)),
    ("5 Nov 2026", dt.date(2026, 11, 5)), ("Nov 5, 2026", dt.date(2026, 11, 5)), ("5 November 2026", dt.date(2026, 11, 5)),
])
def test_date_variants(raw, exp):
    assert parse_date(raw) == exp


def test_embedded_currency_and_bad_values(tmp_path):
    p = make(tmp_path / "a.xlsx", [H, ["Taxi", "THB 450", "A", "ALL"], ["Bad", "abc", "A", "ALL"],
                                   ["Zero", 0, "A", "ALL"], ["NoPay", 5, None, "ALL"], ["NoWho", 5, "A", None]])
    r = read_datatable(p, target_currency="INR")
    assert r.currencies_needing_rates == {"THB": (1, D("450"))}
    assert len(r.errors) == 4 and all(e.startswith("Row ") for e in r.errors)
    assert r.errors[0].startswith("Row 3")


def test_include_and_skip(tmp_path):
    p = make(tmp_path / "a.xlsx", [H + ["Include"], ["a", 1, "A", "ALL", "N"], ["b", 2, "A", "ALL", "Y"], ["c", 3, "A", "ALL", None]])
    assert [t.name for t in read_datatable(p).transactions] == ["b", "c"]
    p = make(tmp_path / "b.xlsx", [H + ["Skip"], ["a", 1, "A", "ALL", "Y"], ["b", 2, "A", "ALL", "N"]])
    assert [t.name for t in read_datatable(p).transactions] == ["b"]


def test_derived_members(tmp_path):
    p = make(tmp_path / "a.xlsx", [H, ["a", 1, "Alice", "Alice, Bob"], ["b", 2, "bob", "ALL -Carol"],
                                   ["c", 3, "Carol", "ALL -Bob"], ["d", 4, " Dave ", "ALL"]])
    r = read_datatable(p)
    assert r.errors == [] and r.members_source == "derived"
    assert r.members == ["Alice", "Bob", "Carol", "Dave"]
    t = r.transactions
    assert t[1].paid_by == "bob" and t[1].participants == ["Alice", "Bob", "Dave"]
    assert t[2].participants == ["Alice", "Carol", "Dave"]
    assert t[3].participants == r.members and t[3].paid_by == "Dave"


def test_given_participants_unknown_name(tmp_path):
    p = make(tmp_path / "a.xlsx", [H, ["a", 1, "ali", "ALL"], ["b", 2, "Zed", "Bob"], ["c", 3, "Bob", "Yan"]])
    r = read_datatable(p, participants=["Alice", "Bob"])
    assert r.members_source == "given" and r.transactions[0].paid_by == "Alice"
    assert len(r.errors) == 2 and "Row 3" in r.errors[0] and "Row 4" in r.errors[1]


def test_review_layout_matches_read_review_excel_and_info_members(tmp_path):
    people = ["Alice", "Bob", "Carol"]
    txs = [
        Transaction(name="Shop", amount=D("50.00"), participants=people, paid_by="Alice", date=dt.date(2026, 1, 2)),
        Transaction(name="Taxi", amount=D("116.10"), participants=["Bob", "Carol"], paid_by="Bob",
                    original_amount=D("45"), original_currency="THB", rate=D("2.58")),
    ]
    p = write_review_excel(tmp_path / "r.xlsx", txs, people, target_currency="INR", rates={"THB": D("2.58")})
    expected, errs = read_review_excel(p, people)
    assert errs == []
    r = read_datatable(p)
    assert r.errors == [] and r.members_source == "info" and r.members == people
    assert [t.model_dump() for t in r.transactions] == [t.model_dump() for t in expected]
    r2 = read_datatable(p, participants=people)
    assert r2.members_source == "given"
    assert [t.model_dump() for t in r2.transactions] == [t.model_dump() for t in expected]


def test_currency_rate_column_rates_arg_and_missing(tmp_path):
    rows = [["Name", "Amount", "Currency", "Rate", "Paid By", "Participants"],
            ["a", 100, "THB", 2.58, "A", "ALL"],
            ["b", 100, "EUR", None, "A", "ALL"],
            ["c", 100, "USD", None, "A", "ALL"],
            ["d", 50, "INR", 9, "A", "ALL"]]
    p = make(tmp_path / "a.xlsx", rows)
    r = read_datatable(p, target_currency="INR", rates={"EUR": D("90")})
    assert r.errors == []
    t = r.transactions
    assert (t[0].amount, t[0].original_currency, t[0].rate) == (D("258.00"), "THB", D("2.58"))
    assert t[1].amount == D("9000.00")
    assert r.currencies_needing_rates == {"USD": (1, D("100"))} and r.pending == [2]
    assert t[3].amount == D("50.00") and t[3].original_currency is None
    out = apply_rates(r, {"USD": D("83.333")})
    assert out[2].amount == D("8333.30") and out[0].amount == D("258.00")


def test_force_currency(tmp_path):
    p = make(tmp_path / "a.xlsx", [H, ["a", 10, "A", "ALL"]])
    r = read_datatable(p, force_currency="thb", target_currency="INR")
    assert r.currencies_needing_rates == {"THB": (1, D("10"))}


def test_bare_dollar_is_rejected_not_treated_as_target(tmp_path):
    # "$12" must never silently become 12 INR.
    p = make(tmp_path / "d.xlsx", [H, ["Lunch", "$12", "Alice", "Alice, Bob"]])
    r = read_datatable(p)
    assert r.transactions == [] and len(r.errors) == 1
    assert "ambiguous" in r.errors[0] and "Currency column" in r.errors[0] and "Row 2" in r.errors[0]


def test_dollar_with_currency_column_is_fine(tmp_path):
    p = make(tmp_path / "d.xlsx", [H + ["Currency", "Rate"], ["Lunch", "$12", "Alice", "Alice, Bob", "USD", 83]])
    r = read_datatable(p)
    assert r.errors == []
    t = r.transactions[0]
    assert t.original_currency == "USD" and t.amount == D("996.00")


def test_template_amount_header_is_plain(tmp_path):
    from expenser.datatable_template import write_datatable_template
    from openpyxl import load_workbook

    p = write_datatable_template(tmp_path / "t.xlsx", ["Alice", "Bob", "Carol"], "INR")
    assert load_workbook(p)["Transactions"]["C1"].value == "Amount"
    r = read_datatable(p)
    assert r.errors == [] and r.target_currency == "INR"
    assert [str(t.amount) for t in r.transactions] == ["1200.00", "1161.00"]
