# Test fixtures

Everything here is fictional. The repo is public.

Each file keeps the layout of a real export, statement or page (columns, sections, wording, page
breaks), so the parsers are tested against what the sites really produce. Every value is made
up: amounts, merchants, store numbers, places, order and reference numbers, item titles, names,
card endings, balances, credit limits, rates and points. Totals still add up, so the statement
math, matching and reconciliation tests still mean something.

## Making one

1. See the real file's layout: `uv run python scripts/redact.py FILE` writes a redacted copy
   into the data dir (`debug/redacted/`), never into the repo.
2. Write the fixture from that layout with invented values. Don't turn the redacted copy into
   the fixture: redaction leaves amounts, dates, merchants, item titles, store numbers and
   locations.
3. Make the numbers agree (lines sum to the totals, charges match their orders), then
   regenerate the files in `tests/golden/` and run `uv run pytest`.
4. Commit. The git hooks check the new lines against your ledger and `config/cards.json`
   first, and pre-push checks again before anything leaves your Mac.

A binary fixture (a PDF) needs a text source next to it, such as `name.src.html`, because the
hooks can't read binary files.

## Conventions

- People: Alex Example (self), Sam Q Example (spouse), Pat Example, Robin Example, as in
  `config/cards.example.json`.
- Card endings `0000`. Phone numbers end in `0000` or use 555. Places: Springfield, Main St.
- Amazon order numbers keep the `114-` shape with made-up digits; seller and product names are
  invented.
