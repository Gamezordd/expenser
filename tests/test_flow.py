"""Schema, loader and templating tests (no browser)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from expenser.automation.flow import FlowError, TemplateError, load_flow, parse_flow, render
from expenser.models import Transaction

MOCK = Path(__file__).parent / "fixtures" / "mock_flow.yaml"


def base(**over):
    raw = {
        "name": "t", "start_url": "https://x.test",
        "selectors": {"btn": "button"}, "per_transaction": [{"click": "btn"}],
    }
    raw.update(over)
    return raw


def test_load_mock_flow():
    flow = load_flow(MOCK)
    assert flow.name == "mock_app"
    assert flow.required_variables() == ["app_url"]
    assert [s.kind for s in flow.per_transaction][:3] == ["click", "wait_for", "fill"]
    assert flow.per_transaction[-1].commit and flow.has_commit_step()
    assert flow.per_transaction[1].params["state"] == "visible"  # default


def test_short_forms():
    flow = parse_flow(base(per_transaction=[
        {"click": "btn"}, {"press": "Enter"}, {"wait_ms": 50}, {"goto": "https://a.b"},
        {"pause": "log in"}, {"screenshot": "out/x.png"},
    ]))
    assert flow.per_transaction[0].params == {"target": "btn"}
    assert flow.per_transaction[1].params == {"key": "Enter"}
    assert flow.per_transaction[2].params == {"ms": 50}


@pytest.mark.parametrize(
    "steps, needle",
    [
        ([{"clik": "btn"}], "unknown key"),
        ([{"click": "btn", "fill": "x"}], "exactly one action"),
        ([{"fill": {"target": "btn"}}], "missing required option"),
        ([{"click": {"target": "btn", "bogus": 1}}], "unknown option"),
        ([{"select": {"target": "btn"}}], "exactly one of"),
        ([{"wait_for": {"target": "btn", "state": "gone"}}], "state must be"),
        ([{"for_each": {"items": "nobody", "steps": [{"click": "btn"}]}}], "items must be"),
        ([{"for_each": {"items": "all_participants", "steps": []}}], "non-empty"),
        ([{"click": "missing_key"}], "unknown selector key"),
        ([{"fill": {"target": "btn", "value": "{nope}"}}], "unknown placeholder"),
        ([{"click": {"target": "btn", "nth": "two"}}], "integer"),
        ([{"click": "btn", "commit": "yes"}], "true/false"),
        ("notalist", "list of steps"),
    ],
)
def test_step_errors_name_the_location(steps, needle):
    with pytest.raises(FlowError) as err:
        parse_flow(base(per_transaction=steps))
    assert needle in str(err.value)
    assert "per_transaction" in str(err.value) or needle == "unknown selector key"


def test_error_location_is_precise():
    raw = base(per_transaction=[{"click": "btn"}, {"for_each": {
        "items": "all_participants", "as": "who", "steps": [{"click": "btn"}, {"fill": {"target": "btn"}}]}}])
    with pytest.raises(FlowError, match=r"per_transaction\[1\]\.for_each\.steps\[1\]\.fill"):
        parse_flow(raw)


def test_top_level_validation():
    with pytest.raises(FlowError, match="unknown top-level"):
        parse_flow(base(bogus=1))
    with pytest.raises(FlowError, match="invalid flow definition"):
        parse_flow({"name": "x"})  # no start_url
    with pytest.raises(FlowError, match="logged_in_selector"):
        parse_flow(base(login={"logged_in_selector": "ghost"}))
    with pytest.raises(FlowError, match="unknown placeholder"):
        parse_flow(base(start_url="{undeclared}"))
    with pytest.raises(FlowError, match="selectors.bad"):
        parse_flow(base(selectors={"bad": "{nope}"}))
    with pytest.raises(FlowError, match="mapping"):
        parse_flow([1])


def test_loop_var_placeholders_allowed_in_selectors_and_steps():
    parse_flow(base(
        selectors={"row": 'li:has-text("{who}")'},
        per_transaction=[{"for_each": {"items": "all_participants", "as": "who",
                                       "steps": [{"click": "row"}, {"fill": {"target": "row", "value": "{who_raw}"}}]}}],
    ))


def test_load_flow_yaml_errors(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text("name: [unclosed")
    with pytest.raises(FlowError, match="invalid YAML"):
        load_flow(p)
    with pytest.raises(FlowError, match="cannot read"):
        load_flow(tmp_path / "missing.yaml")


def test_resolve_variables():
    flow = parse_flow(base(variables={
        "a": {"required": True, "description": "the A"}, "b": {"default": "x"}, "c": None,
    }))
    assert flow.required_variables() == ["a"]
    with pytest.raises(ValueError, match=r"a \(the A\)"):
        flow.resolve_variables({})
    assert flow.resolve_variables({"a": "1"}) == {"a": "1", "b": "x", "c": ""}


# ---- templating ------------------------------------------------------------ #

def tx(**kw):
    d = dict(name="Albert Heijn", amount=Decimal("12.5"), participants=["Alice", "Bobby"],
             paid_by="Bobby", date=date(2024, 3, 5))
    d.update(kw)
    return Transaction(**d)


def test_context_formatting_and_aliases():
    flow = load_flow(MOCK)
    ctx = flow.context({"app_url": "u"}, tx(), ["Alice", "Bobby", "Carol"])
    assert ctx["transaction.amount"] == "12,50"
    assert ctx["transaction.amount_raw"] == "12.50"
    assert ctx["transaction.date"] == "05-03-2024"
    assert ctx["transaction.paid_by"] == "Bob"  # alias applied
    assert ctx["transaction.participants_csv"] == "Alice, Bob"
    assert ctx["transaction.excluded_participants_csv"] == "Carol"
    assert ctx["flow.name"] == "mock_app" and ctx["app_url"] == "u"
    empty = flow.context({}, tx(date=None, paid_by=None), [])
    assert empty["transaction.date"] == "" and empty["transaction.paid_by"] == ""


def test_excluded_and_item_lists():
    flow = load_flow(MOCK)
    lists = flow.item_lists(tx(participants=["alice"]), ["Alice", "Bobby", "Carol"])
    assert lists["transaction.excluded_participants"] == ["Bobby", "Carol"]  # case-insensitive
    assert lists["all_participants"] == ["Alice", "Bobby", "Carol"]
    assert lists["transaction.participants"] == ["alice"]


def test_bind_loop_var_applies_alias():
    flow = load_flow(MOCK)
    ctx = flow.bind_loop_var({"x": "1"}, "p", "Bobby")
    assert ctx["p"] == "Bob" and ctx["p_raw"] == "Bobby" and ctx["x"] == "1"


def test_render_escapes_and_errors():
    assert render("a{{b}}c {x}", {"x": "1"}) == "a{b}c 1"
    assert render("{ transaction.name }", {"transaction.name": "N"}) == "N"
    assert render("plain [a=b] stray } brace", {}) == "plain [a=b] stray } brace"
    with pytest.raises(TemplateError, match=r"step 3: unknown placeholder \{nope\}"):
        render("x{nope}", {"y": "1"}, "step 3")


def test_render_is_not_str_format():
    # attribute access / format specs / indexing must not be interpreted
    with pytest.raises(TemplateError):
        render("{x.__class__}", {"x": "1"})
    with pytest.raises(TemplateError):
        render("{x:>10}", {"x": "1"})
    assert render("{x}", {"x": "{y}"}) == "{y}"  # values are never re-expanded


def test_yaml_roundtrip_of_scaffold_style_templates():
    # Selectors with quotes/brackets survive YAML and braces escaping
    flow = parse_flow(yaml.safe_load(
        'name: t\nstart_url: x\nselectors:\n  a: \'role=button[name=/a{{2}}/i]\'\n'))
    assert render(flow.selectors["a"], {}) == "role=button[name=/a{2}/i]"


# ---- date parts, click_until, group ----------------------------------------- #

def test_date_part_keys():
    flow = load_flow(MOCK)
    ctx = flow.context({"app_url": "u"}, tx(date=date(2026, 3, 5)), [])
    assert (ctx["transaction.day"], ctx["transaction.day2"]) == ("5", "05")
    assert (ctx["transaction.month"], ctx["transaction.month2"]) == ("3", "03")
    assert (ctx["transaction.month_name"], ctx["transaction.month_abbr"]) == ("March", "Mar")
    assert ctx["transaction.year"] == "2026" and ctx["transaction.date_iso"] == "2026-03-05"
    assert ctx["transaction.no_date"] == ""
    assert flow.context({"app_url": "u"}, tx(date=date(2026, 12, 31)), [])["transaction.month_name"] == "December"
    empty = flow.context({"app_url": "u"}, tx(date=None), [])
    for k in ("day", "day2", "month", "month2", "month_name", "month_abbr", "year", "date_iso"):
        assert empty[f"transaction.{k}"] == ""
    assert empty["transaction.no_date"] == "1"
    today = date.today()
    assert empty["today.date_iso"] == today.isoformat() and empty["today.day"] == str(today.day)
    assert flow.base_context({})["today.year"] == str(today.year)


def test_date_keys_allowed_in_selectors_and_steps():
    parse_flow(base(
        selectors={"tile": 'abbr[aria-label="{transaction.day} {transaction.month_name} {transaction.year}"]',
                   "t2": "x[{today.month_abbr}]"},
        per_transaction=[{"click": "tile", "when": "{transaction.date_iso}"}],
    ))


def test_click_until_and_group_parsing():
    flow = parse_flow(base(per_transaction=[
        {"click_until": {"target": "btn", "until": "btn"}},
        {"group": {"steps": [{"click": "btn"}, {"click_until": {"target": "btn", "until": "button", "max": 3}}]},
         "when": "{transaction.date_iso}", "optional": True},
    ]))
    cu = flow.per_transaction[0]
    assert cu.params["max"] == 24 and cu.params["delay_ms"] == 150
    g = flow.per_transaction[1]
    assert g.kind == "group" and g.optional and len(g.steps) == 2 and g.steps[1].params["max"] == 3


@pytest.mark.parametrize(
    "steps, needle",
    [
        ([{"click_until": {"target": "btn"}}], "missing required option"),
        ([{"click_until": {"target": "btn", "until": "btn", "max": 0}}], ">= 1"),
        ([{"click_until": {"target": "btn", "until": "ghost_key"}}], "unknown selector key"),
        ([{"click_until": {"target": "btn", "until": "x[{nope}]"}}], "unknown placeholder"),
        ([{"group": {"steps": []}}], "non-empty"),
    ],
)
def test_click_until_group_errors(steps, needle):
    with pytest.raises(FlowError, match=needle):
        parse_flow(base(per_transaction=steps))


def test_group_error_path_is_precise():
    raw = base(per_transaction=[{"click": "btn"}] * 3 + [
        {"group": {"steps": [{"click": "btn"}, {"click": "ghost_key"}]}}])
    with pytest.raises(FlowError, match=r"per_transaction\[3\]\.group\.steps\[1\]\.click"):
        parse_flow(raw)
    raw = base(per_transaction=[{"group": {"steps": [{"fill": {"target": "btn"}}]}}])
    with pytest.raises(FlowError, match=r"per_transaction\[0\]\.group\.steps\[0\]\.fill"):
        parse_flow(raw)


def test_commit_inside_group_counts():
    flow = parse_flow(base(per_transaction=[{"group": {"steps": [{"click": "btn", "commit": True}]}}]))
    assert flow.has_commit_step()


def test_shipped_flows_load():
    root = Path(__file__).parent.parent / "expenser" / "flows"
    flow = load_flow(root / "splitser.yaml")
    assert flow.group_picker.variable == "list_id" and flow.members.me
    assert flow.missing_variables({}) == ["list_id"]
    assert any(s.kind == "group" for s in flow.per_transaction)
    for f in root.glob("*.yaml"):
        load_flow(f)


# --- equal split / {p_amount} ---------------------------------------------------------------- #
from decimal import Decimal as _D

from expenser.automation.flow import FlowError, equal_split, parse_flow


def test_equal_split_rules():
    assert equal_split(_D("100"), 3) == [_D("33.34"), _D("33.33"), _D("33.33")]
    assert equal_split(_D("0.05"), 3) == [_D("0.02"), _D("0.02"), _D("0.01")]
    assert equal_split(_D("4629.81"), 1) == [_D("4629.81")]
    parts = equal_split(_D("1000"), 9)
    assert sum(parts) == _D("1000") and len(parts) == 9
    assert parts == [_D("111.12")] + [_D("111.11")] * 8
    with pytest.raises(ValueError):
        equal_split(_D("5"), 0)


def test_bind_loop_var_amounts_use_decimal_separator():
    flow = load_flow(MOCK)  # amount_decimal_separator ","
    t = tx(participants=["Alice", "Bob", "Carol"])
    t = t.model_copy(update={"amount": _D("100.00")})
    ctx = flow.context({"app_url": "u"}, t, ["Alice", "Bob", "Carol"])
    assert ctx["transaction.participant_count"] == "3"
    first = flow.bind_loop_var(ctx, "p", "Alice", 0, "transaction.participants")
    last = flow.bind_loop_var(ctx, "p", "Carol", 2, "transaction.participants")
    assert first["p_amount"] == "33,34" and first["p_amount_raw"] == "33.34"
    assert last["p_amount"] == "33,33" and last["p_amount_raw"] == "33.33"
    other = flow.bind_loop_var(ctx, "p", "Carol", 0, "transaction.excluded_participants")
    assert "p_amount" not in other


def _flow_with_loop(items: str, value: str = "{p_amount}", selector: str = "input"):
    return parse_flow({
        "name": "x", "start_url": "http://x", "selectors": {"s": selector},
        "per_transaction": [{"for_each": {
            "items": items, "as": "p", "steps": [{"fill": {"target": "s", "value": value}}],
        }}],
    })


def test_p_amount_valid_for_participants_only():
    _flow_with_loop("transaction.participants")
    _flow_with_loop("transaction.participants", value="x", selector='i[v="{p_amount_raw}"]')
    for items in ("transaction.excluded_participants", "all_participants"):
        with pytest.raises(FlowError, match=r"per_transaction\[0\]\.for_each\.steps\[0\]\.fill\.value: unknown placeholder \{p_amount\}"):
            _flow_with_loop(items)
        with pytest.raises(FlowError, match=r"selectors\.s"):
            _flow_with_loop(items, value="x", selector='i[v="{p_amount}"]')
