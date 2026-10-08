"""Costco receipts ↔ card transactions (SPEC §12.1, §12.3).

- Every warehouse receipt should match a card transaction in a Costco merchant group with the
  exact total, posted 0–2 days after the receipt date (settings.match.costco_warehouse_window).
  Ties go to the nearest date; still tied → review.
- A Costco warehouse charge with no receipt becomes `unmatched_charge`. Costco Gas charges are
  exempt: no receipt is expected (gas receipts that do exist still match and split).
- A receipt with no card transaction becomes `unmatched_order_charge` after the grace period.
  Receipts paid with a card we don't track get an informational flag instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fin.config import CardsConfig, Settings
from fin.db import models as m
from fin.review import ReviewSet

COSTCO_GROUPS = ("costco_warehouse", "costco_gas", "costco_online")


@dataclass
class CostcoResult:
    matched: int = 0
    unmatched_charges: int = 0
    unmatched_receipts: int = 0
    by_method: dict[str, int] = field(default_factory=dict)


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def match_costco(s: Session, cards: CardsConfig, settings: Settings, reviews: ReviewSet,
                 today: date) -> CostcoResult:
    out = CostcoResult()
    lo, hi = settings.match.costco_warehouse_window_days
    groups = {mid: g for mid, g in s.execute(select(m.Merchant.id, m.Merchant.merchant_group)
                                             .where(m.Merchant.merchant_group.in_(COSTCO_GROUPS)))}
    txns = s.scalars(select(m.Transaction).where(
        m.Transaction.merchant_id.in_(list(groups)),
        m.Transaction.type == "purchase")).all() if groups else []
    receipts = s.scalars(select(m.Order).where(m.Order.merchant == "costco",
                                               m.Order.channel == "warehouse")
                         .order_by(m.Order.order_date)).all()
    s.execute(delete(m.Match).where(m.Match.method != "user",
                                    m.Match.transaction_id.in_([t.id for t in txns])))
    s.flush()
    user_matched = set(s.scalars(select(m.Match.transaction_id).where(m.Match.method == "user")))
    taken: set[int] = set(user_matched)

    for r in receipts:
        cands = [t for t in txns if t.id not in taken and t.amount_cents == r.total
                 and lo <= _days(t.post_date, r.order_date) <= hi]
        if not cands:
            if _days(today.isoformat(), r.order_date) >= settings.match.unmatched_grace_days:
                tracked = r.payment_method is None or "visa" in (r.payment_method or "").lower()
                if tracked:
                    reviews.flag("unmatched_order_charge", "orders", r.id,
                                 reason="no card transaction with this receipt's total")
                    out.unmatched_receipts += 1
            continue
        cands.sort(key=lambda t: abs(_days(t.post_date, r.order_date)))
        best = [t for t in cands if abs(_days(t.post_date, r.order_date))
                == abs(_days(cands[0].post_date, r.order_date))]
        if len(best) > 1:
            for t in best:
                reviews.flag("unmatched_charge", "transactions", t.id,
                             reason="several Costco receipts fit equally well")
            continue
        t = best[0]
        taken.add(t.id)
        s.add(m.Match(transaction_id=t.id, order_id=r.id, method="exact", confidence=1.0))
        out.matched += 1
        out.by_method["exact"] = out.by_method.get("exact", 0) + 1

    for t in txns:
        if t.id in taken:
            continue
        if groups.get(t.merchant_id) == "costco_warehouse":
            reviews.flag("unmatched_charge", "transactions", t.id,
                         reason="no Costco receipt with this total (not synced yet?)")
            out.unmatched_charges += 1
    s.flush()
    return out
