# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Best card for a merchant: resolution + facts + engine (docs/SPEC-cards.md §8, §9, §12).

Used by `fin cards best` and `GET /api/cards/best`. Never calls Claude or the network; an
unknown merchant comes back with `needs_lookup`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.cards import engine
from fin.cards.merchants import Resolution, name_keys, resolve, sync_seeds
from fin.cards.models import CardProfile, normalize_name
from fin.cards.status import card_status
from fin.config import CardsConfig, Settings, load_reward_categories
from fin.db import models as m
from fin.db.models import utcnow

REQUIREMENT_KINDS = ("membership", "enrolled", "activated")


def active_profiles(s: Session) -> dict[str, CardProfile]:
    return {v.card_id: CardProfile.model_validate_json(v.profile_json)
            for v in s.scalars(select(m.CardProfileVersion).where(
                m.CardProfileVersion.status == "active"))}


def _facts(s: Session, kind: str) -> list[m.CardFact]:
    return list(s.scalars(select(m.CardFact).where(m.CardFact.kind == kind)))


def load_facts(s: Session, settings: Settings, cards: CardsConfig,
               now: datetime | None = None, *, with_history: bool = True) -> engine.Facts:
    """Settings + my facts, and (with_history) cap and credit use from my transactions."""
    cs = settings.cards
    f = engine.Facts(valuations=dict(cs.valuations), count_unused_credits=cs.count_unused_credits)
    for x in _facts(s, "valuation"):
        f.valuations[x.ref] = float(x.value)
    for x in _facts(s, "currency_pool"):
        if x.card_id:
            f.pools[x.card_id] = x.value
    for kind in REQUIREMENT_KINDS:
        for x in _facts(s, kind):
            if x.card_id:
                f.requirements[(x.card_id, x.ref)] = x.value == "yes"
    for x in _facts(s, "setting"):
        if x.ref == "count_unused_credits":
            f.count_unused_credits = x.value == "yes"
    now = now or datetime.now(UTC)
    for st in card_status(s, cards, cs.refresh_days, now):
        if st.stale and st.last_checked:
            checked = datetime.strptime(st.last_checked, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            f.stale_days[st.card_id] = (now - checked).days
    f.names = name_keys(s)
    if with_history:
        from fin.cards.history import cap_use, credit_use, credit_used_map

        profiles = active_profiles(s)
        today = now.date()
        f.cap_used = {(u.card_id, u.key): u.used_cents for u in cap_use(s, profiles, f, today)}
        f.credit_used = credit_used_map(credit_use(s, profiles, cards, f, today))
    return f


def engine_cards(cards: CardsConfig) -> list[engine.Card]:
    return [engine.Card(c.id, c.product, c.network) for c in cards.cards]


# What I call each currency (Membership Rewards is one pool across my Amex cards).
CURRENCY_LABELS = {"amex_mr": "American Express Rewards Points",
                   "chase_ur": "Ultimate Rewards points", "citi_typ": "ThankYou points",
                   "cash_back": "cash back"}


def currency_names(profiles: dict[str, CardProfile]) -> dict[str, str]:
    """Each currency's name: the well-known label, else the fullest the documents give."""
    out: dict[str, str] = dict(CURRENCY_LABELS)
    for p in profiles.values():
        name = p.currency_name or p.currency
        if p.currency not in CURRENCY_LABELS and len(name) > len(out.get(p.currency, "")):
            out[p.currency] = name
    return out


def kb_freshness(s: Session, settings: Settings, cards: CardsConfig) -> dict[str, Any]:
    rows = card_status(s, cards, settings.cards.refresh_days)
    checked = [r.last_checked for r in rows if r.last_checked]
    return {"last_checked": min(checked) if checked else None,
            "stale": [r.card_id for r in rows if r.stale],
            "without_terms": [r.card_id for r in rows if not r.active_version]}


def best(s: Session, settings: Settings, cards: CardsConfig, query: str, *,
         amount_cents: int | None = None, channel: str | None = None, abroad: bool = False,
         on: date | None = None, merchant_key: str | None = None,
         low_confidence: float = 0.7) -> dict[str, Any]:
    """The ranking for a query, or `needs_lookup` with suggestions."""
    sync_seeds(s)
    if merchant_key:
        p = s.get(m.MerchantProfileRow, merchant_key)
        if p is None:
            raise KeyError(merchant_key)
        from fin.cards.merchants import Merchant

        res = Resolution(query, Merchant.of(p), "picked")
    else:
        res = resolve(s, query)
    s.commit()
    out: dict[str, Any] = {"query": query, "kb": kb_freshness(s, settings, cards),
                           "resolution": res.how, "suggestions": res.suggestions,
                           "needs_lookup": res.needs_lookup}
    if res.needs_lookup:
        out["merchant"] = res.merchant.as_dict() if res.merchant else None
        return out
    mp = res.merchant
    profiles = active_profiles(s)
    opts = engine.Options(amount_cents or settings.cards.default_amount_cents, channel, abroad,
                          on or date.today())
    merchant = engine.Merchant(mp.key, mp.display_name, mp.reward_category, mp.acceptance)
    ranking = engine.rank(merchant, engine_cards(cards), profiles, opts,
                          load_facts(s, settings, cards), currency_names(profiles))
    out.update(ranking.as_dict())
    out["merchant"] = {**mp.as_dict(), "unsure": mp.confidence < low_confidence}
    return out


# --------------------------------------------------------------------------- settings


def requirements_of(profile: CardProfile) -> list[dict[str, str]]:
    """Every membership, enrollment or activation a card's rules and credits mention."""
    out: dict[str, dict[str, str]] = {}
    for r in profile.earn_rules:
        for q in r.requirements:
            ref = normalize_name(q.name) or q.kind
            out.setdefault(ref, {"ref": ref, "kind": q.kind, "label": q.name or q.kind,
                                 "for": engine.scope_text(r)})
    for c in profile.credits:
        if c.enrollment_required:
            ref = normalize_name(c.name)
            out.setdefault(ref, {"ref": ref, "kind": "enrollment", "label": c.name,
                                 "for": "credit"})
    return list(out.values())


FACT_KIND = {"membership": "membership", "enrollment": "enrolled", "activation": "activated"}


def get_settings(s: Session, settings: Settings, cards: CardsConfig) -> dict[str, Any]:
    profiles = active_profiles(s)
    f = load_facts(s, settings, cards)
    # Only currencies a card earns or is pooled into: the rest would be noise in the form.
    currencies = sorted({p.currency for p in profiles.values()} | set(f.pools.values())
                        | (set(f.valuations) if not profiles else set()))
    names = currency_names(profiles)
    out_cards = []
    for c in cards.cards:
        p = profiles.get(c.id)
        reqs = []
        for r in requirements_of(p) if p else []:
            said = f.requirements.get((c.id, r["ref"]))
            reqs.append({**r, "value": None if said is None else ("yes" if said else "no")})
        anniversary = next((x.value for x in _facts(s, "anniversary_month")
                            if x.card_id == c.id), None)
        out_cards.append({"card_id": c.id, "product": c.product,
                          "currency": p.currency if p else None,
                          "pooled_into": f.pools.get(c.id), "anniversary_month": anniversary,
                          "requirements": reqs})
    return {"valuations": [{"currency": cur, "name": names.get(cur, cur),
                            "cents": f.valuation(cur)} for cur in currencies],
            "count_unused_credits": f.count_unused_credits, "cards": out_cards,
            "reward_categories": [{"id": c.id, "name": c.name}
                                  for c in load_reward_categories().categories]}


def set_fact(s: Session, kind: str, ref: str, value: str | None, card_id: str | None = None
             ) -> None:
    """Upsert one fact; value None deletes it."""
    q = select(m.CardFact).where(m.CardFact.kind == kind, m.CardFact.ref == ref)
    q = q.where(m.CardFact.card_id == card_id) if card_id else q.where(
        m.CardFact.card_id.is_(None))
    row = s.scalars(q).first()
    if value is None:
        if row is not None:
            s.delete(row)
        return
    if row is None:
        s.add(m.CardFact(kind=kind, card_id=card_id, ref=ref, value=value, created_at=utcnow()))
    else:
        row.value = value


def put_settings(s: Session, cards: CardsConfig, body: dict[str, Any]) -> None:
    known = {c.id for c in cards.cards}
    for v in body.get("valuations", []):
        cents = float(v["cents"])
        if not 0 < cents <= 10:
            raise ValueError("a valuation must be between 0 and 10 cents")
        set_fact(s, "valuation", v["currency"], f"{cents:g}")
    if "count_unused_credits" in body:
        set_fact(s, "setting", "count_unused_credits",
                 "yes" if body["count_unused_credits"] else "no")
    for c in body.get("cards", []):
        cid = c["card_id"]
        if cid not in known:
            raise ValueError(f"unknown card {cid}")
        if "pooled_into" in c:
            set_fact(s, "currency_pool", "", c["pooled_into"] or None, cid)
        if "anniversary_month" in c:
            month = c["anniversary_month"]
            if month is not None and not (isinstance(month, int) and 1 <= month <= 12):
                raise ValueError("anniversary_month is 1-12")
            set_fact(s, "anniversary_month", "", None if month is None else str(month), cid)
        for r in c.get("requirements", []):
            kind = FACT_KIND.get(r["kind"])
            if kind is None or r.get("value") not in ("yes", "no", None):
                raise ValueError("bad requirement")
            set_fact(s, kind, r["ref"], r.get("value"), cid)
    s.commit()

