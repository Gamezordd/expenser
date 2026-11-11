import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

import expenser.cli as cli
from expenser.extract import ExtractionResult, Usage
from expenser.models import Transaction

runner = CliRunner()
P = ["Alice", "Bob"]


class FakeResult:
    def __init__(self, tx, status="pushed", error=None):
        self.transaction, self.status, self.error, self.screenshot = tx, status, error, None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "p.txt").write_text("Alice\nBob\n")
    (tmp_path / "flow.yaml").write_text("name: x\n")
    Image.new("RGB", (20, 20)).save(tmp_path / "s.png")
    calls = {}

    def fake_extract(images, participants, **kw):
        calls["extract"] = (images, participants, kw)
        return ExtractionResult(
            transactions=[Transaction(name="Shop", amount=Decimal("5"), participants=participants, paid_by=kw["payer"],
                                      original_amount=Decimal("5"), original_currency="INR")],
            currency="EUR", usage=Usage(10, 5),
        )

    monkeypatch.setattr(cli, "extract_transactions", fake_extract)
    monkeypatch.setattr(
        cli.auth_mod, "describe_credentials", lambda: cli.auth_mod.CredentialStatus("api_key", "test", True)
    )

    fail = {"on": False}

    def fake_push(flow, txs, **kw):
        calls["push"] = (flow, txs, kw)
        return [FakeResult(t, "failed" if fail["on"] else "pushed", "boom" if fail["on"] else None) for t in txs]

    mod = types.ModuleType("expenser.automation.runner")
    mod.push_transactions = fake_push
    monkeypatch.setitem(sys.modules, "expenser.automation.runner", mod)
    calls["fail"] = fail
    return tmp_path, calls


def test_extract_no_review(env):
    tmp, calls = env
    r = runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "o/x.xlsx"])
    assert r.exit_code == 0, r.output
    assert (tmp / "o" / "x.xlsx").exists()
    assert calls["extract"][2]["payer"] == "Alice" and "defaulting" in r.output


def test_review_command(env):
    tmp, _ = env
    runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "x.xlsx"])
    r = runner.invoke(cli.app, ["review", "x.xlsx", "-p", "p.txt", "--no-open"], input="a\ny\n")
    assert r.exit_code == 0, r.output
    assert (tmp / "x.xlsx.approved.json").exists()
    r = runner.invoke(cli.app, ["review", "x.xlsx", "-p", "p.txt", "--no-open"], input="q\n")
    assert r.exit_code == 1


def test_push_requires_review_then_pushes(env):
    tmp, calls = env
    runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "x.xlsx"])
    args = ["push", "x.xlsx", "-p", "p.txt", "--flow", "flow.yaml", "--var", "group=Trip", "--no-open", "--yes"]
    r = runner.invoke(cli.app, args, input="a\ny\n")
    assert r.exit_code == 0, r.output
    assert "starting review" in r.output
    assert calls["push"][2]["variables"] == {"group": "Trip"}
    assert calls["push"][2]["all_participants"] == P
    # second time: approval hash matches, no review prompt
    calls.pop("push")
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 0 and "matching approval" in r.output and "push" in calls


def test_push_failure_exit_code(env):
    _, calls = env
    calls["fail"]["on"] = True
    runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "x.xlsx"])
    r = runner.invoke(cli.app, ["push", "x.xlsx", "-p", "p.txt", "--flow", "flow.yaml", "--no-open", "-y"], input="a\ny\n")
    assert r.exit_code == 1 and "boom" in r.output


def test_bad_var(env):
    runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "x.xlsx"])
    r = runner.invoke(cli.app, ["push", "x.xlsx", "-p", "p.txt", "--flow", "flow.yaml", "--var", "oops", "-y"])
    assert r.exit_code == 2 and "key=value" in r.output


def test_run_pipeline(env):
    tmp, calls = env
    r = runner.invoke(
        cli.app, ["run", "s.png", "-p", "p.txt", "--flow", "flow.yaml", "--payer", "bob", "--dry-run", "--no-open"],
        input="a\ny\n",
    )
    assert r.exit_code == 0, r.output
    assert calls["extract"][2]["payer"] == "Bob"
    assert calls["push"][2]["dry_run"] is True and len(calls["push"][1]) == 1
    import os

    assert list(Path(os.environ["EXPENSER_OUTPUT_DIR"]).glob("expenses-*.xlsx"))


