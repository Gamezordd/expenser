# The interactive wizard

Run `expenser` with no arguments (or `expenser start`) and answer the questions. Every menu is numbered; type a number and press Enter, or `q` to quit. Ctrl+C also leaves cleanly (the browser is always closed).

```
expenser start [--flow NAME] [--browser bundled|chrome] [--chrome-path PATH] [--cdp-url URL] [--no-dialog] [--var key=value]
```

`--flow` defaults to `splitser` (see [flows.md](flows.md) for how flows are found). `--no-dialog` never opens a file chooser window; you paste paths instead.

## Stages

1. **Welcome and browser.** Shows the version and flow, and opens one browser session that stays open for the whole wizard.
2. **Sign in.** Goes to the group list page. If you are not signed in, it prints "Sign in to <app> in the browser window..." and waits (no Enter needed). With Splitser a real Chrome window is used, so Google sign-in works; the login is remembered in `~/.expenser/chrome-profiles/`.
3. **Group.** Reads your groups from the page (flow section `group_picker`) and lists them. If none are found you can retry (navigate in the browser yourself first if needed) or paste the group's URL or id.
4. **Members.** Reads the group's member names and who you are (flow section `members`); you are the default payer. If that fails: retry, or type the names (comma-separated).
5. **Source.**
   - *Excel / CSV file*: no Anthropic account needed.
   - *Screenshots*: uses Claude through the Anthropic API. This needs an Anthropic Console account billed per use (a claude.ai subscription does not work). If you are not signed in you are offered to sign in now (`ant auth login`; if the `ant` tool is missing the install instructions are shown), to use an Excel file instead, or to go back.
   - *Template*: writes `~/Expenser/<group>-template.xlsx` with the group's members (you first), opens it, waits for you to fill it in and save it ("press Enter"), and continues as the Excel source. Asks before overwriting an existing file.
6. **File.** A native file chooser window opens when possible (needs tkinter and a display). Otherwise: paste or drag the file into the terminal. Quotes, backslash-escaped spaces, `~`, and several files (separated by newlines, `;` or quotes) are understood. An empty answer or `q` cancels.
7. **Data.**
   - Excel: names are checked against the real members. Problems are listed with the row; fix the file and choose "read it again", pick another file, or go back. Missing currency rates are asked for.
   - Screenshots: optional hint for Claude, currency and rate resolution, then the extracted expenses are saved as `~/Expenser/<group>-<timestamp>.xlsx`, opened, and you approve/reload/quit in the usual review loop.
8. **Identical rows.** If some rows are exactly the same (name, amount, date, payer, participants) you see them in a table and are asked "Remove the duplicates (keep one of each)?" (default: No, since two identical rows can be two real purchases).
9. **Push.** Shows the expenses and the total, then: *Ask before each save (recommended)*, *Save all*, *Dry run (fill forms, save nothing)*, or *Cancel*. A summary table follows. After a dry run you are asked "Push these for real now?". Expenses already pushed to the same group are skipped (ledger; see flows.md). The ledger counts occurrences: two identical rows are pushed as two expenses, and re-running the same file skips both.
10. **Next.** Add more to this group, choose another group, or quit.

## Where files go

- `~/Expenser/`: the Excel files you fill in or review (override with `$EXPENSER_OUTPUT_DIR`).
- `~/.expenser/`: browser logins, the pushed-expenses ledger, failure screenshots, your own flows, and an optional `.env` (override the folder with `$EXPENSER_HOME`).

## Scriptable commands still work

`expenser run`, `push`, `extract`, `review`, `datatable-template` and `flow ...` are unchanged, except: a flow that needs a group (Splitser's `list_id`) must get it via `--var list_id=...` (otherwise: "Which group? Run `expenser` for the interactive mode, or pass --var list_id=..."), and `extract`/`run` now write their Excel files to `~/Expenser/` by default.

## For developers

`expenser.wizard.run(ui=..., flow=..., variables=..., headless=..., before_close=page_hook)`; all prompts go through `expenser.wizard.UI` (inject `input_fn`, `choose_files_fn`, `open_fn`), which is how `tests/test_wizard.py` drives the whole wizard headless against local fixture pages.
