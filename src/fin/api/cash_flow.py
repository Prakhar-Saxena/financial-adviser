# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Checking on the dashboard (docs/SPEC-checking.md §13): money in and out, the transfer a row
is part of, and the checking side of Reconciliation. Cents everywhere; GET never calls Claude.

Money in and out groups rows by posted date, as the bank's statement does, so a month's totals
reproduce by hand from that month's statement (its deposits and withdrawals).
"""

from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fin.api.spending import SPENDING_TYPES
from fin.config import CardsConfig
from fin.db import models as m

TRANSFER_REVIEWS = ("unmatched_transfer", "uncertain_transfer")


def flow_key(t: m.Transaction) -> str:
    """What a checking row is, in one word: its flow kind, or its type when it has none."""
    return t.flow_kind or t.type


def cash_flow(s: Session, account_ids: list[str], month: str) -> dict | None:
    if not account_ids:
        return None
    rows = s.scalars(select(m.Transaction).where(
        m.Transaction.card_id.in_(account_ids),
        func.substr(m.Transaction.post_date, 1, 7) == month)).all()
    sides: dict[str, dict[str, dict]] = {"in": {}, "out": {}}
    for t in rows:
        side = sides["in" if t.amount_cents < 0 else "out"]
        row = side.setdefault(flow_key(t), {"key": flow_key(t), "cents": 0, "count": 0,
                                            "spending": t.type in SPENDING_TYPES})
        row["cents"] += abs(t.amount_cents)
        row["count"] += 1
    linked = set(s.scalars(select(m.TransferLink.bank_txn_id)))
    payments = [t for t in rows if t.flow_kind == "card_payment"]
    other = [t for t in rows if t.flow_kind == "card_payment_other"]
    ids = [str(t.id) for t in rows]
    unmatched = s.scalar(select(func.count(m.ReviewItem.id)).where(
        m.ReviewItem.status == "open", m.ReviewItem.kind.in_(TRANSFER_REVIEWS),
        m.ReviewItem.ref_table == "transactions", m.ReviewItem.ref_id.in_(ids))) if ids else 0

    def ordered(side: dict[str, dict]) -> list[dict]:
        return sorted(side.values(), key=lambda r: -r["cents"])

    return {
        "month": month,
        "money_in_cents": sum(r["cents"] for r in sides["in"].values()),
        "money_out_cents": sum(r["cents"] for r in sides["out"].values()),
        "money_in": ordered(sides["in"]),
        "money_out": ordered(sides["out"]),
        "card_payments": {"rows": len(payments), "linked": sum(t.id in linked for t in payments),
                          "cents": sum(t.amount_cents for t in payments), "other": len(other)},
        "unmatched": unmatched or 0,
    }


def transfer_of(s: Session, t: m.Transaction, cards: CardsConfig) -> list[dict]:
    """The other side of a transfer: for a checking row, the card payments it paid; for a card
    payment, the checking row that paid it."""
    if t.card_id in {a.id for a in cards.bank_accounts}:
        pairs = s.execute(select(m.TransferLink, m.Transaction).join(
            m.Transaction, m.Transaction.id == m.TransferLink.card_txn_id).where(
            m.TransferLink.bank_txn_id == t.id).order_by(m.Transaction.post_date)).all()
    else:
        pairs = s.execute(select(m.TransferLink, m.Transaction).join(
            m.Transaction, m.Transaction.id == m.TransferLink.bank_txn_id).where(
            m.TransferLink.card_txn_id == t.id)).all()
    return [{"txn_id": o.id, "card_id": o.card_id, "name": cards.account_name(o.card_id),
             "date": o.post_date, "amount_cents": o.amount_cents, "method": lk.method,
             "days_apart": lk.days_apart, "confidence": lk.confidence} for lk, o in pairs]


def bank_reconciliation(s: Session, cards: CardsConfig) -> dict:
    accounts = [a.id for a in cards.bank_accounts]
    if not accounts:
        return {"accounts": [], "statements": [], "balance": None, "card_payments": []}
    statements = [{
        "id": st.id, "card_id": st.card_id, "period_start": st.period_start,
        "period_end": st.period_end, "opening_cents": st.opening_balance,
        "deposits_cents": st.deposits, "withdrawals_cents": st.withdrawals,
        "closing_cents": st.closing_balance, "math_ok": st.math_ok,
        "ledger_status": st.ledger_status, "ledger_delta_cents": st.ledger_delta_cents,
    } for st in s.scalars(select(m.BankStatement).where(m.BankStatement.card_id.in_(accounts))
                          .order_by(m.BankStatement.card_id, m.BankStatement.period_end))]

    days = s.scalar(select(func.count(func.distinct(m.Transaction.post_date))).where(
        m.Transaction.card_id.in_(accounts), m.Transaction.balance_after_cents.is_not(None)))
    gaps = []
    for r in s.scalars(select(m.ReviewItem).where(
            m.ReviewItem.status == "open", m.ReviewItem.kind == "bank_balance_gap")):
        gaps.append(json.loads(r.details_json or "{}").get("day"))

    # Card payments per month, from when checking starts: how many a checking row paid.
    start = s.scalar(select(func.min(m.Transaction.post_date)).where(
        m.Transaction.card_id.in_(accounts)))
    linked_cards = set(s.scalars(select(m.TransferLink.card_txn_id)))
    linked_bank = set(s.scalars(select(m.TransferLink.bank_txn_id)))
    months: dict[str, dict] = defaultdict(lambda: {"card_payments": 0, "matched": 0,
                                                   "checking_payments": 0,
                                                   "checking_linked": 0})
    if start:
        for p in s.scalars(select(m.Transaction).where(
                m.Transaction.card_id.in_([c.id for c in cards.cards]),
                m.Transaction.type == "payment", m.Transaction.post_date >= start)):
            row = months[p.post_date[:7]]
            row["card_payments"] += 1
            row["matched"] += p.id in linked_cards
        for t in s.scalars(select(m.Transaction).where(
                m.Transaction.card_id.in_(accounts), m.Transaction.flow_kind == "card_payment")):
            row = months[t.post_date[:7]]
            row["checking_payments"] += 1
            row["checking_linked"] += t.id in linked_bank
    return {
        "accounts": accounts,
        "checking_starts": start,
        "statements": statements,
        "balance": {"days": days or 0, "gaps": sorted(d for d in gaps if d)},
        "card_payments": [{"month": k, **v} for k, v in sorted(months.items())],
    }
