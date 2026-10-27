# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Card review items (docs/SPEC-cards.md §6): one open item per card and problem key.

Refreshes create them, never `fin process`. `ref_table` is "cards" and `ref_id` the card id;
`details.key` tells problems of the same kind apart (a task, a document id, a domain).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.db import models as m
from fin.db.models import utcnow


def _open(s: Session, kind: str, card_id: str) -> list[m.ReviewItem]:
    return list(s.scalars(select(m.ReviewItem).where(
        m.ReviewItem.kind == kind, m.ReviewItem.ref_table == "cards",
        m.ReviewItem.ref_id == card_id, m.ReviewItem.status == "open")))


def open_item(s: Session, kind: str, card_id: str, key: str, details: dict[str, Any]) -> None:
    """Open (or refresh the details of) the item for this card and key."""
    details = {**details, "card_id": card_id, "key": key}
    for item in _open(s, kind, card_id):
        if json.loads(item.details_json).get("key") == key:
            item.details_json = json.dumps(details)
            return
    s.add(m.ReviewItem(kind=kind, ref_table="cards", ref_id=card_id,
                       details_json=json.dumps(details)))


def close_items(s: Session, kind: str, card_id: str, key: str | None = None,
                keep: set[str] | None = None) -> int:
    """Resolve open items for this card: one key, or every key not in `keep`."""
    n = 0
    for item in _open(s, kind, card_id):
        k = json.loads(item.details_json).get("key")
        if (key is not None and k == key) or (key is None and k not in (keep or set())):
            item.status, item.resolved_at = "resolved", utcnow()
            n += 1
    return n
