"""The interactive wizard, driven headless with scripted answers against local fixture pages."""
from __future__ import annotations

from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
from openpyxl import load_workbook
from PIL import Image
from rich.console import Console
from typer.testing import CliRunner

import expenser.cli as cli
from expenser import wizard
from expenser.auth import CredentialStatus
from expenser.automation.runner import LoginRequired
from expenser.datatable_template import write_datatable_template
from expenser.extract import ExtractionResult, Usage
from expenser.models import Transaction
from expenser.wizard import UI, WizardError, WizardQuit

FIXTURES = Path(__file__).parent / "fixtures"
FLOW = FIXTURES / "mock_flow_stages.yaml"
APP_URL = (FIXTURES / "mock_expense_app.html").as_uri()
LISTS_URL = (FIXTURES / "mock_lists.html").as_uri()
UUID2 = "22222222-bbbb-4bbb-8bbb-222222222222"
UUID3 = "33333333-cccc-4ccc-8ccc-333333333333"
VARS = {"app_url": APP_URL, "lists_url": LISTS_URL}
PEOPLE = ["Alice", "Bob", "Carol"]


class ScriptedUI(UI):
    """Answers prompts from a list. Entries may be callables (side effect, then return the answer)."""

    def __init__(self, answers, files=()):
        self.answers = list(answers)
        self.files = list(files)
        self.prompts: list[str] = []
        self.opened: list[Path] = []
        self.buf = StringIO()
        super().__init__(
            console=Console(file=self.buf, width=200, force_terminal=False),
            input_fn=self._input, choose_files_fn=self._choose, open_fn=self.opened.append,
        )

    def _input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"script exhausted; wizard asked: {prompt!r}\n{self.output}")
        ans = self.answers.pop(0)
        return ans() if callable(ans) else ans

    def _choose(self, kind, multiple):
        assert self.files, f"no file scripted for {kind}"
        got = self.files.pop(0)
        return [] if got is None else (list(got) if isinstance(got, (list, tuple)) else [got])

    @property
    def output(self) -> str:
        return self.buf.getvalue()


@pytest.fixture
def session(tmp_path):
    """Run the wizard and capture window.__expenses from the page right before it closes."""
    captured: dict = {}

    def go(ui, variables=VARS, **kw):
        def grab(page):
            captured["expenses"] = page.evaluate("window.__expenses")
            captured["url"] = page.url

        result = wizard.run(
            ui=ui, flow=FLOW, variables=variables, headless=True, profile_dir=tmp_path / "profile",
            state_dir=tmp_path / "state", output_dir=tmp_path / "out", before_close=grab, **kw,
        )
        return result, captured

    return go


def table_file(tmp_path, rows=None, name="e.xlsx"):
    p = tmp_path / name
    write_datatable_template(p, PEOPLE, "INR")
    if rows is not None:
        wb = load_workbook(p)
        ws = wb["Transactions"]
        ws.delete_rows(2, ws.max_row)
        for r in rows:
            ws.append(r)
        wb.save(p)
    return p


# ---- end to end: Excel -> dry run -> real push -------------------------------------- #

def test_wizard_excel_dry_run_then_real_push(session, tmp_path):
    f = table_file(tmp_path)
    ui = ScriptedUI(
        ["2",      # group: Flat
         "1",      # source: Excel
         "3",      # dry run
         "y",      # push for real now?
         "2",      # save all
         "3"],     # next: quit
        files=[[f]],
    )
    result, cap = session(ui)
    assert "Members of Flat: Alice, Bob, Carol  (you: Bob)" in ui.output
    assert "Group: Flat" in ui.output
    assert [r.status for r in result.results] == ["dry_run", "dry_run", "pushed", "pushed"]
    got = cap["expenses"]
    assert [e["title"] for e in got] == ["Dinner", "Taxi"]
    assert got[0]["participants"] == PEOPLE and got[0]["paid_by"] == "Alice"
    assert got[1]["participants"] == ["Alice", "Bob"] and got[1]["paid_by"] == "Carol"
    assert got[1]["amount"] == 1161.0  # 450 THB * 2.58 (rate from the file)
    assert result.groups == ["Flat"]
    assert "Goodbye" in ui.output


