"""Amazon matching and splits (SPEC §12.1, §12.2, §12.4).

Candidates, strongest first:
1. order number: the card statement prints "Order Number 114-..." under each Amazon charge,
   copied to transactions.order_ref by statement linking. Exact.
2. Amazon's own charge list (order_charges, from Your Payments → Transactions): exact amount,
   same card, charge date within the window. Exact.
3. fallback for activity not yet on a statement: the order's grand total, on the card the
   invoice names, within `fallback_days` of the order date. Ties go to the nearest date; still
   tied → review.
Every matched charge is split across the order's item categories (whole-order mix for multi-
charge orders, a documented MVP approximation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from fin.categorize.allocate import largest_remainder, split_by_category
from fin.config import CardsConfig, Settings
from fin.db import models as m
from fin.review import ReviewSet

FALLBACK_DAYS = (0, 10)


@dataclass
class MatchResult:
    by_method: dict[str, int] = field(default_factory=dict)
    unmatched: int = 0
    split: int = 0


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def _same_card(card_name: str | None, card_product: str) -> bool:
    if not card_name:
        return True  # unknown: don't exclude
    name, product = card_name.upper(), card_product.upper()
    return name in product or product in name


def _card_matches(order: m.Order, card_product: str) -> bool:
    return _same_card(order.payment_method, card_product)


def match_amazon(s: Session, cards: CardsConfig, settings: Settings, reviews: ReviewSet,
                 today: date) -> MatchResult:
    out = MatchResult()
    amazon_ids = select(m.Merchant.id).where(m.Merchant.merchant_group == "amazon")
    txns = s.scalars(select(m.Transaction).where(
        m.Transaction.merchant_id.in_(amazon_ids),
        m.Transaction.type.in_(("purchase", "refund")),
    ).order_by(m.Transaction.post_date)).all()
    orders = s.scalars(select(m.Order).where(m.Order.merchant == "amazon")).all()
    by_ref = {o.external_id: o for o in orders}
    # Link Amazon's own charge list to orders by order number (invoices may arrive later).
    charges = s.scalars(select(m.OrderCharge).where(m.OrderCharge.merchant == "amazon")).all()
    for c in charges:
        if c.order_ref in by_ref:
            c.order_id = by_ref[c.order_ref].id

    # Keep user matches; recompute the rest.
    s.execute(delete(m.Match).where(m.Match.method != "user", m.Match.transaction_id.in_(
        [t.id for t in txns])))
    s.flush()
    user = {mt.transaction_id: mt for mt in s.scalars(select(m.Match).where(
        m.Match.method == "user"))}
    matched_total: dict[int, int] = {}
    for mt in user.values():
        t = s.get(m.Transaction, mt.transaction_id)
        if mt.order_id and t:
            matched_total[mt.order_id] = matched_total.get(mt.order_id, 0) + t.amount_cents
    used_charges: set[int] = set()
    lo, hi = settings.match.amazon_window_days

    for t in txns:
        if t.id in user:
            continue
        product = cards.card(t.card_id).product
        order = method = charge = None
        if t.order_ref and t.order_ref in by_ref:
            order, method = by_ref[t.order_ref], "exact"
        if order is None:
            when = t.txn_date or t.post_date
            cands = [c for c in charges if c.id not in used_charges
                     and c.amount_cents == t.amount_cents
                     and lo <= _days(when, c.charge_date) <= hi
                     and _same_card(c.card_name, product)]
            if cands:
                cands.sort(key=lambda c: abs(_days(when, c.charge_date)))
                charge = cands[0]
                used_charges.add(charge.id)
                t.order_ref = t.order_ref or charge.order_ref
                order, method = by_ref.get(charge.order_ref), "exact"
        if order is None and t.type == "purchase":
            when = t.txn_date or t.post_date
            cands = [o for o in orders if o.total == t.amount_cents
                     and FALLBACK_DAYS[0] <= _days(when, o.order_date) <= FALLBACK_DAYS[1]
                     and _card_matches(o, product)
                     and matched_total.get(o.id, 0) + t.amount_cents <= o.total]
            if cands:
                cands.sort(key=lambda o: _days(when, o.order_date))
                best = [o for o in cands if _days(when, o.order_date)
                        == _days(when, cands[0].order_date)]
                if len(best) == 1:
                    order, method = best[0], "window"
                else:
                    reviews.flag("unmatched_charge", "transactions", t.id,
                                 reason="several Amazon orders fit equally well",
                                 candidates=[o.external_id for o in best[:5]])
                    out.unmatched += 1
                    continue
        if order is None and charge is not None:
            s.add(m.Match(transaction_id=t.id, order_charge_id=charge.id, method="exact",
                          confidence=1.0))
            out.by_method["charge_only"] = out.by_method.get("charge_only", 0) + 1
            reviews.flag("unmatched_charge", "transactions", t.id,
                         reason="Amazon lists this charge, but its order's invoice isn't "
                                "imported (no item detail)", order_ref=charge.order_ref)
            continue
        if order is None:
            reason = ("order not imported (digital order, or outside the synced range?)"
                      if t.order_ref else "no Amazon order matches this charge")
            reviews.flag("unmatched_charge", "transactions", t.id, reason=reason,
                         order_ref=t.order_ref)
            out.unmatched += 1
            continue
        s.add(m.Match(transaction_id=t.id, order_id=order.id,
                      order_charge_id=charge.id if charge else None, method=method,
                      confidence=1.0 if method == "exact" else 0.8))
        matched_total[order.id] = matched_total.get(order.id, 0) + t.amount_cents
        out.by_method[method] = out.by_method.get(method, 0) + 1
    s.flush()

    # Orders with no card transaction after the grace period (§12.1 step 4).
    grace = settings.match.unmatched_grace_days
    tracked = [c.product for c in cards.cards]
    for o in orders:
        if o.id in matched_total or o.total <= 0:
            continue
        if _days(today.isoformat(), o.order_date) < grace:
            continue
        if o.payment_method and not any(_card_matches(o, p) for p in tracked):
            continue  # paid with a card we don't track
        reviews.flag("unmatched_order_charge", "orders", o.id,
                     reason="no card transaction found for this order",
                     payment_method=o.payment_method)
    out.split = allocate_matched(s)
    return out


def allocate_matched(s: Session) -> int:
    """Rebuild order-item allocations for every matched transaction."""
    rows = s.execute(select(m.Match, m.Transaction).join(
        m.Transaction, m.Transaction.id == m.Match.transaction_id).where(
        m.Match.order_id.is_not(None))).all()
    s.execute(delete(m.Allocation).where(m.Allocation.source == "order_items"))
    n = 0
    for match, t in rows:
        items = s.scalars(select(m.OrderItem).where(m.OrderItem.order_id == match.order_id)
                          .order_by(m.OrderItem.line_no)).all()
        if not items or t.category_source == "user":
            continue
        weights = [(i.category_id or "uncategorized",
                    max(i.line_total_cents - i.discount_cents, 0)) for i in items]
        parts = _refund_parts(t, items, s.get(m.Order, match.order_id)) or split_by_category(
            t.amount_cents, weights)
        for cat, cents in parts.items():
            if cents:
                s.add(m.Allocation(transaction_id=t.id, category_id=cat, amount_cents=cents,
                                   source="order_items"))
        t.category_id = max(parts.items(), key=lambda kv: abs(kv[1]))[0]
        t.category_source = "split"
        n += 1
    s.flush()
    return n


def _refund_parts(t: m.Transaction, items: list[m.OrderItem], order: m.Order) -> dict | None:
    """A refund equal to one item's price plus its tax share goes to that item (§12.4)."""
    if t.amount_cents >= 0 or not order or not order.subtotal:
        return None
    for i in items:
        tax_share = largest_remainder(order.tax, [i.line_total_cents,
                                                  order.subtotal - i.line_total_cents])[0]
        if -t.amount_cents in (i.line_total_cents, i.line_total_cents + tax_share):
            return {i.category_id or "uncategorized": t.amount_cents}
    return None


def amazon_on_other_cards(s: Session, cards: CardsConfig) -> int:
    """Count Amazon purchases on cards other than the one reserved for Amazon (info only)."""
    reserved = {c.id for c in cards.cards if "amazon" in c.reconcile_with}
    amazon_ids = select(m.Merchant.id).where(m.Merchant.merchant_group == "amazon")
    return s.scalar(select(func.count(m.Transaction.id)).where(
        m.Transaction.merchant_id.in_(amazon_ids), m.Transaction.type == "purchase",
        m.Transaction.card_id.not_in(reserved))) or 0
