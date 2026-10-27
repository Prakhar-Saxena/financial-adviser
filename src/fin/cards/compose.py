# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Compose, diff and approve card profiles (docs/SPEC-cards.md §7.5).

The candidate profile is built from the verified items of the latest `card_terms` and
`card_benefits` extractions. A card's first profile and every material change wait for my
approval (unless `auto_publish`); other changes publish at once. A rejected profile's
content hash is never proposed again.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fin.cards import review
from fin.cards.models import Benefit, Cap, CardProfile, Credit, EarnRule, Fee, normalize_name
from fin.config import Card, Settings
from fin.db import models as m
from fin.db.models import utcnow

CURRENCY_NAMES = (("membership rewards", "amex_mr"), ("ultimate rewards", "chase_ur"),
                  ("thankyou", "citi_typ"), ("thank you", "citi_typ"))
RULE_MATERIAL = ("rate", "cap")
CREDIT_MATERIAL = ("amount_cents", "period")


class IncompleteError(ValueError):
    """The verified items don't make a whole profile (no base rule, fees unverified, ...)."""


# --------------------------------------------------------------------------- building


def currency_id(card: Card, terms: dict[str, Any], settings: Settings) -> str:
    if card.id in settings.cards.currency:
        return settings.cards.currency[card.id]
    name = normalize_name(terms.get("currency_name", ""))
    for needle, cid in CURRENCY_NAMES:
        if needle in name:
            return cid
    if terms.get("currency_kind") == "cash_back":
        return "cash_back"
    return re.sub(r"[^a-z0-9]+", "_", f"{card.issuer}_{terms.get('currency_kind', 'points')}")


def merchant_key(s: Session, name: str) -> str:
    """The merchant profile key for a merchant the documents name. An unknown one becomes a
    `kb` profile with no category yet (merchant_profile fills it in, Phase C2)."""
    alias = normalize_name(name)
    hit = s.get(m.MerchantAlias, alias)
    if hit is not None:
        return hit.profile_key
    key = re.sub(r"[^a-z0-9]+", "_", alias).strip("_") or "merchant"
    if not key[0].isalpha():
        key = f"m_{key}"
    if s.get(m.MerchantProfileRow, key) is None:
        s.add(m.MerchantProfileRow(key=key, display_name=name.strip()[:100], source="kb",
                                   confidence=0.5))
        s.flush()  # the profile row first: the alias references it
    s.add(m.MerchantAlias(alias_norm=alias, profile_key=key, kind="name", source="kb"))
    s.flush()
    return key


def _evidence(items: list[dict], refs: dict[str, int]) -> list[dict]:
    return [{"doc": e["doc"], "page": e.get("page"), "quote": e["quote"],
             "doc_version_id": refs.get(e["doc"])} for e in items]


def _ok(item: dict) -> bool:
    return bool(item.get("_verify", {}).get("ok"))


def _usd(x: float) -> int:
    return round(x * 100)


