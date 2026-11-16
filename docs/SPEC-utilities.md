# Financial Adviser: Utilities

Build spec for Claude Code. Version 1, 2026-11-15.
Extends `docs/SPEC.md`, the MVP spec. SPEC §20 lists "detect subscriptions and recurring charges, and alert on price creep" for later; this does it for utility bills, from the payments already in the ledger.
Section numbers (§1, §2, …) refer to this file. "SPEC §n" means `docs/SPEC.md`; "SPEC-checking §n" means `docs/SPEC-checking.md`. Phases are U0–U2.

**How to start:** open Claude Code in this repo and say:
*"Read docs/SPEC-utilities.md end to end, then CLAUDE.md, docs/SPEC.md §7, §8, §13, §15 and §18, docs/SPEC-checking.md §1.1 and §12, and docs/decisions.md. Then start Phase U0. Stop at the end of each phase and show me the acceptance checks."*

Starting Phase U0 is my approval of the changes in §3.

---

## 0. Working agreement (for Claude Code)

1. Everything in SPEC §0 and CLAUDE.md still applies: one phase at a time, tests passing and acceptance checks run at the end of each, then stop for my review. Decisions and verified facts go in `docs/decisions.md`. Free dependencies with permissive licences are pre-approved; record each one. New source files get the copyright and SPDX header.
2. **The hard rules (SPEC §2) don't change.** No new site to sync, no network host, no AI role, no new kind of file to import. If the work seems to need one, stop and ask. §3 lists the only spec changes; copy them into `docs/SPEC.md` and `CLAUDE.md` in Phase U0.
3. **Everything comes from the ledger.** Utility payments are already there, on the cards and the checking account, categorized by the existing classifier. This feature links them to utility accounts and compares them over time. Nothing is downloaded from a provider.
4. **The repo is public, and my providers are private** (§1.1). Their names, my account numbers and address, and the places I live appear only in `config/cards.json`, `private/` and the data dir. Everywhere else, including `docs/decisions.md`, tests and commit messages, it's "the electric utility", "the phone carrier" or an account id (§5). No payment data goes in the repo either: record shapes and counts ("5 monthly payments, one quarterly account"), never amounts or dates.
5. **Spending doesn't move.** Linking a payment to a utility never changes its type, category or amount. Overview's totals stay the same to the cent at every commit.
6. You may run `fin process` and `fin rebuild` on real data, as CLAUDE.md allows.
7. Tests never call a model, the network or a browser.
8. If reality disagrees with this spec, stop and tell me.

## 1. Goal and scope

- **One tab for utilities.** Electricity, gas, water, internet and phone: what each costs per month, per service and per provider, over as many months as the ledger has.
- **From payments already in the ledger.** No new logins and no downloads. A utility paid from an account the app doesn't track won't appear.
- **Trends I'd otherwise miss:**
  - a flat bill (internet, phone) changing: a promotion ending, a fee going up
  - the same month a year earlier, once the ledger holds a year, since energy is seasonal
  - an unusual payment, a missing one (a failed autopay), a possible double payment
  - a change of provider, a budget plan hiding the seasons, which months carry the year
- **History grows with the ledger.** It starts with the first synced months; comparisons with a year earlier appear once a year of payments exists, and the tab says when.
- **Private.** Nothing in the public repo says who my providers are or where I live (§1.1).
- **Out of scope:** bills and usage data from the providers' sites (§16), paying or scheduling anything, plan or supplier changes, notifications, and other recurring bills (insurance, subscriptions).

### 1.1 Keeping my providers private

My electric, gas and water providers say where I live: each serves one area, and a water bill names a town.

**Private, wherever it appears:** provider names, account numbers, service addresses and place names.

**Where it may appear:**
- `config/cards.json` (gitignored): `utility_accounts` and `private_words` (§5).
- `private/utility-notes.md` (gitignored): the real `cards.json` entries and what the ledger showed.
- The data dir, as for everything else.

