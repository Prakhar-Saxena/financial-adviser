# Costco playbook

## Where things are
- Start page: https://www.costco.com/ . When signed in, the header shows an "Account" button instead of "Sign In / Register".
- Orders: click the header "Orders & Returns" link (it goes to www.costco.com/myaccount/#/app/<id>/ordersandpurchases). Tabs: Online, Warehouse, Installation Services. Online is selected first.
- Warehouse tab: "Showing" dropdown (default Last 3 Months, then quarterly periods) plus a Filter button. 10 entries per page with pagination ("Go to page 2"). Each row shows type (In-Warehouse or Gas Station), date/time, warehouse, total and a "View Receipt" button.
- Online tab: same dropdown (labels are case-sensitive, e.g. "2026 May - July"); shows "No orders are available for the selected date range." when empty.

## Receipt dialog
- "View Receipt" opens a dialog with the full receipt as readable text (item numbers, descriptions, amounts, tax flag Y/N, instant-savings lines ending in "-", subtotal, tax, total, card). Gas receipts show pump, gallons, price per gallon, product and total.
- Snapshot the dialog by its ref, take an element screenshot of it, then click its Close button.
- Element refs change after every dialog close; use browser_find with the amount (e.g. "\$144\.39") to get the fresh View Receipt ref.
- browser_pdf_save was tried once; its contents could not be verified, so transcripts (JSON + PNG) were used for all receipts. In-warehouse items add up to subtotal, plus tax equals total.

## What happened
- First run: redirected to a blank FIDO Consent page on signin.costco.com. Second run: signed in beforehand and the header link worked; 17 receipts saved, no online orders in range.

## Avoid
- Don't navigate directly to /myaccount by URL; use the header link.
- Page snapshots of the home page are huge; use browser_find with a regex instead.
- Don't use lowercase option names in the date dropdown.

## Login hosts
- www.costco.com
- signin.costco.com