def _thb_extract(monkeypatch):
    def fake(images, participants, **kw):
        txs = [
            Transaction(name="Noodles", amount=Decimal("450"), participants=participants, paid_by="Alice",
                        original_amount=Decimal("450"), original_currency="THB"),
            Transaction(name="Taxi", amount=Decimal("100"), participants=participants, paid_by="Alice",
                        original_amount=Decimal("100"), original_currency="INR"),
        ]
        return ExtractionResult(transactions=txs, currency="THB", usage=Usage(1, 1))

    monkeypatch.setattr(cli, "extract_transactions", fake)


def test_extract_with_rate_option(env, monkeypatch):
    from openpyxl import load_workbook

    tmp, _ = env
    _thb_extract(monkeypatch)
    r = runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "x.xlsx", "--rate", "THB=2.5"])
    assert r.exit_code == 0, r.output
    assert "THB" in r.output and "2.5" in r.output
    ws = load_workbook(tmp / "x.xlsx")["Transactions"]
    assert ws["F1"].value == "Amount (INR)" and ws["F2"].value == "=ROUND(C2*E2,2)" and ws["E2"].value == 2.5
    from expenser.excel import read_review_excel

    txs, errs = read_review_excel(tmp / "x.xlsx", P)
    assert not errs and txs[0].amount == Decimal("1125.00") and txs[1].amount == Decimal("100.00")


def test_extract_prompts_for_rate(env, monkeypatch):
    tmp, _ = env
    _thb_extract(monkeypatch)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    r = runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "x.xlsx"], input="nope\n2,5\n")
    assert r.exit_code == 0, r.output
    assert "1 THB = ? INR" in r.output and "Invalid rate" in r.output
    from expenser.excel import read_review_excel

    assert read_review_excel(tmp / "x.xlsx", P)[0][0].amount == Decimal("1125.00")


def _unknown_currency_extract(monkeypatch):
    def fake(images, participants, **kw):
        txs = [Transaction(name="Grab", amount=Decimal("450"), participants=participants, paid_by="Alice")]
        return ExtractionResult(transactions=txs, notes={0: "Unrecognised currency 'B'"}, usage=Usage(1, 1))

    monkeypatch.setattr(cli, "extract_transactions", fake)


def test_unknown_currency_is_never_silently_target(env, monkeypatch):
    _unknown_currency_extract(monkeypatch)
    r = runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "x.xlsx"])
    assert r.exit_code == 2 and "Could not determine the currency" in r.output and "'Grab'" in r.output


def test_unknown_currency_is_asked_then_converted(env, monkeypatch):
    tmp, _ = env
    _unknown_currency_extract(monkeypatch)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    # bad code -> re-ask; THB -> then the THB rate prompt
    r = runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "x.xlsx"],
                      input="baht!\nTHB\n2.5\n")
    assert r.exit_code == 0, r.output
    assert "Currency for 'Grab'" in r.output and "1 THB = ? INR" in r.output
    from expenser.excel import read_review_excel

    tx = read_review_excel(tmp / "x.xlsx", P)[0][0]
    assert tx.original_currency == "THB" and tx.amount == Decimal("1125.00")


def test_extract_missing_rate_non_interactive(env, monkeypatch):
    _thb_extract(monkeypatch)
    r = runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "x.xlsx"])
    assert r.exit_code == 2 and "No conversion rate for THB" in r.output


def test_bad_rate_and_source_passthrough(env):
    _, calls = env
    r = runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--rate", "THB"])
    assert r.exit_code == 2 and "Malformed" in r.output
    r = runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "y.xlsx", "--source-currency", "eur"])
    assert r.exit_code == 0, r.output
    assert calls["extract"][2]["source_currency"] == "EUR"


# --- datatable mode -------------------------------------------------------------------
def _table(path, rows, header=("Transaction Name", "Amount", "Currency", "Paid By", "Participants")):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    wb.save(path)
    return path


def _no_auth(monkeypatch):
    monkeypatch.setattr(
        cli.auth_mod, "describe_credentials", lambda: cli.auth_mod.CredentialStatus("none", "nope", False)
    )