def build_profile(s: Session, card: Card, settings: Settings, terms: dict[str, Any],
                  benefits: dict[str, Any] | None) -> tuple[CardProfile, list[str]]:
    """The candidate profile from verified items only. Returns it and the items dropped
    while building it. Raises IncompleteError when a required part is missing."""
    refs = terms.get("_refs", {})
    dropped: list[str] = []
    for part, label in (("annual_fee", "annual fee"),
                        ("foreign_transaction_fee", "foreign transaction fee")):
        if not _ok(terms[part]):
            raise IncompleteError(f"the {label} didn't verify")
    annual = Fee(value=_usd(terms["annual_fee"]["usd"]),
                 evidence=_evidence(terms["annual_fee"]["evidence"], refs))
    ftf = Fee(value=terms["foreign_transaction_fee"]["percent"],
              evidence=_evidence(terms["foreign_transaction_fee"]["evidence"], refs))

    rules: list[EarnRule] = []
    for r in terms["earn_rules"]:
        if not _ok(r):
            continue
        cap = r.get("cap")
        try:
            rules.append(EarnRule(
                rate=r["rate"], is_base=r["is_base"], categories=sorted(r["categories"]),
                merchants=sorted({merchant_key(s, n) for n in r["merchant_names"]}),
                merchant_names=r["merchant_names"], channel=r["channel"], portal=r["portal"],
                geography=r["geography"], prepaid_only=r["prepaid_only"],
                requirements=r["requirements"], valid_from=r["valid_from"],
                valid_to=r["valid_to"],
                cap=None if not cap else Cap(
                    amount_cents=_usd(cap["amount_usd"]), period=cap["period"],
                    rate_after=cap["rate_after"],
                    shared_id=(f"cap_{cap['period']}_{_usd(cap['amount_usd'])}"
                               if cap.get("shared_with") else None)),
                excluded_categories=sorted(r["excluded_categories"]),
                exclusions=r["exclusions"], description=r["description"],
                evidence=_evidence(r["evidence"], refs)))
        except ValidationError as e:
            dropped.append(f"earn rule {r.get('description', '')!r}: {e.errors()[0]['msg']}")
    if sum(r.is_base for r in rules) != 1:
        raise IncompleteError("no verified base rule (\"all other purchases\")")

    credits: list[Credit] = []
    for c in terms["credits"]:
        if not _ok(c):
            continue
        try:
            credits.append(Credit(
                name=c["name"], amount_cents=_usd(c["amount_usd"]), period=c["period"],
                merchants=sorted({merchant_key(s, n) for n in c["merchant_names"]}),
                merchant_names=c["merchant_names"], categories=sorted(c["categories"]),
                enrollment_required=c["enrollment_required"], posts_as=c["posts_as"],
                valid_from=c["valid_from"], valid_to=c["valid_to"],
                description=c["description"], evidence=_evidence(c["evidence"], refs)))
        except ValidationError as e:
            dropped.append(f"credit {c.get('name', '')!r}: {e.errors()[0]['msg']}")

    bens: list[Benefit] = []
    for b in (benefits or {}).get("benefits", []):
        if not _ok(b):
            continue
        try:
            bens.append(Benefit(kind=b["kind"], name=b["name"], summary=b["summary"],
                                limits=b["limits"], coverage=b["coverage"],
                                evidence=_evidence(b["evidence"], benefits.get("_refs", {}))))
        except ValidationError as e:
            dropped.append(f"benefit {b.get('name', '')!r}: {e.errors()[0]['msg']}")

    profile = CardProfile(
        card_id=card.id, currency=currency_id(card, terms, settings),
        currency_name=terms.get("currency_name", ""), annual_fee_cents=annual,
        foreign_txn_fee_pct=ftf,
        earn_rules=sorted(rules, key=lambda r: (not r.is_base, r.identity())),
        credits=sorted(credits, key=lambda c: c.identity()),
        benefits=sorted(bens, key=lambda b: b.identity()),
        notes=list(terms.get("notes", [])) + list((benefits or {}).get("notes", [])),
    )
    return profile, dropped


def content_hash(p: CardProfile) -> str:
    return hashlib.sha256(p.model_dump_json().encode()).hexdigest()


# --------------------------------------------------------------------------- diff


def _keyed(items: list) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for it in items:
        k, n = it.identity(), 2
        while k in out:  # two items with the same scope: keep both apart
            k = f"{it.identity()}#{n}"
            n += 1
        out[k] = it
    return out


def _label(section: str, it: Any) -> str:
    if section == "earn_rules":
        scope = "all other purchases" if it.is_base else ", ".join(
            it.categories + it.merchant_names)
        return f"{it.rate:g}x {scope}"
    if section == "credits":
        return f"{it.name} (${it.amount_cents / 100:,.0f}/{it.period})"
    return it.name


def _changed_fields(a: Any, b: Any) -> dict[str, list]:
    da = a.model_dump(exclude={"evidence", "description", "summary"})
    db = b.model_dump(exclude={"evidence", "description", "summary"})
    return {k: [da[k], db[k]] for k in da if da[k] != db.get(k)}


def diff(old: CardProfile | None, new: CardProfile) -> dict[str, Any]:
    """added, removed, changed (field: [old, new]) and reworded, plus whether it's material
    (§7.5). Additions of earn rules and credits count as material: they change what a search
    recommends."""
    d: dict[str, Any] = {"added": [], "removed": [], "changed": [], "reworded": [],
                         "material": old is None}
    if old is None:
        return d
    for name, attr in (("annual fee", "annual_fee_cents"),
                       ("foreign transaction fee", "foreign_txn_fee_pct")):
        a, b = getattr(old, attr).value, getattr(new, attr).value
        if a != b:
            d["changed"].append({"section": "fees", "item": name, "fields": {"value": [a, b]},
                                 "material": True})
    if old.currency != new.currency:
        d["changed"].append({"section": "fees", "item": "currency",
                             "fields": {"currency": [old.currency, new.currency]},
                             "material": True})
    for section, material_fields in (("earn_rules", RULE_MATERIAL),
                                     ("credits", CREDIT_MATERIAL), ("benefits", ())):
        a, b = _keyed(getattr(old, section)), _keyed(getattr(new, section))
        for k in a.keys() - b.keys():
            d["removed"].append({"section": section, "item": _label(section, a[k]),
                                 "material": True})
        for k in b.keys() - a.keys():
            d["added"].append({"section": section, "item": _label(section, b[k]),
                               "material": section != "benefits"})
        for k in a.keys() & b.keys():
            fields = _changed_fields(a[k], b[k])
            if fields:
                d["changed"].append({"section": section, "item": _label(section, b[k]),
                                     "fields": fields,
                                     "material": any(f in material_fields for f in fields)})
            elif a[k] != b[k]:
                d["reworded"].append({"section": section, "item": _label(section, b[k])})
    if old.notes != new.notes:
        d["reworded"].append({"section": "notes", "item": "notes"})
    for key in ("added", "removed", "changed"):
        d[key].sort(key=lambda x: (x["section"], x["item"]))
    d["material"] = any(x["material"] for k in ("added", "removed", "changed") for x in d[k])
    return d


