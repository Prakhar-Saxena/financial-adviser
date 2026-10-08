# Site: Chase (chase.com)

Collect posted transactions and closed statements for each card in the run parameters.
Read-only. Never use "Pay card", "Make a payment", transfers, offers, rewards redemption,
card lock, paperless or any settings.

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

## Run report
- `collected`: one row per card and kind with the number of files.
- `gaps`: anything in the plan you couldn't get, with the reason.
- `export_formats_offered`: what the download dialog offered (e.g. CSV, QFX, QBO, PDF).
- notes: the download file names Chase used, and where the download and statement links were.

## Playbook
Record where the activity download and the statements list are, which controls you used,
and every login or SSO host you saw under `## Login hosts`.
