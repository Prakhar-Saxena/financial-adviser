# Site: Citi (citi.com)

Collect posted transactions and closed statements for each card in the run parameters
(the Costco Anywhere Visa). Read-only. Never use "Make a payment", balance transfers,
credit-line increase requests, rewards redemption, card lock, paperless or settings, and never
enroll in an offer (the Offers step below only reads the list).

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

## Offers (only when `collect_offers` is true in the run parameters)
After the transactions and statements, for each card in `cards`: open the card's Citi Merchant
Offers list ("Offers" or "Merchant Offers" for the card). Read only.
**Never click "Enroll", Enroll, Activate or Add, and never remove an offer**; the guard blocks
those controls (and an "Added" filter, which looks the same to it). Use "All offers", "View all"
or "Load more" to show the whole list, and scroll.
- Save the whole list with `browser_snapshot` and `filename` =
  `<run folder>/offers_<card_id>.md`. If the snapshot is cut short or empty, use
  `browser_pdf_save` to `<run folder>/offers_<card_id>.pdf` instead.
- Manifest entry: kind `offers`, this card's `card_id`, period_start null, period_end = today,
  note = how many offers the page showed.
- If a card has no Citi Merchant Offers page, or it won't load after two tries, record a gap
  (kind `offers`) and move on.
- Where the playbook says not to use offers, it means never add or enroll; reading the list
  here is fine. Record where the offers list is in the playbook.

## Run report
- `collected`: one row per card and kind with the number of files.
- `gaps`: anything you couldn't get, with the reason.
- `export_formats_offered`: what the download offered.
- notes: the file names Citi used and where the download and statements controls were.

## Playbook
Where the activity download and statements are, the controls you used, and the login and SSO
hosts under `## Login hosts`.
