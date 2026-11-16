# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read utility accounts and linked payments from the database and hand them to the engine
(docs/SPEC-utilities.md §8). Insights are computed on demand; nothing is stored."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fin.config import CardsConfig, Settings
from fin.db import models as m
from fin.review import ReviewSet
from fin.utilities.engine import Account, Payment, Result, compute
from fin.utilities.payments import UTILITY_KINDS, LinkResult, link_payments


def _day(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def load(s: Session) -> tuple[list[Account], list[Payment], dict[str, date]]:
    accounts = [Account(a.id, a.service, a.provider, a.bill_every_months, a.paid_in_advance,
                        _day(a.active_from), _day(a.active_to), a.in_config)
                for a in s.scalars(select(m.UtilityAccount).order_by(m.UtilityAccount.id))]
    day = func.coalesce(m.Transaction.txn_date, m.Transaction.post_date)
    payments = [Payment(tid, acct, date.fromisoformat(d), cents, covers, card)
                for tid, acct, d, cents, covers, card in s.execute(
                    select(m.UtilityPayment.transaction_id, m.UtilityPayment.account_id, day,
                           m.Transaction.amount_cents, m.UtilityPayment.covers_months,
                           m.Transaction.card_id)
                    .join(m.Transaction, m.Transaction.id == m.UtilityPayment.transaction_id))]
    # Where each card's or account's history starts in the ledger, to tell a new account's
    # first payment from the first payment the ledger happens to hold.
    starts = {card: date.fromisoformat(d) for card, d in s.execute(
        select(m.Transaction.card_id, func.min(day)).group_by(m.Transaction.card_id))}
    return accounts, payments, starts


def utilities(s: Session, settings: Settings, today: date | None = None,
              until: str | None = None, months: int | None = None) -> Result:
    accounts, payments, starts = load(s)
    return compute(accounts, payments, settings.utilities, today or date.today(), until,
                   months, starts)


def flag_missing(s: Session, settings: Settings, reviews: ReviewSet, today: date) -> int:
    """A `utility_missing` item for every account whose next payment is late (§8.3 rule 4)."""
    n = 0
    for v in utilities(s, settings, today).accounts.values():
        if v.missing:
            reviews.flag("utility_missing", "utility_accounts", v.account.id,
                         last_payment=v.last.day.isoformat(), cadence_days=v.cadence_days,
                         expected=v.next_expected.isoformat())
            n += 1
    return n


def relink(s: Session, cards: CardsConfig, settings: Settings,
           today: date | None = None) -> tuple[LinkResult, int]:
    """Links and utility review items again, as `fin process` would, after a correction."""
    reviews = ReviewSet(s)
    res = link_payments(s, cards, reviews)
    late = flag_missing(s, settings, reviews, today or date.today())
    reviews.close_stale(UTILITY_KINDS)
    s.commit()
    return res, late
