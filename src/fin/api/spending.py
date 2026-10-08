"""Spending math for the dashboard (SPEC §13). Cents everywhere; dollars only in the UI.

A transaction's spending lines are its allocations when it has any (item-level splits),
otherwise one line for its own category. Refunds net against their category. Payments,
credits, rewards and adjustments are not spending; credits and rewards are reported
separately as "Card credits & rewards".
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fin.db import models as m

SPENDING_TYPES = ("purchase", "refund", "fee", "interest")
CREDIT_TYPES = ("credit", "reward")


def txn_month():
    return func.substr(func.coalesce(m.Transaction.txn_date, m.Transaction.post_date), 1, 7)


@dataclass
class Line:
    txn_id: int
    month: str
    card_id: str
    person_id: str | None
    merchant: str | None
    merchant_group: str | None
    category_id: str
    amount_cents: int


def spending_lines(s: Session, months: list[str] | None = None) -> list[Line]:
    q = (select(m.Transaction, m.Merchant.name, m.Merchant.merchant_group, txn_month())
         .outerjoin(m.Merchant, m.Merchant.id == m.Transaction.merchant_id)
         .where(m.Transaction.type.in_(SPENDING_TYPES)))
    if months:
        q = q.where(txn_month().in_(months))
    rows = s.execute(q).all()
    ids = [t.id for t, *_ in rows]
    allocs: dict[int, list[tuple[str, int]]] = defaultdict(list)
    if ids:
        for a in s.scalars(select(m.Allocation).where(m.Allocation.transaction_id.in_(ids))):
            allocs[a.transaction_id].append((a.category_id, a.amount_cents))
    out = []
    for t, name, group, month in rows:
        parts = allocs.get(t.id) or [(t.category_id or "uncategorized", t.amount_cents)]
        for cat, cents in parts:
            out.append(Line(t.id, month, t.card_id, t.person_id, name or t.description_clean,
                            group, cat, cents))
    return out


def parent(category_id: str) -> str:
    return category_id.split(".")[0]


def summarize(s: Session, month: str, trend_months: list[str]) -> dict:
    lines = spending_lines(s, [month])
    total = sum(x.amount_cents for x in lines)

    def by(key) -> list[dict]:
        acc: dict[str, int] = defaultdict(int)
        for x in lines:
            acc[key(x) or "unknown"] += x.amount_cents
        return [{"key": k, "cents": v} for k, v in sorted(acc.items(), key=lambda kv: -kv[1])]

    credits = s.scalar(select(func.coalesce(func.sum(m.Transaction.amount_cents), 0)).where(
        m.Transaction.type.in_(CREDIT_TYPES), txn_month() == month)) or 0
    trend_lines = spending_lines(s, trend_months)
    trend = []
    for mo in trend_months:
        mo_lines = [x for x in trend_lines if x.month == mo]
        trend.append({
            "month": mo,
            "cents": sum(x.amount_cents for x in mo_lines),
            "amazon_cents": sum(x.amount_cents for x in mo_lines
                                if x.merchant_group in ("amazon", "whole_foods")),
            "costco_cents": sum(x.amount_cents for x in mo_lines
                                if (x.merchant_group or "").startswith("costco")),
        })
    return {
        "month": month,
        "total_cents": total,
        "by_category": by(lambda x: parent(x.category_id)),
        "by_subcategory": by(lambda x: x.category_id),
        "by_person": by(lambda x: x.person_id),
        "by_card": by(lambda x: x.card_id),
        "top_merchants": by(lambda x: x.merchant)[:10],
        "credits_rewards_cents": credits,
        "trend": trend,
    }


def months_with_data(s: Session) -> list[str]:
    return sorted({mo for (mo,) in s.execute(select(txn_month()).distinct()) if mo})
