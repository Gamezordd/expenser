"""End-to-end tests of the flow executor against a local mock app (headless Chromium, no network)."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from expenser.automation import runner
from expenser.automation.flow import load_flow
from expenser.automation.runner import FlowExecutor, push_transactions
from expenser.models import Transaction

FIXTURES = Path(__file__).parent / "fixtures"
FLOW = FIXTURES / "mock_flow.yaml"
APP_URL = (FIXTURES / "mock_expense_app.html").as_uri()
PEOPLE = ["Alice", "Bob", "Carol"]


def tx(name="Groceries", amount="12.50", parts=("Alice", "Bob"), paid_by="Alice", d=None):
    return Transaction(
        name=name, amount=Decimal(amount), participants=list(parts), paid_by=paid_by, date=d
    )


@pytest.fixture
def page():
    # Function-scoped: push_transactions starts its own Playwright, and two sync
    # Playwright instances cannot be active in one thread at once.
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        p = browser.new_page()
        yield p
        browser.close()


def make_executor(page, tmp_path, flow_path=FLOW, people=PEOPLE, **kw):
    flow = load_flow(flow_path)
    return FlowExecutor(
        page, flow, variables={"app_url": APP_URL}, all_participants=people,
        state_dir=tmp_path / "state", verbose=False, **kw,
    )


def expenses(page):
    return page.evaluate("window.__expenses")


def test_executor_pushes_with_correct_participants(page, tmp_path):
    ex = make_executor(page, tmp_path)
    ex.goto_start()
    ex.ensure_logged_in(interactive=False)
    assert ex.run_transaction(tx(d=date(2024, 3, 5))) == "pushed"
    assert ex.run_transaction(tx("Taxi", "8.00", ("Bob", "Carol"), "Bob")) == "pushed"
    got = expenses(page)
    assert [e["title"] for e in got] == ["Groceries", "Taxi"]
    assert got[0]["amount"] == 12.5 and got[0]["paid_by"] == "Alice"
    assert got[0]["date"] == "05-03-2024"
    assert got[0]["participants"] == ["Alice", "Bob"]
    assert got[1]["participants"] == ["Bob", "Carol"]
    assert page.locator("[data-testid=expense-row]").count() == 2


def test_alias_and_optional_fields(page, tmp_path):
    ex = make_executor(page, tmp_path, people=["Alice", "Bobby", "Carol"])
    ex.goto_start()
    ex.run_transaction(tx(parts=("Alice", "Bobby"), paid_by=None))
    (e,) = expenses(page)
    assert e["participants"] == ["Alice", "Bob"]
    assert e["paid_by"] == "" and e["date"] == ""


def test_dry_run_does_not_commit_and_cleans_up(page, tmp_path):
    ex = make_executor(page, tmp_path, dry_run=True)
    ex.goto_start()
    assert ex.run_transaction(tx()) == "dry_run"
    assert expenses(page) == []
    assert page.locator("role=dialog").is_hidden()  # dry_run_cleanup clicked Cancel
    assert ex.dry_run_screenshot is not None and ex.dry_run_screenshot.exists()


def test_confirm_commit_decline_and_accept(page, tmp_path):
    answers = iter(["n", "y"])
    ex = make_executor(page, tmp_path, confirm_commit=True, input_fn=lambda _: next(answers))
    ex.goto_start()
    with pytest.raises(runner.UserSkip):
        ex.run_transaction(tx("One"))
    ex.recover()
    ex.run_transaction(tx("Two"))
    assert [e["title"] for e in expenses(page)] == ["Two"]


def test_step_failure_names_the_step(page, tmp_path):
    text = FLOW.read_text().replace("input[name=title]", "input[name=nope]")
    bad = tmp_path / "bad.yaml"
    bad.write_text(text.replace("default_timeout_ms: 3000", "default_timeout_ms: 500"))
    ex = make_executor(page, tmp_path, flow_path=bad)
    ex.goto_start()
    with pytest.raises(runner.StepFailure) as err:
        ex.run_transaction(tx())
    assert "per_transaction[2].fill" in str(err.value)


def test_optional_and_if_visible(page, tmp_path):
    flow_yaml = FLOW.read_text().replace(
        "per_transaction:\n", "per_transaction:\n  - click: {target: '#missing'}\n    optional: true\n"
        "  - if_visible:\n      target: '#missing'\n      timeout_ms: 100\n      then:\n"
        "        - click: '#missing'\n      else:\n        - wait_ms: 10\n"
    )
    f = tmp_path / "f.yaml"
    f.write_text(flow_yaml.replace("default_timeout_ms: 3000", "default_timeout_ms: 500"))
    ex = make_executor(page, tmp_path, flow_path=f)
    ex.goto_start()
    ex.run_transaction(tx())
    assert len(expenses(page)) == 1


# ---- full push_transactions (own persistent browser) ---------------------- #

def push(tmp_path, txs, **kw):
    return push_transactions(
        FLOW, txs, all_participants=PEOPLE, variables={"app_url": APP_URL}, headless=True,
        state_dir=tmp_path / "state", profile_dir=tmp_path / "profile", **kw,
    )


def test_push_transactions_ledger_dedupe_force(tmp_path):
    txs = [tx("Groceries"), tx("Taxi", "8.00", ("Bob", "Carol"), "Bob")]
    res = push(tmp_path, txs)
    assert [r.status for r in res] == ["pushed", "pushed"]
    ledger = next((tmp_path / "state" / "pushed").glob("mock_app-*.jsonl"))
    lines = [json.loads(ln) for ln in ledger.read_text().splitlines()]
    assert [ln["fingerprint"] for ln in lines] == [t.fingerprint() for t in txs]
    assert lines[0]["name"] == "Groceries" and lines[0]["amount"] == "12.50"

    again = push(tmp_path, txs)
    assert [r.status for r in again] == ["skipped_duplicate"] * 2

    forced = push(tmp_path, txs[:1], force=True)
    assert [r.status for r in forced] == ["pushed"]


def test_push_dry_run_writes_no_ledger(tmp_path):
    res = push(tmp_path, [tx()], dry_run=True)
    assert [r.status for r in res] == ["dry_run"]
    assert res[0].screenshot and res[0].screenshot.exists()
    assert not list((tmp_path / "state" / "pushed").glob("mock_app*.jsonl"))


def test_push_failure_continues_with_next(tmp_path, monkeypatch):
    # Two transactions; the first includes a participant the dialog lacks -> amount box missing.
    bad = tmp_path / "flow.yaml"
    bad.write_text(
        FLOW.read_text()
        .replace("default_timeout_ms: 3000", "default_timeout_ms: 800")
        .replace("- uncheck: member_checkbox", "- uncheck: member_checkbox\n          optional: true")
    )
    people = ["Alice", "Bob", "Carol", "Zed"]  # Zed has no row in the app
    txs = [tx("Fails", parts=("Alice", "Bob", "Zed")), tx("Works", parts=("Alice", "Bob"))]
    res = push_transactions(
        bad, txs, all_participants=people, variables={"app_url": APP_URL}, headless=True,
        state_dir=tmp_path / "state", profile_dir=tmp_path / "profile",
    )
    assert res[0].status == "failed" and "member_amount" in res[0].error
    assert res[0].screenshot and res[0].screenshot.exists()
    assert res[1].status == "pushed"


def test_push_validates_inputs(tmp_path):
    with pytest.raises(ValueError, match="app_url"):
        push_transactions(FLOW, [tx()], all_participants=PEOPLE, variables={}, headless=True,
                          state_dir=tmp_path, profile_dir=tmp_path / "p")
    with pytest.raises(ValueError, match="Dave"):
        push(tmp_path, [tx(parts=("Alice", "Dave"))])


def test_headless_without_login_fails_clearly(tmp_path):
    with pytest.raises(runner.LoginRequired, match="Not logged in"):
        push_transactions(
            FLOW, [tx()], all_participants=PEOPLE,
            variables={"app_url": APP_URL + "?login=required"}, headless=True,
            state_dir=tmp_path / "state", profile_dir=tmp_path / "profile",
        )


# ---- click_until / group / date picker -------------------------------------- #

DATES_FLOW = FIXTURES / "mock_flow_dates.yaml"


def months_ago(n, day=15):
    m = date.today().year * 12 + date.today().month - 1 - n
    return date(m // 12, m % 12 + 1, day)


def run_inline(page, tmp_path, per_transaction, extra_selectors=""):
    text = (
        "name: inline\nstart_url: '{app_url}'\nvariables:\n  app_url: {required: true}\n"
        "settings: {default_timeout_ms: 800}\nselectors:\n"
        "  prev: 'role=dialog >> .react-calendar__navigation__prev-button'\n"
        "  label: 'role=dialog >> .react-calendar__navigation__label'\n"
        "  tile: 'role=dialog >> abbr[aria-label=\"{transaction.day} {transaction.month_name} {transaction.year}\"]'\n"
        "  dlg: 'role=dialog[name=\"Choose date\"]'\n"
        "  datebtn: '#date-btn'\n  add: '#add-expense'\n"
        + extra_selectors + "per_transaction:\n" + per_transaction
    )
    f = tmp_path / "inline.yaml"
    f.write_text(text)
    ex = make_executor(page, tmp_path, flow_path=f)
    ex.goto_start()
    return ex


OPEN = "  - click: add\n  - click: datebtn\n  - wait_for: dlg\n"


def label(page):
    return page.locator(".react-calendar__navigation__label").inner_text().strip()


def test_click_until_zero_clicks_when_already_there(page, tmp_path):
    ex = run_inline(page, tmp_path, OPEN + "  - click_until: {target: prev, until: tile, max: 1}\n")
    ex.run_transaction(tx(d=date.today()))
    assert page.locator("role=dialog[name='Choose date'] >> .react-calendar__tile--active").count() == 1
    # still on the current month: the label shows this month
    assert str(date.today().year) in label(page)


def test_click_until_clicks_n_times(page, tmp_path):
    ex = run_inline(page, tmp_path, OPEN + "  - click_until: {target: prev, until: tile, delay_ms: 10}\n")
    d = months_ago(5)
    ex.run_transaction(tx(d=d))
    names = ["January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December"]
    assert label(page) == f"{names[d.month - 1]} {d.year}"


def test_click_until_max_exceeded(page, tmp_path):
    ex = run_inline(page, tmp_path, OPEN + "  - click_until: {target: prev, until: tile, max: 2, delay_ms: 10}\n")
    with pytest.raises(runner.StepFailure, match="not reached after 2 clicks"):
        ex.run_transaction(tx(d=months_ago(10)))


def test_group_when_skip_optional_and_error(page, tmp_path):
    body = (
        "  - group:\n      when: '{transaction.date_iso}'\n      steps:\n        - click: '#nope'\n"
        "  - group:\n      optional: true\n      steps:\n        - click: '#nope'\n"
        "  - click: add\n"
    )
    ex = run_inline(page, tmp_path, body)
    ex.run_transaction(tx(d=None))  # first group skipped (no date), second one fails but optional
    assert page.locator("#dlg").is_visible()

    bad = "  - group:\n      description: boom\n      steps:\n        - click: add\n        - click: '#nope'\n"
    ex = run_inline(page, tmp_path, bad)
    with pytest.raises(runner.StepFailure) as err:
        ex.run_transaction(tx(d=None))
    assert "per_transaction[0].group" in str(err.value)


def test_push_dates_end_to_end(tmp_path):
    older, newer = months_ago(14), months_ago(2)
    txs = [
        tx("Old", d=older), tx("Newer", d=newer), tx("Undated", d=None),
    ]
    from playwright.sync_api import sync_playwright
    # Drive through FlowExecutor on one page so window.__expenses can be inspected.
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        ex = make_executor(page, tmp_path, flow_path=DATES_FLOW)
        ex.goto_start()
        for t in txs:
            assert ex.run_transaction(t) == "pushed"
        got = expenses(page)
        browser.close()
    # each row is entered on a fresh dialog, but the mock keeps the last chosen date
    # (like a form that remembers it), so every row must have set its own date explicitly.
    assert [e["date_iso"] for e in got] == [older.isoformat(), newer.isoformat(), date.today().isoformat()]


# --- equal-split amounts ({p_amount}) against the mock's Splitser-like amount boxes --------- #

def test_equal_split_recorded_in_mock(page, tmp_path):
    ex = make_executor(page, tmp_path)
    ex.goto_start()
    assert ex.run_transaction(tx("Dinner", "100.00", ("Alice", "Bob", "Carol"))) == "pushed"
    assert ex.run_transaction(tx("Solo", "4629.81", ("Bob",))) == "pushed"
    first, solo = expenses(page)
    assert first["amount"] == 100.0
    assert first["split"] == {"Alice": 33.34, "Bob": 33.33, "Carol": 33.33}
    assert round(sum(first["split"].values()), 2) == 100.0
    assert solo["split"] == {"Bob": 4629.81}


def test_mock_rejects_member_amounts_that_do_not_add_up(tmp_path):
    bad = tmp_path / "flow.yaml"
    bad.write_text(FLOW.read_text().replace('value: "{p_amount}"', 'value: "1"'))
    res = push_transactions(
        bad, [tx("Bad", "12.50")], all_participants=PEOPLE, variables={"app_url": APP_URL},
        headless=True, state_dir=tmp_path / "state", profile_dir=tmp_path / "profile",
    )
    assert res[0].status == "failed"


# ---- ledger counts occurrences ------------------------------------------------------- #

def _ledger(tmp_path, scope="s"):
    return runner.Ledger(tmp_path / "l.jsonl", scope)


def test_ledger_counts_and_flags(tmp_path):
    led = _ledger(tmp_path)
    a = tx("A")
    assert led.count(a.fingerprint()) == 0 and a.fingerprint() not in led
    led.add(a)
    led.add(a)
    assert led.count(a.fingerprint()) == 2 and a.fingerprint() in led
    assert led.already_pushed_flags([a, a, a]) == [True, True, False]
    # reloaded from disk, same scope counts; other scope does not
    assert _ledger(tmp_path).count(a.fingerprint()) == 2
    assert _ledger(tmp_path, "other").count(a.fingerprint()) == 0


def test_ledger_old_file_each_line_counts_once(tmp_path):
    a = tx("A")
    line = json.dumps({"fingerprint": a.fingerprint(), "scope": "s", "name": "A", "amount": "12.50"})
    (tmp_path / "l.jsonl").write_text(line + "\n")
    led = _ledger(tmp_path)
    assert led.count(a.fingerprint()) == 1
    assert led.already_pushed_flags([a, a]) == [True, False]


def test_identical_rows_pushed_separately_then_skipped(tmp_path):
    a = tx("Entertainment", "20.00")
    res = push(tmp_path, [a, a])
    assert [r.status for r in res] == ["pushed", "pushed"]
    ledger = next((tmp_path / "state" / "pushed").glob("mock_app-*.jsonl"))
    assert len(ledger.read_text().splitlines()) == 2
    assert [r.status for r in push(tmp_path, [a, a])] == ["skipped_duplicate"] * 2
    # a third copy: only the new one is pushed
    assert [r.status for r in push(tmp_path, [a, a, a])] == ["skipped_duplicate"] * 2 + ["pushed"]
    # force pushes everything
    assert [r.status for r in push(tmp_path, [a, a], force=True)] == ["pushed", "pushed"]