**Generic in the repo:**
- Account ids are the service: `electric`, `gas`, `water`, `internet`, `phone`, and `phone_old` for an earlier carrier. They name nothing, so they're safe in commands, settings and commit messages. Code never hard-codes them.
- Payment patterns name providers, so they live in `cards.json` (`payment_patterns`) or in my corrections in the database, never in `rules.yaml` or `bank_rules.yaml`.
- Fixtures use fictional providers ("Example Power", "Example Gas", "Example Water", "Example Fiber", "Example Wireless").

**Checks that catch a slip:**
- `scripts/hooks/privacy_scan.py` adds each utility account's `provider` to the private words, unless the account sets `"private": false`. That's for a national carrier whose name is already in the repo's seed files (`config/merchant_profiles.yaml`, `scripts/ai_spike.py`) and says nothing about where I live.
- My street and town go in `private_words`, as for the bank.
- `make_cards_example.py` writes fictional utility accounts, never mine.

## 2. How it works

```
 fin process
   rules ─► AI categories ─► my overrides                          as today
   link_payments: ledger rows ── my corrections, payment_patterns ──► utility_payments
                  (type, category and amount untouched)

 Utilities tab, fin utilities
   linked payments ─► monthly series per account ─► baselines ─► insights (templates)
                      by payment month; quarterly and prepaid payments spread
```

Principles (they extend SPEC §4):
- **Payments are the data.** They're already spending. The tab regroups them by service and provider and compares them over time; nothing is added to spending or taken out of it.
- **Code counts.** Insights are computed, then worded by templates. No AI.
- **Generic code, private config.** Everything that names a provider or a place lives in `cards.json`.
- **My corrections win** and survive `fin rebuild`.

## 3. Changes to the spec

Add these to `docs/SPEC.md` and `CLAUDE.md` as worded here. None of them names a provider. SPEC §2 doesn't change.

**SPEC §6 (repo layout), add:** `private/` also holds `utility-notes.md`.

**SPEC §7 (repo hygiene), add:** utility providers, account numbers, service addresses and place names appear only in `config/cards.json`, `private/` and the data dir. A payment pattern that names a provider never goes in `rules.yaml` or `bank_rules.yaml`.

**SPEC §8:** add the tables and review kinds in §6.

**SPEC §15, §16:** add the Utilities page and the `fin utilities` commands.

**SPEC §20:** price-change alerts exist for utility bills (`docs/SPEC-utilities.md`); other recurring charges stay later.

**CLAUDE.md:** a pointer to this file and the privacy rule, one line each.

## 4. Concepts

- **Service.** What's delivered: `electric`, `gas`, `water` (water and sewer), `trash`, `internet`, `phone` or `other`. Charts group by service, so a change of provider keeps one line.
  - **Seasonal:** electric, gas and water. A month compares with the same month a year earlier.
  - **Flat:** internet, phone and trash. The amount barely moves, so a change is news.
- **Utility account.** One provider account for one service, with the dates it started and ended if I give them. It's in config (§5) and mirrored to the database, like cards.
- **Payment.** A ledger row linked to an account (§7). A refund from a provider counts as a negative payment.
- **Payment month.** The month a payment counts in: its own. A quarterly account's payment (`bill_every_months: 3`) spreads over its month and the two before; a prepaid plan's (`paid_in_advance`) over its month and the ones after. A payment usually pays for the month before, and the tab says "by payment month".
- **Cadence.** The median gap between an account's last 6 payments, else 30 × `bill_every_months` days. The next payment is expected one cadence after the last.
- **Year over year.** The same month a year earlier. It needs 13 months of an account's payments.

## 5. Config

**`config/cards.json`** (gitignored) gains one list. The values below are the fictional ones `config/cards.example.json` will carry. The real ones are in `private/utility-notes.md`, and they go only in `cards.json`.

