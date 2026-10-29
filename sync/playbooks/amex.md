# American Express playbook

## Login hosts
- www.americanexpress.com (start page, "Log In" link)
- global.americanexpress.com (signed-in area)

## Layout
- Overview: https://global.americanexpress.com/overview lists each card as a tile. Clicking a tile opens its dashboard with `account_key=<key>` in the URL. Card names on the tiles may differ from the run parameters (the Platinum card can appear as a co-brand name).
- Card switcher: combobox at top of the signed-in pages ("Open to manage your other accounts"). Navigating with `?account_key=` also works once the key is known. Keys also appear in overview tile links.
- Activity: /activity?account_key=<key>&cycleIndex=0 is "Since Last Statement" (open period, starts the day after the last closing). The page can take a few seconds to render the Download button. A "Welcome to the New Statements & Activity" dialog may appear: click "Explore On My Own".
- Statements: /activity/statements?account_key=<key>. "Recent Statements" lists closing dates, each with View and Download buttons (test id ends in `<YYYY-MM-DD>/download-button`).

## Transactions CSV
- Click Download above the transaction list, choose CSV in "Select File Type", make sure "Include all additional transaction details" is ticked (re-check in a snapshot; it was once shown unticked, and clicking its label toggles it), then Download.
- Dialog refs change as the page re-renders; click the dialog button with `[role="dialog"][aria-modal="true"] button:has-text("Download")`.
- Pending charges are not exported.

## Statements
- Click Download for a closing date, leave "Billing Statement (PDF)" selected, click Download in the dialog. Other options: PDF screen reader optimized, Excel.

## Avoid
- Every CSV is named `activity.csv`. The guard renames it to `activity-<n>.csv` right after each download; use that name in the manifest.
- PDFs are named `<closing date>.pdf` with no card in the name. Two cards can close on the same date, and then the second download overwrites the first with no rename. Before downloading a second PDF with an already-used name, check the cards' closing dates; if they match, stop and ask the user, or save the PDF with a unique absolute filename instead.
- Do not use Pay Over Time, Plan It, Autopay enroll, Offers, CreditSecure or rewards links.
