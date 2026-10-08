# Site: American Express (americanexpress.com)

Collect posted transactions and closed statements for each card in the run parameters (the
Platinum and Gold charge cards). Read-only. Never make a payment, set up Pay Over Time, Plan It
or Send & Split, enroll in or add Amex Offers, redeem or transfer Membership Rewards points, add
or manage cards, request anything, or change settings.

## For each card in `cards`
1. **Switch to the card** with the account switcher (card product names are in `cards`).
2. **Transactions (CSV).** Use Amex's own download for activity (usually "Download" on the
   Statements & Activity / transactions page):
   - format **CSV**, and tick "include all additional transaction details" if offered,
   - activity since the last statement (the current, open period). Closed periods come from the
     statement PDFs. If only date ranges are offered, use `transactions_from` to
     `transactions_to`; check in a snapshot that the dates held before downloading.
   The file downloads into the run folder by itself. Don't rename it.
   Manifest entry: kind `transactions`, this card's `card_id`, period_start = the first day the
   export covers if shown (else null), period_end = today.
3. **Statements (PDF).** Open the card's statements list. Download each statement that matches
   `statements_wanted`, skipping closing dates in `statements_already_imported`. If a PDF opens in
   a viewer tab instead, save it with `browser_pdf_save` to
   `<run folder>/amex_<card_id>_<closing date YYYY-MM-DD>.pdf` and note it.
   Manifest entry: kind `statement`, this card's `card_id`, period_end = closing date,
   period_start = the period start if shown, else null.

Do the cards one at a time. If one download fails twice, record a gap and move on.

## Run report
Counts per card and kind, gaps with reasons, export formats offered, the file names Amex used,
and where the download and statement controls were.

## Playbook
Where the account switcher, activity download and statements are, the controls you used, and
the login and SSO hosts under `## Login hosts`.