def test_wizard_group_two_ledger_scope_and_add_more_and_choose_another(session, tmp_path):
    f = table_file(tmp_path)
    ui = ScriptedUI(
        ["1", "1", "2",         # group Thai Trip, Excel, save all  -> pushed
         "1",                   # add more to this group
         "1", "2",              # Excel again, save all -> duplicates
         "2",                   # choose another group
         "3", "1", "2",         # group Weekend away, Excel, save all -> pushed (other scope)
         "3"],                  # quit
        files=[[f], [f], [f]],
    )
    result, _ = session(ui)
    assert [r.status for r in result.results] == (
        ["pushed"] * 2 + ["skipped_duplicate"] * 2 + ["pushed"] * 2
    )
    assert result.groups == ["Thai Trip", "Weekend away"]


def test_wizard_file_errors_then_fixed_in_place(session, tmp_path):
    f = table_file(tmp_path, [[None, "Lunch", 30, "INR", None, "Zed", "ALL", "Y"]])

    def fix():
        write_datatable_template(f, PEOPLE, "INR")
        wb = load_workbook(f)
        ws = wb["Transactions"]
        ws.delete_rows(2, ws.max_row)
        ws.append([None, "Lunch", 30, "INR", None, "Bob", "ALL", "Y"])
        wb.save(f)
        return "1"  # "I fixed the file - read it again"

    ui = ScriptedUI(["1", "1", fix, "2", "3"], files=[[f]])
    result, cap = session(ui)
    assert "problem(s)" in ui.output and "Zed" in ui.output
    assert [e["title"] for e in cap["expenses"]] == ["Lunch"]


def test_wizard_cancel_at_push_menu_pushes_nothing(session, tmp_path):
    f = table_file(tmp_path)
    ui = ScriptedUI(["1", "1", "4", "3"], files=[[f]])
    result, cap = session(ui)
    assert result.results == [] and cap["expenses"] == []


# ---- screenshots ---------------------------------------------------------------------- #

def test_screenshots_without_credentials_offers_excel(session, tmp_path, monkeypatch):
    monkeypatch.setattr(wizard.auth, "describe_credentials", lambda: CredentialStatus("none", "no creds", False))
    monkeypatch.setattr(wizard.auth, "find_ant", lambda: None)
    monkeypatch.setattr(wizard, "extract_transactions", lambda *a, **k: pytest.fail("must not call Claude"))
    f = table_file(tmp_path)
    ui = ScriptedUI(
        ["1",   # group
         "2",   # screenshots
         "1",   # sign in now -> no `ant` -> install help, re-check, still not ok
         "2",   # use an Excel file instead
         "2",   # save all
         "3"],  # quit
        files=[[f]],
    )
    result, cap = session(ui)
    out = ui.output
    assert "billed per use" in out and "claude.ai" in out
    assert "anthropics/tap/ant" in out  # ANT_INSTALL_HELP
    assert [e["title"] for e in cap["expenses"]] == ["Dinner", "Taxi"]


def test_screenshots_sign_in_runs_ant_and_rechecks(session, tmp_path, monkeypatch):
    state = {"ok": False}
    monkeypatch.setattr(wizard.auth, "describe_credentials",
                        lambda: CredentialStatus("profile" if state["ok"] else "none", "x", state["ok"]))
    monkeypatch.setattr(wizard.auth, "find_ant", lambda: "/usr/bin/ant")
    ran = []

    def fake_run(args):
        ran.append(args)
        state["ok"] = True
        return 0

    monkeypatch.setattr(wizard.auth, "run_ant", fake_run)
    seen = {}

    def fake_extract(images, members, **kw):
        seen.update(images=images, members=members, **kw)
        t = Transaction(name="Shop", amount=Decimal("40"), participants=["Alice", "Bob"], paid_by=kw["payer"],
                        original_amount=Decimal("40"), original_currency="INR")
        return ExtractionResult(transactions=[t], usage=Usage(1, 1))

    monkeypatch.setattr(wizard, "extract_transactions", fake_extract)
    img = tmp_path / "s.png"
    Image.new("RGB", (10, 10)).save(img)
    ui = ScriptedUI(
        ["1", "2",
         "1",             # sign in now
         "split evenly",  # hint
         "a", "y",        # review: approve
         "2",             # save all
         "3"],
        files=[[img]],
    )
    result, cap = session(ui)
    assert ran == [["auth", "login"]]
    assert seen["payer"] == "Bob" and seen["hint"] == "split evenly" and seen["members"] == PEOPLE
    assert [e["title"] for e in cap["expenses"]] == ["Shop"]
    assert cap["expenses"][0]["paid_by"] == "Bob"
    saved = list((tmp_path / "out").glob("Thai-Trip-*.xlsx"))
    assert len(saved) == 1 and ui.opened == saved


