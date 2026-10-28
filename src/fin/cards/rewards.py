# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rewards check (docs/SPEC-cards.md Phase C5): what each statement says I earned against what
the engine predicts for that period's purchases. A large gap means the card's terms or my
merchants' categories are off. All local; no AI.

Statements that carry a rewards summary:
- Chase: a points box ("+ 5% back on Amazon.com purchases 2,604", "3x on dining 0",
  "+ 1.5% (1.5 Pts)/$1 earned on all purchases 1,959") between "Previous points balance" and
  "Total points available". It sits in a two-column layout, so the text comes from the
  card-docs column splitter.
- Citi Costco: "Earned this period +$47.15", with one line per category.
- Amex statements show only the shared Membership Rewards balance, not points earned in the
  period, so there's nothing to compare.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.cards import engine
from fin.cards.models import CardProfile
from fin.config import CardsConfig
from fin.db import models as m

# A large gap: this share of the statement's figure, and at least this many units
# (points, or cents of cash back).
GAP_SHARE = 0.10
GAP_MIN_UNITS = 200

CHASE_START = re.compile(r"Previous points balance", re.I)
CHASE_END = re.compile(r"Total points available", re.I)
CHASE_SKIP = re.compile(r"redeemed|transferred|previous points|total points", re.I)
CHASE_LINE = re.compile(
    r"(?:^|\s)\+?\s?((?:\d+(?:\.\d+)?\s?(?:%|x\b)|Extra\b).*?)\s(\d{1,3}(?:,\d{3})*|\d+)\s*$",
    re.I)
CITI_EARNED = re.compile(r"Earned this period\s*\+?\$([\d,]+\.\d{2})", re.I)
CITI_LINE = re.compile(r"^(\d+(?:\.\d+)?% on .+?)\s*\+?\$([\d,]+\.\d{2})\s*$", re.I)


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def parse_chase(text: str) -> dict[str, Any] | None:
    lines, inside = [], False
    for line in text.splitlines():
        if CHASE_START.search(line):
            inside = True
            continue
        if inside and CHASE_END.search(line):
            break
        if not inside or CHASE_SKIP.search(line) or line.strip().startswith("-"):
            continue
        hit = CHASE_LINE.search(line)
        if hit:
            lines.append({"text": " ".join(hit.group(1).split()), "value": _int(hit.group(2))})
    if not lines:
        return None
    return {"kind": "points", "earned": sum(x["value"] for x in lines), "lines": lines}


def parse_citi(text: str) -> dict[str, Any] | None:
    earned = CITI_EARNED.search(text)
    if not earned:
        return None
    lines = [{"text": h.group(1), "value": round(float(h.group(2).replace(",", "")) * 100)}
             for h in (CITI_LINE.match(x.strip()) for x in text.splitlines()) if h]
    return {"kind": "cash", "earned": round(float(earned.group(1).replace(",", "")) * 100),
            "lines": lines}


PARSERS = {"chase": parse_chase, "citi": parse_citi}


def statement_rewards(path: Path, issuer: str) -> dict[str, Any]:
    """The rewards summary on one statement PDF, or {} when it has none."""
    parser = PARSERS.get(issuer)
    if parser is None or not path.exists():
        return {}
    from fin.cards.docs import pdf_text

    text, _ = pdf_text(path.read_bytes())
    return parser(text) or {}


def fill_statement_rewards(s: Session, cards: CardsConfig) -> int:
    """`fin process` step: read each statement's rewards summary once. Returns how many were
    read. `fin rebuild` recreates statements, and the next process reads them again."""
    issuers = {c.id: c.issuer for c in cards.cards}
    n = 0
    for st in s.scalars(select(m.Statement).where(m.Statement.rewards_json.is_(None))):
        try:
            found = statement_rewards(Path(st.pdf_path), issuers.get(st.card_id, ""))
        except Exception:  # an unreadable PDF has no summary; the ledger check reports it
            found = {}
        st.rewards_json = json.dumps(found)
        n += 1
    s.commit()
    return n


# --------------------------------------------------------------------------- the check


@dataclass
class Predicted:
    units: float = 0
    by_rule: dict[str, float] = field(default_factory=dict)
    purchases: int = 0
    refunds: int = 0
    unknown_merchants: int = 0