```json
"utility_accounts": [
  { "id": "electric", "service": "electric", "provider": "Example Power", "private": true,
    "payment_patterns": ["^EXAMPLE POWER\\b"], "bill_every_months": 1,
    "paid_in_advance": false, "active_from": null, "active_to": null }
]
```

- `service`: one of §4's services.
- `provider`: the name the tab shows.
- `private`: default true; false only for a national carrier (§1.1).
- `payment_patterns`: regexes run with `re.search`, case-insensitive, on `description_clean`.
- `bill_every_months`: 1, 2, 3 or 12.
- `paid_in_advance`: true for a prepaid plan (§4).
- `active_from`, `active_to`: optional dates. Outside them no payment is expected, and patterns stop matching 60 days past them.

Validation (pydantic): account ids are unique across cards, bank accounts and utility accounts; every pattern compiles; `active_to` ≥ `active_from`. A missing list loads as empty.

**`config/settings.toml`** gains:

```toml
[utilities]                     # docs/SPEC-utilities.md
stale_grace_days = 10           # "missing" after one cadence plus this
yoy_min_change = 0.10           # a year-over-year insight needs a 10% change...
yoy_min_cents = 500             # ...and $5
step_min_payments = 2           # a flat bill's new amount must hold this many payments...
step_min_cents = 100            # ...and differ by at least $1
outlier_ratio = 1.3             # a payment 30% above its baseline is unusual
duplicate_days = 7              # two equal payments to one account this close together
budget_min_months = 4           # a seasonal account paid the same amount this many months running
default_months = 24             # the tab's range
```

## 6. Data model (migration 0011, batch mode; indicative, like SPEC §8)

| Table | Key columns |
|---|---|
| `utility_accounts` | id (PK), service, provider, bill_every_months, paid_in_advance, active_from NULL, active_to NULL, in_config. Mirrored from `cards.json` by `fin init` and `fin process`, like `seed.sync_categories`. An account removed from config keeps its row with `in_config = false` |
| `utility_payments` | transaction_id (PK) → transactions ON DELETE CASCADE, account_id → utility_accounts, method (`override`/`merchant`/`pattern`), covers_months |

Also:
- **Review kinds** (`REVIEW_KINDS` and `PROCESS_KINDS`):
  - `utility_unassigned`: a payment in a bills category that matches no account
  - `utility_missing`: an active account whose next payment is a cadence plus `stale_grace_days` late
- **My corrections** live in `user_overrides`, under stable keys, so they survive `fin rebuild`:
  - `('transaction', dedupe_key, 'utility_account')`: an account id, or `none` (not a utility payment)
  - `('transaction', dedupe_key, 'utility_covers_months')`
  - `('merchant', merchant name, 'utility_account')`
- **`fin rebuild`:** `DERIVED` gains `UtilityPayment`; `utility_accounts` stays. Add a test.

## 7. Linking payments (`src/fin/utilities/payments.py`)

`link_payments` runs in `fin process` after `apply_overrides`. It's idempotent: it recomputes `utility_payments` from scratch.

**Which rows:** `purchase` and `refund` rows on any card or bank account. Never transfers, deposits or card payments.

**First match wins:**
1. My transaction override: an account, or `none`.
2. My merchant override, by the row's merchant name.
3. An account's `payment_patterns`, within its active dates (60 days of margin either side). A row that matches two accounts goes to step 4, with the reason "matches electric and gas".
4. Otherwise a row in `bills.utilities`, `bills.internet` or `bills.phone` becomes `utility_unassigned`. Rows in other categories are never pulled in.

**`covers_months`:** my override, else the account's `bill_every_months`.

**Nothing else changes.** Linking adds rows to `utility_payments` and never touches a transaction's type, category, amount or review status. A test hashes `transactions` before and after.

**Corrections:** `fin utilities assign` (U1) and the tab (U2) write the transaction override; "every payment from this merchant" writes the merchant override; "Not a utility" writes `none`.

## 8. Series and trends (`src/fin/utilities/`)

