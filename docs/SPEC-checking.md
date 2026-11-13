# Financial Adviser: Checking account

Build spec for Claude Code. Version 1, 2026-11-06.
Extends `docs/SPEC.md`, the MVP spec. SPEC §1 left checking out of the MVP and SPEC §20 lists it for later; this is it.
Section numbers (§1, §2, …) refer to this file. "SPEC §n" means `docs/SPEC.md`; "SPEC-cards §n" means `docs/SPEC-cards.md`. Phases are B0–B4.

**How to start:** open Claude Code in this repo and say:
*"Read docs/SPEC-checking.md end to end, then CLAUDE.md, docs/SPEC.md §2, §7, §8, §9, §10, §13 and §18, docs/decisions.md, and private/bank-notes.md. Then start Phase B0. Stop at the end of each phase and show me the acceptance checks."*

Starting Phase B0 is my approval of the changes listed in §3.

---

## 0. Working agreement (for Claude Code)

1. Everything in SPEC §0 and CLAUDE.md still applies: one phase at a time, tests passing and acceptance checks run at the end of each, then stop for my review. Decisions and verified facts go in `docs/decisions.md`. Free dependencies with permissive licences are pre-approved; record each one. GPL stays out, so `ofxtools` from SPEC §5 is not an option. New source files get the copyright and SPDX header.
2. §3 lists the only changes to the hard rules. Copy them into `docs/SPEC.md` and `CLAUDE.md` in Phase B0. If the work seems to need anything beyond them (another site, another tool, a new AI role), stop and ask.
3. No new AI role and no paid service. Checking rows go through the existing classifier (SPEC §14); everything else here is plain code. No Plaid, Finicity or any other aggregator.
4. The bank's site can move real money: person-to-person payments, bill pay and external transfers are one menu away. Never weaken a guard rule to get a download working. Stop and ask.
5. I run `fin sync bank`, in my own terminal. You never start it. You may run `fin import`, `fin process`, `fin rebuild` and `scripts/redact.py` on real data, as CLAUDE.md allows.
6. **The repo is public, and which bank I use is private** (§1.1). Its name, domains, routing number, platform vendor and product names appear only in `config/cards.json`, `private/` and the data dir. Everywhere else, including `docs/decisions.md`, tests, other sites' playbooks and commit messages, it's "the bank". No checking data goes in the repo either: no amounts, balances, employers, payees, people's names or account numbers. Record shapes and counts instead ("ACH rows carry 5 fields", "3 rows unmatched"). Snapshots of real totals go in the data dir's `debug/`.
7. At no commit may a card payment count as spending. That's why the phases run in this order: Phase B1 downloads files, and they wait in the inbox with no parser (the importer already leaves such files alone). The parser arrives in Phase B2 together with the transfer code.
8. Tests never call a model, the network or a browser.
9. If reality disagrees with this spec, stop and tell me. The bank's public pages don't describe its export (`private/bank-notes.md`), so expect that in Phase B1.

## 1. Goal and scope

- **One more account.** My checking account joins the ledger. Rent, bills, debit card purchases, checks, ATM cash and person-to-person payments show up in Overview and Transactions next to my card spending, in the same categories.
- **No double counting.** A payment from checking to a card is the same money as the purchases already on that card, so it's never spending. The same goes for moves between my own accounts. Excluded rows stay visible, and a card payment links to the payment on the card, so I can always see why it was left out.
- **Same login pattern.** `fin sync bank` works like every other site: I log in, Claude drives Chrome and downloads files, and Python parses them and checks them with math.
- **Private.** Nothing in the public repo says which bank it is (§1.1).
- **Money in is recorded.** Pay, interest and rewards are stored and shown. They're neither spending nor "Card credits & rewards".
- **In scope:** one checking account: posted transactions and monthly statements.
- **Optional later:** debit purchases in Missed rewards (Phase B4).
- **Out of scope:** savings, money market, loans and cards at the bank (they appear only as the checking side of a transfer), Venmo and PayPal histories, pending transactions, budgets, forecasts, and anything that moves money.

### 1.1 Keeping the bank private

