"""Dedicated-card checks (SPEC §12.2, §12.3): charges outside a card's reserved merchants."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.config import CardsConfig
from fin.db import models as m
from fin.review import ReviewSet


def unexpected_merchants(s: Session, cards: CardsConfig, reviews: ReviewSet) -> int:
    n = 0
    for card in cards.cards:
        if not card.expected_merchant_groups:
            continue
        rows = s.execute(select(m.Transaction, m.Merchant.merchant_group).outerjoin(
            m.Merchant, m.Merchant.id == m.Transaction.merchant_id).where(
            m.Transaction.card_id == card.id, m.Transaction.type == "purchase")).all()
        for t, group in rows:
            if not card.expects_group(group):
                reviews.flag("unexpected_merchant", "transactions", t.id, card_id=card.id,
                             expected=card.expected_merchant_groups, info=True)
                n += 1
    return n
