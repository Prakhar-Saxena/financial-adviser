"""Sync reference tables (people, cards, card_holders, categories) from config files."""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from fin.config import CardsConfig, Taxonomy
from fin.db import models as m


def sync_reference(session: Session, cards: CardsConfig, taxonomy: Taxonomy) -> dict[str, int]:
    for p in cards.people:
        session.merge(m.Person(id=p.id, display_name=p.display_name, full_name=p.full_name,
                               relationship=p.relationship))
    for c in cards.cards:
        session.merge(m.Card(id=c.id, issuer=c.issuer, product=c.product, network=c.network,
                             card_type=c.card_type))
    session.flush()
    session.execute(delete(m.CardHolder))
    for c in cards.cards:
        for h in c.cardholders:
            session.add(m.CardHolder(card_id=c.id, person_id=h.person,
                                     card_ending=h.card_ending or None, role=h.role))
    # Parents first, so the self-referencing foreign key is satisfied.
    for cat in sorted(taxonomy.categories, key=lambda c: c.parent_id is not None):
        session.merge(m.Category(id=cat.id, parent_id=cat.parent_id, name=cat.name))
    session.commit()
    return {
        "people": len(cards.people),
        "cards": len(cards.cards),
        "card_holders": sum(len(c.cardholders) for c in cards.cards),
        "categories": len(taxonomy.categories),
    }