# ---- template ------------------------------------------------------------------------- #

def test_template_source_user_edits_between_prompts(session, tmp_path):
    def user_edits():
        path = ui.opened[0]
        wb = load_workbook(path)
        info = wb["Info"]
        names = [c.value for c in info["A"] if c.value in PEOPLE]
        assert names[0] == "Bob"  # me first
        ws = wb["Transactions"]
        ws.delete_rows(2, ws.max_row)
        ws.append([None, "Lunch", 30, "INR", None, "Bob", "Alice, Bob", "Y"])
        wb.save(path)
        return ""

    ui = ScriptedUI(["3", "3", user_edits, "2", "3"])
    # group 3 (Weekend away), source 3 (template), <edit + Enter>, save all, quit
    result, cap = session(ui)
    (path,) = ui.opened
    assert path == tmp_path / "out" / "Weekend-away-template.xlsx"
    assert [e["title"] for e in cap["expenses"]] == ["Lunch"]
    assert cap["expenses"][0]["participants"] == ["Alice", "Bob"]


def test_template_overwrite_prompt_and_cancel(session, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "Flat-template.xlsx").write_text("old")
    ui = ScriptedUI(
        ["2", "3",      # group Flat, template
         "2", "mine",   # exists -> new name "mine"
         "q",           # cancel at "Fill it in"
         "4",           # back from the source menu
         "3"],          # quit
    )
    session(ui)
    assert (out / "Flat-template.xlsx").read_text() == "old"
    assert (out / "mine.xlsx").exists() and ui.opened == [out / "mine.xlsx"]


# ---- group / member fallbacks ---------------------------------------------------------- #

def test_no_groups_found_manual_url_entry(session, tmp_path):
    empty = tmp_path / "empty.html"
    empty.write_text("<header data-testid='group-header'>x</header>")
    ui = ScriptedUI([
        "1",                                                   # try again (still nothing)
        "2", f"https://app.test/lists/{UUID3}/transactions",   # manual
        "4",                                                   # back from source menu
        "3",
    ])
    result, cap = session(ui, variables={**VARS, "lists_url": empty.as_uri()})
    assert "could not find any groups" in ui.output
    assert f"list={UUID3}" in cap["url"]  # the members page was opened for the pasted group


def test_members_failure_type_names_manually(session, tmp_path):
    # members selector finds nothing on this app page
    broken = tmp_path / "flow.yaml"
    broken.write_text(FLOW.read_text().replace("#member-dropdown select option:checked", "#zzz").replace(
        "items: '#member-dropdown select option'", "items: '#nothing option'").replace(
        "default_timeout_ms: 3000", "default_timeout_ms: 600"))
    ui = ScriptedUI(["1", "2", "Ann, Ben, Ann", "4", "3"])
    wizard.run(ui=ui, flow=broken, variables=VARS, headless=True, profile_dir=tmp_path / "p",
               state_dir=tmp_path / "s", output_dir=tmp_path / "o")
    assert "Could not read the members" in ui.output
    assert "Members of Thai Trip: Ann, Ben" in ui.output


def test_missing_variable_and_headless_login(session, tmp_path):
    with pytest.raises(WizardError, match="app_url"):
        wizard.run(ui=ScriptedUI([]), flow=FLOW, variables={"lists_url": LISTS_URL}, headless=True)
    with pytest.raises(LoginRequired, match="Not logged in"):
        session(ScriptedUI([]), variables={**VARS, "lists_url": APP_URL + "?login=required"})


