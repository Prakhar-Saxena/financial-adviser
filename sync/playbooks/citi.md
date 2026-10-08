# Citi playbook

## Login hosts
- www.citi.com (start page, sign-on form)
- online.citi.com (dashboard, statements after sign-on)

## Transactions (CSV)
- Card dashboard (online.citi.com/US/ag/dashboard/credit-card) > "Transactions" section > Download icon (after "Hide Running Balance" and the "|" separator).
- Dialog offers CSV, TXT (tab delimited), OFX. CSV is preselected; the period defaults to "Since <last statement close date>".
- Click "Export"; the file downloads into the run folder. Then click "OK" on the "Done!" dialog, otherwise it blocks later clicks.
- Download name looks like "Since Sep 26, 2026.CSV" (saved with hyphens).

## Statements (PDF)
- Dashboard card tile > "View Statements" (opens a loading overlay, wait a few seconds) > "View All Statements".
- Statements page (online.citi.com/US/nga/accstatement) lists the last 24 months with View and Download buttons per month; Download saves the PDF directly (e.g. "September 25.pdf").
- Year dropdown selects other years. "Request Older Statements" exists for older ones (not used).
- The list shows only the closing date, not the period start.

## Avoid
- Make Payment, Enroll in Autopay, offers/Activate Now banners, paperless settings, Request Older Statements.
- Element refs change after every dialog or re-render; re-find before clicking.
