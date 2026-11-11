# expenser

Add shared expenses to a group in a web expense app (built for Splitser) without typing them in.

- **Excel mode**: fill in a spreadsheet (or CSV); expenser enters every row for you.
- **Screenshot mode**: give it screenshots of bank/card transactions; Claude reads them into a spreadsheet you review first.
- Either way you pick your group from the app and choose whether to confirm each expense, save all, or do a dry run.

# For users

## Requirements

- **Google Chrome** installed (expenser drives your own Chrome; nothing else to download).
- Screenshot mode only: an **Anthropic Console** account (usage is billed per use as API usage; a claude.ai subscription does not count). Excel mode needs no account.

You do not need to install Python: the installer sets everything up.

## Install

macOS / Linux (Terminal):

```bash
curl -LsSf https://raw.githubusercontent.com/Gamezordd/expenser/main/scripts/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/Gamezordd/expenser/main/scripts/install.ps1 | iex
```

Run the same command again to **update** to the latest version.

Got a wheel file (`expenser-X.Y.Z-py3-none-any.whl`, e.g. from the
[Releases page](https://github.com/Gamezordd/expenser/releases)) and the install script instead?

```bash
sh install.sh ./expenser-0.2.0-py3-none-any.whl          # macOS / Linux
```
```powershell
.\install.ps1 -Source .\expenser-0.2.0-py3-none-any.whl   # Windows
```

The installer installs [uv](https://docs.astral.sh/uv/) if needed, installs expenser with its own Python (git is not needed), and checks for Chrome. Then **open a new terminal**.

## First run

Type `expenser` and follow the steps:

1. A Chrome window opens: sign in to the expense app there.
2. Pick your group from the app.
3. Choose your source: an Excel/CSV file, screenshots, or "create a template".
4. Choose the files (file dialog or paste a path), review, then choose how to push: ask before each expense, save all, or dry run.

Files: Excel files you create go to `~/Expenser`; your login profile and state live in `~/.expenser`. On Windows `~` is your user folder.

Problems? Run `expenser doctor`: it checks Chrome, folders, credentials and your terminal and says how to fix each item.

## Uninstall

```bash
sh uninstall.sh        # macOS / Linux  (or: uv tool uninstall expenser)
```
```powershell
.\uninstall.ps1        # Windows
```

`~/.expenser` and `~/Expenser` are left in place; delete them yourself if you want.

## Excel mode (push from your own spreadsheet)

Already have the expenses in a spreadsheet? Skip screenshots, Claude and the review step:

```bash
expenser datatable-template expenses.xlsx [-p people.txt] [--target-currency INR]   # optional starting point
expenser run --datatable expenses.xlsx --flow splitser --dry-run          # try it first
expenser run --datatable expenses.xlsx --flow splitser --confirm-each [--yes] [--rate THB=2.58]
```

Identical rows (same name, amount, date, payer and participants) can be real, separate purchases, so they are
pushed separately by default. When some exist you are asked "Remove the duplicates (keep one of each)?"
(default No). Skip the question with `--dedupe` (remove) or `--keep-duplicates` (keep); both work on `run` and
`push`, and without a terminal the rows are kept with a note. Re-running the same file pushes nothing twice.

No Anthropic login, no participants file and no approval sidecar are needed. Because nothing is
reviewed in Excel, check the table printed by the command, and use `--dry-run` (fill forms, do not submit)
or `--confirm-each` (ask before each submission) when in doubt. `-p` is optional here, as it is for `push`.

**Accepted files**: `.xlsx`/`.xlsm` (the sheet named "Transactions", else the first sheet) and `.csv`.
The header row is the first row containing an amount and a name column (a title row above is fine); headers
are matched ignoring case, spaces and punctuation, in any column order. Reading stops at the first blank row
or a row named TOTAL. Formulas are not evaluated, so use plain values.

| Field | Accepted headers |
|---|---|
| Name (required) | Transaction Name, Transaction, Description, Name, What, Expense |
| Amount (required) | Amount, Amount (XXX) (XXX sets the target currency), Original Amount (with Rate) |
| Paid by (required) | Paid By, Payer, Paid, Who Paid |
| Participants (required) | Participants, Split Between, Split, For, For Whom, Shared With |
| Date | Date, Transaction Date |
| Currency | Currency, Original Currency, Cur |
| Rate | Rate, Exchange Rate, FX Rate |
| Include | Include (Y/N), Push, or Skip (Y means skip) |

Amounts may be `1,234.50`, `1234,50`, `₹1,200` or `THB 450` (an embedded currency is used when there is no
Currency cell). Dates may be Excel dates, `2026-11-05`, `5 Nov 2026`, `Nov 5, 2026` or `dd-mm-yyyy` / `dd/mm/yyyy`;
ambiguous numeric dates are read day-first (05/11/2026 is 5 November). Participants cells accept names,
`ALL`, and `ALL -Bob` / `ALL except Bob`.

**Who the members are**: with `-p FILE`, names are resolved against that file (unknown names are errors).
Otherwise the "Participants" list on an `Info` sheet is used if the workbook has one (files written by
`expenser extract` and `datatable-template -p` do); otherwise the members are every Paid By and Participants
name in the table (exact spelling, case-insensitive de-duplication), and `ALL` means all of them. The command
prints which source it used.

**Currencies**: the target currency is `--target-currency`, else the `Amount (XXX)` header, else
`$EXPENSER_TARGET_CURRENCY`, else INR. Rows with no currency are in the target currency. A foreign row uses its
Rate cell (1 unit of the currency = rate target), else `--rate CODE=RATE`, else you are asked (or, without a
terminal, the command stops). `--source-currency` forces the currency of every row. Amounts are
`ROUND_HALF_UP(original x rate, 2)`. Any problem in any row is listed with its spreadsheet row number and
nothing is pushed.

## Screenshot mode

Needs an Anthropic Console login or API key. Sign in with `expenser auth login` (requires the `ant` CLI: macOS `brew install anthropics/tap/ant`; Linux/Windows download from https://github.com/anthropics/anthropic-cli/releases), or put `ANTHROPIC_API_KEY=sk-ant-...` in a `.env` file in the working directory. A set `ANTHROPIC_API_KEY` (even empty) overrides a login; `expenser auth status` warns about this. Billing is API usage, not a claude.ai subscription.

Claude reads the screenshots and writes an Excel file for you to edit; the file opens in your default app and you approve in the terminal. Group members come from the app; for scripting, `-p FILE` (one name per line, or a CSV) still supplies them.

### The Excel file

Columns: `Date | Transaction Name | Original Amount | Original Currency | Rate | Amount (<TARGET>) | Paid By | Participants | Include | Notes`.

- **Participants**: comma-separated names (`Alice, Bob`), `ALL`, or exclusions: `ALL -Bob` / `ALL except Bob, Carol`. Case-insensitive; unique prefixes work.
- **Paid By**: required, must be a participant.
- **Include**: `N` skips the row.
- **Original Amount / Original Currency / Rate**: what the screenshot showed and the rate used (1 unit of the original currency in the target currency). **Amount (<TARGET>)** is `=ROUND(Original Amount x Rate, 2)` and is what gets pushed; you can overwrite it with a plain number. The `TOTAL` row is ignored.
- **Notes**: Claude's remarks; ignored on read. Don't rename headers.

### Currencies and conversion

Splitser books whatever you type in the list's own currency, so amounts are converted *before* review and push.

1. Claude detects an ISO currency code per transaction (฿/THB, ₹/INR, € ...; ambiguous symbols such as `$` are guessed from context and flagged in the row's Notes). `--source-currency THB` states the currency up front and forces it on every row.
2. expenser lists the detected currencies (row count and total) and asks for a rate for each one that is not the target, e.g. `THB detected in 5 transactions (total 3,450.00 THB). Rate: 1 THB = ? INR:`. Rates are positive, up to 6 decimals; a comma decimal works.
3. Pass rates up front with `--rate THB=2.58` (repeatable; `THB:2.58` also works). Without a terminal (no tty) a missing rate is an error telling you which `--rate` to add.
4. Amounts are converted (`original x rate`, rounded half up to 0.01) and written to the Excel with Original Amount / Currency / Rate; the Info sheet lists the target currency and rates used. Change a Rate in Excel before approving if needed.

`--target-currency` (default `INR`, or `$EXPENSER_TARGET_CURRENCY`) sets the currency pushed; it must match the Splitser list currency. `extract` and `run` accept `--target-currency`, `--rate`, `--source-currency`; `review` and `push` infer the target from the `Amount (XXX)` header.

### Approval flow

After extraction the file opens in your default app. Back in the terminal: `[a]pprove  [r]eload  [o]pen again  [q]uit`. Approve re-reads the file, reports validation problems (with Excel row numbers), and otherwise asks for a final `y/N`. On approval a sidecar `<file>.xlsx.approved.json` stores the transactions, timestamp and a SHA-256 of the file. `push` re-runs the review if the file changed since approval.

# For the author / advanced

## Dev setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/playwright install chromium
.venv/bin/pytest
```

Playwright only drives the user's installed Google Chrome over CDP by default, so end users never run `playwright install`; the bundled Chromium is for tests and the optional `browser: bundled` flow mode. The Claude API is always mocked in tests.

## Build and share

```bash
scripts/build.sh      # sdist + wheel into dist/, prints the wheel to share
```

Friends install the latest `main` with the one-line commands under [Install](#install) (the installers default to
`https://github.com/Gamezordd/expenser/archive/refs/heads/main.zip`, so pushing to `main` ships an update; they re-run
the command to get it). Alternatively share the wheel together with `scripts/install.sh` / `install.ps1`. Pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`, which attaches the wheel, sdist and install scripts to a GitHub Release. CI (`ci.yml`) runs the tests on Linux/macOS/Windows and smoke-tests the built wheel.

## Scriptable commands

```bash
expenser run shot1.png shot2.png -p people.txt --flow splitser --payer Alice --hint "Dave skipped the restaurant"
expenser extract shot1.png -p people.txt --out trip.xlsx
expenser review trip.xlsx -p people.txt
expenser push trip.xlsx --flow splitser --dry-run --var list_id=...
expenser run --datatable expenses.xlsx --flow splitser --confirm-each
expenser datatable-template expenses.xlsx
expenser flow record|validate|login ...
expenser doctor
```

Common options: `--payer`, `--hint`, `--model`, `--out`, `--no-review`, `--no-open`, `--var key=value` (repeatable), `--dry-run`, `--confirm-each`, `--headless`; `push` also `--force` and `--yes`. `python -m expenser` works too.

## Model and cost

Default model is `claude-opus-5-5`; override with `--model` or `EXPENSER_MODEL`. One request is made per run (all screenshots together). Token usage is printed after extraction.

## Defining flows for other apps

Flows are YAML files describing the browser steps: see [docs/flows.md](docs/flows.md) and [docs/wizard.md](docs/wizard.md). `expenser flow record` captures one by clicking through the app; `expenser flow validate` checks it. Splitser signs in with Google, which blocks Playwright-launched browsers, so its flow uses `browser: chrome` (your real Chrome over CDP); see "Browser modes" in docs/flows.md.

## Security

The login profile for the expense app is stored locally in `~/.expenser/chrome-profiles`. Never share or commit that folder. API keys belong in `.env` (not committed) or your shell.
