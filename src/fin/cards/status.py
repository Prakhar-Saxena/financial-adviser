# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`fin cards status`: what the knowledge base knows per card (docs/SPEC-cards.md §14)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.config import CardsConfig
from fin.db import models as m


@dataclass
class CardStatus:
    card_id: str
    product: str
    active_version: int | None = None
    pending_version: int | None = None
    docs: Counter[str] = field(default_factory=Counter)
    last_checked: str | None = None
    last_changed: str | None = None
    stale: bool = True

    @property
    def docs_summary(self) -> str:
        if not self.docs:
            return "none"
        return ", ".join(f"{n} {s}" for s, n in sorted(self.docs.items()))


def card_status(s: Session, cards: CardsConfig, refresh_days: int,
                now: datetime | None = None) -> list[CardStatus]:
    """One row per card in cards.json order. A card is stale when its documents' last
    successful check is older than `refresh_days`, or it has never been checked."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=refresh_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for card in cards.cards:
        st = CardStatus(card.id, card.product)
        for p in s.scalars(select(m.CardProfileVersion).where(
                m.CardProfileVersion.card_id == card.id,
                m.CardProfileVersion.status.in_(("active", "pending")))):
            if p.status == "active":
                st.active_version = p.version
            else:
                st.pending_version = p.version
        docs = s.scalars(select(m.CardDocument).where(m.CardDocument.card_id == card.id)).all()
        st.docs = Counter(d.status for d in docs)
        checked = [d.last_checked_at for d in docs if d.status == "active" and d.last_checked_at]
        changed = [d.last_changed_at for d in docs if d.last_changed_at]
        st.last_checked = max(checked) if checked else None
        st.last_changed = max(changed) if changed else None
        st.stale = st.last_checked is None or st.last_checked < cutoff
        out.append(st)
    return out
