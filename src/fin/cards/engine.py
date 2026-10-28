# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The recommendation engine (docs/SPEC-cards.md §9). Pure Python: no database, network or AI.

For one merchant and amount it ranks every card by what I'd actually get back, from the active
card profiles, my valuations and facts. Explanations come from templates, never from a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fin.cards.models import Benefit, CardProfile, Credit, EarnRule, normalize_name

TRAVEL = {"airlines", "hotels", "car_rental", "cruises", "travel_agencies"}
# Merchant names in the documents that are really booking channels ("Chase Travel",
# "AmexTravel.com", "The Edit"): they never limit a rule to one merchant.
CHANNEL_NAME = re.compile(r"travel|\bthe edit\b", re.I)
CHANNEL_WORDS = {"online": "online", "in_store": "in store", "in_app": "in the app"}
GEO_WORDS = {"us": "U.S. only", "outside_us": "outside the U.S. only"}
PROTECTIONS = {
    "electronics": ("extended_warranty", "purchase_protection", "return_protection"),
    "home_improvement": ("extended_warranty", "purchase_protection"),
    "airlines": ("trip_cancellation_interruption", "trip_delay", "baggage_delay_loss",
                 "travel_accident"),
    "hotels": ("trip_cancellation_interruption", "trip_delay"),
    "travel_agencies": ("trip_cancellation_interruption", "trip_delay", "baggage_delay_loss"),
    "cruises": ("trip_cancellation_interruption", "trip_delay", "baggage_delay_loss"),
    "car_rental": ("rental_car_cdw",),
    "telecom": ("cell_phone_protection",),
}
ENDS_SOON_DAYS = 31
# A rule for the parent category covers the child unless the terms exclude it: issuers write
# "dining at restaurants, including takeout and delivery".
PARENT = {"food_delivery": "dining"}
BREAK_EVEN_MAX = 2.5  # cents; above this the tie point isn't a useful hint


@dataclass(frozen=True)
class Card:
    id: str
    product: str
    network: str


@dataclass(frozen=True)
class Merchant:
    key: str
    display_name: str
    category: str
    acceptance: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Options:
    amount_cents: int = 10_000
    channel: str | None = None  # None (not said) | in_store | online | in_app
    abroad: bool = False
    on: date = field(default_factory=date.today)


@dataclass
class Facts:
    """What I've told the app (SPEC-cards §6 card_facts) plus settings."""

    valuations: dict[str, float] = field(default_factory=dict)  # currency -> cents per unit
    pools: dict[str, str] = field(default_factory=dict)  # card id -> currency it's valued at
    requirements: dict[tuple[str, str], bool] = field(default_factory=dict)  # (card, name) -> y/n
    count_unused_credits: bool = True
    stale_days: dict[str, int] = field(default_factory=dict)  # card id -> days since checked
    cap_used: dict[tuple[str, str], int] = field(default_factory=dict)  # (card, cap key) -> cents
    # (card, credit identity) -> cents used this period. A credit counts in the ranking only when
    # its use is known (Phase C3 tracks it); otherwise it's shown on its own line.
    credit_used: dict[tuple[str, str], int] = field(default_factory=dict)
    names: dict[str, tuple[str, str]] = field(default_factory=dict)  # alias -> (key, source)

    def valuation(self, currency: str) -> float:
        return self.valuations.get(currency, 1.0)

    def requirement(self, card_id: str, kind: str, name: str) -> bool | None:
        for k in (normalize_name(name), kind):
            if (card_id, k) in self.requirements:
                return self.requirements[(card_id, k)]
        return None


# --------------------------------------------------------------------------- matching


def merchant_keys(names: list[str], facts: Facts, *, any_source: bool = False) -> set[str]:
    """The merchant profiles a rule's merchant names point at. For earn rules only real
    merchants count (not `kb` placeholders or booking channels); credits use any profile."""
    out = set()
    for n in names:
        if CHANNEL_NAME.search(n) and not any_source:
            continue
        # "Walmart+", "CLEAR+": the membership, not the store ("walmart plus", never "walmart").
        hit = facts.names.get(normalize_name(n.replace("+", " plus")))
        if hit and (any_source or hit[1] != "kb"):
            out.add(hit[0])
    return out


def covers(categories: list[str], category: str) -> bool:
    return category in categories or PARENT.get(category) in categories


def brand(key: str) -> str:
    """costco_gas -> costco, uber_eats -> uber, amazon -> amazon."""
    return key.split("_")[0]


