# Flows: driving any expense app from YAML

A **flow** is a YAML file that tells expenser how to add one transaction to a web app (Splitser, Tricount, Splitwise, ...). The Python code is generic; everything app-specific lives in the flow.

## Concepts

- **Selectors** (the "HTML accessors") are named locators in the `selectors:` mapping. Any Playwright selector syntax works: CSS, `text="Save"`, `role=button[name=/save|opslaan/i]`, `xpath=...`, chains like `label:has-text("Bob") >> input`. Steps refer to them by key; a target that is not a key is used as a raw selector.
- **Steps** are lists under `setup`, `per_transaction`, `dry_run_cleanup`, `teardown`. `setup` runs once after login, `per_transaction` once per transaction, `dry_run_cleanup` replaces committing in `--dry-run` (default: reload `start_url` and re-run `setup`), `teardown` once at the end.
- **Templating**: `{name}` placeholders in selectors and step values, rendered from a flat dictionary (no eval, no `str.format`). Unknown placeholders fail with the step location. Write `{{` / `}}` for literal braces (e.g. regex `a{{2}}`).

  | Placeholder | Value |
  |---|---|
  | `{transaction.name}` | name |
  | `{transaction.amount}` | amount with 2 decimals and `settings.amount_decimal_separator` (`12,50`) |
  | `{transaction.amount_raw}` | `12.50` |
  | `{transaction.date}` | per `settings.date_format`, empty if none |
  | `{transaction.day}`, `{transaction.day2}` | day of month without / with leading zero (`5` / `05`); empty if no date |
  | `{transaction.month}`, `{transaction.month2}` | month number without / with leading zero |
  | `{transaction.month_name}`, `{transaction.month_abbr}` | English `March` / `Mar` (locale-independent) |
  | `{transaction.year}`, `{transaction.date_iso}` | `2026`, `2026-03-05` |
  | `{transaction.no_date}` | `1` when the transaction has no date, otherwise empty |
  | `{today.day}`, `{today.day2}`, `{today.month}`, `{today.month2}`, `{today.month_name}`, `{today.month_abbr}`, `{today.year}`, `{today.date_iso}` | the same pieces for today's date (always set) |
  | `{transaction.paid_by}` | payer (alias applied), empty if none |
  | `{transaction.participants_csv}`, `{transaction.excluded_participants_csv}` | comma-joined names |
  | `{transaction.participant_count}` | number of participants |
  | `{flow.name}` | flow name |
  | `{group_url}` ... | your `variables:` (passed with `--var group_url=...`) |
  | `{p}`, `{p_raw}` | loop variable from `for_each` (aliased / original name) |
  | `{p_amount}`, `{p_amount_raw}` | only inside a `for_each` over `transaction.participants` (`as: p`): that participant's equal share of the total, formatted like `{transaction.amount}` (separator applied) / dot-decimal. Computed in whole cents; the remainder cents go one each to the FIRST participants (100.00 among 3 = 33.34, 33.33, 33.33), so the shares always sum exactly to the amount. Using them in loops over `excluded_participants` / `all_participants` is a validation error. |

  `transaction.excluded_participants` = all participants minus the transaction's participants. `participant_aliases` maps names in your participants file to names shown in the app.
- **Commit / dry-run**: mark the step(s) that actually save with `commit: true` (typically the Save click and the wait that depends on it). In `--dry-run` they are skipped, logged, and a screenshot is stored; `--confirm-each` asks before the first commit step of each transaction. A flow without any `commit: true` step refuses to dry-run.
- **Ledger / dedupe**: each successfully pushed transaction is appended to `~/.expenser/pushed/<flow>-<hash>.jsonl` (fingerprint, scope, name, amount, timestamp). The file is scoped by the flow name plus a hash of *all resolved flow variables* (sorted), so the same expense pushed to a different group (`list_id`) is not a duplicate, while pushing it again to the same group is skipped unless `--force`. Dry runs never write it. Old ledger lines without a `scope` are ignored.
- **Persistent login profile**: Chromium runs with a profile in `~/.expenser/profiles/<flow name>`. If `login.logged_in_selector` is not visible, expenser prints "Sign in to <app> in the browser window..." and polls until the selector appears (no Enter needed, Ctrl+C cancels); the session then persists in the profile. Headless runs fail immediately with a clear message when not logged in.

## File layout

