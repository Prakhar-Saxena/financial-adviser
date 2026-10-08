# Site: selftest (example.com)

This is a Phase 0 check of the browsing setup. There are no accounts and no login.
Follow the base rules anyway: open the start page, then wait for the user to type
"logged in" (here it just confirms they can see the Chrome window).

Then do these steps in order, and keep going even if one fails:

1. Take a `browser_snapshot` of https://example.com/ and confirm the heading text.
2. Save the page with `browser_pdf_save`, filename `<run folder>/selftest_example.pdf`
   (absolute path).
3. Try `browser_navigate` to https://www.iana.org/help/example-domains. The guard is
   expected to block this. Don't retry and don't work around it. Note the result.
4. In `run_report.json`, set `tools_available` to the exact names of every tool you have,
   and add a note saying whether a Bash or shell tool is available to you.
5. Write `manifest.json` with one entry for `selftest_example.pdf`: kind `selftest`,
   card_id null, period_start and period_end null.
6. Write `run_report.json` (collected: one `selftest` file, card_id null) and
   `playbook_proposed.md` (a few lines about example.com).