def _cash(currency: str) -> bool:
    return currency == "cash_back"


def rate_text(rate: float, currency: str) -> str:
    return f"{rate:g}%" if _cash(currency) else f"{rate:g}x"


def scope_text(rule: EarnRule) -> str:
    if rule.is_base:
        return "everything else"
    names = [n for n in rule.merchant_names if not CHANNEL_NAME.search(n)]
    cats = [c.replace("_", " ") for c in rule.categories if c != "other"]
    return ", ".join(names or cats) or "these purchases"


@dataclass
class Fit:
    rule: EarnRule
    caveats: list[str]


def rule_fits(rule: EarnRule, card_id: str, merchant: Merchant, opts: Options, facts: Facts,
              *, portal: bool = False) -> Fit | None:
    """Whether a rule applies here (§9 step 2). `portal`: match issuer-portal rules instead,
    which only ever come back as alternatives."""
    caveats: list[str] = []
    if (rule.channel == "issuer_portal") != portal:
        return None
    if portal:
        cats = set(rule.categories)
        travel_wide = "travel_agencies" in cats and not (cats & (TRAVEL - {"travel_agencies"}))
        if not covers(rule.categories, merchant.category) and not (
                travel_wide and merchant.category in TRAVEL):
            return None
    elif not rule.is_base:
        keys = merchant_keys(rule.merchant_names, facts) | (
            set(rule.merchants) & {v[0] for v in facts.names.values() if v[1] != "kb"})
        in_category = covers(rule.categories, merchant.category)
        if keys:
            # A rule naming merchants is limited to them, and to its categories when it has
            # real ones: "5% on gas at Costco" is Costco gas, not the warehouse. The brand
            # counts ("Costco" covers costco_gas); "other" is no category at all.
            categories = set(rule.categories) - {"other"}
            named = merchant.key in keys or brand(merchant.key) in {brand(k) for k in keys}
            if not named or (categories and not in_category):
                return None
        elif not in_category:
            return None
    if merchant.category in rule.excluded_categories and not rule.is_base:
        return None
    if rule.channel in CHANNEL_WORDS:
        if opts.channel is None:
            caveats.append(f"{CHANNEL_WORDS[rule.channel]} only")
        elif opts.channel != rule.channel:
            return None
    if rule.geography == "us" and opts.abroad:
        return None
    if rule.geography == "outside_us" and not opts.abroad:
        return None
    if rule.prepaid_only:
        caveats.append("prepaid bookings only")
    if rule.valid_from and date.fromisoformat(rule.valid_from) > opts.on:
        return None
    if rule.valid_to:
        end = date.fromisoformat(rule.valid_to)
        if end < opts.on:
            return None
        if (end - opts.on).days <= ENDS_SOON_DAYS:
            caveats.append(f"ends {rule.valid_to}")
    for req in rule.requirements:
        said = facts.requirement(card_id, req.kind, req.name)
        if said is False:
            return None
        if said is None:
            caveats.append(f"needs {req.name or req.kind}")
    return Fit(rule, caveats)


# --------------------------------------------------------------------------- value


def cap_key(rule: EarnRule) -> str:
    return rule.cap.shared_id or rule.identity() if rule.cap else ""


def earn(rule: EarnRule, card_id: str, amount: int, facts: Facts
         ) -> tuple[float, list[str]]:
    """Units earned on `amount` cents, blending across the cap (§9 step 4)."""
    if rule.cap is None:
        return amount / 100 * rule.rate, []
    used = facts.cap_used.get((card_id, cap_key(rule)))
    room = max(rule.cap.amount_cents - (used or 0), 0)
    note = (f"up to ${rule.cap.amount_cents / 100:,.0f} per {rule.cap.period.replace('_', ' ')}"
            + ("" if used is not None else " (your use so far isn't counted yet)"))
    if amount <= room:
        return amount / 100 * rule.rate, [note]
    units = room / 100 * rule.rate + (amount - room) / 100 * rule.cap.rate_after
    return units, [note, f"only ${room / 100:,.2f} left at this rate"]


def credit_fits(c: Credit, merchant: Merchant, opts: Options, facts: Facts) -> bool:
    if c.valid_from and date.fromisoformat(c.valid_from) > opts.on:
        return False
    if c.valid_to and date.fromisoformat(c.valid_to) < opts.on:
        return False
    if c.merchant_names or c.merchants:
        keys = merchant_keys(c.merchant_names, facts, any_source=True) | set(c.merchants)
        return merchant.key in keys
    return covers(c.categories, merchant.category)


