# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Checking account importers (docs/SPEC-checking.md §8). Parsers register for each site in
`bank_accounts` when `load_parsers()` runs; no site id is hard-coded.

Activity export (documented from the first real export; fictional fixture in its layout:
tests/fixtures/bank/ExportedTransactions.csv):

- 14 columns, every field quoted, CRLF, newest row first. See `EXPORT_HEADER`.
- Posting and effective dates are M/D/YYYY without zero padding. Amount and Balance carry five
  decimals (always zeros past the cents); debits are negative, so the sign is flipped to the
  SPEC §8 convention (positive = money out).
- The type column is ACH, Deposit, Transfer, Debit Card, or empty for checks; the kind column
  ("Extended Description") says what the row is (External Withdrawal, Point Of Sale
  Withdrawal, Overdraft Protection Deposit, a check with its number...). Together they are
  the row's `bank_type`, which classification reads (§9 step 4).
- Balance is the running balance after the row. It is kept for the balance walk (§11) and
  stays out of the dedupe key, since a bank may re-sequence same-day rows between exports.
- The export offers no posted-only option; rows whose status isn't "Posted" are dropped.

Statement (fictional fixture: tests/fixtures/bank/statement_checking_2026-10-31.txt):

- One PDF per month for every account held at the bank: a summary of accounts, then one
  section per account headed "<product>: XXXXXX1234", with "(Continued)" when it runs onto
  the next page.
- Each section has "MM/DD/YY Beginning Balance $x" and "MM/DD/YY Ending Balance $x" (they
  give the period), activity lines, and an ACCOUNT SUMMARY: "Previous Balance as of MM/DD/YY
  is $x", "Total Deposits $x", "Total Withdrawals $x", "Total Dividends Earned $x", "Ending
  Balance as of MM/DD/YY is $x". Dividends are not in Total Deposits.
- A checking statement never creates transactions (§8.2): it only proves the ledger (§11).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.config import BankAccount, CardsConfig
from fin.db import models as m
from fin.ingest.bank_csv import ParsedTxn, coverage_problem, insert_transactions
from fin.ingest.inbox import ParseOutcome, register
from fin.normalize.bank import CleanTerms, clean_bank_description, export_type
from fin.normalize.merchants import clean_description
from fin.normalize.signs import to_cents

EXPORT_HEADER = [
    "Transaction ID", "Posting Date", "Effective Date", "Transaction Type", "Posting Status",
    "Amount", "Check Number", "Reference Number", "Description", "Transaction Category", "Type",
    "Balance", "Memo", "Extended Description",
]


def _columns(text: str) -> list[str]:
    return [c.strip() for c in next(csv.reader(io.StringIO(text.lstrip("﻿"))), [])]


def is_bank_export(text: str) -> bool:
    return _columns(text) == EXPORT_HEADER


def _iso(mdy: str) -> str:
    return datetime.strptime(mdy.strip(), "%m/%d/%Y").date().isoformat()


def parse_bank_export(text: str, terms: CleanTerms) -> tuple[list[ParsedTxn], int]:
    """Return (posted rows, number of other rows dropped). Types are provisional, by sign:
    `fin process` sets the real ones (§9), never `classify()` from normalize/signs.py."""
    if not is_bank_export(text):
        raise ValueError(f"not a checking activity export; columns were {_columns(text)}")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    rows, dropped = [], 0
    for r in reader:
        if (r.get("Posting Status") or "").strip().lower() != "posted":
            dropped += 1
            continue
        amount = -to_cents(r["Amount"])
        desc = (r.get("Description") or "").strip()
        effective = (r.get("Effective Date") or "").strip()
        rows.append(ParsedTxn(
            txn_date=_iso(effective) if effective else None,
            post_date=_iso(r["Posting Date"]),
            amount_cents=amount,
            description_raw=desc,
            description_clean=clean_description(clean_bank_description(desc, terms)),
            type="purchase" if amount > 0 else "deposit",
            needs_review=False,
            issuer_category=(r.get("Transaction Category") or "").strip() or None,
            bank_type=export_type(r.get("Type") or "", r.get("Extended Description") or ""),
            balance_cents=to_cents(r["Balance"]) if (r.get("Balance") or "").strip() else None,
        ))
    return rows, dropped


def import_bank_export(s: Session, path: Path, entry: dict, imp: m.Import) -> ParseOutcome:
    from fin.config import load_cards

    cards = load_cards()
    acct = _account(cards, entry["card_id"])
    rows, _ = parse_bank_export(path.read_text(), CleanTerms.from_cards(cards))
    inserted, _ = insert_transactions(s, acct.id, rows, imp)
    problems = []
    gap = coverage_problem(rows, entry["period_start"])
    if gap:
        problems.append(gap)
    dates = [r.post_date for r in rows]
    return ParseOutcome(
        row_count=inserted,
        period_start=min(dates) if gap or not entry["period_start"] or not dates
        else entry["period_start"],
        period_end=entry["period_end"] or (max(dates) if dates else None),
        problems=problems,
    )


def _account(cards: CardsConfig, card_id: str | None) -> BankAccount:
    if card_id is None:
        raise ValueError("checking file without a card_id")
    return cards.account(card_id)


# ------------------------------------------------------------------ statements (§8.2)

SECTION = re.compile(r"^(?P<name>.+?):\s*X+(?P<ending>\d{2,4})(?:\s*\(Continued\))?\s*$",
                     re.IGNORECASE)
