# Decisions log

Dated record of what was discovered and decided. Newest at the bottom of each day.

## 2026-10-06 (Phase 0)

### Environment and pinned versions
- Python **3.12.15** (uv-managed; `.python-version` = 3.12). The system/Homebrew Pythons are 3.11 or
  broken (`platform.mac_ver()` empty), so uv installs its own.
- uv 0.12.23. Node.js **v26.10.0** (even major; `fin doctor` warns on odd majors). Google Chrome in /Applications.
- Claude Code **2.1.285**. Details in `docs/ai-cli-notes.md`.
- `@playwright/mcp` pinned to **0.0.83** (latest on npm today; Apache-2.0). Set in `config/settings.toml`.
- Python deps (all from SPEC §5): typer 0.27.3, pydantic 2.13.5, jsonschema 4.26.0, SQLAlchemy 2.1.3,
  alembic 1.20.0, pdfplumber 0.11.10, beautifulsoup4 4.15.0, lxml 6.1.3; dev: pytest 9.1.1, ruff 0.16.10.
  fastapi/uvicorn/google-* come in their phases.

### Claude CLI (verified, see ai-cli-notes.md)
- All §9.1/§14.1 flags work. `--system-prompt-file`, `--append-system-prompt-file` and `--max-turns`
  are hidden from `--help` but accepted. `--permission-mode default` is accepted (help lists `manual`).
- Browsing sessions add `--setting-sources ""` (verified accepted), so user/project/local settings can't
  widen permissions. Only the run's `--settings` file loads.
- Classifier: structured output takes 2 turns; `--max-turns 3` kept.

### Playwright MCP 0.0.83 (verified via `--help` and `tools/list`, no browser launched)
- Tools: browser_close, resize, console_messages, handle_dialog, emulate_media, evaluate, file_upload,
  drop, find, fill_form, press_key, type, navigate, navigate_back, network_requests, network_request,
  pdf_save (needs `--caps pdf`), run_code_unsafe, take_screenshot, snapshot, click, drag, hover,
  select_option, tabs, wait_for. **No install tool** in this version.
- `--caps` only offers `vision, pdf, devtools`. `network`/`storage`/`testing` from the spec don't exist
  as caps. The network tools (`browser_network_requests`, `browser_network_request`) are always on, so
  they're **disallowed** with `--disallowedTools`. So are `browser_emulate_media` and `browser_resize`.
  Rule: anything not in the always-allowed or approve-mode lists is removed.
- **Added `--no-webmcp`.** By default the server exposes tools that *web pages* register through the
  WebMCP API. A bank or Amazon page could otherwise hand the agent new tools. Not in the spec; it fits §7.
- Added `--file-paths absolute` so tool results show absolute paths, for writing the manifest.
- **Deviation: session working folder.** `--output-dir` only applies to auto-named files. Help:
  "Files with an explicit name are resolved against the workspace root", and file access is limited
  to workspace roots (cwd if none). With cwd = `sync-sessions/<site>/` (spec), `browser_pdf_save` with
  `amazon_<id>.pdf` couldn't write into `inbox/<site>/<date>/`. Decision:
  - **session cwd = `inbox/<site>/`** (fixed per site, trusted once; the repo stays out of scope)
  - run folder `inbox/<site>/<date>/` sits inside it; the agent passes absolute file names there
  - **control files** (`mcp.json`, `settings.json`, `guard_config.json`, `prompt.md`) go in
    `sync-sessions/<site>/<date>/`, outside the run folder, so the agent's Write tool can't edit them
  - To confirm in `fin sync selftest`: that Claude Code's MCP roots / process cwd make
    `inbox/selftest/` the workspace root.
- `--save-session` output location and contents: to confirm in the selftest. The selftest prints what it finds.
- Chrome "Download PDFs": `fin sync` writes `plugins.always_open_pdf_externally=true` into
  `<profile>/Default/Preferences` before Chrome starts (skips if the profile is in use). Not yet
  verified with a real PDF link; example.com has none. Check on the first bank run.

