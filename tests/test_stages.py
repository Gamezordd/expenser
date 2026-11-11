"""group_picker / members stages, flow resolution, ledger scoping, wait_for_login, push_on_page."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from expenser.automation import runner
from expenser.automation.flow import FlowError, list_flows, load_flow, parse_flow, resolve_flow
from expenser.automation.runner import Ledger, ledger_path, ledger_scope, push_on_page, wait_for_login
from expenser.automation.stages import StageError, discover_groups, discover_members, extract_group_value
from expenser.models import Transaction

FIXTURES = Path(__file__).parent / "fixtures"
STAGES = FIXTURES / "mock_flow_stages.yaml"
APP_URL = (FIXTURES / "mock_expense_app.html").as_uri()
LISTS_URL = (FIXTURES / "mock_lists.html").as_uri()
UUID1 = "11111111-aaaa-4aaa-8aaa-111111111111"
UUID2 = "22222222-bbbb-4bbb-8bbb-222222222222"
UUID3 = "33333333-cccc-4ccc-8ccc-333333333333"
VARS = {"app_url": APP_URL, "lists_url": LISTS_URL}


def base(**over):
    raw = {
        "name": "t", "start_url": "https://x.test", "variables": {"gid": {"required": True}},
        "selectors": {"btn": "button"}, "per_transaction": [{"click": "btn"}],
    }
    raw.update(over)
    return raw


@pytest.fixture
def page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        p = browser.new_page()
        yield p
        browser.close()


def tx(name="Groceries", amount="12.50", parts=("Alice", "Bob"), paid_by="Alice"):
    return Transaction(name=name, amount=Decimal(amount), participants=list(parts), paid_by=paid_by)


# ---- schema -------------------------------------------------------------------------- #

def test_stage_sections_parse_and_picker_variable_counts_as_satisfied():
    flow = load_flow(STAGES)
    assert flow.group_picker.variable == "list_id" and flow.members.me
    assert flow.missing_variables({}) == ["app_url", "lists_url", "list_id"]
    assert flow.missing_variables({}, picker_provides=True) == ["app_url", "lists_url"]
    assert flow.resolve_variables({"app_url": "a"}, partial=True)["list_id"] == ""
    with pytest.raises(ValueError, match="list_id"):
        flow.resolve_variables({"app_url": "a", "lists_url": "b"})


@pytest.mark.parametrize("section, match", [
    ({"group_picker": {"items": "a", "variable": "nope"}}, "must be declared"),
    ({"group_picker": {"items": "a", "variable": "gid", "value_regex": "(["}}, "invalid regular"),
    ({"group_picker": {"items": "a", "variable": "gid", "value_regex": "/x/"}}, "capture group"),
    ({"group_picker": {"items": "a", "variable": "gid", "url": "https://x/{gid}"}}, "cannot use"),
    ({"group_picker": {"items": "a", "variable": "gid", "url": "https://x/{zzz}"}}, "unknown placeholder"),
    ({"group_picker": {"variable": "gid"}}, "items"),
    ({"group_picker": {"items": "a", "variable": "gid", "bogus": 1}}, "bogus"),
    ({"members": {"items": "a", "url": "https://x/{zzz}"}}, "unknown placeholder"),
    ({"members": {"url": "https://x"}}, "items"),
])
def test_stage_schema_errors(section, match):
    with pytest.raises(FlowError, match=match):
        parse_flow(base(**section))


def test_members_url_may_use_picker_variable():
    flow = parse_flow(base(
        group_picker={"items": "a", "variable": "gid"},
        members={"items": "option", "url": "https://x/{gid}/new"},
    ))
    assert flow.members.url == "https://x/{gid}/new"


# ---- discovery ----------------------------------------------------------------------- #

def test_discover_groups_dedupes_filters_and_labels(page):
    flow = load_flow(STAGES)
    page.goto(LISTS_URL)
    groups = discover_groups(page, flow)
    # /lists/new, /account, /help ignored; edit links deduped (first real label wins);
    # whitespace collapsed; label falls back to the first text line without a heading
    assert groups == [("Thai Trip", UUID1), ("Flat", UUID2), ("Weekend away", UUID3)]


def test_discover_groups_edit_link_first_gets_real_label_later(page):
    flow = load_flow(STAGES)
    page.goto(LISTS_URL)
    # group 1's edit link comes first in the DOM and has no text: must not stick as the label
    assert discover_groups(page, flow)[0][0] == "Thai Trip"


def test_discover_groups_nothing_found(page):
    flow = load_flow(STAGES)
    page.goto("about:blank")
    assert discover_groups(page, flow) == []  # wait_for times out -> empty, not an exception


def test_discover_groups_text_attribute_without_regex(page):
    flow = parse_flow(base(group_picker={
        "items": "a", "value_attribute": "text", "variable": "gid",
    }))
    page.set_content("<a>One</a><a>Two</a><a>One</a><a> </a>")
    assert discover_groups(page, flow) == [("One", "One"), ("Two", "Two")]


def test_label_capped_at_80_chars(page):
    flow = parse_flow(base(group_picker={"items": "a", "variable": "gid", "value_regex": r"/g/(\d+)"}))
    page.set_content('<a href="/g/1">' + "word " * 40 + "</a>")
    (label, value), = discover_groups(page, flow)
    assert value == "1" and len(label) <= 80


def test_discover_members_and_me(page):
    flow = load_flow(STAGES)
    members, me = discover_members(page, flow, {**VARS, "list_id": UUID2})
    assert members == ["Alice", "Bob", "Carol"] and me == "Bob"
    assert f"list={UUID2}" in page.url


def test_discover_members_dedupes_and_no_me(page):
    flow = parse_flow(base(members={"items": "li"}))
    page.set_content("<ul><li> Ann </li><li>Bo\n b</li><li>Ann</li><li> </li></ul>")
    assert discover_members(page, flow, {"gid": "x"}) == (["Ann", "Bo b"], None)


def test_discover_members_failure(page):
    flow = parse_flow(base(members={"items": "li"}, settings={"default_timeout_ms": 300}))
    page.set_content("<p>nothing</p>")
    with pytest.raises(StageError, match="No members"):
        discover_members(page, flow, {"gid": "x"})


def test_extract_group_value():
    flow = load_flow(STAGES)
    assert extract_group_value(flow, f"https://app.x/lists/{UUID1}/transactions") == UUID1
    assert extract_group_value(flow, "  'abc-123' ") == "abc-123"
    assert extract_group_value(flow, "not a url at all") is None
    assert extract_group_value(flow, "") is None


# ---- resolve_flow -------------------------------------------------------------------- #

def test_resolve_flow_path_bundled_user_and_error(tmp_path, monkeypatch):
    assert resolve_flow(STAGES) == STAGES
    assert resolve_flow(str(STAGES)) == STAGES
    bundled = resolve_flow("splitser")
    assert bundled.name == "splitser.yaml" and resolve_flow("splitser.yaml") == bundled
    load_flow(bundled)

    from expenser.paths import user_flows_dir

    user = user_flows_dir()
    user.mkdir(parents=True)
    (user / "mine.yaml").write_text("name: mine\n")
    assert resolve_flow("mine") == user / "mine.yaml"
    with pytest.raises(FlowError) as err:
        resolve_flow("nope")
    msg = str(err.value)
    assert "nope" in msg and "splitser" in msg and "mine" in msg
    assert "_template" not in msg
    assert list_flows() == {"bundled": ["splitser"], "user": ["mine"]}


# ---- ledger scoping ------------------------------------------------------------------ #

def test_ledger_scope_depends_on_variables(tmp_path):
    flow = load_flow(STAGES)
    a = {**VARS, "list_id": UUID1}
    b = {**VARS, "list_id": UUID2}
    assert ledger_scope(flow, a) == ledger_scope(flow, dict(a))
    assert ledger_scope(flow, a) != ledger_scope(flow, b)
    assert ledger_path(tmp_path, flow, a) != ledger_path(tmp_path, flow, b)

    t = tx()
    la = Ledger(ledger_path(tmp_path, flow, a), ledger_scope(flow, a))
    assert t.fingerprint() not in la
    la.add(t)
    assert t.fingerprint() in Ledger(ledger_path(tmp_path, flow, a), ledger_scope(flow, a))
    # same transaction, different group: not a duplicate
    assert t.fingerprint() not in Ledger(ledger_path(tmp_path, flow, b), ledger_scope(flow, b))


def test_ledger_ignores_old_and_foreign_scope_lines(tmp_path):
    p = tmp_path / "l.jsonl"
    t = tx()
    p.write_text(
        json.dumps({"fingerprint": t.fingerprint(), "name": "old"}) + "\n"      # no scope
        + json.dumps({"fingerprint": "zz", "scope": "other"}) + "\n"
        + "garbage\n"
    )
    led = Ledger(p, "mine")
    assert t.fingerprint() not in led and "zz" not in led
    assert t.fingerprint() in Ledger(p)  # unscoped use still reads everything


# ---- wait_for_login ------------------------------------------------------------------ #

def test_wait_for_login_already_logged_in_returns_immediately(page):
    flow = load_flow(STAGES)
    page.goto(LISTS_URL)
    wait_for_login(page, flow, variables=VARS, timeout_s=1)


def test_wait_for_login_headless_not_logged_in_errors(page):
    flow = load_flow(STAGES)
    page.goto("about:blank")
    with pytest.raises(runner.LoginRequired, match="Not logged in"):
        wait_for_login(page, flow, variables=VARS, interactive=False)


def test_wait_for_login_polls_until_visible(page):
    flow = load_flow(STAGES)
    page.set_content("<p>login please</p>")
    page.evaluate(
        "setTimeout(() => { document.body.innerHTML = '<header data-testid=group-header>hi</header>'; }, 800)"
    )
    wait_for_login(page, flow, variables=VARS, timeout_s=10, poll_s=0.2)


def test_wait_for_login_gives_up(page):
    flow = load_flow(STAGES)
    page.set_content("<p>login please</p>")
    with pytest.raises(runner.LoginRequired, match="Gave up"):
        wait_for_login(page, flow, variables=VARS, timeout_s=0.5, poll_s=0.1)


def test_wait_for_login_no_selector_is_noop(page):
    flow = parse_flow(base())
    wait_for_login(page, flow)


# ---- push_on_page -------------------------------------------------------------------- #

def test_push_on_page_pushes_dedupes_scopes_and_dry_runs(page, tmp_path):
    flow = load_flow(STAGES)
    people = ["Alice", "Bob", "Carol"]
    kw = dict(all_participants=people, state_dir=tmp_path / "state", interactive=False)
    v1 = {**VARS, "list_id": UUID1}
    v2 = {**VARS, "list_id": UUID2}
    t = tx()

    res = push_on_page(page, flow, [t], variables=v1, **kw)
    assert [r.status for r in res] == ["pushed"]
    assert page.evaluate("window.__expenses")[0]["title"] == "Groceries"
    # same group again -> duplicate; other group -> pushed
    assert [r.status for r in push_on_page(page, flow, [t], variables=v1, **kw)] == ["skipped_duplicate"]
    assert [r.status for r in push_on_page(page, flow, [t], variables=v2, **kw)] == ["pushed"]
    assert [r.status for r in push_on_page(page, flow, [t], variables=v1, force=True, **kw)] == ["pushed"]

    dry = push_on_page(page, flow, [tx("Taxi", "5.00")], variables=v1, dry_run=True, **kw)
    assert [r.status for r in dry] == ["dry_run"]
    assert page.evaluate("window.__expenses") == []  # fresh page load, nothing committed


def test_push_on_page_validates_participants(page, tmp_path):
    flow = load_flow(STAGES)
    with pytest.raises(ValueError, match="Dave"):
        push_on_page(page, flow, [tx(parts=("Alice", "Dave"))], all_participants=["Alice", "Bob"],
                     variables={**VARS, "list_id": UUID1}, state_dir=tmp_path, interactive=False)
