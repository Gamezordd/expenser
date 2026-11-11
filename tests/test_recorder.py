"""Recorder tests: picker candidate generation, validation, YAML saving, scaffolding."""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from rich.console import Console

from expenser.automation import recorder
from expenser.automation.flow import load_flow, render
from expenser.automation.recorder import (
    PICKER_JS, Candidate, choose_selector, evaluate_candidates, find_participant, new_flow,
    save_selector, templatize,
)

APP = (Path(__file__).parent / "fixtures" / "mock_expense_app.html").as_uri()


@pytest.fixture
def page():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page()
        p.add_init_script(PICKER_JS)
        p.goto(APP)
        p.click("#add-expense")
        yield p
        b.close()


def cands(page, css):
    return page.evaluate(
        "css => { const el = document.querySelector(css); window.__expenserPicker.mark(el);"
        " return window.__expenserPicker.candidates(el); }", css)


def test_candidates_for_button_and_field(page):
    save = cands(page, "#save-btn")
    assert save[0]["kind"] == "id" and save[0]["selector"] == "#save-btn"
    assert 'role=button[name="Save"]' in [c["selector"] for c in save]
    title = [c["selector"] for c in cands(page, "input[name=title]")]
    assert 'input[name="title"]' in title and 'input[placeholder="Title"]' in title
    assert any(s.startswith("role=textbox") for s in title)


def test_checkbox_label_candidates(page):
    sels = [c["selector"] for c in cands(page, "[data-member=Bob]")]
    assert 'role=checkbox[name="Bob"]' in sels
    assert 'label:has-text("Bob") >> input[type=checkbox]' in sels


def test_testid_first_and_validation(page):
    c = cands(page, "[data-testid=group-header]")
    assert c[0] == {"selector": '[data-testid="group-header"]', "kind": "test-id"}
    reports = evaluate_candidates(page, [Candidate(x["selector"], x["kind"]) for x in c])
    assert reports[0].is_unique_hit
    assert recorder.default_choice(reports) == 0


def test_validation_flags_ambiguous_and_misses(page):
    cands(page, "[data-member=Bob]")
    reps = evaluate_candidates(page, [
        Candidate("input[type=checkbox]", "x"), Candidate("#nope", "x"),
        Candidate('role=checkbox[name="Bob"]', "x"),
    ])
    assert reps[0].matches == 3 and reps[0].hit_index == 1
    assert reps[1].matches == 0 and reps[1].hit_index == -1
    assert reps[2].is_unique_hit
    assert recorder.default_choice(reps) == 2


def test_css_path_is_unique_for_unlabelled_element(page):
    c = cands(page, "#members li:nth-of-type(2)")
    css = c[-1]["selector"]
    (rep,) = evaluate_candidates(page, [Candidate(css, "css")])
    assert rep.is_unique_hit


def test_picker_blocks_click_only_when_armed(page):
    page.evaluate("window.__expenserPicked = (p) => { window.__got = p; }")
    page.evaluate("window.__expenserPicker.arm()")
    page.click("#cancel-btn")  # captured, not executed
    assert page.locator("#dlg").is_visible()
    got = page.evaluate("window.__got")
    assert got["tag"] == "button" and not got["cancelled"]
    assert page.evaluate("window.__expenserPicker.isArmed()") is False
    page.click("#cancel-btn")  # disarmed -> executes
    assert page.locator("#dlg").is_hidden()


# ---- pure helpers ---------------------------------------------------------- #

def test_templatize_and_find_participant():
    sel = 'label:has-text("Alice") >> input[type=checkbox]'
    assert find_participant(sel, ["Bob", "alice"]) == "Alice"
    out = templatize(sel, "Alice")
    assert out == 'label:has-text("{p}") >> input[type=checkbox]'
    assert render(out, {"p": "Bob"}) == 'label:has-text("Bob") >> input[type=checkbox]'
    assert templatize("a{2}", None) == "a{{2}}"


