from decimal import Decimal
from io import StringIO

from rich.console import Console

from expenser.models import Transaction
from expenser.pipeline import ask_dedupe, drop_duplicates, find_duplicates


def t(name, amt="10"):
    return Transaction(name=name, amount=Decimal(amt), participants=["A"], paid_by="A")


def con():
    buf = StringIO()
    return Console(file=buf, width=200, force_terminal=False), buf


def test_find_and_drop():
    txs = [t("x"), t("y"), t("x"), t("z"), t("y"), t("x")]
    d = find_duplicates(txs)
    assert [(tx.name, idx) for tx, idx in d] == [("x", [0, 2, 5]), ("y", [1, 4])]
    assert [x.name for x in drop_duplicates(txs)] == ["x", "y", "z"]
    assert find_duplicates([t("x"), t("y")]) == []


def test_ask_dedupe_no_duplicates_does_not_prompt():
    c, _ = con()
    txs = [t("x"), t("y")]
    assert ask_dedupe(txs, confirm=lambda *a, **k: (_ for _ in ()).throw(AssertionError), console=c) is txs


def test_ask_dedupe_yes_and_no():
    txs = [t("x"), t("x"), t("y")]
    seen = []

    def yes(prompt, default=True):
        seen.append((prompt, default))
        return True

    c, buf = con()
    out = ask_dedupe(txs, confirm=yes, console=c)
    assert [x.name for x in out] == ["x", "y"]
    assert seen == [("Remove the duplicates (keep one of each)?", False)]
    assert "Removed 1 duplicate row(s)" in buf.getvalue() and "rows 1, 2" in buf.getvalue()
    assert "no date" in buf.getvalue()

    c, buf = con()
    assert ask_dedupe(txs, confirm=lambda p, default=False: False, console=c) == txs
    assert "Keeping all rows as they are" in buf.getvalue()