def test_run_datatable_pushes_without_claude_or_auth(env, monkeypatch):
    tmp, calls = env
    _no_auth(monkeypatch)
    _table(tmp / "e.xlsx", [("Dinner", 100, "INR", "Alice", "ALL"), ("Taxi", 10, None, "Bob", "Bob")])
    r = runner.invoke(cli.app, ["run", "--datatable", "e.xlsx", "--flow", "flow.yaml", "--yes", "--var", "g=T"])
    assert r.exit_code == 0, r.output
    assert "extract" not in calls
    flow, txs, kw = calls["push"]
    assert [t.name for t in txs] == ["Dinner", "Taxi"] and txs[0].participants == ["Alice", "Bob"]
    assert kw["all_participants"] == ["Alice", "Bob"] and kw["variables"] == {"g": "T"}
    assert "derived from table" in r.output


def test_run_datatable_with_participants_file_and_confirm(env):
    tmp, calls = env
    _table(tmp / "e.xlsx", [("Dinner", 100, "INR", "alice", "ALL")])
    r = runner.invoke(cli.app, ["run", "--datatable", "e.xlsx", "-p", "p.txt", "--flow", "flow.yaml"], input="n\n")
    assert r.exit_code == 1 and "push" not in calls and "totalling 100.00 INR" in r.output and "participants file" in r.output
    r = runner.invoke(cli.app, ["run", "--datatable", "e.xlsx", "-p", "p.txt", "--flow", "flow.yaml"], input="y\n")
    assert r.exit_code == 0 and calls["push"][1][0].paid_by == "Alice"


def test_run_usage_errors(env):
    tmp, calls = env
    _table(tmp / "e.xlsx", [("Dinner", 100, "INR", "Alice", "ALL")])
    r = runner.invoke(cli.app, ["run", "s.png", "--datatable", "e.xlsx", "--flow", "flow.yaml"])
    assert r.exit_code == 2
    r = runner.invoke(cli.app, ["run", "--flow", "flow.yaml"])
    assert r.exit_code == 2
    r = runner.invoke(cli.app, ["run", "s.png", "--flow", "flow.yaml"])
    assert r.exit_code == 2 and "extract" not in calls


def test_run_datatable_errors_exit_1(env):
    tmp, calls = env
    _table(tmp / "e.xlsx", [("Dinner", 100, "INR", "Zed", "ALL"), ("Bad", "x", "INR", "Alice", "ALL")])
    r = runner.invoke(cli.app, ["run", "--datatable", "e.xlsx", "-p", "p.txt", "--flow", "flow.yaml", "--yes"])
    assert r.exit_code == 1 and "push" not in calls
    assert "Row 2" in r.output and "Row 3" in r.output


def test_run_datatable_foreign_prompt_and_rate_option(env, monkeypatch):
    tmp, calls = env
    _table(tmp / "e.xlsx", [("Dinner", 100, "INR", "Alice", "ALL"), ("Taxi", 10, "THB", "Alice", "ALL")])
    base = ["run", "--datatable", "e.xlsx", "--flow", "flow.yaml", "--yes"]
    r = runner.invoke(cli.app, base)  # non-interactive, no rate
    assert r.exit_code == 2 and "THB" in r.output and "push" not in calls
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    r = runner.invoke(cli.app, base, input="nope\n2,5\n")
    assert r.exit_code == 0, r.output
    assert calls["push"][1][1].amount == Decimal("25.00") and calls["push"][1][1].original_currency == "THB"
    calls.pop("push")
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    r = runner.invoke(cli.app, base + ["--rate", "THB=3"])
    assert r.exit_code == 0 and calls["push"][1][1].amount == Decimal("30.00")
    r = runner.invoke(cli.app, base + ["--source-currency", "THB", "--rate", "THB=2"])
    assert [t.amount for t in calls["push"][1]] == [Decimal("200.00"), Decimal("20.00")]


def test_push_without_participants_file(env):
    tmp, calls = env
    runner.invoke(cli.app, ["extract", "s.png", "-p", "p.txt", "--no-review", "--out", "x.xlsx"])
    r = runner.invoke(cli.app, ["push", "x.xlsx", "--flow", "flow.yaml", "--no-open", "-y"], input="a\ny\n")
    assert r.exit_code == 0, r.output
    assert calls["push"][2]["all_participants"] == ["Alice", "Bob"] and "Info sheet" in r.output


