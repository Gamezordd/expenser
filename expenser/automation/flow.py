"""Declarative flow definitions: YAML schema, loader, validation and safe templating.

A *flow* describes, for one web app, how to add a transaction: a set of named
selectors ("HTML accessors") and lists of steps that use them. This module knows
nothing about browsers; :mod:`expenser.automation.runner` executes flows.
"""
from __future__ import annotations

import re
from datetime import date as _date
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from expenser.models import Transaction

__all__ = [
    "FlowError",
    "TemplateError",
    "Flow",
    "Step",
    "Settings",
    "VariableSpec",
    "GroupPicker",
    "MembersSpec",
    "resolve_flow",
    "list_flows",
    "load_flow",
    "parse_flow",
    "render",
    "placeholders",
    "escape_braces",
    "equal_split",
    "STEP_KINDS",
    "ITEM_SOURCES",
]


class FlowError(ValueError):
    """The flow definition is invalid."""


class TemplateError(FlowError):
    """A placeholder could not be rendered."""


# --------------------------------------------------------------------------- #
# Templating
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")


def placeholders(template: str) -> list[str]:
    """Return the placeholder keys used in ``template`` (``{{``/``}}`` ignored)."""
    return [m.group(1).strip() for m in _TOKEN_RE.finditer(template) if m.group(1) is not None]


def render(template: str, ctx: dict[str, str], where: str = "") -> str:
    """Substitute ``{key}`` placeholders from a flat dict. ``{{``/``}}`` are literal braces.

    No attribute access, formatting specs or evaluation: a placeholder is a plain
    dictionary key (``transaction.name`` is just a key containing a dot).
    """

    def sub(m: re.Match[str]) -> str:
        token = m.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        key = (m.group(1) or "").strip()
        if key not in ctx:
            known = ", ".join(sorted(ctx)) or "(none)"
            prefix = f"{where}: " if where else ""
            raise TemplateError(
                f"{prefix}unknown placeholder {{{key}}} in {template!r}. Available: {known}. "
                "Use {{ and }} for literal braces."
            )
        return ctx[key]

    return _TOKEN_RE.sub(sub, template)


def escape_braces(text: str) -> str:
    """Escape literal braces so ``text`` survives :func:`render` unchanged."""
    return text.replace("{", "{{").replace("}", "}}")


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

STEP_KINDS = (
    "goto", "click", "fill", "type", "select", "check", "uncheck", "press", "wait_for",
    "wait_for_url", "wait_ms", "for_each", "if_visible", "pause", "screenshot", "assert_visible", "click_until", "group",
)
ITEM_SOURCES = ("transaction.participants", "transaction.excluded_participants", "all_participants")
WAIT_STATES = ("visible", "hidden", "attached", "detached")
_MODIFIERS = ("commit", "optional", "description", "when")
_TRANSACTION_KEYS = (
    "transaction.name", "transaction.amount", "transaction.amount_raw", "transaction.date",
    "transaction.paid_by", "transaction.participant_count", "transaction.participants_csv", "transaction.excluded_participants_csv",
    "transaction.day", "transaction.day2", "transaction.month", "transaction.month2",
    "transaction.month_name", "transaction.month_abbr", "transaction.year",
    "transaction.date_iso", "transaction.no_date",
    "today.day", "today.day2", "today.month", "today.month2", "today.month_name",
    "today.month_abbr", "today.year", "today.date_iso",
)
_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December",
)


