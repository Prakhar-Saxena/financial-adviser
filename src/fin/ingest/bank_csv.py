"""Issuer activity CSV parsers (SPEC §9.2).

Chase (documented from the first redacted export, tests/fixtures/chase/activity_prime_visa.csv):

    Transaction Date,Post Date,Description,Category,Type,Amount,Memo
    10/05/2026,10/05/2026,AMAZON MKTPL*597SC48P1,Shopping,Sale,-53.49,

- Dates are MM/DD/YYYY. Amount is negative for purchases (Type "Sale"), so the sign is flipped
  to the §8 convention. Type values seen: Sale; documented by Chase: Return, Payment, Fee,
  Adjustment.
- There is no cardholder column, so rows default to self (person_source = "default_self")
  until a statement's per-cardholder section says otherwise.
- There is no posted-only option in the download dialog. Rows without a Post Date are
  treated as pending and dropped.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.db import models as m
from fin.ingest.inbox import ParseOutcome, register
from fin.normalize.merchants import clean_description, normalize_raw
from fin.normalize.signs import classify, to_cents

CHASE_HEADER = ["Transaction Date", "Post Date", "Description", "Category", "Type", "Amount",
                "Memo"]
COVERAGE_SLACK_DAYS = 10


@dataclass(frozen=True)
class ParsedTxn:
    txn_date: str | None
    post_date: str
    amount_cents: int
    description_raw: str
    description_clean: str
    type: str
    needs_review: bool
    issuer_category: str | None = None
    memo: str | None = None
    member_name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _iso(mdy: str) -> str:
    return datetime.strptime(mdy.strip(), "%m/%d/%Y").date().isoformat()


def is_chase_csv(text: str) -> bool:
    first = next(csv.reader(io.StringIO(text.lstrip("﻿"))), [])
    return [c.strip() for c in first] == CHASE_HEADER


def parse_chase_csv(text: str) -> tuple[list[ParsedTxn], int]:
    """Return (posted rows, number of pending rows dropped)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if [c.strip() for c in reader.fieldnames or []] != CHASE_HEADER:
        raise ValueError(f"not a Chase activity CSV; header was {reader.fieldnames}")
    rows, pending = [], 0
    for r in reader:
        if not (r.get("Post Date") or "").strip():
            pending += 1
            continue
        amount = -to_cents(r["Amount"])  # Chase: negative = purchase
        desc = r["Description"].strip()
        typ, review = classify(amount, desc, r.get("Type"))
        rows.append(ParsedTxn(
            txn_date=_iso(r["Transaction Date"]) if r["Transaction Date"].strip() else None,
            post_date=_iso(r["Post Date"]),
            amount_cents=amount,
            description_raw=desc,
            description_clean=clean_description(desc),
            type=typ,
            needs_review=review,
            issuer_category=(r.get("Category") or "").strip() or None,
            memo=(r.get("Memo") or "").strip() or None,
        ))
    return rows, pending


def dedupe_keys(card_id: str, rows: list[ParsedTxn]) -> list[str]:
    """sha256(card|post_date|amount|description|occurrence_index) (SPEC §9.2).

    Uses the normalized raw description, not description_clean, so improving the cleaning
    rules never changes existing keys. occurrence_index numbers identical rows within one
    file; inserting only missing keys keeps max(count_in_db, count_in_file).
    """
    seen: Counter[tuple] = Counter()
    keys = []
    for r in rows:
        ident = (r.post_date, r.amount_cents, normalize_raw(r.description_raw))
        idx = seen[ident]
        seen[ident] += 1
        raw = f"{card_id}|{ident[0]}|{ident[1]}|{ident[2]}|{idx}"
        keys.append(hashlib.sha256(raw.encode()).hexdigest())
    return keys


def coverage_problem(rows: list[ParsedTxn], period_start: str | None) -> str | None:
    if not rows or not period_start:
        return None
    first = min(r.txn_date or r.post_date for r in rows)
    gap = (date.fromisoformat(first) - date.fromisoformat(period_start)).days
    if gap > COVERAGE_SLACK_DAYS:
        return (f"CSV starts {first}, {gap} days after the requested start {period_start}. "
                "The download's date range may not have applied; the missing weeks need a "
                "new download.")
    return None


def insert_transactions(
    s: Session, card_id: str, rows: list[ParsedTxn], imp: m.Import, people=None
) -> tuple[int, int]:
    """Insert rows not already present. Returns (inserted, skipped as already imported).

    `people` (a PeopleIndex) attributes rows that carry a member name."""
    keys = dedupe_keys(card_id, rows)
    existing = set(s.scalars(select(m.Transaction.dedupe_key).where(
        m.Transaction.dedupe_key.in_(keys))))
    inserted = 0
    for r, key in zip(rows, keys, strict=True):
        if key in existing:
            continue
        person, source = "self", "default_self"
        if people is not None and r.member_name:
            a = people.attribute(r.member_name)
            person, source = a.person_id, "name"
        t = m.Transaction(
            card_id=card_id, person_id=person, person_source=source,
            txn_date=r.txn_date, post_date=r.post_date, amount_cents=r.amount_cents,
            description_raw=r.description_raw, description_clean=r.description_clean,
            type=r.type, dedupe_key=key, import_id=imp.id,
            review_status="needs_review" if r.needs_review else "ok",
        )
        s.add(t)  # needs_review rows get their review item from `fin process`
        inserted += 1
    return inserted, len(rows) - inserted


