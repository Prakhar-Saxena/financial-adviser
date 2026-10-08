"""Review items that `fin process` owns (SPEC §8 review kinds).

Each run declares the items it still finds. New ones are created, existing ones are left as
they are (whatever their status), and open ones no longer found are resolved automatically.
parse_failure and sync_gap items come from imports and syncs, not from here.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.db import models as m
from fin.db.models import utcnow

PROCESS_KINDS = (
    "unmatched_charge", "unmatched_order_charge", "unexpected_merchant", "statement_mismatch",
    "low_confidence", "uncategorized", "unknown_person",
)


class ReviewSet:
    def __init__(self, s: Session):
        self.s = s
        self.seen: set[tuple[str, str, str]] = set()

    def flag(self, kind: str, ref_table: str, ref_id: Any, **details: Any) -> None:
        assert kind in PROCESS_KINDS, kind
        key = (kind, ref_table, str(ref_id))
        if key in self.seen:
            return
        self.seen.add(key)
        existing = self.s.scalar(select(m.ReviewItem).where(
            m.ReviewItem.kind == kind, m.ReviewItem.ref_table == ref_table,
            m.ReviewItem.ref_id == str(ref_id)))
        if existing is None:
            self.s.add(m.ReviewItem(kind=kind, ref_table=ref_table, ref_id=str(ref_id),
                                    details_json=json.dumps(details)))
        elif existing.status == "open":
            existing.details_json = json.dumps(details)

    def close_stale(self, kinds: tuple[str, ...] = PROCESS_KINDS) -> int:
        n = 0
        for item in self.s.scalars(select(m.ReviewItem).where(
                m.ReviewItem.status == "open", m.ReviewItem.kind.in_(kinds))):
            if (item.kind, item.ref_table, item.ref_id) not in self.seen:
                item.status, item.resolved_at = "resolved", utcnow()
                n += 1
        return n