### Guard hook
- Registered for **all tools** (`matcher: "*"`), Pre and Post. It denies anything that isn't Write or an
  allowed Playwright tool, and logs the attempt. This makes "no Bash" observable in `guard.log`.
- Allow = print nothing, so approve-mode prompts still happen. Deny = PreToolUse
  `permissionDecision: "deny"`. Post off-site = `{"decision": "block", "reason": ...}`. Bad
  config/payload = exit 2 (fail closed).
- Clicks/selects/types need an `element` description, or they're denied (the schema marks it optional).
- Browser `filename` arguments (pdf_save, snapshot, screenshot, console, find) must resolve inside the
  run folder. Write is limited to `.json`/`.md` in the run folder, and reserved names are refused.
- `guard.log` stores host only for navigation (no paths/queries), the element label (40 chars) for
  actions, and file names for writes. Never typed text.
- Selftest only: the guard also saves full hook payloads to `sync-sessions/selftest/<date>/payloads/`.
  They become recorded Playwright fixtures for the guard tests (example.com, no personal data).
  `write_pre/post.json` fixtures were recorded today from a real `claude -p` session.
- Same-day rerun: the session has no Read tool, and Write refuses to overwrite unread files. So
  `prepare()` moves an earlier `manifest.json`/`run_report.json`/`playbook_proposed.md` to
  `sync-sessions/<site>/<date>/previous/<ts>/`. Downloads stay, and the agent is told about them.
- Permission rule for silent writes: `Edit(//ABS/run/dir/**)`. `//` = absolute path in Claude Code
  rules. The spec's `~/...` form would break when `FIN_DATA_DIR` is outside home.

- Dry run (2026-10-06): the full selftest argv in `-p` mode, against a scratch data dir, with a
  "use no tools" prompt. Accepted. The agent saw exactly the 16 intended `mcp__playwright__` tools and
  no Bash. With `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1`, no `.jsonl` was written.