MONEY = r"(-?\$-?[\d,]+\.\d{2})"
BEGINNING = re.compile(rf"^(\d{{2}}/\d{{2}}/\d{{2}})\s+Beginning Balance\s+{MONEY}$")
ENDING = re.compile(rf"^(\d{{2}}/\d{{2}}/\d{{2}})\s+Ending Balance\s+{MONEY}$")
SUMMARY = {
    "opening": re.compile(rf"^Previous Balance as of \S+ is {MONEY}$"),
    "deposits": re.compile(rf"^Total Deposits {MONEY}$"),
    "withdrawals": re.compile(rf"^Total Withdrawals {MONEY}$"),
    "dividends": re.compile(rf"^Total Dividends Earned {MONEY}$"),
    "closing": re.compile(rf"^Ending Balance as of \S+ is {MONEY}$"),
}


@dataclass(frozen=True)
class ParsedBankStatement:
    period_start: str
    period_end: str
    opening: int
    deposits: int  # all money in: deposits plus dividends
    withdrawals: int
    closing: int
    account_ending: str

    @property
    def math_delta(self) -> int:
        """opening + deposits − withdrawals − closing; 0 when the statement adds up (§11)."""
        return self.opening + self.deposits - self.withdrawals - self.closing


def _mdy2(s: str) -> str:
    return datetime.strptime(s, "%m/%d/%y").date().isoformat()


def parse_bank_statement(text: str, section_names: list[str],
                         ending: str = "") -> ParsedBankStatement:
    """The checking section of a statement covering every account held at the bank.
    `section_names` are the account's names as its section heading prints them (its
    `product`); `ending`, when set, must match the heading's digits too. Raises ValueError
    when no section matches or a figure is missing."""
    wanted = {" ".join(n.upper().split()) for n in section_names if n.strip()}
    lines, current, found_ending = [], None, None
    for raw in text.replace("\f", "\n").splitlines():
        line = " ".join(raw.split())
        h = SECTION.match(line)
        if h:
            name = " ".join(h.group("name").upper().split())
            current = name in wanted and (not ending or h.group("ending").endswith(ending))
            if current:
                found_ending = h.group("ending")
            continue
        if line.upper().startswith("END OF STATEMENT"):
            current = False
        if current:
            lines.append(line)
    if found_ending is None:
        raise ValueError("no section for this account in the statement")

    def first(pattern: re.Pattern) -> re.Match | None:
        return next((mt for ln in lines if (mt := pattern.match(ln))), None)

    begin, end = first(BEGINNING), first(ENDING)
    figures = {k: first(p) for k, p in SUMMARY.items()}
    missing = [k for k, v in {"beginning": begin, "ending": end, **figures}.items()
               if v is None and k != "dividends"]
    if missing:
        raise ValueError(f"statement section is missing {', '.join(missing)}")
    cents = {k: to_cents(v.group(1)) if v else 0 for k, v in figures.items()}
    return ParsedBankStatement(
        period_start=_mdy2(begin.group(1)), period_end=_mdy2(end.group(1)),
        opening=cents["opening"], deposits=cents["deposits"] + cents["dividends"],
        withdrawals=cents["withdrawals"], closing=cents["closing"],
        account_ending=found_ending,
    )


def import_bank_statement(s: Session, path: Path, entry: dict, imp: m.Import) -> ParseOutcome:
    import json

    from fin.config import load_cards
    from fin.ingest.statements import pdf_text

    acct = _account(load_cards(), entry["card_id"])
    text = path.read_text() if path.suffix == ".txt" else pdf_text(path)
    st = parse_bank_statement(text, [acct.product, *acct.statement_markers],
                              acct.account_ending)
    problems = []
    if entry["period_end"] and entry["period_end"] != st.period_end:
        problems.append(f"manifest period_end {entry['period_end']} but the statement closes "
                        f"{st.period_end}; using the statement")
    if s.scalar(select(m.BankStatement.id).where(
            m.BankStatement.card_id == acct.id, m.BankStatement.period_end == st.period_end)):
        return ParseOutcome(0, st.period_start, st.period_end, problems)
    row = m.BankStatement(
        card_id=acct.id, import_id=imp.id, period_start=st.period_start,
        period_end=st.period_end, opening_balance=st.opening, deposits=st.deposits,
        withdrawals=st.withdrawals, closing_balance=st.closing, pdf_path=imp.file_path,
        parse_method="regex", math_ok=st.math_delta == 0,
    )
    s.add(row)
    s.flush()
    if st.math_delta:
        s.add(m.ReviewItem(kind="parse_failure", ref_table="bank_statements", ref_id=str(row.id),
                           details_json=json.dumps({"reason": "statement math does not balance",
                                                    "delta_cents": st.math_delta})))
        problems.append(f"statement math off by {st.math_delta} cents")
    return ParseOutcome(1, st.period_start, st.period_end, problems)


# ------------------------------------------------------------------ registration


def register_bank_sites(cards: CardsConfig | None = None) -> list[str]:
    """Register both parsers for every site in `bank_accounts`. Returns the sites."""
    if cards is None:
        from fin.ingest.inbox import _cards_if_configured

        cards = _cards_if_configured()
    sites = cards.bank_sites if cards else []
    for site in sites:
        register(site, "transactions")(import_bank_export)
        register(site, "statement")(import_bank_statement)
    return sites