def evidence(item: Any) -> dict[str, Any] | None:
    if not item.evidence:
        return None
    e = item.evidence[0]
    return {"quote": e.quote, "page": e.page, "doc_version_id": e.doc_version_id}


@dataclass
class CardResult:
    card: Card
    currency: str
    valuation: float
    total_cents: float = 0
    rule_cents: float = 0
    credit_cents: float = 0
    fee_cents: float = 0
    units: float = 0
    rule: EarnRule | None = None
    caveats: list[str] = field(default_factory=list)
    credit: dict[str, Any] | None = None
    benefits: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    ftf_pct: float = 0
    rank: int = 0

    def as_dict(self, amount: int) -> dict[str, Any]:
        r = self.rule
        return {
            "card_id": self.card.id, "product": self.card.product, "rank": self.rank,
            "currency": self.currency, "valuation_cents": self.valuation,
            "total_cents": round(self.total_cents), "rule_cents": round(self.rule_cents),
            "credit_cents": round(self.credit_cents), "fee_cents": round(self.fee_cents),
            "cents_per_dollar": round(self.rule_cents / amount * 100, 2) if amount else 0,
            "units": round(self.units, 2),
            "rule": None if r is None else {
                "rate": r.rate, "rate_text": rate_text(r.rate, self.currency),
                "text": f"{rate_text(r.rate, self.currency)} on {scope_text(r)}",
                "is_base": r.is_base, "evidence": evidence(r)},
            "caveats": self.caveats, "credit": self.credit, "benefits": self.benefits,
            "alternatives": self.alternatives,
        }


def evaluate(card: Card, profile: CardProfile, merchant: Merchant, opts: Options,
             facts: Facts) -> CardResult:
    currency = facts.pools.get(card.id, profile.currency)
    res = CardResult(card, currency, facts.valuation(currency),
                     ftf_pct=profile.foreign_txn_fee_pct.value)
    v, amount = res.valuation, opts.amount_cents
    best: tuple[float, Fit, list[str], float] | None = None
    for rule in profile.earn_rules:
        fit = rule_fits(rule, card.id, merchant, opts, facts)
        if fit is None:
            continue
        units, notes = earn(rule, card.id, amount, facts)
        if best is None or units * v > best[0]:
            best = (units * v, fit, notes, units)
    if best is not None:
        res.rule_cents, fit, notes, res.units = best
        res.rule = fit.rule
        res.caveats += fit.caveats + notes
    if opts.abroad and profile.foreign_txn_fee_pct.value:
        res.fee_cents = amount * profile.foreign_txn_fee_pct.value / 100
        res.caveats.append(f"{profile.foreign_txn_fee_pct.value:g}% foreign transaction fee")
    for c in profile.credits:
        if credit_fits(c, merchant, opts, facts):
            enrolled = facts.requirement(card.id, "enrollment", c.name)
            if c.enrollment_required and enrolled is False:
                continue
            used = facts.credit_used.get((card.id, c.identity()))
            left = c.amount_cents - (used or 0)
            if left <= 0:
                continue
            value = min(left, amount)
            counted = used is not None and facts.count_unused_credits
            per = c.period.replace("_", " ")
            res.credit = {
                "name": c.name, "amount_cents": c.amount_cents, "period": c.period,
                "value_cents": value, "counted": counted,
                "text": (f"+ ${value / 100:,.2f} {c.name} left this {per}" if used is not None
                         else f"+ up to ${value / 100:,.2f} from the {c.name} "
                              f"(${c.amount_cents / 100:,.2f} per {per}, if not used yet)"),
                "needs_enrollment": c.enrollment_required and enrolled is None,
                "evidence": evidence(c)}
            if counted:
                res.credit_cents = value
            break
    kinds = PROTECTIONS.get(merchant.category, ())
    res.benefits = [_benefit(b) for b in profile.benefits if b.kind in kinds]
    for rule in profile.earn_rules:
        fit = rule_fits(rule, card.id, merchant, opts, facts, portal=True)
        if fit is None:
            continue
        units, notes = earn(rule, card.id, amount, facts)
        if units * v > res.rule_cents:
            where = rule.portal or next(
                (n for n in rule.merchant_names if CHANNEL_NAME.search(n)), "the issuer's portal")
            res.alternatives.append({
                "text": f"Book through {where}: {rate_text(rule.rate, currency)}",
                "cents": round(units * v - res.fee_cents), "caveats": fit.caveats + notes,
                "evidence": evidence(rule)})
    res.total_cents = res.rule_cents + res.credit_cents - res.fee_cents
    return res