def predict(s: Session, card: engine.Card, profile: CardProfile, facts: engine.Facts,
            st: m.Statement) -> Predicted:
    """Units the engine expects for the statement's purchases less refunds, at current terms:
    the transactions linked to the statement (they add up to its purchases), else those posted
    in its period. A "credit" at a known merchant is a refund (the issuer takes the points
    back); a statement credit matches no merchant and is left out. Merchants not known yet
    count at the base rate."""
    from fin.cards.history import as_engine, merchant_index, merchant_of

    aliases, merchants = merchant_index(s)
    out = Predicted()
    kinds = ("purchase", "refund", "credit")
    rows = s.scalars(select(m.Transaction).where(
        m.Transaction.statement_id == st.id, m.Transaction.type.in_(kinds))).all()
    if not rows:
        rows = s.scalars(select(m.Transaction).where(
            m.Transaction.card_id == card.id, m.Transaction.type.in_(kinds),
            m.Transaction.post_date >= st.period_start,
            m.Transaction.post_date <= st.period_end)).all()
    for t in rows:
        mp = merchant_of(t, aliases, merchants)
        if t.type == "credit" and (mp is None or mp.reward_category is None):
            continue
        if mp is None or mp.reward_category is None:
            merchant = engine.Merchant("unknown", "", "other")
            out.unknown_merchants += 1
        else:
            merchant = as_engine(mp)
        day = date.fromisoformat(t.txn_date or t.post_date)
        r = engine.evaluate(card, profile, merchant, engine.Options(abs(t.amount_cents), None,
                                                                    False, day), facts)
        sign = -1 if t.amount_cents < 0 else 1
        out.units += sign * r.units
        label = engine.rate_text(r.rule.rate, profile.currency) + " on " + engine.scope_text(
            r.rule) if r.rule else "no rule"
        out.by_rule[label] = out.by_rule.get(label, 0) + sign * r.units
        if sign < 0:
            out.refunds += 1
        else:
            out.purchases += 1
    return out


def rewards_check(s: Session, cards: CardsConfig, profiles: dict[str, CardProfile],
                  facts: engine.Facts, months: int = 6, today: date | None = None
                  ) -> dict[str, Any]:
    """Each recent statement with a rewards summary, against the engine's prediction."""
    from fin.sync.params import months_before

    today = today or date.today()
    since = months_before(today, months).isoformat()
    # Earn rates only: no caps used so far, no credits, no offers (one-time, not points).
    facts = replace(facts, cap_used={}, credit_used={}, offers={})
    by_id = {c.id: c for c in cards.cards}
    rows, without = [], set()
    for st in s.scalars(select(m.Statement).where(m.Statement.period_end >= since)
                        .order_by(m.Statement.card_id, m.Statement.period_end.desc())):
        card = by_id.get(st.card_id)
        profile = profiles.get(st.card_id)
        summary = json.loads(st.rewards_json) if st.rewards_json else {}
        if card is None or profile is None or not summary:
            if card is not None:
                without.add(card.product)
            continue
        p = predict(s, engine.Card(card.id, card.product, card.network), profile, facts, st)
        stated = summary["earned"]
        diff = p.units - stated
        flagged = abs(diff) >= GAP_MIN_UNITS and abs(diff) > GAP_SHARE * max(stated, 1)
        rows.append({
            "card_id": card.id, "product": card.product, "statement_id": st.id,
            "period_start": st.period_start, "period_end": st.period_end,
            "kind": summary["kind"], "stated": stated, "predicted": round(p.units),
            "difference": round(diff), "flagged": flagged,
            "stated_lines": summary["lines"],
            "predicted_lines": [{"text": k, "value": round(v)} for k, v in
                                sorted(p.by_rule.items(), key=lambda kv: -abs(kv[1]))],
            "purchases": p.purchases, "refunds": p.refunds,
            "unknown_merchants": p.unknown_merchants})
    with_summary = {r["product"] for r in rows}
    return {"months": months, "since": since, "statements": rows,
            "without_summary": sorted(without - with_summary),
            "note": ("Predicted at current terms from the purchases posted in each statement "
                     "period. Points for points cards, cents for cash back. Amex statements "
                     "show no points earned per period.")}
