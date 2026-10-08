# Chase playbook

## Login hosts
- www.chase.com (start page)
- secure.chase.com (logged-in dashboard, after the user logs in)

## Layout
- After login the dashboard lists each credit card as "<Product> (...last4)".
- Clicking a card name opens its summary page. Its toolbar has a "Statements" button and a "Download account activity" icon button (above the transaction list).
- Returning to the dashboard: navigate to secure.chase.com/web/auth/dashboard#/dashboard/overview, then use browser_find to get the card button ref.

## Transactions (CSV)
- Download page: Download Account Activity (secure.chase.com/web/auth/dashboard#/dashboard/transactions/downloads/<accountId>/CARD/BAC). Opening it from the card summary page preselects that card and CSV ("Spreadsheet (Excel, CSV)").
- Controls: Account, File type, Activity. File types offered: Spreadsheet (Excel, CSV), QFX, QIF, QBO.
- Activity dropdown options: Since last statement, Choose a date range, Previous selection. Use "Since last statement": typed date ranges are ignored by Chase (two runs), and statements cover closed periods.
- The first Download click does nothing; click again once. A file named Chase<last4>_Activity_<date>.csv lands in the run folder (saved with hyphens).
- After a download the page shows "Download other activity" and "Go back to accounts"; the page Back button returns to the card summary.
- There is no posted-only choice; online history is limited to 24 months.

## Statements (PDF)
- Card summary page > "Statements" button opens the statements list for that card.
- Each row has "opens document" and "Saves document" links. Use "Saves document": it downloads <YYYYMMDD>-statements-<last4>-.pdf straight into the run folder. Several can be clicked in a row.
- Only the closing date is shown. Statements are newest first.

## Avoid
- Pay card, Paperless, Redeem, Order a copy, offers, rewards, and anything in Profile & settings.
- The dashboard has marketing tiles (pre-approvals, offers); ignore them.
- The datepicker logs console errors; they are harmless.
