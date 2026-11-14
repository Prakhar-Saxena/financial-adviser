# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sync reference tables (people, cards, card_holders, categories) from config files.

A bank account is a `cards` row too (docs/SPEC-checking.md §4), so every `card_id` column and
filter works for it unchanged; it has no card_holders.
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from fin.config import CardsConfig, Taxonomy
from fin.db import models as m


def sync_categories(session: Session, taxonomy: Taxonomy) -> None:
    """Add or rename categories from categories.json. `fin process` runs this too, because
    seed rules (reloaded on every run) may name a category added since `fin init`."""
    # Parents first, so the self-referencing foreign key is satisfied.
    for cat in sorted(taxonomy.categories, key=lambda c: c.parent_id is not None):
        session.merge(m.Category(id=cat.id, parent_id=cat.parent_id, name=cat.name))
    session.flush()


def sync_reference(session: Session, cards: CardsConfig, taxonomy: Taxonomy) -> dict[str, int]:
    for p in cards.people:
        session.merge(m.Person(id=p.id, display_name=p.display_name, full_name=p.full_name,
                               relationship=p.relationship))
    for c in cards.cards:
        session.merge(m.Card(id=c.id, issuer=c.issuer, product=c.product, network=c.network,
                             card_type=c.card_type))
    for a in cards.bank_accounts:
        session.merge(m.Card(id=a.id, issuer=a.site, product=a.product, network="none",
                             card_type=a.account_type))
    session.flush()
    session.execute(delete(m.CardHolder))
    for c in cards.cards:
        for h in c.cardholders:
            session.add(m.CardHolder(card_id=c.id, person_id=h.person,
                                     card_ending=h.card_ending or None, role=h.role))
    sync_categories(session, taxonomy)
    session.commit()
    return {
        "people": len(cards.people),
        "cards": len(cards.cards),
        "bank_accounts": len(cards.bank_accounts),
        "card_holders": sum(len(c.cardholders) for c in cards.cards),
        "categories": len(taxonomy.categories),
    }