- That run still created `~/.claude/projects/<cwd>/memory/` (empty): **auto memory was on** in browsing
  sessions. Sessions now also set `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. Verified: with it, no project
  folder is created at all; without it, `memory/` appears. Not in the spec; fits §7 (no bank data in
  memory files). The classifier already skips auto memory via `--safe-mode`.

### Config and data model
- `expected_merchant_groups: ["costco"]` (in cards.json) is treated as a **family**: it matches `costco`
  and every `costco_*` group (warehouse, gas, online). `Card.expects_group()`.
- cards.example.json uses relationship-based person ids (`spouse`, `father`, `mother`) and fake names,
  so no real names reach the repo. Real person ids stay in the gitignored cards.json.
- Category ids: parent ids from §13 plus `parent.sub` slugs (e.g. `dining.coffee`), 71 in all. The
  classifier schema has an `"enum": ["__CATEGORY_IDS__"]` placeholder, filled from categories.json
  at call time, so editing the taxonomy needs no schema edit.
- Schema refinements to §8:
  - `transactions.card_ending` (for attribution)
  - `imports.card_id`
  - `statements.import_id` + UNIQUE(card_id, period_end)
  - `orders.import_id`, `orders.location`, `orders.gift_card_applied`
  - `order_items.line_no` and `seller`
  - `card_holders.id` surrogate key + UNIQUE(card_id, person_id)
  - `sync_runs.status` also allows `running`
  - `review_status` enum `ok|needs_review|reviewed`
  - `rules.match_field` enum
  - `created_at` on rules, overrides and ai_runs
  - CHECK constraints for every enum
  - SQLite runs with foreign_keys=ON and WAL
- Manifest: the `selftest` kind is allowed only for site `selftest`. The manifest wraps entries as
  `{site, run_date, files: [...]}`. `run_report.json` adds optional `export_formats_offered` (§9.2
  QFX question) and `tools_available` (selftest).

### Open questions for the user
- (resolved) `config/rules.yaml` will use PyYAML; see Dependency policy below.

### Dependency policy (user, 2026-10-06)
- Any free, open-source dependency is pre-approved; no need to ask first. Still record each one here
  with its version and license. PyYAML (MIT) is approved for `config/rules.yaml` and lands with Phase 1.

### Selftest run 1 (2026-10-06, user-run): two bugs found and fixed
- **Writes failed.** A bare `Read` in the deny list (`--disallowedTools` and `permissions.deny`) also
  blocks Write: "File is covered by a Read deny rule in your permission settings and cannot be
  written." Removed `Read`, `Edit` and `NotebookEdit` from the deny list (`--tools Write` already removes
  them). Verified with the real launcher argv in `-p`: Write into the run folder works, the guard
  denies writes elsewhere, and the read-before-write check refuses `guard_config.json`
  before the hook runs. A deny rule also stops a call before PreToolUse hooks run, so those attempts
  don't show up in guard.log.
- **The first turn saw no browser tools.** The MCP server was most likely still connecting when the
  opening prompt arrived. The agent then wrote its report files early. Fixes: `fin sync` pre-loads the
  pinned package with `npx -y @playwright/mcp@<v> --version` before launch, and base_rules.md tells the
  agent to say so and wait, and never to write outputs before the work is done.

### Selftest run 2 (2026-10-06, user-run)
- The wait rule worked: the agent said the browser wasn't connected and wrote nothing. But pre-loading
  didn't fix the race. Interactive Claude Code sends the CLI's opening prompt before MCP servers
  connect; `-p` waits, which is why the dry runs saw the tools. Fix: **no opening prompt**. The user
  types `start` when Claude's prompt appears (by then the server has connected). Pre-load and wait
  rule kept as backups.

### Selftest run 3 (2026-10-06, user-run): 4 of 6 checks passed
- Passed: Chrome opened example.com (guard.log confirms navigation and page URL), PDF saved
  (`browser_pdf_save` with an absolute name inside `inbox/selftest/<date>/` works, which confirms the
  workspace-root decision above), guard denied navigation to www.iana.org, and no transcript was saved
  in interactive mode with `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1`.
- Failed: the manifest shape (a bare array, and `collected` entries with `file` instead of `count`). Cause: base_rules
  pointed at `sync/manifest.schema.json`, which the session can't read. Fix: the prompt now ends with
  "Output file formats", a worked example of both files plus the full schema. A test checks that the
  examples validate. The "no Bash" check now reads the raw report, so an invalid report doesn't hide
  `tools_available`.
- `--save-session` (0.0.83) writes `session-<epoch-ms>/session.md` folders in the output dir. Auto-named
  snapshots land as `page-<timestamp>.yml`. Both count as session logs; the importer moves them to
  `raw/sync-logs/` (Phase 1). session.md probably holds page snapshots, so treat it as sensitive (data dir only).

### Selftest run 4 (2026-10-06, user-run): 6 of 6 checks passed, one gap found
- All checks passed after the output-format fix.
- **Gap:** the agent reported 40 tools, including the **Claude-in-Chrome** tools and `EndConversation`.
  Claude in Chrome is a built-in integration, not an MCP server, so `--strict-mcp-config` and
  `--setting-sources ""` don't remove it. It drives the user's everyday Chrome profile with live
  logins, which breaks "one site per session, own profile" (§7). The guard held: it denies every tool
  except Playwright and Write, and this run logged one block (iana.org) and no calls to other tools.
  Fix: launcher passes `--no-chrome`, and guard tests cover `mcp__claude-in-chrome__*`. In `-p` mode
  the Chrome tools don't appear with or without the flag, so only an interactive run can confirm it.
  Run 5 should report about 17 tools.
- guard.log is appended across same-day reruns. Run 4 alone: 14 decisions, 1 block.

### Selftest run 5 (2026-10-06, user-run): Phase 0 browsing acceptance passed (7/7)
- `--no-chrome` confirmed: the agent reported 18 tools (16 `mcp__playwright__browser_*`, Write,
  EndConversation), down from 40. There's no Claude-in-Chrome or other MCP tool. All 7 selftest checks passed.
- `EndConversation` is a built-in that `--tools Write` doesn't remove. It's harmless (it can only end the
  chat), and the guard denies it anyway.

## 2026-10-06 (Phase 1)

### Sync for Chase and Amazon
- `fin sync chase|amazon` builds the run parameters from the DB (`src/fin/sync/params.py`):
  - per card: `transactions_from` = latest `post_date` − 14 days (first run: today − 3 months),
    the imported statement closing dates, and which statements are wanted
  - Amazon: `orders_from` from the latest order date, plus the order ids already imported in range
- After the session: validate the manifest (allowed card ids = the issuer's cards; none for Amazon/Costco),
  record a `sync_runs` row, turn report gaps into `sync_gap` review items, import, then show the
  playbook diff.
  - status: `failed` if the manifest is invalid; `partial` if there are gaps, unlisted files or failed parses
- Import (`src/fin/ingest/inbox.py`): a parser registry keyed by (site, manifest kind).
  - Each file: sha256 duplicate check, then the `imports` row, the parse inside a savepoint, and a move to `raw/<site>/<date>/`.
  - A parser crash rolls back that file only and leaves it in the inbox.
  - A file with no parser stays in the inbox (Chase/Amazon until the redacted samples arrive).
  - Session logs (`session-*/`, `page-*.yml`, guard.log) move to `raw/sync-logs/<site>/<date>/`.
  - Re-importing a folder whose files already moved reports them `already_imported` (validation also looks in `raw/`).
- A second run of a site on the same day after a clean run asks for confirmation (§9.1 pacing).
- Guard deny list extended for Amazon/Chase:
  - "Buy it again", archive / unarchive, hide order, start a return, return or replace items
  - write a review, leave seller feedback, pay card, paperless, dispute
  - report lost/stolen, request credit/limit/replacement
  - "Archived statements" still allowed (word boundary).

### Cardholder attribution (user, 2026-10-06), replacing the §9.2 chain
- Authorized users' names appear on their transactions. A matched name (full_name or any
  name_alias in cards.json) → that person. **No name → self** (the primary). A name that matches nobody →
  `person_id = NULL` + `unknown_person` review, never defaulted. `src/fin/normalize/people.py`.
- Card endings aren't used for attribution. They're optional in cards.json, and `fin doctor` no longer warns
  about blank ones.
- To check against the first redacted samples: where the name appears (a CSV column, or per-cardholder
  sections in the statement PDF). If a source has no name field at all, "no name → self" would also
  catch authorized users' charges. Then names have to come from the statement sections for that card.

### First real syncs (user-run, 2026-10-06)
- Chase: 3 activity CSVs + 9 statement PDFs, valid manifest, 0 guard blocks, playbook accepted.
  - The CSV has no posted-only choice, so pending rows must be dropped by the parser.
  - QFX/QIF/QBO are offered too (§5 note).
  - Statement list shows closing dates only.
  - "Saves document" downloads `<YYYYMMDD>-statements-<last4>-.pdf`.
- Amazon: 27 order invoices via `gp/css/summary/print.html?orderID=…`, playbook accepted.
  - Gap: the digital orders view wasn't checked. amazon.md now makes it required.
  - The invoice shows card brand, last 4 and amount, checked on 1 of 27. The parser checks every invoice.
- redact.py changes before the first fixtures:
  - Each person gets a distinct fake name matching cards.example.json, instead of one "FAKE NAME".
    This deviates from §18, so attribution stays testable.
  - PDF link targets go into a `[LINKS]` section (needed for ASINs).
  - Output file names are redacted (they carried order numbers and card endings).
  - `--endings` masks the given endings everywhere except inside amounts. Also covers `XXXX XXXX XXXX 1234`.

### First redacted samples (2026-10-06): what the real files show
- **Chase activity CSV**: `Transaction Date,Post Date,Description,Category,Type,Amount,Memo`; MM/DD/YYYY;
  purchases negative ("Sale"). **No cardholder column and no order number.** Parser: `ingest/bank_csv.py`.
- **The Prime Visa CSV covered only Sep 27 to Oct 5**, the activity since the last statement, though 2026-07-06 to
  2026-10-06 was asked for. The typed dates didn't register. The importer now flags a CSV that starts more than 10 days
  after the requested start (`parse_failure`, document over manifest) and records the actual coverage. chase.md
  now has the agent type dates slowly, check them in a snapshot, and fall back to "All transactions".
- **Chase statement**: ACCOUNT SUMMARY parses cleanly, and the Aug 22 to Sep 21 Prime Visa statement balances to the cent.
  - "Payment, Credits" is one combined figure → stored in `payments`, `credits` = 0.
  - ACCOUNT ACTIVITY lines carry **Amazon order numbers** ("Order Number 114-…") under each Amazon charge. They're stored in
    the new `statement_lines` table and will link card transactions to orders exactly.
  - This statement has no per-cardholder sections. The pattern for them is a guess until a statement with
    authorized-user charges is seen.
- **Amazon printable invoice**: header, items, seller, prices, subtotal, tax and grand total parse, and both samples add up.
  - ASINs come from the link targets by item index.
  - The payment method is shown as "Prime Visa••••1234". It's matched by card name, because the ship-to name shares the line.
  - **The invoice has no list of individual charges** (spec §9.3 assumed it would). Charges come from the
    statement's order numbers instead. For not-yet-billed activity: grand total + card name + date window.
- Dedupe keys use the normalized *raw* description (upper case, single spaces), not `description_clean`, so
  improving the cleaning rules never changes existing keys (refines §9.2).
- Migration 0002:
  - new `statement_lines` table
  - `transactions.person_source` (`name|statement|default_self|user`) and `transactions.order_ref`
  - `orders.payment_method` and `orders.payment_card_ending`
- An unexpected money-in row becomes `credit` + a `low_confidence` review item.
- redact.py: city/state/ZIP is now caught anywhere in a line. The first run missed "CITY, ST 12345" when a second column
  followed it on the same line. Fixed in the two invoice fixtures.

### 2026-10-06 (evening)
- **Amazon charges: the user chose "Your Payments → Transactions".** The Amazon sync also saves that list (every
  charge and refund, with date, card, amount and order number) as `amazon_payments_<n>.pdf`, kind
  `payment_transactions`. It will fill `order_charges` exactly, including charges not yet on a statement. Its parser
  will be built from the first redacted sample. Statement order numbers stay as the cross-check.
- **Bug: an existing DB wasn't migrated.** The second Chase sync failed every import with "no column person_source".
  Only `fin init` ran migrations. Now every command that opens the DB runs `ensure_current()`: it copies
  the DB to `backups/pre-migration-<rev>-<ts>.sqlite` (SQLite online backup) and upgrades to head. `fin doctor`
  warns if the schema is behind.
- **Bug: failure messages leaked row data.** SQLAlchemy's error text includes the SQL parameters (merchants,
  amounts, places), and it was printed to the terminal. Against §7. `safe_error()` now keeps only the
  driver error's type and first line.
- Chase sync 2: all three CSVs confirmed to cover 07/06–10/06 (dates typed slowly and checked before download).
  The new downloads replaced the earlier files of the same names in the run folder. Playbook update accepted.

### fin process on real data (2026-10-06, late)
- **Design change: statements are the ledger source for closed periods.** Chase ignored typed CSV date ranges
  twice and always exported "since last statement". `reconcile/link.py`:
  - Each statement line links to its CSV row (same card and amount, ±3 days, matching description).
  - A line with no CSV row becomes a transaction (`source = statement`, post_date = transaction date).
  - A later CSV row for such a line replaces the statement copy, so nothing is counted twice.
  - The ledger check is exact: every line linked once, linked rows reproduce the summary components,
    and CSV rows posted inside the period but missing from the statement are a mismatch.
  - chase.md now asks for "Since last statement" only, which saves usage. The playbook line about date
    ranges was corrected by Claude to match, flagged to the user.
- Chase prints sub-dollar amounts as ".60". The amount pattern missed them, which caused 3 ledger mismatches of
  60/75/97 cents. Fixed; all 9 statements now match to the cent.
- Amazon invoice fixes from the 27 real invoices:
  - discounts are derived as subtotal + shipping − "Total before tax" (labels vary: "Free Shipping", "Promotion applied:",
    "Exclusive Promotion")
  - quantity is a bare number after the unit price
  - all 27 now pass the internal check
- `fin rebuild` implemented: re-runs each import's parser (new `imports.file_kind`, migration 0003, which also adds
  `transactions.source`) over raw/, keeping imports, user overrides, user rules and sync_gap items.
- Matching on real data: 23 exact by statement order number, 3 by grand total + card + window, 7 unmatched.
  - The unmatched ones are Jun 26 – Jul 8 charges from before the Amazon sync's range.
  - The Amazon range now reaches back 14 days before the earliest unmatched Amazon charge.
- Cleaner:
  - "WF *WAYFAIR1234567890" → "WF WAYFAIR" (a name glued to a number is kept; mixed codes like "123AB45C6" are dropped)
  - "FEDEX12345678" → "FEDEX"
  - stray punctuation tokens are dropped
- Review items are owned by `fin process` (`review.py`): created once, refreshed while open, auto-resolved when the
  cause is gone. AI-dependent items aren't auto-resolved while AI is paused.
- PyYAML 6.0.3 (MIT) added for config/rules.yaml (27 seed rules).

### Driven syncs (user, 2026-10-06)
- The user asked Claude to run syncs and to be prompted only for logins. The interactive `claude` UI can't run
  inside Claude's shell, so `fin sync <site> --driven` (`src/fin/sync/driver.py`) runs the identical session
  (same tools, guard, MCP config, prompt) in `-p --input-format stream-json` mode. Verified: a real session stays
  open across messages and keeps context.
- Driven sessions are **trusted mode**: no per-click approvals; `dontAsk` denies anything not allowed. Every guard
  rule still applies: domains, the deny list, login fields, writes. The user still does all logins: the agent
  checks the start page and answers LOGIN_NEEDED if signed out.
- Messages go in via `fin sync-send <site> <text>`. driver.log holds only agent text and tool names, never tool
  results (page content).
- In driven mode, playbook proposals aren't accepted automatically. They wait in `sync-sessions/<site>/<date>/`
  until the user approves; then `fin playbook accept <site>`.
- Bug fixed: the playbook review read `playbook_proposed.md` after the import had already moved it to raw/.
  It now uses the text captured before the import.

### Dashboard and Phase 1 close-out (2026-10-06, night)
- Dashboard: Vite 8 + React 19 + TypeScript 5.9 + Tailwind 4, shadcn-style components (Button, Card,
  Badge, Select) written in place with clsx/tailwind-merge/class-variance-authority, Recharts 3,
  TanStack Query 5 and Table 8.
  - TanStack Table v9 and TypeScript 7 were skipped on purpose (new major versions).
  - Native `<select>` instead of Radix Select (no Radix dependency yet).
  - Charts follow the dataviz method: single-series one-hue bars (slot 1 `#2a78d6` / dark `#3987e5`,
    validated in both modes), no dual axes, hover tooltips, a table view on every bar list, no animation.
  - The spec's category "donut" is left out: a donut compares close values badly. The bars carry the same data.
  - The header month picker is the single month control.
  - Checked in the browser on real data: Overview, Transactions (Amazon item splits) and Review.
