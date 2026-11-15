# Site: the bank (checking account)

Collect posted transactions and closed statements for each account in `accounts` in the run
parameters. This site can move real money, so it is strictly read-only. Never use transfers,
person-to-person payments (Zelle or similar), bill pay, wires, external accounts, check deposit,
stop payment, card controls, alerts, direct deposit setup or settings. Never reveal a full
account or routing number (an eye icon or "show" control next to the number), and never open
account details. A guard blocks money pages and typing amounts; if it blocks something, don't try
another way. Note it and move on.

The site's name and layout are in the playbook. Use only the menus the steps below need, and
don't open any of the playbook's `## Avoid` items.

## For each account in `accounts`
1. **Choose the account.** Pick the account whose name matches `product` (or whose ending
   matches `account_ending`, when that isn't empty). Don't open savings, loans, certificates or
   cards. If no name matches but exactly one checking account is listed, use it and say so in
   the run report. If several could match, stop and ask the user.
2. **Transactions (export).** Find the site's own download or export for this account's
   activity (often an icon or "Export"/"Download" above the transaction list):
   - format **CSV** if offered (note every format offered in the run report),
   - date range `transactions_from` to `transactions_to`. Type dates with their slashes or
     dashes (e.g. 03/04/2025): the guard blocks bare digits here, because they look like an
     amount. If a date field only takes bare digits, use its calendar picker instead, or note
     it as a gap. Check in a snapshot that the dates held before downloading. On the first
     run (`first_run` true) the range is long, so the card payments in the ledger can find
     their checking rows. If the site won't go back that far, use the earliest date it
     accepts and note how far back it reached,
   - posted rows only, if the site lets you choose.
   The file downloads into the run folder by itself. Don't rename it.
   Manifest entry: kind `transactions`, this account's `card_id`, period_start = the first day
   the export covers, period_end = the last day it covers.
3. **Statements (PDF).** Open the statements (often "eStatements" or "Documents"). Download each
   statement in `statements_wanted`, skipping closing dates in `statements_already_imported`.
   If a PDF opens in a viewer tab instead of downloading, save it with `browser_pdf_save` to
   `<run folder>/<site>_<card_id>_<closing date YYYY-MM-DD>.pdf` and note it. If the site gives
   every statement the same file name, save each one with `browser_pdf_save` under that unique
   name instead, so a later download can't overwrite an earlier one.
   Manifest entry: kind `statement`, this account's `card_id`, period_end = closing date,
   period_start = the period start if shown, else null.

Go straight to these pages. Don't open single transactions, and don't snapshot the transaction
list more than the task needs. If one download fails twice, record a gap and move on.

If a page leaves the site's domains (statements often sit on another company's host), the guard
blocks it. Record the host in the run report and stop that step. The user decides whether it
joins the playbook's `## Login hosts`.

## Run report
- `collected`: one row per account and kind with the number of files.
- `gaps`: anything you couldn't get, with the reason.
- `export_formats_offered`: every format the export offered.
- notes, one fact each:
  - how far back the export reached (the earliest date it accepted, or "transactions_from
    held"),
  - whether pending rows are in the export, or could be left out,
  - the file names the site used for the export and the statements,
  - whether a statement covers only this account or every account held at the bank,
  - how many statements the list showed and how far back they go,
  - any host outside the site's domains that a page tried to open.

## Playbook
Where the account list, the activity export and the statements are, the controls you used, the
date format the date fields take, and the login hosts under `## Login hosts`. Keep `## Avoid`.
No account names or nicknames, numbers, balances or payees.