def equal_split(amount: Decimal, n: int) -> list[Decimal]:
    """Split ``amount`` into ``n`` equal shares of whole cents that sum exactly to it.

    The remainder cents go one each to the FIRST shares (e.g. 100/3 -> 33.34, 33.33, 33.33).
    """
    if n <= 0:
        raise ValueError(f"cannot split an amount among {n} participants")
    total = int((Decimal(amount) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    base, rem = divmod(total, n)
    return [Decimal(base + (1 if i < rem else 0)) / 100 for i in range(n)]


def date_parts(prefix: str, d: _date | None) -> dict[str, str]:
    """Locale-independent date pieces as template keys (all empty when ``d`` is None)."""
    if d is None:
        names = ("day", "day2", "month", "month2", "month_name", "month_abbr", "year", "date_iso")
        return {f"{prefix}.{n}": "" for n in names}
    name = _MONTH_NAMES[d.month - 1]
    return {
        f"{prefix}.day": str(d.day),
        f"{prefix}.day2": f"{d.day:02d}",
        f"{prefix}.month": str(d.month),
        f"{prefix}.month2": f"{d.month:02d}",
        f"{prefix}.month_name": name,
        f"{prefix}.month_abbr": name[:3],
        f"{prefix}.year": str(d.year),
        f"{prefix}.date_iso": d.isoformat(),
    }
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HTML_TAGS = {
    "a", "body", "button", "dialog", "div", "form", "h1", "h2", "h3", "header", "img", "input",
    "label", "li", "main", "nav", "option", "p", "section", "select", "span", "table", "tbody",
    "td", "textarea", "tr", "ul", "ol", "html", "footer", "aside", "article", "summary", "details",
}


@dataclass(frozen=True)
class _Spec:
    short: str | None  # key a bare scalar maps to
    allowed: frozenset[str]
    required: frozenset[str] = frozenset()


def _spec(short: str | None, allowed: str, required: str = "") -> _Spec:
    return _Spec(short, frozenset(allowed.split()), frozenset(required.split()))


_SPECS: dict[str, _Spec] = {
    "goto": _spec("url", "url", "url"),
    "click": _spec("target", "target nth force", "target"),
    "fill": _spec(None, "target value", "target value"),
    "type": _spec(None, "target value delay_ms clear", "target value"),
    "select": _spec(None, "target value label", "target"),
    "check": _spec("target", "target nth force", "target"),
    "uncheck": _spec("target", "target nth force", "target"),
    "press": _spec("key", "target key", "key"),
    "wait_for": _spec("target", "target state timeout_ms nth", "target"),
    "wait_for_url": _spec("url", "url timeout_ms", "url"),
    "wait_ms": _spec("ms", "ms", "ms"),
    "for_each": _spec(None, "items as steps", "items steps"),
    "if_visible": _spec(None, "target then else timeout_ms", "target"),
    "pause": _spec("message", "message"),
    "screenshot": _spec("path", "path", "path"),
    "assert_visible": _spec("target", "target timeout_ms nth", "target"),
    "click_until": _spec(None, "target until max delay_ms", "target until"),
    "group": _spec(None, "steps", "steps"),
}
_INT_PARAMS = {"nth", "delay_ms", "timeout_ms", "ms", "max"}
_BOOL_PARAMS = {"force", "clear"}
_STR_PARAMS = {"target", "value", "label", "url", "key", "message", "path", "as", "items", "until"}


class Step(BaseModel):
    """One parsed step. ``params`` holds the normalised options for ``kind``."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    commit: bool = False
    optional: bool = False
    description: str | None = None
    when: str | None = None
    steps: list["Step"] = Field(default_factory=list)  # for_each body / if_visible "then"
    else_steps: list["Step"] = Field(default_factory=list)
    where: str = ""  # location in the YAML, e.g. per_transaction[2].for_each.steps[0]

    def walk(self) -> Iterator["Step"]:
        yield self
        for s in (*self.steps, *self.else_steps):
            yield from s.walk()

    def summary(self) -> str:
        """Short human-readable form, e.g. ``click add_expense_button``."""
        p = self.params
        if self.kind == "for_each":
            return f"for_each {p['as']} in {p['items']}"
        bits = [str(p[k]) for k in ("target", "url", "key", "message", "path", "ms") if k in p]
        extra = [f"{k}={p[k]!r}" for k in ("value", "label", "state", "nth") if k in p]
        return " ".join([self.kind, *bits, *extra])


class VariableSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: bool = False
    default: str | None = None
    description: str = ""


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_timeout_ms: int = Field(10000, gt=0)
    slow_mo_ms: int = Field(0, ge=0)
    amount_decimal_separator: str = "."
    date_format: str = "%d-%m-%Y"
    login_check_timeout_ms: int = Field(5000, gt=0)
    browser: Literal["bundled", "chrome"] = "bundled"
    chrome_path: str | None = None


class GroupPicker(BaseModel):
    """Stage: let the user choose which group/list to work in (items are read from the app)."""

    model_config = ConfigDict(extra="forbid")
    url: str | None = None  # defaults to start_url
    items: str  # selector: one (or more) element per group
    value_attribute: str = "href"  # attribute holding the id, or "text"
    value_regex: str | None = None  # group 1 = value; items not matching are ignored
    label: str | None = None  # selector of a descendant whose text names the group
    variable: str  # flow variable that receives the chosen value
    wait_for: str | None = None  # selector to wait for before reading


class MembersSpec(BaseModel):
    """Stage: read the group's member names from the app."""

    model_config = ConfigDict(extra="forbid")
    url: str | None = None  # may use variables, including the picker's
    items: str  # text of each element = member name
    me: str | None = None  # the signed-in user (default payer)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logged_in_selector: str | None = None


# --------------------------------------------------------------------------- #
# Step parsing
# --------------------------------------------------------------------------- #

def _err(where: str, msg: str) -> FlowError:
    return FlowError(f"{where}: {msg}")


def _coerce_param(where: str, kind: str, key: str, value: Any) -> Any:
    if key in _INT_PARAMS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _err(where, f"'{key}' of {kind} must be an integer, got {value!r}")
        if key != "nth" and value < 0:
            raise _err(where, f"'{key}' of {kind} must be >= 0")
        return value
    if key in _BOOL_PARAMS:
        if not isinstance(value, bool):
            raise _err(where, f"'{key}' of {kind} must be true/false, got {value!r}")
        return value
    if key in _STR_PARAMS:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if not isinstance(value, str):
            raise _err(where, f"'{key}' of {kind} must be a string, got {value!r}")
        return value
    return value


def parse_steps(raw: Any, where: str) -> list[Step]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(where, f"expected a list of steps, got {type(raw).__name__}")
    return [parse_step(item, f"{where}[{i}]") for i, item in enumerate(raw)]


def parse_step(raw: Any, where: str) -> Step:
    if not isinstance(raw, dict):
        raise _err(where, f"a step must be a mapping like '- click: some_selector', got {raw!r}")
    kinds = [k for k in raw if k in _SPECS]
    unknown = [k for k in raw if k not in _SPECS and k not in _MODIFIERS]
    if unknown:
        hint = f" (valid step types: {', '.join(STEP_KINDS)}; modifiers: {', '.join(_MODIFIERS)})"
        raise _err(where, f"unknown key(s) {unknown}{hint}")
    if len(kinds) != 1:
        raise _err(where, f"a step needs exactly one action key, found {kinds or 'none'}")
    kind = kinds[0]
    where = f"{where}.{kind}"
    spec = _SPECS[kind]
    body = raw[kind]

    if isinstance(body, dict):
        params = dict(body)
    elif body is None:
        params = {}
    elif spec.short is not None:
        params = {spec.short: body}
    else:
        raise _err(where, f"{kind} needs options {sorted(spec.allowed)}, got scalar {body!r}")

    if kind == "group":  # modifiers may also be written inside the group mapping
        raw = dict(raw)
        for m in ("when", "optional", "description"):
            if m in params:
                raw[m] = params.pop(m)

    bad = [k for k in params if k not in spec.allowed]
    if bad:
        raise _err(where, f"unknown option(s) {bad}; allowed: {sorted(spec.allowed)}")
    missing = [k for k in sorted(spec.required) if k not in params]
    if missing:
        raise _err(where, f"missing required option(s) {missing}")

    nested: dict[str, list[Step]] = {"steps": [], "else": []}
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in ("steps", "then", "else"):
            nested["else" if key == "else" else "steps"] = parse_steps(value, f"{where}.{key}")
        elif key == "as":
            out[key] = _coerce_param(where, kind, key, value)
        else:
            out[key] = _coerce_param(where, kind, key, value)

    if kind == "select" and ("value" in out) == ("label" in out):
        raise _err(where, "select needs exactly one of 'value' or 'label'")
    if kind == "wait_for":
        out.setdefault("state", "visible")
    if "state" in out and out["state"] not in WAIT_STATES:
        raise _err(where, f"state must be one of {WAIT_STATES}, got {out['state']!r}")
    if kind == "for_each":
        out.setdefault("as", "p")
        if out["items"] not in ITEM_SOURCES:
            raise _err(where, f"items must be one of {ITEM_SOURCES}, got {out['items']!r}")
        if not _IDENT_RE.match(out["as"]):
            raise _err(where, f"'as' must be a simple identifier, got {out['as']!r}")
        if not nested["steps"]:
            raise _err(where, "for_each needs a non-empty 'steps' list")
    if kind == "click_until":
        out.setdefault("max", 24)
        out.setdefault("delay_ms", 150)
        if out["max"] < 1:
            raise _err(where, "'max' of click_until must be >= 1")
    if kind == "group" and not nested["steps"]:
        raise _err(where, "group needs a non-empty 'steps' list")
    if kind == "if_visible" and not nested["steps"]:
        raise _err(where, "if_visible needs a non-empty 'then' list")

    mods: dict[str, Any] = {}
    for m in _MODIFIERS:
        if m in raw:
            v = raw[m]
            if m in ("commit", "optional") and not isinstance(v, bool):
                raise _err(where, f"'{m}' must be true/false, got {v!r}")
            if m in ("description", "when"):
                v = None if v is None else str(v)
            mods[m] = v
    return Step(
        kind=kind, params=out, steps=nested["steps"], else_steps=nested["else"], where=where, **mods
    )


# --------------------------------------------------------------------------- #
# Flow
# --------------------------------------------------------------------------- #

_TOP_LEVEL = {
    "name", "description", "start_url", "variables", "settings", "participant_aliases", "login",
    "selectors", "setup", "per_transaction", "dry_run_cleanup", "teardown", "group_picker", "members",
}


class Flow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    start_url: str
    variables: dict[str, VariableSpec] = Field(default_factory=dict)
    settings: Settings = Field(default_factory=Settings)
    participant_aliases: dict[str, str] = Field(default_factory=dict)
    login: Login = Field(default_factory=Login)
    group_picker: GroupPicker | None = None
    members: MembersSpec | None = None
    selectors: dict[str, str] = Field(default_factory=dict)
    setup: list[Step] = Field(default_factory=list)
    per_transaction: list[Step] = Field(default_factory=list)
    dry_run_cleanup: list[Step] = Field(default_factory=list)
    teardown: list[Step] = Field(default_factory=list)
    source: str | None = None

    # ---- introspection ------------------------------------------------- #
    def sections(self) -> dict[str, list[Step]]:
        return {
            "setup": self.setup,
            "per_transaction": self.per_transaction,
            "dry_run_cleanup": self.dry_run_cleanup,
            "teardown": self.teardown,
        }

    def all_steps(self) -> Iterator[Step]:
        for steps in self.sections().values():
            for s in steps:
                yield from s.walk()

    def loop_var_names(self) -> set[str]:
        return {s.params["as"] for s in self.all_steps() if s.kind == "for_each"}

    def has_commit_step(self) -> bool:
        return any(inner.commit for top in self.per_transaction for inner in top.walk())

    def required_variables(self) -> list[str]:
        return [n for n, v in self.variables.items() if v.required and v.default is None]

    @property
    def picker_variable(self) -> str | None:
        """Variable that the ``group_picker`` stage fills in (None without a picker)."""
        return self.group_picker.variable if self.group_picker else None

    def missing_variables(self, given: dict[str, str] | None, *, picker_provides: bool = False) -> list[str]:
        """Required variables that are neither given nor defaulted.

        With ``picker_provides`` the group picker's variable counts as satisfied (it is set when
        the picker runs).
        """
        given = given or {}
        skip = {self.picker_variable} if picker_provides else set()
        return [n for n in self.required_variables() if n not in given and n not in skip]

    def resolve_variables(self, given: dict[str, str] | None, *, partial: bool = False) -> dict[str, str]:
        """Apply defaults; raise ``ValueError`` listing missing required variables.

        ``partial=True`` leaves missing required variables empty instead of raising (used before the
        group picker has run).
        """
        given = dict(given or {})
        out: dict[str, str] = {}
        missing: list[str] = []
        for name, spec in self.variables.items():
            if name in given:
                out[name] = str(given.pop(name))
            elif spec.default is not None:
                out[name] = spec.default
            elif spec.required:
                missing.append(name)
            else:
                out[name] = ""
        if missing and partial:
            for n in missing:
                out[n] = ""
            missing = []
        if missing:
            details = "; ".join(
                f"{n} ({self.variables[n].description})" if self.variables[n].description else n
                for n in missing
            )
            raise ValueError(f"Missing required variable(s) for flow '{self.name}': {details}. "
                             "Pass them with --var name=value.")
        out.update({k: str(v) for k, v in given.items()})  # extras are harmless
        return out

    # ---- rendering helpers --------------------------------------------- #
    def alias(self, name: str) -> str:
        """Name as shown in the app (``participant_aliases`` lookup, case-insensitive fallback)."""
        if name in self.participant_aliases:
            return self.participant_aliases[name]
        lowered = {k.lower(): v for k, v in self.participant_aliases.items()}
        return lowered.get(name.lower(), name)

    def base_context(self, variables: dict[str, str]) -> dict[str, str]:
        ctx = {k: str(v) for k, v in variables.items()}
        ctx["flow.name"] = self.name
        ctx.update(date_parts("today", _date.today()))
        return ctx

    def context(
        self,
        variables: dict[str, str],
        transaction: Transaction | None = None,
        all_participants: list[str] | tuple[str, ...] = (),
    ) -> dict[str, str]:
        ctx = self.base_context(variables)
        if transaction is None:
            return ctx
        raw = f"{transaction.amount:.2f}"
        excluded = self.excluded_participants(transaction, all_participants)
        ctx.update({
            "transaction.name": transaction.name,
            "transaction.amount_raw": raw,
            "transaction.amount": raw.replace(".", self.settings.amount_decimal_separator),
            "transaction.date": (
                transaction.date.strftime(self.settings.date_format) if transaction.date else ""
            ),
            **date_parts("transaction", transaction.date),
            "transaction.no_date": "" if transaction.date else "1",
            "transaction.paid_by": self.alias(transaction.paid_by) if transaction.paid_by else "",
            "transaction.participant_count": str(len(transaction.participants)),
            "transaction.participants_csv": ", ".join(self.alias(p) for p in transaction.participants),
            "transaction.excluded_participants_csv": ", ".join(self.alias(p) for p in excluded),
        })
        return ctx

    @staticmethod
    def excluded_participants(
        transaction: Transaction, all_participants: list[str] | tuple[str, ...]
    ) -> list[str]:
        mine = {p.lower() for p in transaction.participants}
        return [p for p in all_participants if p.lower() not in mine]

    def item_lists(
        self, transaction: Transaction, all_participants: list[str] | tuple[str, ...]
    ) -> dict[str, list[str]]:
        return {
            "transaction.participants": list(transaction.participants),
            "transaction.excluded_participants": self.excluded_participants(
                transaction, all_participants
            ),
            "all_participants": list(all_participants),
        }

    def bind_loop_var(
        self,
        ctx: dict[str, str],
        var: str,
        name: str,
        index: int | None = None,
        items: str | None = None,
    ) -> dict[str, str]:
        """Bind a loop variable. Over ``transaction.participants`` (``items``) it also binds
        ``{var}_amount`` / ``{var}_amount_raw``: the item's equal share of the total."""
        child = dict(ctx)
        child[var] = self.alias(name)
        child[f"{var}_raw"] = name
        if items == "transaction.participants" and index is not None:
            n = int(ctx["transaction.participant_count"])
            share = equal_split(Decimal(ctx["transaction.amount_raw"]), n)[index]
            raw = f"{share:.2f}"
            child[f"{var}_amount_raw"] = raw
            child[f"{var}_amount"] = raw.replace(".", self.settings.amount_decimal_separator)
        return child

    def resolve_target(self, target: str) -> str:
        """Selector key -> its (still templated) selector; otherwise the raw selector."""
        return self.selectors.get(target, target)


# --------------------------------------------------------------------------- #
# Loading and validation
# --------------------------------------------------------------------------- #

def _known_names(flow: Flow, extra: set[str]) -> set[str]:
    return {"flow.name", *_TRANSACTION_KEYS, *flow.variables, *extra}


def _check_template(flow: Flow, text: str, scope: set[str], where: str) -> None:
    known = _known_names(flow, scope)
    for key in placeholders(text):
        if key not in known:
            hint = ""
            if key in flow.selectors:
                hint = f" ('{key}' is a selector, not a variable)"
            raise FlowError(
                f"{where}: unknown placeholder {{{key}}} in {text!r}{hint}. "
                f"Declare it under 'variables:' or use one of: {', '.join(sorted(known))}. "
                "Write {{ and }} for literal braces."
            )


def _check_target(flow: Flow, target: str, scope: set[str], where: str) -> None:
    if target in flow.selectors:
        # selector templates are checked once, with all loop vars allowed; but the per-share
        # amount keys exist only inside a loop over transaction.participants
        for key in placeholders(flow.selectors[target]):
            if key in _amount_keys(flow) and key not in scope:
                raise FlowError(
                    f"{where}: selector '{target}' uses {{{key}}}, which is only defined inside "
                    "a for_each over transaction.participants"
                )
        return
    if _IDENT_RE.match(target) and target not in _HTML_TAGS:
        near = ", ".join(sorted(flow.selectors)) or "(no selectors defined)"
        raise FlowError(f"{where}: unknown selector key '{target}'. Defined selectors: {near}")
    _check_template(flow, target, scope, where)


def _amount_keys(flow: Flow) -> set[str]:
    out: set[str] = set()
    for v in flow.loop_var_names():
        out |= {f"{v}_amount", f"{v}_amount_raw"}
    return out


def _check_steps(flow: Flow, steps: list[Step], scope: set[str]) -> None:
    for s in steps:
        p = s.params
        if s.when:
            _check_template(flow, s.when, scope, f"{s.where}.when")
        for key in ("target", "until"):
            if key in p:
                _check_target(flow, p[key], scope, s.where)
        for key in ("value", "label", "url", "message", "path"):
            if key in p:
                _check_template(flow, p[key], scope, f"{s.where}.{key}")
        if s.kind == "for_each":
            inner = scope | {p["as"], f"{p['as']}_raw"}
            if p["items"] == "transaction.participants":
                inner |= {f"{p['as']}_amount", f"{p['as']}_amount_raw"}
            else:  # same name reused for another list must not inherit an outer _amount
                inner -= {f"{p['as']}_amount", f"{p['as']}_amount_raw"}
            _check_steps(flow, s.steps, inner)
        elif s.kind in ("if_visible", "group"):
            _check_steps(flow, s.steps, scope)
            _check_steps(flow, s.else_steps, scope)


def _validate_flow(flow: Flow) -> None:
    loop_vars = flow.loop_var_names()
    loop_scope = loop_vars | {f"{v}_raw" for v in loop_vars}
    for st in flow.all_steps():
        if st.kind == "for_each" and st.params["items"] == "transaction.participants":
            loop_scope |= {f"{st.params['as']}_amount", f"{st.params['as']}_amount_raw"}
    for key, sel in flow.selectors.items():
        _check_template(flow, sel, loop_scope, f"selectors.{key}")
    _check_template(flow, flow.start_url, set(), "start_url")
    sel = flow.login.logged_in_selector
    if sel is not None and sel not in flow.selectors:
        raise FlowError(
            f"login.logged_in_selector: '{sel}' is not a key in 'selectors' "
            f"(defined: {', '.join(sorted(flow.selectors)) or 'none'})"
        )
    for section, steps in flow.sections().items():
        _check_steps(flow, steps, set())
    for name, spec in flow.variables.items():
        if spec.required and spec.default is not None:
            raise FlowError(f"variables.{name}: 'required' and 'default' are mutually exclusive")
    _validate_stages(flow)


def _check_stage_selector(flow: Flow, text: str, where: str) -> None:
    """Stage selectors are raw selectors or selector keys; templates may use variables."""
    _check_template(flow, flow.selectors.get(text, text), set(), where)


def _validate_stages(flow: Flow) -> None:
    gp, members = flow.group_picker, flow.members
    if gp is not None:
        if gp.variable not in flow.variables:
            raise FlowError(
                f"group_picker.variable: '{gp.variable}' must be declared under 'variables:' "
                f"(declared: {', '.join(sorted(flow.variables)) or 'none'})"
            )
        if gp.value_regex is not None:
            try:
                rx = re.compile(gp.value_regex)
            except re.error as exc:
                raise FlowError(f"group_picker.value_regex: invalid regular expression: {exc}") from exc
            if rx.groups < 1:
                raise FlowError("group_picker.value_regex: needs a capture group; group 1 is the value")
        if gp.url is not None:
            _check_template(flow, gp.url, set(), "group_picker.url")
            if gp.variable in placeholders(gp.url):
                raise FlowError(
                    f"group_picker.url: cannot use {{{gp.variable}}}; it is chosen after this page is shown"
                )
        for key in ("items", "label", "wait_for"):
            val = getattr(gp, key)
            if val is not None:
                _check_stage_selector(flow, val, f"group_picker.{key}")
    if members is not None:
        if members.url is not None:
            _check_template(flow, members.url, set(), "members.url")
        for key in ("items", "me"):
            val = getattr(members, key)
            if val is not None:
                _check_stage_selector(flow, val, f"members.{key}")


def parse_flow(raw: Any, source: str | None = None) -> Flow:
    """Validate a decoded YAML document and build a :class:`Flow`."""
    if not isinstance(raw, dict):
        raise FlowError("flow file must be a YAML mapping at the top level")
    unknown = sorted(set(raw) - _TOP_LEVEL)
    if unknown:
        raise FlowError(f"unknown top-level key(s) {unknown}; allowed: {sorted(_TOP_LEVEL)}")
    data = dict(raw)
    for section in ("setup", "per_transaction", "dry_run_cleanup", "teardown"):
        data[section] = parse_steps(data.get(section), section)
    for section in ("variables", "settings", "participant_aliases", "login", "selectors", "group_picker", "members"):
        if section in data and data[section] is None:
            data.pop(section)
    if isinstance(data.get("variables"), dict):
        data["variables"] = {
            k: ({} if v is None else v) for k, v in data["variables"].items()
        }
    if isinstance(data.get("selectors"), dict):
        for k, v in data["selectors"].items():
            if not isinstance(v, str):
                raise FlowError(f"selectors.{k}: selector must be a string, got {v!r}")
    if isinstance(data.get("participant_aliases"), dict):
        data["participant_aliases"] = {str(k): str(v) for k, v in data["participant_aliases"].items()}
    data["source"] = source
    try:
        flow = Flow(**data)
    except ValidationError as exc:
        lines = [
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()
        ]
        raise FlowError("invalid flow definition:\n  " + "\n  ".join(lines)) from None
    _validate_flow(flow)
    return flow


def load_flow(path: str | Path) -> Flow:
    """Load and validate a flow YAML file. Raises :class:`FlowError` with a precise location."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FlowError(f"cannot read flow file {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FlowError(f"{path}: invalid YAML: {exc}") from exc
    try:
        return parse_flow(raw, source=str(path))
    except FlowError as exc:
        raise FlowError(f"{path}: {exc}") from None


# --------------------------------------------------------------------------- #
# Finding flows by name
# --------------------------------------------------------------------------- #

def _flow_names(directory: Path, *, hidden: bool = False) -> list[str]:
    if not directory.is_dir():
        return []
    names = sorted(p.stem for p in directory.glob("*.yaml") if hidden or not p.name.startswith("_"))
    return names


def list_flows() -> dict[str, list[str]]:
    """``{"bundled": [...], "user": [...]}`` flow names."""
    from expenser.paths import bundled_flows_dir, user_flows_dir

    return {"bundled": _flow_names(bundled_flows_dir()), "user": _flow_names(user_flows_dir())}


def resolve_flow(name_or_path: str | Path) -> Path:
    """An existing file path, else a bundled flow by name, else ``~/.expenser/flows/<name>.yaml``."""
    from expenser.paths import bundled_flows_dir, user_flows_dir

    raw = str(name_or_path).strip()
    direct = Path(raw).expanduser()
    if raw and direct.is_file():
        return direct
    stem = raw
    for suffix in (".yaml", ".yml"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    if stem and not any(c in stem for c in "/\\"):
        for directory in (bundled_flows_dir(), user_flows_dir()):
            for suffix in (".yaml", ".yml"):
                candidate = directory / f"{stem}{suffix}"
                if candidate.is_file():
                    return candidate
    found = list_flows()
    lines = [f"Flow '{raw}' not found (not a file, not a bundled flow, not in {user_flows_dir()})."]
    lines.append("Bundled flows: " + (", ".join(found["bundled"]) or "(none)"))
    lines.append(f"Your flows ({user_flows_dir()}): " + (", ".join(found["user"]) or "(none)"))
    raise FlowError("\n".join(lines))
