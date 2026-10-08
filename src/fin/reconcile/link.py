"""Link statement lines to transactions (decided 2026-10-06; see docs/decisions.md).

Chase's activity export only covers "since last statement", but every statement lists all
activity in its period. So:
- each statement line is linked to the CSV transaction it describes (same card and amount,
  transaction date within 3 days, matching description);
- a line with no CSV row becomes a transaction itself (source = "statement");
- if a CSV row for such a line arrives later, the line moves to the CSV row and the
  statement-sourced copy is deleted, so nothing is ever counted twice.
Linking also copies the line's Amazon order number to the transaction and applies the
statement's cardholder section, when it names one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from fin.config import CardsConfig
from fin.db import models as m
from fin.normalize.merchants import clean_description, normalize_raw
from fin.normalize.people import PeopleIndex
from fin.normalize.signs import classify
from fin.review import ReviewSet

DATE_SLACK_DAYS = 3
SECTION_TYPES = {"FEES CHARGED": "fee", "INTEREST CHARGED": "interest"}
CREDIT_WORD = re.compile(r"\bCREDIT\b")


@dataclass
class LinkResult:
    linked: int = 0
    created: int = 0
    replaced: int = 0
    attributed: int = 0


def _similar(csv_desc: str, line_desc: str) -> bool:
    a, b = normalize_raw(csv_desc), normalize_raw(line_desc)
    if b.startswith(a[:12]) or a.startswith(b[:12]):
        return True
    return clean_description(csv_desc) == clean_description(line_desc)


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _statement_key(card_id: str, period_end: str, line_no: int) -> str:
    return hashlib.sha256(f"stmt|{card_id}|{period_end}|{line_no}".encode()).hexdigest()


def _delete_txn(s: Session, txn_id: int) -> None:
    s.execute(update(m.StatementLine).where(m.StatementLine.transaction_id == txn_id)
              .values(transaction_id=None))
    s.execute(delete(m.Allocation).where(m.Allocation.transaction_id == txn_id))
    s.execute(delete(m.Match).where(m.Match.transaction_id == txn_id))
    s.execute(delete(m.ReviewItem).where(m.ReviewItem.ref_table == "transactions",
                                         m.ReviewItem.ref_id == str(txn_id)))
    s.execute(delete(m.Transaction).where(m.Transaction.id == txn_id))


def link_statements(s: Session, cards: CardsConfig, reviews: ReviewSet) -> LinkResult:
    out = LinkResult()
    people = PeopleIndex(cards)
    linked_ids = set(s.scalars(select(m.StatementLine.transaction_id).where(
        m.StatementLine.transaction_id.is_not(None))))

    for st in s.scalars(select(m.Statement).order_by(m.Statement.period_end)).all():
        lines = s.scalars(select(m.StatementLine).where(m.StatementLine.statement_id == st.id)
                          .order_by(m.StatementLine.line_no)).all()
        csv_txns = s.scalars(select(m.Transaction).where(
            m.Transaction.card_id == st.card_id, m.Transaction.source == "csv")).all()
        for line in lines:
            current = s.get(m.Transaction, line.transaction_id) if line.transaction_id else None
            if current is not None and current.source == "csv":
                _apply_line(current, line, st, people, reviews, out)
                continue
            candidates = [
                t for t in csv_txns
                if t.id not in linked_ids and t.amount_cents == line.amount_cents
                and _days(t.txn_date or t.post_date, line.txn_date) <= DATE_SLACK_DAYS
                and _similar(t.description_raw, line.description)
            ]
            candidates.sort(key=lambda t: _days(t.txn_date or t.post_date, line.txn_date))
            if candidates:
                txn = candidates[0]
                if current is not None:  # a statement-sourced copy existed: CSV row wins
                    _delete_txn(s, current.id)
                    linked_ids.discard(current.id)
                    out.replaced += 1
                line.transaction_id = txn.id
                linked_ids.add(txn.id)
                out.linked += 1
                _apply_line(txn, line, st, people, reviews, out)
                continue
            if current is None:
                current = _txn_from_line(s, st, line)
                line.transaction_id = current.id
                linked_ids.add(current.id)
                out.created += 1
            _apply_line(current, line, st, people, reviews, out)
    s.flush()
    return out


def _txn_from_line(s: Session, st: m.Statement, line: m.StatementLine) -> m.Transaction:
    typ = SECTION_TYPES.get(line.section)
    review = False
    if typ is None and line.section == "CREDITS":
        # Amex: statement credits ("Platinum Resy Credit") vs merchant returns
        typ = "credit" if CREDIT_WORD.search(line.description.upper()) else "refund"
    if typ is None:
        typ, review = classify(line.amount_cents, line.description, None)
    t = m.Transaction(
        card_id=st.card_id, person_id="self", person_source="default_self",
        txn_date=line.txn_date, post_date=line.txn_date, amount_cents=line.amount_cents,
        description_raw=line.description, description_clean=clean_description(line.description),
        type=typ, source="statement", dedupe_key=_statement_key(st.card_id, st.period_end,
                                                                line.line_no),
        import_id=st.import_id, statement_id=st.id,
        review_status="needs_review" if review else "ok",
    )
    s.add(t)
    s.flush()
    return t


def _apply_line(txn: m.Transaction, line: m.StatementLine, st: m.Statement,
                people: PeopleIndex, reviews: ReviewSet, out: LinkResult) -> None:
    txn.statement_id = st.id
    if line.order_ref and not txn.order_ref:
        txn.order_ref = line.order_ref
    if line.cardholder_name and txn.person_source in (None, "default_self", "statement"):
        a = people.attribute(line.cardholder_name)
        if a.person_id:
            if txn.person_id != a.person_id or txn.person_source != "statement":
                out.attributed += 1
            txn.person_id, txn.person_source = a.person_id, "statement"
        else:
            txn.person_id, txn.person_source = None, "statement"
            reviews.flag("unknown_person", "transactions", txn.id,
                         reason="cardholder name on the statement matches nobody in cards.json")