@register("chase", "transactions")
def import_chase_csv(s: Session, path: Path, entry: dict, imp: m.Import) -> ParseOutcome:
    rows, pending = parse_chase_csv(path.read_text())
    if entry["card_id"] is None:
        raise ValueError("transactions file without a card_id")
    inserted, skipped = insert_transactions(s, entry["card_id"], rows, imp)
    problems = []
    gap = coverage_problem(rows, entry["period_start"])
    if gap:
        problems.append(gap)
    dates = [r.txn_date or r.post_date for r in rows] + [r.post_date for r in rows]
    start = entry["period_start"]
    if gap or not start:
        start = min(dates) if dates else None
    return ParseOutcome(
        row_count=inserted, period_start=start,
        period_end=entry["period_end"] or (max(dates) if dates else None),
        problems=problems,
    )



# ------------------------------------------------------------------- Citi

CITI_HEADER = ["Status", "Date", "Description", "Debit", "Credit", "Member Name"]


def is_citi_csv(text: str) -> bool:
    first = next(csv.reader(io.StringIO(text.lstrip("\ufeff"))), [])
    return [c.strip() for c in first] == CITI_HEADER


def parse_citi_csv(text: str) -> tuple[list[ParsedTxn], int]:
    """Citi (documented from the first export, "Since Sep 26, 2026.CSV"):

        Status,Date,Description,Debit,Credit,Member Name
        Cleared,09/28/2026,"COSTCO GAS #0000 SPRINGFIELD  IL",39.66,,<CARDHOLDER NAME>

    - One date per row (the transaction date). Debit = money out, positive. Credit = money in
      (sign as exported is normalized: always money in).
    - Status "Pending" rows are dropped.
    - Member Name names the cardholder.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if [c.strip() for c in reader.fieldnames or []] != CITI_HEADER:
        raise ValueError(f"not a Citi activity CSV; header was {reader.fieldnames}")
    rows, pending = [], 0
    for r in reader:
        if (r.get("Status") or "").strip().lower() == "pending":
            pending += 1
            continue
        debit, credit = (r.get("Debit") or "").strip(), (r.get("Credit") or "").strip()
        if debit:
            amount = abs(to_cents(debit))
        elif credit:
            amount = -abs(to_cents(credit))
        else:
            continue
        desc = r["Description"].strip()
        typ, review = classify(amount, desc, None)
        d = _iso(r["Date"])
        rows.append(ParsedTxn(
            txn_date=d, post_date=d, amount_cents=amount, description_raw=desc,
            description_clean=clean_description(desc), type=typ, needs_review=review,
            member_name=(r.get("Member Name") or "").strip() or None,
        ))
    return rows, pending


@register("citi", "transactions")
def import_citi_csv(s: Session, path: Path, entry: dict, imp: m.Import) -> ParseOutcome:
    from fin.config import load_cards
    from fin.normalize.people import PeopleIndex

    rows, _ = parse_citi_csv(path.read_text())
    if entry["card_id"] is None:
        raise ValueError("transactions file without a card_id")
    inserted, _ = insert_transactions(s, entry["card_id"], rows, imp, PeopleIndex(load_cards()))
    dates = [r.post_date for r in rows]
    return ParseOutcome(row_count=inserted,
                        period_start=entry["period_start"] or (min(dates) if dates else None),
                        period_end=entry["period_end"] or (max(dates) if dates else None))



# ------------------------------------------------------------------- Amex

AMEX_REQUIRED = {"Date", "Description", "Amount"}


def _amex_columns(text: str) -> list[str]:
    return [c.strip() for c in next(csv.reader(io.StringIO(text.lstrip("\ufeff"))), [])]


def is_amex_csv(text: str) -> bool:
    cols = _amex_columns(text)
    return set(cols) >= AMEX_REQUIRED and "Post Date" not in cols and "Status" not in cols


def parse_amex_csv(text: str) -> tuple[list[ParsedTxn], int]:
    """American Express (documented from the first export, "activity.csv"):

        Date,Description,Amount                                  (Platinum)
        Date,Description,Card Member,Account #,Amount            (Gold)

    - MM/DD/YYYY. Amount is positive for charges and negative for payments and credits, which
      is already the SPEC §8 sign. "Card Member", when present, names the cardholder.
    - Other extra columns (Account #, additional details) are ignored.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not is_amex_csv(text):
        raise ValueError(f"not an Amex activity CSV; header was {reader.fieldnames}")
    rows = []
    for r in reader:
        if not (r.get("Amount") or "").strip():
            continue
        amount = to_cents(r["Amount"])
        desc = " ".join(r["Description"].split())
        typ, review = classify(amount, desc, None)
        d = _iso(r["Date"])
        rows.append(ParsedTxn(txn_date=d, post_date=d, amount_cents=amount, description_raw=desc,
                              description_clean=clean_description(desc), type=typ,
                              needs_review=review,
                              member_name=(r.get("Card Member") or "").strip() or None))
    return rows, 0


@register("amex", "transactions")
def import_amex_csv(s: Session, path: Path, entry: dict, imp: m.Import) -> ParseOutcome:
    from fin.config import load_cards
    from fin.normalize.people import PeopleIndex

    rows, _ = parse_amex_csv(path.read_text())
    if entry["card_id"] is None:
        raise ValueError("transactions file without a card_id")
    inserted, _ = insert_transactions(s, entry["card_id"], rows, imp, PeopleIndex(load_cards()))
    dates = [r.post_date for r in rows]
    return ParseOutcome(row_count=inserted,
                        period_start=entry["period_start"] or (min(dates) if dates else None),
                        period_end=entry["period_end"] or (max(dates) if dates else None))