- API test client needs `httpx` (dev).
- `fin weekly` runs the interactive syncs for the ready sites in order, then process, backup and serve.
- Same-day reruns: later manifests/reports go into raw/ as `manifest.<timestamp>.json`.
- Phase 1 acceptance on real data:
  1. 9/9 Chase statements pass the math check, and 9/9 ledgers match.
  2. 33/33 Amazon charges on the Prime Visa matched exactly (statement order numbers + Amazon's charge
     list), all split by item.
  3. Item-level splits are visible in the Transactions detail.
  4. Re-importing all 39 raw files gives 39 duplicates and unchanged table counts; `fin process` is idempotent.
  5. The Chase playbook is accepted; the Amazon playbook proposal from the driven run awaits the user. "Next weekly
     run picks up only new data" can only be confirmed by the next weekly run (params: latest date − 14 days).

### Priorities (user, 2026-10-06)
- Who spent it matters little; tracking expenses is the goal. Keep "no name → self", don't chase per-cardholder
  attribution, and treat SPEC Phase 3's attribution acceptance ("every transaction has a person") as satisfied
  by that default.
- The user approved the Amazon playbook from the driven run (`fin playbook accept amazon`).

## 2026-10-07

### Costco: syncs through the user's own Chrome (Playwright extension mode)
- In the separate automated Chrome profile, Costco's Orders & Purchases (and later even password sign-in)
  ended on a blank `signin.costco.com` "FIDO Consent" page, so its site layer blocks the automated profile. The user
  ruled out manual receipts.
