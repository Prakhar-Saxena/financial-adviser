# Amazon (amazon.com) playbook

## Login hosts
- www.amazon.com (user logs in; start at https://www.amazon.com/)

## Your Orders
- URL: https://www.amazon.com/your-orders/orders?timeFilter=year-2026 (also &page=N, zero-based, 10 orders per page). Year filter covers any range inside the year; months-3 works for short ranges.
- Pagination by &page=N; last page has fewer than 10 orders.
- Digital orders: https://www.amazon.com/your-orders/orders?orderFilter=digital&timeFilter=year-2026 (link "Digital Orders" in tab list). Digital order IDs look like D01-...; the page header says "N order placed in".

## Finding order IDs
- The page snapshot is too large to return inline. Use browser_find with regex `/Order placed [A-Z][a-z]+ \d+, \d{4} Total [^"]*Order # [\d-]+/`. Each match gives date, total and order number (format 114-1234567-1234567).
- Compare against orders_already_imported; only new ones need invoices.

## Invoice
- URL pattern: https://www.amazon.com/gp/css/summary/print.html?orderID=<order-id>
- Navigating directly works. Then browser_pdf_save to `<run folder>/amazon_<order-id>.pdf`.
- Invoice has a "Payment method" section with card brand and last 4 digits.
- Navigate + pdf_save for several orders can go in one batch; they run in order.

## Your Payments > Transactions
- URL: https://www.amazon.com/cpe/yourpayments/transactions
- Lists charges with date, card, order number. About 15 per page; click the "Next Page" button (page stays on same URL). Save each page with browser_pdf_save.
- To read dates, use browser_find regex on month names; stop when the oldest date is before orders_from.

## Avoid
- Don't use "Buy it again", review, return or other buttons on the orders list.