def test_datatable_template_roundtrip(env):
    from expenser.datatable import read_datatable

    tmp, _ = env
    r = runner.invoke(cli.app, ["datatable-template", "t.xlsx"])
    assert r.exit_code == 0, r.output
    res = read_datatable(tmp / "t.xlsx", rates={})
    assert res.errors == [] and len(res.transactions) == 2 and res.members == ["Alice", "Bob"]
    r = runner.invoke(cli.app, ["datatable-template", "t.xlsx"])
    assert r.exit_code != 0 and "--force" in r.output
    r = runner.invoke(cli.app, ["datatable-template", "u.xlsx", "-p", "p.txt", "--target-currency", "EUR"])
    assert r.exit_code == 0, r.output
    res = read_datatable(tmp / "u.xlsx")
    assert res.errors == [] and len(res.transactions) == 2
    assert res.members_source == "info" and res.members == ["Alice", "Bob"] and res.target_currency == "EUR"
    assert runner.invoke(cli.app, ["datatable-template", "u.xlsx", "--force"]).exit_code == 0


# --- duplicate rows ---------------------------------------------------------------------
def _dup_table(tmp):
    _table(tmp / "d.xlsx", [("Fun", 20, "INR", "Alice", "Alice")] * 2 + [("Taxi", 10, "INR", "Bob", "Bob")])
    return ["run", "--datatable", "d.xlsx", "--flow", "flow.yaml", "--yes"]


def test_dedupe_flag_removes(env):
    tmp, calls = env
    r = runner.invoke(cli.app, _dup_table(tmp) + ["--dedupe"])
    assert r.exit_code == 0, r.output
    assert [t.name for t in calls["push"][1]] == ["Fun", "Taxi"] and "Removed 1 duplicate row(s)" in r.output


def test_keep_duplicates_flag_keeps(env):
    tmp, calls = env
    r = runner.invoke(cli.app, _dup_table(tmp) + ["--keep-duplicates"])
    assert r.exit_code == 0, r.output
    assert [t.name for t in calls["push"][1]] == ["Fun", "Fun", "Taxi"] and "Note:" not in r.output


def test_dedupe_and_keep_together_is_usage_error(env):
    tmp, calls = env
    r = runner.invoke(cli.app, _dup_table(tmp) + ["--dedupe", "--keep-duplicates"])
    assert r.exit_code == 2 and "push" not in calls
    r = runner.invoke(cli.app, ["push", "d.xlsx", "--dedupe", "--keep-duplicates"])
    assert r.exit_code == 2


def test_non_interactive_default_keeps_with_note(env, monkeypatch):
    tmp, calls = env
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    r = runner.invoke(cli.app, _dup_table(tmp))
    assert r.exit_code == 0, r.output
    assert len(calls["push"][1]) == 3
    assert "1 duplicate row(s) are kept" in r.output and "--dedupe" in r.output


def test_interactive_prompt_yes_and_no(env, monkeypatch):
    tmp, calls = env
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    args = _dup_table(tmp)[:-1]  # no --yes: also asked to confirm the push
    r = runner.invoke(cli.app, args, input="y\ny\n")
    assert r.exit_code == 0, r.output
    assert "Remove the duplicates (keep one of each)?" in r.output and "rows 1, 2" in r.output
    assert "Push 2 transactions" in r.output and len(calls["push"][1]) == 2
    r = runner.invoke(cli.app, args, input="\ny\n")  # default No
    assert r.exit_code == 0, r.output
    assert "Keeping all rows as they are" in r.output and len(calls["push"][1]) == 3


def test_push_command_dedupe(env, monkeypatch):
    tmp, calls = env
    _dup_table(tmp)
    monkeypatch.setattr(cli, "is_approved", lambda p: True)
    monkeypatch.setattr(cli, "read_review_excel", lambda p, people: (
        [Transaction(name="Fun", amount=Decimal("20"), participants=["Alice"], paid_by="Alice")] * 2, []))
    r = runner.invoke(cli.app, ["push", "d.xlsx", "-p", "p.txt", "--flow", "flow.yaml", "--yes", "--dedupe"])
    assert r.exit_code == 0, r.output
    assert len(calls["push"][1]) == 1
