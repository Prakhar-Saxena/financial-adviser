# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""No double counting (docs/SPEC-checking.md §9, §10).

`classify_checking` gives every checking row its type and flow kind; `link_transfers` pairs
checking rows with the card payments they paid. Both recompute from stored data on every
`fin process`, like `match_amazon`, and neither changes a card row.

A card payment is a card row of type `payment`: money in on the card (negative), or a payment
taken back (positive). `days` is the checking date minus the card date.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fin.categorize.rules import compiled_rules, first_rule
from fin.config import CardsConfig, Settings
from fin.db import models as m
from fin.normalize.bank import BankClass, BankRule, classify_bank_row
from fin.review import ReviewSet

HAND_TYPED = ("override", "user_rule")
# Flows amount-only matching never touches (§10.3): a person or cash isn't a card payment.
NOT_AMOUNT_ONLY = ("p2p", "atm")


@dataclass
class TransferResult:
    classified: int = 0
    by_flow: Counter = field(default_factory=Counter)
    linked: Counter = field(default_factory=Counter)  # method -> checking rows linked
    unmatched: int = 0  # unmatched_transfer items raised
    uncertain: int = 0  # uncertain_transfer items raised


# ------------------------------------------------------------------ §9


def checking_ids(cards: CardsConfig) -> list[str]:
    return [a.id for a in cards.bank_accounts]


def bank_overrides(s: Session) -> dict[str, tuple[str, str | None]]:
    """My per-row choices, keyed by dedupe_key: field `type`, with `flow_kind` beside it."""
    rows = s.scalars(select(m.UserOverride).where(
        m.UserOverride.target_type == "transaction",
        m.UserOverride.field.in_(("type", "flow_kind")))).all()
    types = {o.stable_key: o.value for o in rows if o.field == "type"}
    flows = {o.stable_key: o.value or None for o in rows if o.field == "flow_kind"}
    return {k: (v, flows.get(k)) for k, v in types.items()}


def type_rules(s: Session) -> list[tuple[re.Pattern, str, str | None]]:
    """My rules that say what a checking row is (§9 step 2)."""
    rows = s.scalars(select(m.Rule).where(m.Rule.created_by == "user",
                                          m.Rule.txn_type.is_not(None))
                     .order_by(m.Rule.priority, m.Rule.id)).all()
    return [(re.compile(r.pattern, re.IGNORECASE), r.txn_type, r.flow_kind) for r in rows]


def classify_checking(s: Session, cards: CardsConfig,
                      rules: list[BankRule]) -> dict[int, BankClass]:
    """Set type and flow_kind on every checking row. Returns each row's class by id."""
    overrides = bank_overrides(s)
    mine = type_rules(s)
    merchants = [r for r in compiled_rules(s) if r.category_id]
    classes: dict[int, BankClass] = {}
    for t in s.scalars(select(m.Transaction).where(
            m.Transaction.card_id.in_(checking_ids(cards)))):
        rule = next(((ty, fl) for rx, ty, fl in mine if rx.match(t.description_clean)), None)
        c = classify_bank_row(
            t.amount_cents, t.description_clean, t.bank_type, rules,
            override=overrides.get(t.dedupe_key), user_rule=rule,
            known_merchant=first_rule(merchants, t.description_clean) is not None,
        )
        t.type, t.flow_kind = c.type, c.flow_kind
        classes[t.id] = c
    return classes


# ------------------------------------------------------------------ §10


def _days(bank: m.Transaction, card: m.Transaction) -> int:
    return (date.fromisoformat(bank.post_date) - date.fromisoformat(card.post_date)).days