```yaml
name: myapp
description: ...
start_url: "{group_url}"
variables:
  group_url: {required: true, description: "..."}   # or default: "x"
settings: {default_timeout_ms: 10000, slow_mo_ms: 100, amount_decimal_separator: ",", date_format: "%d-%m-%Y", login_check_timeout_ms: 5000}
participant_aliases: {Bobby: Bob}
login: {logged_in_selector: group_header}
group_picker: {...}   # optional stage, see below
members: {...}        # optional stage, see below
selectors: {group_header: "...", ...}
setup: []
per_transaction: []
dry_run_cleanup: []
teardown: []
```

## Stages: group picker and members (used by the wizard)

Two optional top-level sections let the interactive wizard (`expenser`, see [wizard.md](wizard.md)) discover things from the app instead of asking for ids and name lists. They are generic: all app knowledge is in the YAML.

```yaml
variables:
  list_id: {required: true, description: "Group id"}

group_picker:                       # stage: choose which group/list to work in
  url: "https://app.splitser.com/lists"     # default: start_url; may use variables (not the picker's own)
  items: 'a[href*="/lists/"]'       # elements, one (or more) per group
  value_attribute: href             # attribute holding the id, or "text"
  value_regex: '/lists/([0-9a-f]{8}-...)'   # group 1 = value; items not matching are ignored
  label: 'h1, h2, h3, h4, h5'       # optional: text of the first matching descendant; fallback = first non-empty line of the item's text
  variable: list_id                 # flow variable that receives the chosen value (must be declared)
  wait_for: 'a[href*="/lists/"]'    # optional; waited for (settings.default_timeout_ms) before reading
members:                            # stage: read the group's member names
  url: "https://app.splitser.com/lists/{list_id}/transactions/create"   # may use variables incl. the picker's
  items: '#transaction-member-dropdown select option'    # text of each = a member name (trimmed, deduped, order kept)
  me: '#transaction-member-dropdown select option:checked'   # optional: the signed-in user (default payer)
```

Groups are deduplicated by value (an icon-only "edit" link to the same group does not replace the real label; labels are whitespace-collapsed and capped at 80 characters; empty labels fall back to the value). The picker's `variable` counts as satisfied for the "required variable" check while the picker runs; outside the wizard it must be given with `--var` (otherwise `run`/`push` stop with "Which group? Run `expenser` for the interactive mode, or pass --var list_id=..."). `flow validate`/`test` still need `--var list_id=...`.

Python entry points: `expenser.automation.stages.discover_groups(page, flow) -> [(label, value)]` and `discover_members(page, flow, variables) -> (members, me)`.

## Finding flows (`--flow`)

`--flow` (default `splitser`) accepts, in this order: an existing file path; the name of a bundled flow (`splitser` or `splitser.yaml`; they live in the package, `expenser/flows/`); the name of one of your own flows in `~/.expenser/flows/<name>.yaml`. Anything else is an error listing the available bundled and user flows. `expenser flow new NAME --url ...` writes to `~/.expenser/flows/NAME.yaml` by default. In Python: `expenser.automation.flow.resolve_flow(name_or_path)` and `list_flows()`.

## Where things are stored

`~/.expenser/` (override with `$EXPENSER_HOME`): `profiles/` and `chrome-profiles/` (browser logins), `pushed/` (ledger), `screenshots/` (failure and dry-run screenshots), `flows/` (your flows), `.env` (optional, loaded after `./.env`). Excel files you work with go to `~/Expenser/` (override with `$EXPENSER_OUTPUT_DIR`).

## Step reference

A step is a mapping with one action key plus optional modifiers. A bare scalar is shorthand for the main option (`- click: save_button`).

| Step | Options |
|---|---|
| `goto` | `url` |
| `click` | `target`, `nth`, `force` |
| `fill` | `target`, `value` |
| `type` | `target`, `value`, `delay_ms`, `clear` (default true) |
| `select` | `target`, `value` or `label` |
| `check` / `uncheck` | `target`, `nth`, `force` |
| `press` | `key`, optional `target` |
| `wait_for` | `target`, `state` (visible/hidden/attached/detached, default visible), `timeout_ms`, `nth` |
| `wait_for_url` | `url` (glob or `/regex/`), `timeout_ms` |
| `wait_ms` | `ms` |
| `for_each` | `items` (`transaction.participants` / `transaction.excluded_participants` / `all_participants`), `as` (default `p`), `steps` |
| `if_visible` | `target`, `then`, `else`, `timeout_ms` (default 1500) |
| `click_until` | `target` (clicked repeatedly), `until` (selector, templated, that must become visible), `max` (default 24), `delay_ms` (default 150, wait after each click). Zero clicks if `until` is already visible; fails with "not reached after N clicks" otherwise. Generic: e.g. paging a date picker with `target: prev_month_button, until: day_tile` |
| `group` | `steps` (`when`, `optional`, `description` may be written inside the group mapping or beside it); `when` / `optional` / `description` apply to the whole block (skipped when `when` is empty; with `optional` a failure inside is logged and the flow continues) |
| `pause` | `message`; waits for Enter (manual step) |
| `screenshot` | `path` (template) |
| `assert_visible` | `target`, `timeout_ms` |

