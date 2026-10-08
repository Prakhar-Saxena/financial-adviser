# American Express playbook

## Login hosts
- www.americanexpress.com (start page, "Log In" link)
- global.americanexpress.com (signed-in area)

## Layout
- Overview: https://global.americanexpress.com/overview lists each card as a tile. Clicking a tile opens its dashboard with `account_key=<key>` in the URL.
- Card switcher: combobox at top of the signed-in pages ("Open to manage your other accounts"). Navigating with `?account_key=` also works once the key is known.
- Activity: /activity?account_key=<key>&cycleIndex=0 is "Since Last Statement" (open period). A "Welcome to the New Statements & Activity" dialog may appear: click "Explore On My Own".
- Statements: /activity/statements?account_key=<key>. "Recent Statements" lists closing dates, each with View and Download buttons (test id ends in `<YYYY-MM-DD>/download-button`).

## Transactions CSV
- Click Download above the transaction list, choose CSV in "Select File Type", keep "Include all additional transaction details" ticked, then Download.
- Dialog refs change as the page re-renders; click the dialog button with `[role="dialog"] button:has-text("Download")`.
- Pending charges are not exported.

## Statements
- Click Download for a closing date, leave "Billing Statement (PDF)" selected, click Download in the dialog. Other options: PDF screen reader optimized, Excel.

## Avoid
- Every CSV is named `activity.csv` and every PDF `<closing date>.pdf`, with no card in the name. The guard renames `activity.csv` to `activity-<n>.csv` right after each download and says so; use that name in the manifest. Closing dates differ by card, so PDFs don't collide.
- Do not use Pay Over Time, Plan It, Autopay enroll, Offers, CreditSecure or rewards links.
