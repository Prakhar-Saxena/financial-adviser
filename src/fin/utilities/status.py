# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per utility account: its linked payments so far and open review items
(docs/SPEC-utilities.md §11). Reads the database only."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fin.db import models as m


@dataclass
class AccountStatus:
    id: str
    service: str
    provider: str
    in_config: bool
    active_from: str | None
    active_to: str | None
    payments: int = 0
    last_payment: str | None = None  # date of the latest linked payment
    last_amount_cents: int | None = None
    open_items: int = 0  # open utility_missing items


def account_status(s: Session) -> list[AccountStatus]:
    """Accounts in config first, then earlier ones, each in id order."""
    rows = [AccountStatus(a.id, a.service, a.provider, a.in_config, a.active_from, a.active_to)
            for a in s.scalars(select(m.UtilityAccount).order_by(
                m.UtilityAccount.in_config.desc(), m.UtilityAccount.id))]
    by_id = {r.id: r for r in rows}
    day = func.coalesce(m.Transaction.txn_date, m.Transaction.post_date)
    for acct, day_, cents in s.execute(
            select(m.UtilityPayment.account_id, day, m.Transaction.amount_cents)
            .join(m.Transaction, m.Transaction.id == m.UtilityPayment.transaction_id)
            .order_by(day, m.Transaction.id)):
        r = by_id[acct]
        r.payments += 1
        r.last_payment, r.last_amount_cents = day_, cents
    for ref_id, n in s.execute(
            select(m.ReviewItem.ref_id, func.count())
            .where(m.ReviewItem.kind == "utility_missing", m.ReviewItem.status == "open",
                   m.ReviewItem.ref_table == "utility_accounts")
            .group_by(m.ReviewItem.ref_id)):
        if ref_id in by_id:
            by_id[ref_id].open_items = n
    return rows


def unassigned_count(s: Session) -> int:
    """Open `utility_unassigned` items: payments in a bills category that match no account."""
    return s.scalar(select(func.count()).select_from(m.ReviewItem).where(
        m.ReviewItem.kind == "utility_unassigned", m.ReviewItem.status == "open")) or 0