`series.py` reads the database. `engine.py` is pure Python with no I/O, so golden tests can pin it. Insights are computed on demand by the CLI and the API; nothing is stored.

### 8.1 Monthly series per account

- A payment counts in its payment month (§4), spread evenly over `covers_months` months with largest-remainder rounding, so the parts add up to the payment exactly.
- **Drift.** A monthly bill's due date drifts, so now and then a calendar month gets two payments and a neighbor none. When that happens on a monthly account, the payment nearer the empty month moves there: the earlier one back, the later one forward. The payment list always shows real dates, with a "counted in September" marker.
- Refunds net in their month.
- Totals per service and overall add up the accounts.

### 8.2 Baselines

- **Seasonal services:** the same month in earlier years. Until one exists there's no baseline: winter would make every month look unusual.
- **Flat services:** the median of the last 6 months, leaving out first payments.

### 8.3 Insights

Each one has a kind, an account or service, a period, its numbers, its text and its links. Severity is `info`, `notice` or `warning`, always shown with an icon and words, never color alone. The texts are templates; the numbers come from the series. They're ranked by dollar impact, then severity.

1. **Flat bill changed.** The median before and after differs by at least `step_min_cents` and holds for `step_min_payments` payments: "Internet went from $X to $Y a month with the March payment."
2. **Year over year** (seasonal, 13 months on). At least `yoy_min_change` and `yoy_min_cents`: "Gas cost 38% more this October than last ($X against $Y)."
3. **Unusual payment.** At least `outlier_ratio` times its baseline: "Water payment 2.1× your usual for a quarter."
4. **Missing.** Once an account has a payment: "No gas payment since Oct 8; one usually comes every 30 days." It also opens a `utility_missing` review item, since a failed autopay looks like this.
5. **Possible double payment.** Two payments to one account with the same amount within `duplicate_days` days: "Paid the electric bill $X twice, on Oct 3 and Oct 5."
6. **Provider switch.** An account's first payment comes within 45 days of the last payment of another account with the same service: "Phone: new provider since September; $X a month before, $Y since."
7. **First payment.** "First payment: it may include setup charges or a partial month." It stays out of baselines.
8. **Budget plan.** A seasonal account paid the same amount `budget_min_months` months running: "The same gas payment 5 months running: a budget plan evens out the seasons, so these payments don't show your use."
9. **Seasonality,** once 12 months exist: "November to March are 71% of a year's gas cost."

### 8.4 What payments can't say

The tab says it plainly, next to the charts: these are payments from tracked accounts, counted in the month they left; a utility paid from elsewhere doesn't show; and payments can't tell using more apart from paying more per unit.

## 9. API (`src/fin/api/routes/utilities.py`, an `APIRouter`)

| Endpoint | Does |
|---|---|
| `GET /api/utilities/summary?months=24&until=YYYY-MM` | Accounts, the monthly series per account and service, totals, the top insights, and the last and next expected payment per account |
| `GET /api/utilities/accounts/{id}` | The account, its payments (date, amount, account paid from, covers-months, the month each counts in), its monthly series and all its insights |
| `GET /api/utilities/unassigned` | Payments in bills categories with no account |
| `POST /api/utilities/assign` `{transaction_id, account_id, all_from_merchant}` | Corrections (§7). `account_id` may be `"none"` |
| `PATCH /api/utilities/payments/{transaction_id}` `{covers_months}` | Correction |

Rules: no GET calls Claude or the network. POST and PATCH require `Content-Type: application/json` (415 otherwise), as today. `/api/meta` lists utility accounts (id, service, provider).

## 10. Dashboard: the Utilities tab

`web/src/pages/Utilities.tsx` at `/utilities`, with a "Utilities" nav item after "Best card"; small components go in `web/src/components/utilities/`. Same pieces as the other pages: Card, Badge, Button, Select, `ChartCard` and the palette in `lib/palette.ts` (one slot per service), light and dark themes, status never shown by color alone. The header's month picker sets the last month shown.