Modifiers on any step: `commit: true`, `optional: true` (failure logged, not fatal), `description: "..."`, `when: "{template}"` (run only if the rendered text is non-empty).

Selectors are strict: a selector matching several elements fails the action; narrow it or add `nth`.

## Workflow for a new app

1. `expenser flow new myapp --url https://app.example.com` creates `~/.expenser/flows/myapp.yaml` from the commented template.
2. `expenser flow record myapp [--key add_button] [--participants people.txt]` opens a browser; for each key you navigate to where the element is visible, press Enter in the terminal to arm the picker, then click the element (the click is captured, not executed). You get ranked candidates with match counts and pick one or type your own. Selectors containing a participant name can be turned into `{p}` templates. Each key is saved immediately, comments preserved.
3. Write the `per_transaction` steps in the YAML (see `expenser/flows/_template.yaml` in the package).
4. `expenser flow validate myapp --var ...` checks the schema and counts matches for each selector on the page after login/setup. Selectors that only appear later (inside a dialog) show 0 here; use the next step for them.
5. `expenser flow test myapp --participants people.txt --step` runs one sample transaction in dry-run mode, pausing before each step, and reports the failing step.
6. `expenser push ... --dry-run`, then without `--dry-run`.

Alternative for discovering selectors: `.venv/bin/playwright codegen <url>` records your clicks as code; copy the locators into `selectors:`.

## Browser modes / Google login

`settings.browser` (override per run with `--browser`) selects how the browser is started:

- `bundled` (default): Playwright's own Chromium with a persistent profile in `~/.expenser/profiles/<flow>`.
- `chrome`: your real Google Chrome is started as a normal process (no automation flags) with a remote-debugging port and its own user-data-dir `~/.expenser/chrome-profiles/<flow>`; Playwright attaches over CDP and Chrome is closed when the run ends. Chrome is found via `--chrome-path`, `settings.chrome_path`, `$EXPENSER_CHROME`, then `PATH` and standard install locations. This profile is deliberately not your normal Chrome profile (Chrome 136+ refuses remote debugging on the default one), and it must not be open in another Chrome window while expenser runs.
- `--cdp-url http://127.0.0.1:9222`: attach to a browser you started yourself (`chrome --remote-debugging-port=9222 --user-data-dir=...`). expenser opens a new tab and closes only that tab afterwards; your browser stays open.

Why: Google refuses to sign in ("This browser or app may not be secure") in browsers launched by Playwright, because of automation flags and `navigator.webdriver`. Apps that log in with Google (Splitser) therefore need `browser: chrome`.

One-time login, then everything else reuses the stored session:

```bash
expenser flow login splitser --var list_id=<uuid>
```

It opens the browser at the start URL, you log in by hand, press Enter in the terminal, and the flow's `logged_in_selector` is checked. (The wizard does this automatically: it waits until you are signed in.) `--browser`, `--cdp-url` and `--chrome-path` are accepted by `push`, `run` and `flow record|validate|test`.

## Worked example (fictional app "SplitIt")

```yaml
name: splitit
start_url: "https://splitit.example/groups/{group_id}"
variables:
  group_id: {required: true, description: "Group id from the URL"}
settings: {amount_decimal_separator: ".", date_format: "%Y-%m-%d"}
login: {logged_in_selector: new_button}
selectors:
  new_button: "role=button[name='New expense']"
  dialog: "role=dialog"
  title: "role=textbox[name='Title']"
  amount: "role=spinbutton[name='Amount']"
  payer: "role=combobox[name='Paid by']"
  person_toggle: 'role=checkbox[name="{p}"]'
  save: "role=button[name='Save']"
per_transaction:
  - click: new_button
  - fill: {target: title, value: "{transaction.name}"}
  - fill: {target: amount, value: "{transaction.amount}"}
  - select: {target: payer, label: "{transaction.paid_by}"}
    when: "{transaction.paid_by}"
  - for_each:
      items: transaction.excluded_participants
      steps:
        - uncheck: person_toggle
  - click: save
    commit: true
  - wait_for: {target: dialog, state: hidden}
    commit: true
dry_run_cleanup:
  - press: Escape
  - wait_for: {target: dialog, state: hidden}
```

Then: `expenser flow test splitit --var group_id=42 --participants people.txt --step`.
