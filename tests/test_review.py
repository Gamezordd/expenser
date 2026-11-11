import json
from decimal import Decimal

from openpyxl import load_workbook
from rich.console import Console

from expenser.excel import write_review_excel
from expenser.models import Transaction
from expenser.review import approval_path, is_approved, review_loop

P = ["Alice", "Bob"]


def make(tmp_path):
    t = [Transaction(name="Shop", amount=Decimal("10"), participants=P, paid_by="Alice")]
    return write_review_excel(tmp_path / "r.xlsx", t, P)


def script(answers):
    it = iter(answers)
    return lambda prompt: next(it)


def run(path, answers):
    c = Console(record=True, width=120)
    r = review_loop(path, P, open_file=False, console=c, input_fn=script(answers))
    return r, c.export_text()


def test_approve(tmp_path):
    x = make(tmp_path)
    r, out = run(x, ["a", "y"])
    assert r and r[0].name == "Shop"
    side = json.loads(approval_path(x).read_text())
    assert side["transactions"][0]["name"] == "Shop" and side["sha256"] and side["approved_at"]
    assert is_approved(x)
    wb = load_workbook(x); wb["Transactions"]["C2"] = 11; wb.save(x)
    assert not is_approved(x)


def test_quit_and_decline(tmp_path):
    x = make(tmp_path)
    assert run(x, ["q"])[0] is None
    r, _ = run(x, ["a", "n", "q"])
    assert r is None and not approval_path(x).exists()


def test_errors_then_fix(tmp_path):
    x = make(tmp_path)
    wb = load_workbook(x); wb["Transactions"]["G2"] = "Nobody"; wb.save(x)
    c = Console(record=True, width=120)

    def inp(prompt, _s=iter(["a", "FIX", "a", "y"])):
        v = next(_s)
        if v == "FIX":
            wb = load_workbook(x); wb["Transactions"]["G2"] = "Bob"; wb.save(x)
            return "r"
        return v

    r = review_loop(x, P, open_file=False, console=c, input_fn=inp)
    assert r and r[0].paid_by == "Bob"
    assert "Nobody" in c.export_text()