- New per-site setting `sync.site_browser`. Costco = `"extension"`: the MCP server runs with `--extension`, which
  connects to the user's running Chrome through Microsoft's "Playwright Extension" (Chrome Web Store), instead of
  `--browser chrome --user-data-dir`.
  - Per the extension docs, the user approves each connection, and each client gets its own tab group and can only
    reach tabs in it.
  - The connection-approval token (`PLAYWRIGHT_MCP_EXTENSION_TOKEN`) is deliberately not stored, so the user approves
    every session, like a login prompt.
- Trade-off: the agent's tabs carry the user's real cookies rather than an isolated profile's. The guard still
  limits navigation to costco.com, and everything else is unchanged (`--no-webmcp`, PDF caps, output dir, session
  logs, deny list). The spec warned that downloads may fail in extension mode; Costco only needs `browser_pdf_save`.
  Other sites stay on separate profiles.
- `sync-send` now finds the newest running session, even one that started the day before.
- First extension-mode run: the connect page (the extension's tab picker) opened, but the connection never completed.
  The server waits indefinitely without a token, so the agent's first action hung, and the session had to be stopped
  with pkill (closing stdin doesn't end a tool call that's waiting).
- At the user's suggestion, the extension token is now supported. The user saves it to
  `secrets/playwright_extension_token` (mode 600, data dir only). The launcher puts it in the extension-mode MCP
  server's `env` in the private mcp.json. With a token the extension connects automatically, and the server fails
  after 30 s instead of hanging. This replaces "user approves each session". `fin doctor` checks the file's
  presence and mode.

### Costco sync works in extension mode (2026-10-07)
- Clicks timed out ("waiting for element to be stable") while the agent's tab was in the background: Chrome pauses
  rendering in hidden tabs. Dragging the tab into its own window broke the extension connection, and the guard then
  blocked the extension's connect page. What worked: open a fresh front window **before** starting the session, and
  leave it untouched. Weekly runs need the same.
- `browser_pdf_save` doesn't capture the receipt dialog (the PDF has the page behind it). So every receipt is a §9.4
  transcript plus a screenshot. Importer: `src/fin/ingest/costco.py`, accepted only when items − instant savings
  = subtotal and subtotal + tax = total, to the cent. 17/17 transcripts were accepted, and 17/17 receipts matched a
  Citi charge exactly (0–2 day window).
- 11 Costco warehouse charges have no receipt:
  - 7 are July charges before the first receipt fetched. The Costco reach-back ("14 days before the earliest
    unmatched charge") never fired, because it looked for group "costco" while the groups are `costco_*`. Fixed.
  - 4 are in range but not in the account's receipts (3 in the spouse's statement section, so probably another
    membership card). They stay as review items.
- Transcript screenshots now move to raw/ with their transcript. The first run's 17 were placed there by hand,
  and the agent's two leftover test files went to raw/sync-logs/.
- Five stale Costco sync gaps were resolved with notes: superseded by this run, or "nothing to collect".

### Phase 3: Amex (2026-10-07)
- Amex statements (`parse_amex_statement`):
  - Summary from the "Account Total" block; payments and credits combined into `payments`.
  - Period start = closing date − ("Days in Billing Period" − 1).
  - Activity sections: Payments, Credits, New Charges (grouped per cardholder), Fees, Interest Charged.
    Lines are "MM/DD/YY[*] desc ±$amount[⧫]".
  - Statement credits ("Platinum Resy Credit") → `credit`, shown as card credits & rewards. Other Credits-section
    lines → `refund`.
  - All 6 Amex statements balance, and their lines sum exactly.
- Amex CSV: Platinum exported `Date,Description,Amount`; Gold `Date,Description,Card Member,Account #,Amount`.
  The parser needs only Date/Description/Amount and uses Card Member when present.
- Amex names every export `activity.csv`, so the Gold CSV overwrote Platinum's. Claude had copied Platinum's first and
  imported it manually. The guard's PostToolUse now renames `activity.csv` to `activity-<n>.csv` after each download
  and tells the agent the new name via `additionalContext`. The accepted playbook line was corrected to match.
- Fixture note: merchant descriptors carry the user's city. The redacted Amex fixtures replace it with
  SPRINGFIELD.
- **Phase 3 acceptance on real data:** all 6 cards × 3 statements = 18 statements, all math-ok and ledger `match`.
  288 transactions, all with a person (name where one is given, otherwise the self default).

### Phase 4 (Gmail) deferred (user, 2026-10-07)
- The user expects email receipt classification to be hard and has set Phase 4 aside. Nothing was built. The Gmail
  rows in §5/§19.2 stay unused, and `fin sync gmail` still says it's a later phase. Next: Phase 5 (polish).

### Phase 5 (2026-10-07)
- Dashboard additions:
  - **Amazon & Costco** page: orders/receipts, item spending by category, top items, unmatched markers.
  - **Reconciliation** page: math and ledger per statement, with icon + label, never color alone; last sync per site
    with gaps and guard blocks.
  - **Rules** editor: your rules, read-only seed rules, a live preview of what a pattern matches; "Save & apply"
    re-runs `fin process` without AI through `POST /api/process`.
  - **Review**: bulk "All look right" / "Ignore all" per group. Order-item reviews show the item and take a category
    correction, stored as an override keyed `merchant|order|sku` (survives `fin rebuild`); the charge's split
    is recomputed at once.
- Guard tuning from the logs: 8 logs, about 1,040 decisions, 5 blocks, all correct (3 selftest iana.org, 2 the
  extension's connect page). The deny list never fired on a real site, so no false positives and nothing to tune yet.
- Trusted mode: Chase and Amazon have two clean runs each (0 blocks), so they go in `trusted_sites`, per spec. That
  only changes interactive `fin sync`/`fin weekly` (no per-click prompts); driven runs were already trusted.
- `match_adjudicate`: not built. The matching queue isn't noisy: 0 unmatched Amazon charges, and all Costco
  leftovers have known causes.

### Licence: AGPL-3.0-only (user, 2026-10-07)
- After comparing permissive, weak/strong/network copyleft and fair-source licences, the user chose
  **AGPL-3.0-only**. Its network clause covers hosted forks, it keeps "open source" status, and dual licensing stays
  possible while there's a single copyright holder. `-only` keeps future licence changes with the owner.
- Files: `LICENSE` (GitHub's AGPL-3.0 text, from the repo's initial commit), `COPYRIGHT` (notice + AGPL statement + commercial licences
  available), SPDX `AGPL-3.0-only` in pyproject.toml and web/package.json.
- To keep dual licensing possible: outside code contributions only under a CLA granting relicensing rights (policy
  still to be decided), and only permissive dependencies (now a CLAUDE.md rule).
