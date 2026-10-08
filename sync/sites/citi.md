# Site: Citi (citi.com)

Collect posted transactions and closed statements for each card in the run parameters
(the Costco Anywhere Visa). Read-only. Never use "Make a payment", balance transfers,
credit-line increase requests, offers, rewards redemption, card lock, paperless or settings.

## For each card in `cards`
1. **Transactions (CSV).** Use Citi's own download for account activity (often a
   "Download" or "Export" icon above the transaction list):
   - choose this card's account,
   - format **CSV** (note in the run report what other formats are offered),
   - time period: activity **since the last statement** (Citi may call it "Current
     statement period" or "Since last statement"). Closed periods come from the statement
     PDFs. If only date ranges are offered, use `transactions_from` to `transactions_to`.
   The file downloads into the run folder by itself. Don't rename it.
   Manifest entry: kind `transactions`, this card's `card_id`, period_start = the first day
   the export covers if shown (else null), period_end = today.
2. **Statements (PDF).** Open the card's statements list. Download each statement that matches
   `statements_wanted`, skipping closing dates in `statements_already_imported`. If a PDF opens
   in a viewer tab instead of downloading, save it with `browser_pdf_save` to
   `<run folder>/citi_<card_id>_<closing date YYYY-MM-DD>.pdf` and note it.
   Manifest entry: kind `statement`, this card's `card_id`, period_end = closing date,
   period_start = the period start if shown, else null.

If one download fails twice, record a gap and move on.

## Run report
- `collected`: one row per card and kind with the number of files.
- `gaps`: anything you couldn't get, with the reason.
- `export_formats_offered`: what the download offered.
- notes: the file names Citi used and where the download and statements controls were.

## Playbook
Where the activity download and statements are, the controls you used, and the login and SSO
hosts under `## Login hosts`.