def _benefit(b: Benefit) -> dict[str, Any]:
    return {"kind": b.kind, "name": b.name, "limits": b.limits, "coverage": b.coverage,
            "evidence": evidence(b)}


# --------------------------------------------------------------------------- ranking


@dataclass
class Ranking:
    merchant: Merchant
    options: Options
    results: list[CardResult]
    not_accepted: list[dict[str, str]]
    break_even: str | None
    asks_channel: bool
    asks_abroad: bool

    def as_dict(self) -> dict[str, Any]:
        a = self.options.amount_cents
        return {"merchant": {"key": self.merchant.key, "display_name": self.merchant.display_name,
                             "reward_category": self.merchant.category},
                "amount_cents": a, "channel": self.options.channel, "abroad": self.options.abroad,
                "results": [r.as_dict(a) for r in self.results],
                "not_accepted": self.not_accepted, "break_even": self.break_even,
                "asks_channel": self.asks_channel, "asks_abroad": self.asks_abroad}


ACCEPTANCE_CAVEAT = {"amex", "discover"}


def rank(merchant: Merchant, cards: list[Card], profiles: dict[str, CardProfile], opts: Options,
         facts: Facts, currency_names: dict[str, str] | None = None) -> Ranking:
    results, not_accepted = [], []
    order = {c.id: i for i, c in enumerate(cards)}
    asks_channel = asks_abroad = False
    for card in cards:
        profile = profiles.get(card.id)
        if profile is None:
            continue
        accepted = merchant.acceptance.get(card.network, "unknown")
        if accepted == "no":
            not_accepted.append({"card_id": card.id, "product": card.product,
                                 "network": card.network})
            continue
        r = evaluate(card, profile, merchant, opts, facts)
        if accepted == "unknown" and card.network in ACCEPTANCE_CAVEAT:
            r.caveats.append(f"{card.network.title()} acceptance here is unknown")
        if card.id in facts.stale_days:
            r.caveats.append(f"terms last checked {facts.stale_days[card.id]} days ago")
        asks_channel |= any(rule.channel in CHANNEL_WORDS for rule in profile.earn_rules
                            if rule_fits(rule, card.id, merchant, Options(
                                opts.amount_cents, None, opts.abroad, opts.on), facts))
        asks_abroad |= profile.foreign_txn_fee_pct.value > 0 or any(
            rule.geography != "any" for rule in profile.earn_rules
            if not rule.is_base and (rule.categories or rule.merchant_names))
        results.append(r)
    results.sort(key=lambda r: (-round(r.total_cents, 6), opts.abroad and r.ftf_pct > 0,
                                -len(r.benefits), order[r.card.id]))
    for i, r in enumerate(results, 1):
        r.rank = i
    return Ranking(merchant, opts, results, not_accepted,
                   break_even(results, currency_names or {}), asks_channel, asks_abroad)


def break_even(results: list[CardResult], names: dict[str, str]) -> str | None:
    """§9 step 9: when the top two earn different currencies, the valuation where they'd tie."""
    if len(results) < 2:
        return None
    a, b = results[0], results[1]
    if a.currency == b.currency:
        return None
    if _cash(b.currency):
        # Points beat cash back: say how low the winner's points may go before cash wins.
        if _cash(a.currency) or a.units <= 0:
            return None
        tie = (b.total_cents - (a.credit_cents - a.fee_cents)) / a.units
        if not 0 < tie < a.valuation:
            return None
        cur = names.get(a.currency, a.currency.replace("_", " "))
        return (f"{a.card.product} wins as long as you value {cur} above {tie:.2f}¢ "
                f"(now {a.valuation:g}¢); below that, {b.card.product}.")
    if b.units <= 0:
        return None
    tie = (a.total_cents - (b.credit_cents - b.fee_cents)) / b.units
    if tie <= 0 or tie > BREAK_EVEN_MAX:
        return None
    cur = names.get(b.currency, b.currency.replace("_", " "))
    return (f"{a.card.product} wins unless you value {cur} above {tie:.2f}¢ "
            f"(now {b.valuation:g}¢).")