1. **Stat tiles,** like Overview's: "Utilities, last 12 months" (and the change against the 12 before, once it exists); the selected month (against a year ago, else against the 12-month average); "Average month"; "Biggest change" (the top insight's headline).
2. **Monthly cost:** stacked bars by service over the range (12, 24 or 36 months, or all), switchable to by account, with the note "By payment month". Clicking a segment opens that account.
3. **Year over year:** one line per year on a January–December axis, for all utilities or one service; this year in the series color, earlier years muted. Until 13 months exist it says when it will appear ("from July 2027").
4. **Insights:** the top five, each with "See account".
5. **Accounts:** service, provider, paid from (the card's or account's name), last payment (date and amount), last 12 months, average month, against a year ago, a sparkline, and chips: missing, first payment, budget plan, no payments yet. Ended accounts fold under "Earlier accounts".
6. **Account panel** (`/utilities?account=gas`):
   - its monthly chart and its insights
   - payments: date, amount, paid from, the month it counts in, a covers-months control, and a link to the transaction
   - for internet and phone, "Best card for this bill" opens Best card with the merchant filled in (add a `?q=` parameter to Find a card), noting "Some providers charge a fee to pay by card"
7. **Unassigned payments:** each with an account select, "every payment from this merchant" and "Not a utility".
8. **Footer:** per account, "Last payment Nov 3 · next expected around Dec 3", and §8.4's note.

The Review page gets a "Utilities" group for the two new kinds, linking here. Overview doesn't change.

## 11. CLI (`fin utilities`, a Typer sub-app)

| Command | Does |
|---|---|
| `fin utilities` / `fin utilities status` | Per account: last payment, months covered, cadence, next expected, open items |
| `fin utilities trends [--account ID] [--months 24]` | The insights |
| `fin utilities unassigned` | Payments in bills categories with no account, with their transaction ids |
| `fin utilities assign TXN_ID ACCOUNT [--all-from-merchant]` | A correction (§7). `ACCOUNT` may be `none` |
| `fin utilities check [--snapshot]` | Invariants: linking changed no transaction; spending by month, card and category equals the snapshot in `debug/` (`--snapshot` writes one); a payment links to at most one account; each account's series adds up to its linked payments. Exits non-zero on a failure |

Make target: `make utilities`.

## 12. Where the code must change

| Where | Why |
|---|---|
| `config.py` | `UtilityAccount`; `utility_accounts` on `CardsConfig`; unique ids (§5); a `utility_account` helper |
| `scripts/make_cards_example.py` | Fictional utility accounts, whatever the real ones are |
| `scripts/hooks/privacy_scan.py` | §1.1: providers of private accounts join the private words |
| `db/models.py`, migration 0011 | §6. Autogenerate doesn't compare CHECKs, so write those by hand, as in 0009 |
| `db/seed.py` | Mirror utility accounts from config, in `fin init` and `fin process` |
| `review.py`, `rebuild.py` | The two kinds; `DERIVED` (§6) |
| `pipeline.py` | `link_payments` after `apply_overrides`, the missing check with the other checks, counts in `ProcessResult`, and one summary line ("Utilities: 14 payments linked, 1 unassigned") |
| `fin/cli.py` | The `fin utilities` sub-app |
| `api/app.py` | Include the router; utility accounts in `/api/meta` |
| `web/src/App.tsx`, `api.ts`, `pages/Utilities.tsx`, `components/utilities/`, `pages/Review.tsx`, `components/cards/FindCard.tsx` | §10 |
| `doctor.py` | The utility config loads and its patterns compile |
| `scripts/make_screenshots.py` | A Utilities screenshot from the mock database, which already has fictional bills rows |
| `README.md` | Everyday use (`fin utilities`) and Dashboard (the Utilities tab) |

Already fine, and worth a test: `api/spending.py` and the Overview totals (linking changes nothing), and `cards/history.py` (utility payments stay ordinary purchases).

## 13. Phases and acceptance