def test_choose_selector_default_and_custom():
    reps = [recorder.CandidateReport("a", "x", 2, 0), recorder.CandidateReport("b", "x", 1, 0)]
    quiet = Console(quiet=True)
    assert choose_selector(quiet, lambda _: "", "k", reps, lambda s: 1) == ("pick", "b")
    assert choose_selector(quiet, lambda _: "1", "k", reps, lambda s: 1) == ("pick", "a")
    answers = iter(["css=.foo", "y"])
    assert choose_selector(quiet, lambda _: next(answers), "k", reps, lambda s: 1) == ("pick", "css=.foo")
    assert choose_selector(quiet, lambda _: "r", "k", reps, lambda s: 1) == ("repick", None)
    assert choose_selector(quiet, lambda _: "s", "k", reps, lambda s: 1) == ("skip", None)


# ---- YAML saving and scaffolding ------------------------------------------- #

def test_save_selector_preserves_comments(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text(
        "# header comment\nname: t\nstart_url: x\nselectors:\n"
        "  a: 'old'   # keep me\n  # between\n  b: \"role=button[name=/x/i]\"\n"
        "per_transaction: []  # trailing\n"
    )
    save_selector(p, "a", 'role=button[name="Save"]')
    save_selector(p, "c", "text=\"New\"")
    text = p.read_text()
    assert "# header comment" in text and "# keep me" in text
    assert "# between" in text and "# trailing" in text
    flow = load_flow(p)
    assert list(flow.selectors) == ["a", "b", "c"]
    assert flow.selectors["a"] == 'role=button[name="Save"]'
    assert flow.selectors["c"] == 'text="New"'


def test_save_selector_creates_section_and_expands_flow_style(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text("name: t\nstart_url: x\nselectors: {}\n")
    save_selector(p, "k", "#x")
    assert load_flow(p).selectors == {"k": "#x"}


def test_new_flow_scaffold(tmp_path):
    out = new_flow(tmp_path / "sub" / "app.yaml", "app", "https://x.test/app")
    text = out.read_text()
    assert "name: app" in text and 'start_url: "https://x.test/app"' in text
    assert "# STEP REFERENCE" in text  # comments kept
    with pytest.raises(FileExistsError):
        new_flow(out, "app", "https://x.test")
    with pytest.raises(ValueError):
        new_flow(tmp_path / "b.yaml", "bad name", "https://x.test")


def test_new_flow_loads(tmp_path):
    out = new_flow(tmp_path / "ok.yaml", "ok", "https://x.test/app")
    assert load_flow(out).name == "ok"


# ---- validate / test commands (headless against the mock app) -------------- #

FLOW = Path(__file__).parent / "fixtures" / "mock_flow.yaml"


def test_validate_flow_reports_matches(tmp_path):
    reps = recorder.validate_flow(
        FLOW, variables={"app_url": APP}, headless=True, sample_participant="Alice",
        profile_dir=tmp_path / "p", console=Console(quiet=True),
    )
    by = {r.key: r.matches for r in reps}
    assert by["group_header"] == 1 and by["add_expense_button"] == 1
    assert by["title_input"] == 1  # present in DOM although hidden
    assert by["member_checkbox"] == 1  # {p} rendered with the sample participant


def test_test_flow_dry_run_and_failure(tmp_path):
    res = recorder.test_flow(
        FLOW, variables={"app_url": APP}, all_participants=["Alice", "Bob", "Carol"],
        headless=True, profile_dir=tmp_path / "p", state_dir=tmp_path / "s",
        console=Console(quiet=True),
    )
    assert res.ok
    bad = tmp_path / "bad.yaml"
    bad.write_text(FLOW.read_text().replace("input[name=amount]", "input[name=zzz]")
                   .replace("default_timeout_ms: 3000", "default_timeout_ms: 500"))
    res = recorder.test_flow(
        bad, variables={"app_url": APP}, all_participants=["Alice", "Bob"],
        headless=True, profile_dir=tmp_path / "p2", state_dir=tmp_path / "s",
        console=Console(quiet=True),
    )
    assert not res.ok and res.failed_step == "per_transaction[3].fill"