def test_q_quits_from_any_menu(session):
    ui = ScriptedUI(["q"])
    result, _ = session(ui)
    assert result.results == [] and "Goodbye" in ui.output


# ---- UI ---------------------------------------------------------------------------------- #

def test_ui_menu_reasks_on_invalid_input():
    ui = ScriptedUI(["", "x", "0", "5", "2"])
    assert ui.menu("Pick", ["a", "b", "c"]) == 1
    assert ui.output.count("Please type a number") == 4
    with pytest.raises(WizardQuit):
        ScriptedUI(["q"]).menu("Pick", ["a"])
    assert ScriptedUI(["q", "1"]).menu("Pick", ["a"], quit_ok=False) == 0


def test_ui_confirm_and_eof():
    assert ScriptedUI(["maybe", "y"]).confirm("ok?") is True
    assert ScriptedUI([""]).confirm("ok?", default=True) is True

    def eof(_):
        raise EOFError

    with pytest.raises(WizardQuit):
        UI(console=Console(file=StringIO()), input_fn=eof).ask_raw("x")


# ---- CLI wiring ---------------------------------------------------------------------------- #

def test_cli_help_and_start_help():
    runner = CliRunner()
    r = runner.invoke(cli.app, ["--help"])
    assert r.exit_code == 0 and "start" in r.output and "wizard" in r.output.lower()
    r = runner.invoke(cli.app, ["start", "--help"])
    assert r.exit_code == 0 and "--no-dialog" in r.output and "--flow" in r.output


def test_bare_command_launches_wizard(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "_start_wizard", lambda *a, **k: called.setdefault("args", a))
    r = CliRunner().invoke(cli.app, [])
    assert r.exit_code == 0 and called["args"][0] == "splitser"


def test_push_asks_for_group_when_flow_needs_it(tmp_path):
    x = tmp_path / "x.xlsx"
    x.write_text("x")
    r = CliRunner().invoke(cli.app, ["push", str(x), "--flow", str(FLOW), "-y",
                                     "--var", "app_url=a", "--var", "lists_url=b"])
    assert r.exit_code == 2 and "Which group?" in r.output and "list_id" in r.output


def test_unknown_flow_name_lists_available(tmp_path):
    x = tmp_path / "x.xlsx"
    x.write_text("x")
    r = CliRunner().invoke(cli.app, ["push", str(x), "--flow", "nope"])
    assert r.exit_code == 2 and "splitser" in r.output


# ---- identical rows ------------------------------------------------------------------- #

DUP_ROWS = [[None, "Entertainment", 2000, "INR", None, "Alice", "Alice", "Y"]] * 2


def test_wizard_keep_identical_rows_pushes_both_and_rerun_pushes_nothing(session, tmp_path):
    f = table_file(tmp_path, DUP_ROWS)
    ui = ScriptedUI(["1", "1", "n", "2", "3"], files=[[f]])
    result, cap = session(ui)
    assert "Remove the duplicates (keep one of each)?" in ui.prompts[2]
    assert "rows 1, 2" in ui.output and "no date" in ui.output and "Keeping all rows as they are" in ui.output
    assert [e["title"] for e in cap["expenses"]] == ["Entertainment", "Entertainment"]
    assert [r.status for r in result.results] == ["pushed", "pushed"]

    ui2 = ScriptedUI(["1", "1", "", "2", "3"], files=[[f]])  # Enter = default No
    result2, cap2 = session(ui2)
    assert [r.status for r in result2.results] == ["skipped_duplicate"] * 2
    assert cap2["expenses"] == []


def test_wizard_dedupe_yes_keeps_one(session, tmp_path):
    f = table_file(tmp_path, DUP_ROWS)
    ui = ScriptedUI(["1", "1", "y", "2", "3"], files=[[f]])
    result, cap = session(ui)
    assert "Removed 1 duplicate row(s)" in ui.output
    assert [e["title"] for e in cap["expenses"]] == ["Entertainment"]