**Where the bank is named, and nowhere else:**
- `config/cards.json` and the `private/` folder at the repo root. Both are gitignored, and the pre-commit hook refuses both.
  - `cards.json` holds the site's name, start URL and domains, the account's product name and ending, and `private_words` (§5). `private_words` is already filled in.
  - `private/` holds `bank-notes.md` (what the bank's public pages say, and the real `cards.json` entries) and the bank's playbook, `playbooks/<site>.md`.
- The data dir: run folders, raw files, the browser profile and logs, as for every site.

**Generic in the repo:**
- The site id is `bank` and the account id is `checking`. They name nothing, so they're safe in commands, settings and commit messages. Code doesn't hard-code them either: bank sites come from `cards.bank_accounts` (§7.5).
- One generic site file, `sync/sites/bank.md`, serves any bank site. The launcher already puts the site's name, start URL and domains into the run parameters from `cards.json`.
- A label that a parser needs and that names the bank comes from `bank_accounts[].statement_markers` in `cards.json`.
- A classification pattern that would name the bank becomes one of my corrections (§9 step 2), which live in the database, never in `bank_rules.yaml`.
- Bank fixtures are fictional ("Example Bank", `bank.example`), built to match the real files' structure. They're never redacted copies of real files: statements print the bank's name, address, phone and routing number on every page, and redaction doesn't catch those.
- `scripts/make_cards_example.py` writes a fictional bank into `config/cards.example.json`. Today it copies sites as they are, so it would publish the real one.

**Checks that catch a slip (already built, SPEC §7):**
- `scripts/hooks/privacy_scan.py` (stdlib) reads `cards.json` and the ledger at run time and keeps no copy. Its private words are the `private_words` list plus, once `bank_accounts` exists, each bank site's name, start URL host and domains and each account's product; `account_ending` counts next to words like "ending". Matching is case-insensitive and whole-word, and words under 4 characters are ignored.
- `pre-commit` refuses anything under `private/` and any staged line containing a private word. `commit-msg` checks the message and `pre-push` checks every commit about to be pushed. Findings give the file and line, never the word.
- `fin doctor` fails if a tracked file contains one and warns if a commit message ever did. `make privacy-check` scans all of history.
- Git history keeps whatever was ever committed. Nothing about the bank has been committed; the checks are a seatbelt, not permission.

## 2. How it works

```
 fin sync bank   (I log in; Claude drives Chrome; approve mode)
   activity export + statement PDFs ──► inbox/bank/<date>/ + manifest.json
                     │
 fin import ─► raw/ ─► parse ─► transactions (card_id = checking)
                     │
 fin process
   link card statements                                        as today
   classify checking rows ◄── config/bank_rules.yaml + my corrections   no AI
   link card payments ↔ checking rows                                   no AI
   rules ─► AI categories (spending rows only) ─► orders ─► checks ─► review items
                     │
 Overview: spending is purchase + refund + fee + interest rows, as today.
           A row has one type, so a dollar can't be a transfer and spending at once.
```

Principles (they extend SPEC §4):
- **One ledger.** Checking rows live in `transactions`, so totals, filters, rules, corrections and `fin rebuild` keep working.
- **Two independent locks on card payments.** The row's own description, and a match against the card's payment by amount and date. Either one alone keeps it out of spending (§10).
- **The card side doesn't change.** Card rows keep their types and their statements keep matching. All new logic sits on the checking side. The one exception is a small classifier fix for a rare row (§10.5).
- **Code decides, I correct.** No AI in transfer detection. My corrections survive `fin rebuild`.
- **Math proves it.** Statements and the running balance catch a row that was missed or imported twice (§11).

## 3. Changes to the hard rules and the spec

Add these to `docs/SPEC.md` and `CLAUDE.md` as worded here. None of them names the bank.

**SPEC §1:** remove "checking" from the out-of-scope list. Savings, Venmo and Zelle histories stay out.

**SPEC §2.3 (local only), add:** the bank in `config/cards.json` (`bank_accounts`) is one of the sites being synced. It's named only there, in `private/` and in the data dir.

**SPEC §2.5 (read-only), add:** on a bank's site the agent also never uses Zelle, bill pay, wires, external transfers, check deposit, stop payment, card controls or direct deposit setup, and never reveals a full account number.

**SPEC §6 (repo layout), add:** `private/` (gitignored): `bank-notes.md`, and `playbooks/<site>.md` for bank sites.

**SPEC §7 (what Claude sees), add:**
- Browsing agent on the bank's site: it also sees checking balances and recent rows on the pages it passes through, including who paid me and whom I paid. It goes straight to the export and the statements, and it doesn't open single transactions.
- Classifier: checking rows reach it only after §8.3 cleaning. Person-to-person and check rows are never sent, and neither are transfers or deposits.

**SPEC §7 (repo hygiene), add:** the bank is named only in `config/cards.json`, `private/` and the data dir (the public-repo rule, `private/` and the hooks are already in §7). Checking data never goes in docs, playbooks, fixtures, test names or commit messages.

**SPEC §7 (browsing guardrails), add:** a site that holds a bank account gets the stricter guard in SPEC-checking §7.3 and stays in approve mode.

**SPEC §8:** add the types, columns, tables and review kinds in §6.

**SPEC §13 (spending totals):** `transfer` and `deposit` are excluded from spending too.

**SPEC §16 (CLI):** add `fin sync bank` and `fin transfers`.

**SPEC §20:** drop "Checking" from the last bullet. Venmo and Zelle histories stay later.

**CLAUDE.md:** update the first line ("6 cards and a checking account"), add a pointer to this file and the privacy rule in one line each, and keep it short.

## 4. Concepts

**A bank account in the ledger.** In the database it's a row in `cards` with `card_type = "checking"` and `network = "none"`, so `transactions.card_id`, `imports.card_id`, the manifest's `card_id` and every filter work unchanged. `card_id` keeps its name: renaming it would touch 38 Python files, the manifest schema and the dashboard for no change in behavior. In config it's a separate list (§5), so every `cards.cards` loop in the optimizer and the dedicated-card checks stays cards-only.

**Two new types.** `TXN_TYPES` gains `transfer` (my own money moving) and `deposit` (money in from outside). Neither is spending, and neither is "Card credits & rewards". A checking row is never `payment`, `credit`, `reward`, `interest` or `adjustment`: those mean something specific on a card, and `credit` and `reward` feed the Overview's credits figure.

**Flow kind.** `transactions.flow_kind` says what a checking row is. It's NULL on card rows.

| A checking row that is | `type` | `flow_kind` | Spending? |
|---|---|---|---|
| A payment to a card in cards.json | `transfer` | `card_payment` | No |
| A payment to any other card | `transfer` | `card_payment_other` | Not until I decide (§10.4) |
| A move to or from my own account | `transfer` | `own_transfer` | No |
| Money to or from a brokerage or retirement account | `transfer` | `investment` | No |
| A debit card purchase, ACH bill or bill pay to a biller | `purchase` | NULL | Yes |
| A check | `purchase` | `check` | Yes, uncategorized until I say |
| ATM cash | `purchase` | `atm` | Yes, `cash.atm` |
| Zelle, Venmo or Cash App sent | `purchase` | `p2p` | Yes, uncategorized until I say |
| A bank fee | `fee` | NULL | Yes |
| A merchant refund | `refund` | NULL | Nets against its category |
| Pay | `deposit` | `income` | No |
| A dividend or interest payment | `deposit` | `interest` | No |
| Debit rewards paid as cash | `deposit` | `reward` | No |
| A tax refund | `deposit` | `tax_refund` | No |
| Zelle, Venmo or Cash App received | `deposit` | `p2p` | No |
| Any other money in | `deposit` | `other_in` | No |

Rent, loan payments and taxes are `purchase` rows: that money really left. A payment to a card I don't track is held out only until I decide, because that card's purchases aren't in the ledger, so the payment may be the only record of that spending.

## 5. Config

**`config/cards.json`** (gitignored). The values below are the fictional ones `config/cards.example.json` will carry. The real ones are in `private/bank-notes.md`, and they go only in `cards.json`.

```json
"issuers": [
  { "id": "bank", "name": "Example Bank",
    "start_url": "https://online.bank.example/",
    "allowed_domains": ["bank.example"] }
],
"bank_accounts": [
  { "id": "checking", "site": "bank", "product": "Example Bank Checking",
    "account_type": "checking", "account_ending": "", "statement_markers": [] }
],
"private_words": []
```

`issuers` is the existing list of sites. A bank that issues no card fits it: only the config validator and `/api/sources/status` loop over it, and the second should list this site.

`fin.config`:
- `BankAccount(_Strict)`: `id`, `site` (must be an issuer id), `product`, `account_type: Literal["checking"]`, `account_ending` (`^(\d{4})?$`), `statement_markers: list[str] = []`. Rows default to `self`, like a card row with no name.
- `CardsConfig.bank_accounts: list[BankAccount] = []` (`private_words: list[str] = []` already exists). Ids are unique across cards and accounts.
- `bank_sites`, `accounts_for_site(site)`, and `account_name(id)` (a card's or an account's product, for code that may meet either).

`db/seed.py` adds each account to `cards`. `scripts/make_cards_example.py` replaces every bank site and account with the values above, fakes `account_ending`, and empties `statement_markers` (it already empties `private_words`).

**`config/settings.toml`**, with fields on `MatchSettings`:

```toml
[match]
transfer_window_days = [-10, 6]  # checking date minus card date (§10). Tighten from real data in B2
transfer_grace_days = 10         # how long a card payment may wait for its other side
```

The window is wide on the negative side because bill pay services can pay card companies: money leaves checking first, and a mailed check can take days to reach the card. A payment made on the issuer's site goes the other way: the card posts first and checking follows.

**`config/bank_rules.yaml`** decides what a checking row is; `config/rules.yaml` still decides merchant and category. Patterns are case-insensitive, run with `re.search` on the cleaned description, and the first hit wins. Every pattern is a starting guess, like SPEC §10's seeds. Check them against real rows in B2. Short patterns need word boundaries: a bare `NSF` matches TRANSFER, and a bare `ATM` matches TREATMENT. A pattern that would name the bank never goes here (§1.1).

```yaml
version: 1
rules:
  # Payments to the cards in cards.json. `issuer` is an issuer id.
  - {pattern: '\bAMEX\b|AMERICAN EXPRESS', flow: card_payment, issuer: amex}
  - {pattern: 'CHASE CREDIT|CHASE CARD|CHASE EPAY|CHASE AUTOPAY', flow: card_payment, issuer: chase}
  - {pattern: 'CITI ?CARD|CITI AUTOPAY|CITI CRD|CITIBANK', flow: card_payment, issuer: citi}
  # Any other card, the bank's own included.
  - {pattern: 'CAPITAL ONE|\bDISCOVER\b|BARCLAY|SYNCHRONY|APPLECARD|BK OF AMER|CARDMEMBER SERV|CREDIT ?CARD|CRCARDPMT', flow: card_payment_other}
  - {pattern: 'ZELLE|VENMO|CASH ?APP', flow: p2p}
  - {pattern: 'TRANSFER (TO|FROM)|XFER|OVERDRAFT TRANSFER', flow: own_transfer}
  - {pattern: 'FIDELITY|VANGUARD|SCHWAB|ROBINHOOD|E\*?TRADE|BETTERMENT|WEALTHFRONT|COINBASE', flow: investment}
  - {pattern: '^CHECK\b', flow: check}
  - {pattern: '\bATM\b|CASH WITHDRAWAL', flow: atm}
  - {pattern: 'SERVICE (FEE|CHARGE)|MONTHLY FEE|\bNSF\b|RETURNED ITEM FEE', flow: fee}
  # These five flows apply to money-in rows only.
  - {pattern: 'DIVIDEND|INTEREST (PAID|EARNED|DEPOSIT|CREDIT)', flow: interest}
  - {pattern: 'PAYROLL|DIRECT DEP|DIR DEP|SALARY', flow: income}
  - {pattern: 'REWARD|CASH BACK', flow: reward}
  - {pattern: 'IRS TREAS|TAX REF', flow: tax_refund}
  - {pattern: 'REFUND|\bRETURN\b|REVERSAL', flow: refund}
```

**`config/categories.json`** (version 2) adds: `housing` (`.rent`, `.mortgage`), `loans` (`.auto`, `.student`, `.other`), `taxes`, `cash` (`.atm`) and `fees_interest.bank_fee`. Add matching seed rules to `config/rules.yaml`.

## 6. Data model (migration 0009, batch mode; indicative, like SPEC §8)

**`transactions` gains:**
- `flow_kind` (CHECK on §4's values, NULL allowed)
- `bank_type`: the export's own row type, if it has one
- `balance_after_cents`: the export's running balance, if it has one

**`TXN_TYPES`** gains `transfer` and `deposit`.

**`REVIEW_KINDS` and `PROCESS_KINDS`** gain:
- `unmatched_transfer`: looks like a card payment, but no card shows it
- `uncertain_transfer`: matched to a card payment by amount alone
- `bank_balance_gap`: the running balance doesn't add up, so a row is missing or doubled

A checking statement that doesn't tie uses the existing `statement_mismatch`, with `ref_table = "bank_statements"`.

**New tables:**

| Table | Key columns |
|---|---|
| `transfer_links` | id, bank_txn_id → transactions ON DELETE CASCADE (indexed), card_txn_id → transactions ON DELETE CASCADE UNIQUE, method (`named`/`group`/`amount_only`), days_apart, confidence |
| `bank_statements` | id, card_id → cards, import_id → imports, period_start, period_end, opening_balance, deposits, withdrawals, closing_balance, pdf_path, parse_method, math_ok, ledger_status, ledger_delta_cents. UNIQUE(card_id, period_end) |

- **The CASCADE is required.** `link_statements` deletes a statement-sourced card row when its CSV row arrives (`_delete_txn`). With foreign keys on, a link left from the last run would make that delete fail.
- **`fin rebuild`:** add both tables to `DERIVED`, with `TransferLink` first. Links are recomputed by every `fin process`.
- **My corrections** use `user_overrides`, so they survive a rebuild:
  - one row: `target_type = "transaction"`, `stable_key` = dedupe key, field `type` or `flow_kind`
  - all rows like it: `target_type = "merchant"`, `stable_key` = the cleaned description, field `flow`

## 7. Sync: `fin sync bank`

### 7.1 What's known about the site

`private/bank-notes.md` has what the bank's public pages say: the login host, menu names, and what's there to avoid. What matters for the design:
- The site may ask to verify by text, email or phone. I handle that.
- The account number is partly hidden, with a control that reveals it, and there's an account details page. Neither is needed.
- Statements stay online for 24 months.
- Its bill pay can pay card companies, and debit card rewards can be paid as cash into checking.
- **Not found:** any public description of a transaction export. Don't assume its formats, columns, date limits or where it lives.

### 7.2 Site file and playbook

Write `sync/sites/bank.md` in the style of `sync/sites/citi.md`, naming no bank. Start the bank's playbook, `private/playbooks/<site>.md`, from the draft in `private/bank-notes.md`. The site file says:
- Read-only. Never use transfers, person-to-person payments, bill pay, card controls or direct deposit setup. Never reveal a full account number or open account details.
- Choose the account by `product` (or its ending). Don't open savings, loans or cards.
- **Transactions.** Use the site's own export for `transactions_from` to `transactions_to`, CSV if offered. Check in a snapshot that the dates held. Don't rename the file. Manifest kind `transactions`, this account's `card_id`.
- **Statements.** Download each statement in `statements_wanted`, skipping `statements_already_imported`. If a PDF opens in a viewer, use `browser_pdf_save` to `<run folder>/<site>_<card_id>_<closing date>.pdf`. Manifest kind `statement`.
- Go straight to these pages. Don't open single transactions, and don't snapshot the list more than the task needs.
- If the site gives every file the same name, later downloads overwrite earlier ones (Amex did this). Save each under a unique name with `browser_pdf_save`, or extend the guard's rename for downloads. If the file name names the bank, pass the pattern to the guard through `cards.json` and `guard_config.json` instead of writing it into `guard.py`.
- If a page leaves the site's domains (statements often sit on a vendor's host), the guard blocks it. Record the host in the run report and stop. I decide whether it joins the playbook's `## Login hosts`.
- **Run report:** every format offered, how far back the export reached, whether pending rows are in it, the file names, and whether a statement covers one account or every account I hold there.

Add one line to `sync/base_rules.md` under "Never", from §3's SPEC §2.5 wording.

### 7.3 Stricter guard (`sync/guard.py`, stdlib only)

`launcher.guard_config()` sets `money_site: true` for any site in `bank_accounts`. Rules 1 to 3 apply only there, so other sites behave as before. Rules 4 and 5 apply everywhere, since no session needs those controls.

1. **Money pages, by URL.** This is the strongest check here, because the guard reads the URL from the tool result rather than from text the model wrote. Match `MONEY_URL` against path, query and fragment: `transfer|billpay|bill-pay|pay-?bills?|zelle|wire|p2p|send-?money|money-?movement|external-?account|direct-?deposit|remote-?deposit|deposit-?check|card-?management|card-?control`. PreToolUse denies a navigation to such a URL. PostToolUse returns `block` and tells the agent to go back and stop, as it does for off-site pages. `guard.log` records only the label `money page`, never the path. Tune the list from the first real run, with generic words only. A word unique to the bank goes in `bank_accounts[].money_url_words` in `cards.json`; the launcher adds it to `guard_config.json`, which lives in the data dir. Playbooks never change the list, because the agent writes them.
2. **Amounts can't be typed.** Deny `browser_type` and `browser_fill_form` when the value looks like an amount (`^\s*\$?\s*\d[\d,]*(\.\d{1,2})?\s*$`), and deny digit keys in `browser_press_key`. Dates with slashes still work. Nothing read-only needs an amount, and moving money always does. Log only `amount-like text`.
3. **Full numbers stay hidden.** Deny clicks described as showing, viewing or revealing an account, routing or card number, and "account details". It's money-site only because a card site may use those words harmlessly. The reveal control may have no label for the guard to read, so the site file forbids it, and in approve mode I'd deny that click.
4. **More action labels (`DENY_ACTIONS`):** wire; bill pay, pay bills; payee, recipient; stop payment, stop a check; order checks; deposit a check; external account, link an account, open an account; overdraft; card controls, card management; direct deposit.
5. **More page labels (`PAGE_DENY`), so a control is blocked however the agent describes it:** `payments? (and|&) transfers?` and `transfers? (and|&) payments?`; send money; Zelle; wire; transfer money, make a transfer; bill pay, pay bills.

Keep every label in `test_guard.py`'s allowed list working ("Apply", "Payment activity tab", "Download account activity", …). Test rules 4 and 5 with a fictional recorded page, like `tests/fixtures/hook_payloads/offers_*_post.json`.

### 7.4 Approve mode stays on

- The bank site is not in `trusted_sites`. I approve each click in my terminal. A run is only a handful of clicks.
- `fin sync bank --driven` refuses: nobody can answer prompts there, and `sync_driven` forces trusted mode.
- `fin doctor` warns if a site with a bank account is in `trusted_sites`, and fails if its `site_browser` is `extension`. Extension mode would act in my everyday Chrome with all my logins.
- Each site keeps its own Chrome profile (`browser-profiles/bank/`).

**If the site blocks the automated browser,** stop and tell me. The fallback is SPEC §2.6: I download the files myself and run `fin import FILE --card checking`.

### 7.5 Plumbing

- **Bank sites come from `cards.bank_accounts`;** no site id is hard-coded. `fin/cli.py` adds them to the sites `fin sync` accepts and puts them after `amex` in `fin weekly`. Fix `fin import`'s "Which card is…" list and "Supported:" message.
- The launcher uses `sync/sites/bank.md` for any bank site, whatever its id. `fin.sync.playbooks` resolves a bank site's playbook to `private/playbooks/<site>.md`, for `current`, `login_hosts`, `diff`, `accept` and `fin playbook accept`.
- `sync/params.py`: run parameters for an account have the same shape as a card's (`card_id`, `product`, `transactions_from`, `transactions_to`, `first_run`, `statements_wanted`, `statements_already_imported`). `statements_already_imported` reads `bank_statements`.
- **First run reaches back to the earliest card transaction**, not `history_months`, so every card payment in the ledger can find its checking row, and it asks for every closed statement in that span. If the export can't reach that far, note it in the run report.
- `run_params` must work for a site with no cards (`min()` of an empty list fails today). `allowed_card_ids` includes accounts.
- `ingest/inbox.py`: `detect()` recognizes the export by its header and a statement by `statement_markers`. `statement_card` falls back to the Chase parser for an unknown site, so handle bank sites.
- Manifest kinds stay `transactions` and `statement`. `(site, kind)` already keeps parsers apart, so `manifest.schema.json` doesn't change.

## 8. Import (`src/fin/ingest/bank.py`)

Its parsers register for each site in `bank_accounts` when `load_parsers()` runs, so `fin rebuild` re-runs them and no site id is hard-coded.

### 8.1 Activity export

- Don't assume columns. Document them from my first real file in a docstring, under generic names, without quoting a header or footer that names the bank. Test them with a fictional fixture of the same shape.
- `detect()` recognizes the export (§7.5), so a manual download works.
- Normalize to the SPEC §8 sign (positive = money out). Banks often export debits as negative; test it.
- Drop pending rows.
- Reuse `ParsedTxn` (add optional `bank_type` and `balance_cents`), `dedupe_keys`, `insert_transactions`, `coverage_problem` and `to_cents`.
- **Don't use `classify()` from `normalize/signs.py`.** It reads card meaning into words. It would type a $3,000 card payment out of checking as a `purchase`, and any money-in row containing "PAYMENT" as a `payment`. Insert rows with a provisional type by sign: `fin process` sets the real one (§9).
- **The running balance stays out of the dedupe key.** A bank can re-sequence same-day rows, so a row's balance can differ between exports, and a changed key would import the same row twice. Keep the existing key: account, post date, amount, normalized description, occurrence index.
- If the export offers no CSV, stop and show me what it does offer. Don't build a QFX or PDF path until I've chosen.

### 8.2 Statements

- `parse_bank_statement(text, markers)` reads opening balance, deposits, withdrawals, closing balance and the period into `bank_statements`.
- A statement may cover every account I hold at the bank. Read only the checking section. Check the account ending when `account_ending` is set; the document wins over the manifest.
- **A checking statement never creates transactions.** Card statements fill gaps through `link_statements`, which pairs rows by similar description. A checking statement's wording may differ from the export, so a statement copy could become a second row for the same money. The export is the ledger, and the statement is the proof (§11).

### 8.3 Cleaning (`src/fin/normalize/bank.py`)

`clean_bank_description()` runs before `clean_description()`, so existing merchant rules still match from the start of the text.
- Strip prefixes such as debit card, POS and ACH wording, plus dates, card endings, trace, reference and member numbers, and any run of 6 or more digits.
- Keep the merchant, or the ACH originator and its entry description.
- Remove my name, every name in cards.json, the private words, and name fields in ACH detail.
- Build the patterns from real rows in B2. Put only fictional examples in tests.

Bank descriptions are the most sensitive text in this project: ACH lines can carry my name, employer, IDs and account fragments. SPEC §7's rule for the classifier must still hold after this step.

## 9. Classifying checking rows (`fin.normalize.bank.classify_bank_row`, pure)

`fin process` recomputes this for every checking row on every run, so a better pattern needs no rebuild. First hit wins:

1. My override on this row.
2. My rule for this cleaned description.
3. `config/bank_rules.yaml`. The flows `interest`, `income`, `reward`, `tax_refund` and `refund` are skipped on money-out rows.
4. The export's own row type, if it has one, mapped from the real values in B2.
5. By sign: money out is `purchase`. Money in at a merchant a rule knows is `refund`. Any other money in is `deposit` / `other_in`.

The result sets `type` and `flow_kind` as in §4.

What reaches AI: only rows that `apply_rules` leaves over, and those must look like merchants. `p2p` and `check` rows skip AI and go straight to `uncategorized` plus a review item, because `clean_description` collapses every check into "CHECK" and a person's name isn't a merchant. A rule I create from one of those rows then handles it.

## 10. No double counting (`src/fin/reconcile/transfers.py`, `link_transfers`)

A **card payment** is a card row with `type = "payment"`, including Chase's positive "Reversal" rows and statement-sourced rows. `days` is the checking date minus the card date. The window is `transfer_window_days`.

`link_transfers` runs right after classification, and it deletes and recomputes every link, like `match_amazon`. It never changes a card row.

### 10.1 Rows that name an issuer

For each `card_payment` checking row, look for an unlinked card payment of that issuer's with the exact opposite amount, inside the window. Take the nearest date, then the lowest id. Link it (`named`, confidence 1.0). A row that names one issuer never links to another issuer's card.

If two payments tie, any pairing gives the same totals, so pick deterministically and don't raise a review item.

### 10.2 One row, several cards

If a named row is still unlinked and equals the sum of 2 or 3 unlinked payments from that issuer in the window, link them all (`group`).

### 10.3 Rows that don't say what they are

Patterns will miss some descriptions, so amounts are the second lock. A `purchase` row (plain or `check`, never `p2p` or `atm`) with the exact amount of a still-unlinked card payment in the window becomes `transfer` / `card_payment` (`amount_only`, confidence 0.6), plus an `uncertain_transfer` review item. This leans toward excluding, since my stated worry is double counting, and I can undo it in one click. Rows I typed by hand are never touched.

### 10.4 No other side

- A `card_payment` row with no link stays `transfer`, so it never counts as spending while it waits. After `transfer_grace_days` it becomes an `unmatched_transfer` item, but only if its date falls inside the card ledger. Rows older than the first card transaction can't be checked, so they get no item.
- A `card_payment_other` row becomes an `unmatched_transfer` item at once: "A payment to a card you don't track. Those purchases aren't in the ledger, so this may be real spending."
- In review I choose **It's spending** (a `purchase` override) or **Not spending** (stays a transfer). Either can apply to all rows like it.
- A card payment with no checking row raises no item. It may have come from another account. The Reconciliation page counts these.

### 10.5 Money coming back

Run the same matching the other way. A money-in checking row with the exact opposite amount of a positive card payment (a returned or reversed payment) is `transfer` / `card_payment`.

Also add `CREDIT BALANCE REFUND` to `normalize/signs.py`, so that rare card row is a `payment`. Today `classify()` would type it as a purchase, and the money coming back would count as spending. This is the one card-side change in this spec.

### 10.6 Invariants

1. A linked checking row is never a spending type.
2. A card payment links to at most one checking row.
3. No checking row of a spending type has the exact amount of an unlinked card payment in the window, unless I typed it by hand.
4. No checking row is `payment`, `credit`, `reward`, `interest` or `adjustment`.
5. Spending from card rows is the same with and without checking data, to the cent.

Tests cover all five. `fin transfers --check` runs 1 to 4 on real data. Number 5 needs a before snapshot, so it's a Phase B2 acceptance check.

## 11. Proving the ledger (`src/fin/reconcile/bank.py`)

- **Statement math:** `opening + deposits − withdrawals == closing`. A failure is a `parse_failure`.
- **Ledger check:** rows posted in the period must add up to the statement: money in to `deposits`, money out to `withdrawals`. Transfers count here, because the statement sees all the money. A mismatch is a `statement_mismatch` with likely duplicates or gaps. This is what catches a double import in closed periods.
- **Running balance,** when the export has one. It also covers the open period, before any statement exists. Walk the account by day: `close(day) = close(previous day) − sum(amounts that day)` (positive = money out), and that value must appear among the day's `balance_after_cents`. Compare by day, not row by row, because same-day order can change between exports. A break is a `bank_balance_gap`.
- `ledger_check` and `link_statements` stay card-only: they read `statements`, and checking statements live in `bank_statements`.

## 12. Where the code must change

These are the places a new account or type would break or leak. Each needs a test.

| Where | Why |
|---|---|
| `pipeline.process` | Run classification and `link_transfers` after `link_statements` and before `apply_rules`. Add `bank_ledger_check` with the other checks. Add results to `ProcessResult` and the `fin process` summary |
| `categorize/rules.py` `NON_SPENDING` | Add `transfer` and `deposit`, or they go to AI and get categories |
| `categorize/rules.py` `apply_rules` | Keep `p2p` and `check` rows out of `leftovers` |
| `match/amazon.py` `cards.card(t.card_id)` | Raises `KeyError` on a checking row. Use `account_name`. An Amazon purchase paid by debit that doesn't match goes to review as usual |
| `cards/history.py` `resolve_history` | Reads every `purchase` descriptor and sends unknown ones to Claude. Limit it to card rows until Phase B4 |
| `reconcile/transfers.py`, `review.py` | `ReviewSet.flag` asserts `PROCESS_KINDS`, so add the new kinds there |
| `api/app.py` `txn_row`, `/api/meta` | Add `flow_kind` and `counts_as_spending` to rows. Add accounts to `meta.cards` so charts and filters can name them |
| `web/src/pages/Transactions.tsx` | Three hard-coded type lists (`TYPES`, the Category cell, `spending`). The last two are deny lists, so a `transfer` would look like spending. Use `counts_as_spending` |
| `web/src/pages/Review.tsx` | Titles and actions for the new kinds |
| `rebuild.py` | `DERIVED` (§6) |
| `fin/cli.py`, `sync/launcher.py`, `sync/playbooks.py`, `ingest/__init__.py` | Bank sites from config, the generic site file and the private playbook location (§7.5, §8) |
| `scripts/make_cards_example.py` | A fictional bank (§5) |
| `.gitignore`, `scripts/hooks/` | Already done (§1.1). Add a test that a `bank_accounts` site's name and domain are refused |
| `doctor.py` | §7.4 checks. `bank_rules.yaml` loads, its patterns compile and its issuers exist. The privacy scan already covers private words (§1.1) |
| `README.md` | Tagline, Supported sources ("your bank: one checking account, named only in your config"), Safety model |

Already fine, and worth a test: `api/spending.py` (allow-list of types), `reconcile/dedicated_cards.py`, the `tips` and `cap_use` loops in `cards/history.py` (they skip ids without a profile), `/api/sources/status`.

## 13. API and dashboard

Same stack and pieces as the other pages. Status is never shown by color alone.

**API**
- `PATCH /api/transactions/{id}` also takes `type`, `flow_kind` and `apply_to_all`, on checking rows only (422 on a card row). Allowed types: `purchase`, `transfer`, `deposit`, `refund`. Close the row's open transfer review items.
- `GET /api/transactions/{id}` adds `transfer`: the linked rows, with card, date, amount and method.
- `GET /api/summary` adds `cash_flow`: money in by kind, card payments, own transfers, investments, unmatched.
- `GET /api/reconciliation/bank`: checking statements, balance status, and card payments linked vs. not, per month.
- GET never calls Claude.

**Overview:** a "Money in and out" card, shown when a bank account exists, with the note "Card payments aren't spending: those purchases already count on each card." The "By card" chart becomes "By card or account".

**Transactions:** a Checking option in the account filter, and `transfer` and `deposit` in the type filter. An excluded row shows a badge and no category control. Its detail panel says "Paid American Express Gold, Oct 3" with a link to that row, and offers "This is spending" / "This isn't spending".

**Review:** a "Card payments to check" group for the two transfer kinds, with one-click choices.

**Reconciliation:** a checking section with statement math, ledger and balance status, plus "Card payments: 14 of 16 matched to checking; 2 came from elsewhere or aren't synced yet."

## 14. CLI

| Command | Does |
|---|---|
| `fin sync bank` | §7, then import. Part of `fin weekly` |
| `fin transfers [--month YYYY-MM]` | Card payments, own transfers and investments, with what each linked to and what's unmatched |
| `fin transfers --check` | §10.6 invariants and §11 status. Exits non-zero on a failure |

## 15. Phases and acceptance

### Phase B0: Foundation (no real checking data)

**Build**
- First the remaining privacy pieces (§1.1): `make_cards_example.py`'s fictional bank, the private playbook location and the generic site plumbing (§7.5). The hooks, `fin doctor`'s privacy scan and `private_words` already exist.
- The §3 edits, config, `bank_rules.yaml` validation, the migration, categories.
- §7.3's guard rules with tests.
- The §12 rows that stop a new type or account from breaking anything: `NON_SPENDING`, `account_name` in `match/amazon.py`, `txn_row` and `/api/meta`, `counts_as_spending` in `Transactions.tsx`, `rebuild.py`, `doctor.py`.

**Accept**
- `uv run pytest` and `make check` pass, and `fin doctor` is green or explains each failure.
- The hook tests in `tests/unit/test_privacy_scan.py` still pass, plus a new one where the private word comes from a `bank_accounts` site.
- `make_cards_example.py`, run on a test `cards.json` holding a realistic bank, writes none of its words.
- With the new code and no checking data, `fin process` on real data changes no spending number to the cent. Compare a before and after snapshot by month, card and category, kept in `debug/`.
- Guard tests prove a money page is blocked by URL before and after navigation, an amount can't be typed, and the fictional page's Zelle and transfer controls are blocked by their own labels.
- Then I add the real entries to `config/cards.json`, and `fin doctor` finds no private word in tracked files or commit messages.

### Phase B1: Sync (files only)

**Build**
- The site file and the playbook (§7.2). No parser is registered, so files wait in the inbox.
- I run `fin sync bank`, and we iterate on the site file and playbook.

**Accept**
1. Chrome opens the login page, I log in and pass verification, and the export and statements land in the run folder. The manifest validates.
2. `guard.log` shows no money page reached, and every block is explained.
3. The run report answers §7.2's questions, and they're recorded in `docs/decisions.md` without naming the bank.
4. `transactions` has no checking rows.
5. Fictional fixtures in `tests/fixtures/bank/` copy the real files' structure: columns, signs, date format and the statement labels the parser needs. I've compared them with the real files.

If there's no CSV, or the export can't reach the earliest card transaction, stop here and ask.

### Phase B2: Import and no double counting

**Build**
- §8–§11, the `PATCH` overrides and `fin transfers`.
- The remaining §12 rows in Python: `pipeline.process`, `apply_rules`, `resolve_history`, `review.py`.
- Run `fin backup` and take the §10.6 invariant 5 snapshot **before** the first real import.

**Accept, on real data**
1. Spending from card rows is unchanged to the cent, by month, card and category.
2. Every card payment inside the checking period is linked, or the Reconciliation count explains it. Every checking row naming an issuer is linked or in review.
3. `fin transfers --check` passes.
4. Every imported statement balances and ties to the ledger, and the running balance has no gaps.
5. Importing the same run folder again adds zero rows. `fin process` twice gives identical links and types. `fin rebuild` then `fin process` gives identical monthly totals, with my overrides intact.
6. `transfer_window_days` is tightened to what real rows show, and the spread is recorded (days only).
7. `ai-runs/` inputs hold no `p2p`, check, transfer or deposit rows, and no names, private words or long digit runs: a test, plus a look at one real run folder.
8. I've reviewed the first batch of rows by hand and corrected what's wrong.

### Phase B3: Dashboard

**Build:** §13, `Review.tsx` and the README.

**Accept**
- For one month I pick, the "Money in and out" numbers reproduce by hand from the statement.
- Each excluded row shows why, and a card payment links to its card row.
- A correction made on the dashboard survives `fin rebuild`.
- Light and dark themes both work, and `make check` passes.

### Phase B4 (optional): Debit purchases in Missed rewards

Extend `resolve_history` and `tips` to plain debit purchases, never `p2p`, `check` or `atm` rows: "You paid Whole Foods from checking; Prime Visa would have earned $4.20." Value the debit side at its own earn rate and point value, kept in `cards.json` (`bank_accounts[].debit_rewards`), since the rate could identify the bank.

**Accept:** 5 rows reproduce by hand. No person's name appears in a `merchant_profile` input.

## 16. Testing

SPEC §18 applies. No model, network or browser in tests.

- **Fixtures:** `tests/fixtures/bank/`, fictional files ("Example Bank", `bank.example`) with the real files' structure, plus goldens in `tests/golden/`. Never redacted copies of real bank files (§1.1).
- **Parsers:** columns, signs, pending dropped, overlapping exports, identical same-day rows, statement section picked out of a statement covering several accounts, document over manifest.
- **Cleaning:** a table of fictional descriptors, including ACH lines carrying a name, IDs and an account fragment.
- **Classification:** every row of §4's table, with precedence (override > my rule > `bank_rules.yaml` > export type > sign).
- **Transfers:** one test per §10 case:
  - exact match, either date order, window edges
  - wrong issuer rejected
  - two payments with the same amount
  - group of 2 and of 3
  - amount-only match, and my override of it
  - no other side: inside grace, after grace, older than the card ledger
  - card not tracked
  - returned payment
  - credit balance refund
  - a statement-sourced card row replaced by its CSV row (the CASCADE)
- **End to end:** import card and checking fixtures, run `fin process --no-ai`, assert that a card payment is in no spending figure and that `credits_rewards_cents` is unchanged. Then process again and rebuild, and assert nothing moved.
- **Types:** a test that every `TXN_TYPES` value is exactly one of spending or `NON_SPENDING`, so a future type can't slip through.
- **Checks:** a doubled row and a missing row each fail the ledger check and the balance walk.
- **Config:** a missing `bank_accounts` key still loads, and an id used by both a card and an account is refused. `tests/unit/test_config.py` pins the issuer set, so update it.
- **Privacy:** the hooks have tests already (`tests/unit/test_privacy_scan.py`); add bank-site words, `make_cards_example.py`, the private playbook location, and the launcher choosing `sync/sites/bank.md` for a bank site with any id.
- **Guard:** §7.3. **Launcher:** `money_site` set only for bank sites, `--driven` refused.
- **API:** `PATCH` refuses card rows, and no GET calls `claude`.

## 17. Manual steps (me)

- Approve §3 by starting Phase B0.
- After Phase B0, copy the real entries from `private/bank-notes.md` into `config/cards.json` (never `cards.example.json`), check `product` and `account_ending` (`private_words` is already filled in), then run `fin init` so the account reaches the database.
- Run `fin sync bank` when asked: type `start`, log in, pass the verification, approve each click, and deny anything that looks wrong.
- Compare the fictional fixtures with the real files before they're committed.
- Go through the first Review batch: person-to-person payees, checks, and any payment to a card not in cards.json.
- Say which accounts are mine: savings at the bank, accounts at other banks, brokerages.

## 18. Later

- Savings and money market as accounts, so own transfers link on both sides.
- Venmo and PayPal histories, so `p2p` rows get real merchants.
- Subscriptions and recurring bills across cards and checking.
- A scripted sync (SPEC Phase 6), once the playbook is stable.
