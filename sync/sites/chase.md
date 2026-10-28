# Site: Chase (chase.com)

Collect posted transactions and closed statements for each card in the run parameters.
Read-only. Never use "Pay card", "Make a payment", transfers, rewards redemption, card lock,
paperless or any settings, and never add or activate an offer (the Offers step below only
reads the list).

## For each card in `cards`
1. **Transactions (CSV).** Use Chase's own download for account activity:
   - choose this card's account (match the product name),
   - file type **CSV** (note in the run report whether QFX / OFX / Quicken is offered),
   - Activity: **"Since last statement"**. Chase ignores typed date ranges (seen twice), and
     closed periods come from the statement PDFs anyway, so this is all that's needed.
   The file downloads into the run folder by itself. Don't rename it.
   Manifest entry: kind `transactions`, this card's `card_id`, period_start = the last
   statement's closing date + 1 day if you know it (else null), period_end = today.
2. **Statements (PDF).** Open the card's statements list. Download each statement that matches
   `statements_wanted`, skipping closing dates in `statements_already_imported`.
   The PDF should download into the run folder. If it opens in a viewer tab instead, note it
   in the run report and save it with `browser_pdf_save` to
   `<run folder>/chase_<card_id>_<closing date YYYY-MM-DD>.pdf`.
   Manifest entry: kind `statement`, this card's `card_id`, period_start/period_end = the
   statement period shown in the list (if only the closing date is shown, set period_end to it
   and period_start to null).

Do the cards one at a time. If one card's download fails twice, record a gap and move on.

## Offers (only when `collect_offers` is true in the run parameters)
After the transactions and statements, for each card in `cards`: open the card's Chase Offers
list ("Chase Offers" on the card's account page, then "See all offers"). Read only.
**Never click "Add offer" or the "+" on an offer tile, Enroll, Activate or Add, and never
remove an offer**; the guard blocks those controls (and an "Added" filter, which looks the
same to it). Use "All offers", "View all" or "Load more" to show the whole list, and scroll.
- Save the whole list with `browser_snapshot` and `filename` =
  `<run folder>/offers_<card_id>.md`. If the snapshot is cut short or empty, use
  `browser_pdf_save` to `<run folder>/offers_<card_id>.pdf` instead.
- Manifest entry: kind `offers`, this card's `card_id`, period_start null, period_end = today,
  note = how many offers the page showed.
- If a card has no Chase Offers page, or it won't load after two tries, record a gap (kind
  `offers`) and move on.
- Where the playbook says not to use offers, it means never add or enroll; reading the list
  here is fine. Record where the offers list is in the playbook.

## Run report
- `collected`: one row per card and kind with the number of files.
- `gaps`: anything in the plan you couldn't get, with the reason.
- `export_formats_offered`: what the download dialog offered (e.g. CSV, QFX, QBO, PDF).
- notes: the download file names Chase used, and where the download and statement links were.

## Playbook
Record where the activity download and the statements list are, which controls you used,
and every login or SSO host you saw under `## Login hosts`.
