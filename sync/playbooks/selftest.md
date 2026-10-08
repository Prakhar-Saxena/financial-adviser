# Playbook: selftest (example.com)

- Start page: https://example.com/ (no login, no accounts).
- Allowed domain: example.com only. Navigation to www.iana.org (the "Learn more" link target) is blocked by the guard; don't retry.
- `browser_snapshot` works; the page title is "Example Domain". The snapshot lists paragraphs and a "Learn more" link, not an h1.
- `browser_pdf_save` with an absolute filename in the run folder works.
- Run folder may already hold auto-saved snapshot .yml files; they are not part of the manifest.
- No shell tool is available; only browser tools, Write and EndConversation.