# --------------------------------------------------------------------------- versions


def current(s: Session, card_id: str, status: str) -> m.CardProfileVersion | None:
    return s.scalars(select(m.CardProfileVersion).where(
        m.CardProfileVersion.card_id == card_id, m.CardProfileVersion.status == status)).first()


def load(v: m.CardProfileVersion | None) -> CardProfile | None:
    return CardProfile.model_validate_json(v.profile_json) if v else None


@dataclass
class ProposeOutcome:
    kind: str  # unchanged | published | pending | rejected_before
    version: int | None = None
    diff: dict[str, Any] = field(default_factory=dict)


def propose(s: Session, card_id: str, profile: CardProfile, extraction_ids: list[int],
            auto_publish: bool = False) -> ProposeOutcome:
    h = content_hash(profile)
    active, pending = current(s, card_id, "active"), current(s, card_id, "pending")
    if active is not None and active.content_hash == h:
        if pending is not None:  # the proposed change went away again
            _decide(s, pending, "superseded")
        return ProposeOutcome("unchanged", active.version)
    if pending is not None and pending.content_hash == h:
        return ProposeOutcome("unchanged", pending.version)
    if s.scalars(select(m.CardProfileVersion.id).where(
            m.CardProfileVersion.card_id == card_id, m.CardProfileVersion.status == "rejected",
            m.CardProfileVersion.content_hash == h)).first() is not None:
        return ProposeOutcome("rejected_before")

    d = diff(load(active), profile)
    if pending is not None:
        _decide(s, pending, "superseded")
    hold = d["material"] and not auto_publish
    if not hold and active is not None:
        _decide(s, active, "superseded")
    s.flush()  # free the one-active / one-pending slot before the insert
    version = (s.scalar(select(func.max(m.CardProfileVersion.version)).where(
        m.CardProfileVersion.card_id == card_id)) or 0) + 1
    s.add(m.CardProfileVersion(
        card_id=card_id, version=version, status="pending" if hold else "active",
        profile_json=profile.model_dump_json(), content_hash=h, diff_json=json.dumps(d),
        material=d["material"], extraction_ids_json=json.dumps(extraction_ids),
        decided_at=None if hold else utcnow()))
    s.flush()
    if hold:
        review.open_item(s, "card_terms_pending", card_id, "pending",
                         {"version": version, "first": active is None,
                          "summary": summarize(d)})
        return ProposeOutcome("pending", version, d)
    review.close_items(s, "card_terms_pending", card_id, "pending")
    return ProposeOutcome("published", version, d)


def _decide(s: Session, v: m.CardProfileVersion, status: str) -> None:
    v.status, v.decided_at = status, utcnow()


def summarize(d: dict[str, Any]) -> str:
    if not any(d[k] for k in ("added", "removed", "changed")):
        return "first profile" if d.get("material") else "wording only"
    return ", ".join(f"{len(d[k])} {k}" for k in ("added", "removed", "changed") if d[k])


class NothingPendingError(LookupError):
    pass


def accept(s: Session, card_id: str, version: int | None = None) -> m.CardProfileVersion:
    pending = current(s, card_id, "pending")
    if pending is None or (version is not None and pending.version != version):
        raise NothingPendingError(f"{card_id}: no pending version"
                                  + (f" {version}" if version else ""))
    active = current(s, card_id, "active")
    if active is not None:
        _decide(s, active, "superseded")
        s.flush()  # free the one-active slot first
    _decide(s, pending, "active")
    review.close_items(s, "card_terms_pending", card_id, "pending")
    s.flush()
    return pending


def reject(s: Session, card_id: str, version: int | None = None) -> m.CardProfileVersion:
    pending = current(s, card_id, "pending")
    if pending is None or (version is not None and pending.version != version):
        raise NothingPendingError(f"{card_id}: no pending version"
                                  + (f" {version}" if version else ""))
    _decide(s, pending, "rejected")
    review.close_items(s, "card_terms_pending", card_id, "pending")
    s.flush()
    return pending