### Phase U0: Foundation (no real utility data)

**Build**
- §3's edits; the config model and validation; the example config; the privacy scan's new words.
- Migration 0011, the accounts mirror, the review kinds, the rebuild list and the `[utilities]` settings.
- `fin utilities status` (empty state) and the `fin doctor` check.

**Accept**
1. `uv run pytest` and `make check` pass, and `fin doctor` is green or explains each failure.
2. Privacy tests: a provider name from a test `cards.json` is refused in a staged line and in a commit message. An account with `"private": false` adds no word.
3. `make_cards_example.py`, run on a test `cards.json` holding realistic utilities, writes none of their words.
4. With the new code and no utilities in config, `fin process` on real data changes no spending number to the cent: a before and after snapshot by month, card and category, kept in `debug/`.
5. Then I add the real entries from `private/utility-notes.md` to `cards.json` and run `fin init`, and `fin doctor` finds no private word in tracked files or commit messages.

### Phase U1: Payments and trends (terminal)

**Build**
- §7's linking and corrections, §8, and §11's commands.

**Accept, on real data**
1. Every row in `bills.utilities`, `bills.internet` and `bills.phone` is linked to an account, marked not a utility, or listed as unassigned, and I've sorted the unassigned ones.
2. For one month I pick, each account's figure reproduces by hand from its payments.
3. Spending, credits and types per month, card and category are identical to the cent before and after. `fin utilities check` passes.
4. `fin rebuild` then `fin process` gives the same links, and my corrections survive.
5. A quarterly payment spreads over 3 months, and a prepaid one forward over the months I set.
6. The engine's golden tests pass (§14).

### Phase U2: The Utilities tab

**Build**
- §9 and §10, the Review group, the README and a screenshot.

**Accept**
1. For one month I pick, the tab's numbers match `fin utilities status` and `trends`.
2. A test with sockets blocked asserts that no GET calls `claude` (the fake CLI's log stays empty) or opens a connection.
3. A correction made in the tab survives `fin rebuild`.
4. Light and dark themes both work, and `make check` passes.

## 14. Testing

SPEC §18 applies: no model, network or browser in tests.

- **Fixtures:** fictional only. Unit tests build ledger rows directly. The end-to-end test imports a small fictional checking export and card export with utility rows (`tests/fixtures/utilities/`).
- **Linking:** precedence (transaction override, merchant override, patterns), two accounts matching, active dates, unassigned, covers-months backward and forward, and `transactions` unchanged (a hash).
- **Engine golden table,** at least 20 scenarios with the expected series and insight texts, to the cent: spreading with largest remainder, the drift move both ways, refunds, quarterly, prepaid, year over year with and without history, a flat bill change with a one-time spike ignored, an unusual payment, missing (and not missing past `active_to`), a double payment, a provider switch, a first payment, a budget plan, seasonality.
- **Privacy:** §1.1's words, `"private": false`, and `make_cards_example.py`.
- **API:** no GET calls `claude` or the network; 415 without JSON.
- **Rebuild:** corrections and accounts stay; the links are identical.
- **End to end:** import the fixtures, run `fin process --no-ai`, and assert that spending equals the run without utilities in config and that the series and insights match the goldens.

## 15. Manual steps (me)

- Approve §3 by starting Phase U0.
- After U0, copy the real entries from `private/utility-notes.md` into `config/cards.json` (never `cards.example.json`), add my street and town to `private_words`, and run `fin init`.
- In U1, sort the unassigned payments, and say which payments were prepaid for several months.
- In U2, check one month on the tab by hand.

## 16. Later

- Bills and usage data from the providers (bill PDFs, Green Button usage files): usage, price per unit, and using more against paying more. Only if I decide it's worth it, and by hand into `inbox/` rather than another synced site.
- Older history: a longer checking export would bring older utility payments, but also everything else from those months.
- The same price-change checks for other recurring bills, such as insurance and subscriptions (SPEC §20).