def link_transfers(
    s: Session, cards: CardsConfig, settings: Settings, reviews: ReviewSet,
    classes: dict[int, BankClass], today: date,
) -> TransferResult:
    res = TransferResult(classified=len(classes))
    s.execute(delete(m.TransferLink))
    lo, hi = settings.match.transfer_window_days
    issuer_of = {c.id: c.issuer for c in cards.cards}
    payments = s.scalars(select(m.Transaction).where(
        m.Transaction.card_id.in_(list(issuer_of)), m.Transaction.type == "payment")
        .order_by(m.Transaction.post_date, m.Transaction.id)).all()
    free = {p.id: p for p in payments}
    bank = s.scalars(select(m.Transaction).where(m.Transaction.id.in_(list(classes)))
                     .order_by(m.Transaction.post_date, m.Transaction.id)).all()
    linked: set[int] = set()

    def candidates(t: m.Transaction, issuer: str | None) -> list[m.Transaction]:
        out = [p for p in free.values() if lo <= _days(t, p) <= hi
               and (issuer is None or issuer_of[p.card_id] == issuer)]
        return sorted(out, key=lambda p: (abs(_days(t, p)), p.id))

    def link(t: m.Transaction, ps: list[m.Transaction], method: str, confidence: float) -> None:
        for p in ps:
            s.add(m.TransferLink(bank_txn_id=t.id, card_txn_id=p.id, method=method,
                                 days_apart=_days(t, p), confidence=confidence))
            del free[p.id]
        linked.add(t.id)
        res.linked[method] += 1

    named = [t for t in bank if t.flow_kind == "card_payment"]
    # §10.1 and §10.5: a row naming an issuer, the exact opposite amount (either direction).
    for t in named:
        hit = next((p for p in candidates(t, classes[t.id].issuer)
                    if p.amount_cents == -t.amount_cents), None)
        if hit is not None:
            link(t, [hit], "named", 1.0)
    # §10.2: one row paying 2 or 3 of that issuer's payments.
    for t in named:
        if t.id in linked:
            continue
        pool = candidates(t, classes[t.id].issuer)
        group = next((g for n in (2, 3) for g in itertools.combinations(pool, n)
                      if sum(p.amount_cents for p in g) == -t.amount_cents), None)
        if group:
            link(t, list(group), "group", 1.0)
    # §10.3 and §10.5: a row that doesn't say what it is, with a card payment's exact amount.
    for t in bank:
        c = classes[t.id]
        if t.id in linked or c.source in HAND_TYPED or t.flow_kind in NOT_AMOUNT_ONLY:
            continue
        money_out_purchase = t.type == "purchase" and t.flow_kind in (None, "check")
        money_in_other = t.type == "deposit" and t.flow_kind == "other_in"
        if not (money_out_purchase or money_in_other):
            continue
        hit = next((p for p in candidates(t, None) if p.amount_cents == -t.amount_cents), None)
        if hit is None:
            continue
        link(t, [hit], "amount_only", 0.6)
        t.type, t.flow_kind = "transfer", "card_payment"
        reviews.flag("uncertain_transfer", "transactions", t.id, card_txn_id=hit.id,
                     reason="same amount as a card payment, but the description doesn't say so")
        res.uncertain += 1
    # §10.4: no other side.
    first_card = min((p.post_date for p in s.scalars(select(m.Transaction).where(
        m.Transaction.card_id.in_(list(issuer_of))))), default=None)
    grace = settings.match.transfer_grace_days
    for t in bank:
        c = classes[t.id]
        if t.id in linked or c.source == "override":
            continue
        if t.flow_kind == "card_payment_other":
            reviews.flag("unmatched_transfer", "transactions", t.id,
                         reason="A payment to a card you don't track. Those purchases aren't in "
                                "the ledger, so this may be real spending.")
            res.unmatched += 1
        elif (t.flow_kind == "card_payment" and first_card and t.post_date >= first_card
              and (today - date.fromisoformat(t.post_date)).days > grace):
            reviews.flag("unmatched_transfer", "transactions", t.id,
                         reason="A card payment with no matching payment on any card's "
                                "statement or activity yet.")
            res.unmatched += 1
    res.by_flow = Counter(t.flow_kind or t.type for t in bank)
    s.flush()
    return res


# ------------------------------------------------------------------ §10.6


def check_invariants(s: Session, cards: CardsConfig, settings: Settings) -> list[str]:
    """Invariants 1 to 4 on stored data. Returns one line per breach (ids and counts only)."""
    from fin.api.spending import SPENDING_TYPES

    problems = []
    accounts = checking_ids(cards)
    links = s.scalars(select(m.TransferLink)).all()
    bank_linked = {lk.bank_txn_id for lk in links}
    if bank_linked:
        bad = s.scalars(select(m.Transaction.id).where(
            m.Transaction.id.in_(bank_linked), m.Transaction.type.in_(SPENDING_TYPES))).all()
        if bad:
            problems.append(f"1: {len(bad)} linked checking rows count as spending: {bad[:10]}")
    twice = [k for k, n in Counter(lk.card_txn_id for lk in links).items() if n > 1]
    if twice:
        problems.append(f"2: {len(twice)} card payments linked more than once: {twice[:10]}")
    lo, hi = settings.match.transfer_window_days
    hand = set(bank_overrides(s))
    free = s.scalars(select(m.Transaction).where(
        m.Transaction.card_id.in_([c.id for c in cards.cards]), m.Transaction.type == "payment",
        m.Transaction.id.not_in([lk.card_txn_id for lk in links]))).all()
    # The rows §10.3 could have turned into card payments: purchases.
    spending = s.scalars(select(m.Transaction).where(
        m.Transaction.card_id.in_(accounts), m.Transaction.type == "purchase")).all()
    mine = type_rules(s)
    clash = [t.id for t in spending
             if t.dedupe_key not in hand and t.flow_kind not in NOT_AMOUNT_ONLY
             and not any(rx.match(t.description_clean) for rx, _, _ in mine)
             and any(p.amount_cents == -t.amount_cents and lo <= _days(t, p) <= hi
                     for p in free)]
    if clash:
        problems.append(f"3: {len(clash)} spending rows have an unlinked card payment's amount: "
                        f"{clash[:10]}")
    wrong = s.scalars(select(m.Transaction.id).where(
        m.Transaction.card_id.in_(accounts),
        m.Transaction.type.in_(("payment", "credit", "reward", "interest", "adjustment")))).all()
    if wrong:
        problems.append(f"4: {len(wrong)} checking rows have a card-only type: {wrong[:10]}")
    return problems
