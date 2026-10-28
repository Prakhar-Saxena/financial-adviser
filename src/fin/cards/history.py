# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""My data: merchant history, missed rewards, cap use, credits used, cheat sheet (SPEC-cards §10).

All computed locally from the ledger. The only AI is `resolve_history`'s batched merchant_profile
call for descriptors no alias or rule knows, and it sends only the cleaned descriptors.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fin.ai import claude_cli as cc
from fin.cards import engine
from fin.cards.merchants import Merchant, apply_result, resolve, sync_seeds
from fin.cards.models import CardProfile, Credit, normalize_name
from fin.config import CardsConfig, DataPaths, Settings, load_reward_categories
from fin.db import models as m

PURCHASE = "purchase"
# A credit's "nothing posted yet" is believed only when the card's data is this recent: credits
# post days after the purchase, and a stale import would make every credit look unused.
FRESH_DAYS = 10


def _day(t: m.Transaction) -> date:
    return date.fromisoformat(t.txn_date or t.post_date)


# --------------------------------------------------------------------------- history step


@dataclass
class HistoryResult:
    descriptors: int = 0
    resolved: int = 0
    asked: int = 0
    calls: int = 0
    paused: str | None = None


def resolve_history(s: Session, paths: DataPaths, settings: Settings, *, use_ai: bool = True,
                    runner: Any = None) -> HistoryResult:
    """`fin process` step (§8 History): give every purchase descriptor a merchant profile.

    Aliases and rules first; the rest go to merchant_profile in batches, with only the cleaned
    descriptor in each record. Results become `descriptor` aliases (and are cached), so a second
    run makes no calls."""
    from fin.categorize.ai import AIResult, classify_keys

    sync_seeds(s)
    out = HistoryResult()
    descs = sorted(set(s.scalars(select(m.Transaction.description_clean).where(
        m.Transaction.type == PURCHASE))))
    out.descriptors = len(descs)
    todo: dict[str, dict[str, str]] = {}
    for d in descs:
        norm = normalize_name(d)
        if not norm or s.get(m.MerchantAlias, norm) is not None:
            out.resolved += 1
            continue
        r = resolve(s, d)
        if r.merchant is not None:
            s.add(m.MerchantAlias(alias_norm=norm, profile_key=r.merchant.key, kind="descriptor",
                                  source="rule"))
            s.flush()
            out.resolved += 1
        else:
            todo[d] = {"key": d, "query": d}
    s.commit()
    if not todo:
        return out
    out.asked = len(todo)
    task = cc.load_task("merchant_profile")
    known = [{"key": p.key, "name": p.display_name, "reward_category": p.reward_category}
             for p in s.scalars(select(m.MerchantProfileRow).where(
                 m.MerchantProfileRow.reward_category.is_not(None)))]
    categories = [{"id": c.id, "description": c.description}
                  for c in load_reward_categories().categories]

    def call(t, payload, run_dir):
        return cc.run_task(t, {**payload, "known": known, "reward_categories": categories},
                           run_dir, timeout=settings.ai.timeout_seconds,
                           invocation=cc.merchant_invocation(t, effort="low"))

    res = AIResult()
    outputs = classify_keys(s, paths, settings, "merchant_profile", todo, use_ai=use_ai,
                            result=res, runner=runner or call)
    out.calls, out.paused = res.calls, res.paused
    for d, r in outputs.items():
        if apply_result(s, normalize_name(d), r, "ai", task.prompt_version,
                        kind="descriptor") is not None:
            out.resolved += 1
    s.commit()
    return out


# --------------------------------------------------------------------------- shared lookups


def merchant_index(s: Session) -> tuple[dict[str, str], dict[str, Merchant]]:
    """(normalized alias -> profile key, profile key -> Merchant)."""
    aliases = dict(s.execute(select(m.MerchantAlias.alias_norm, m.MerchantAlias.profile_key)).all())
    profiles = {p.key: Merchant.of(p) for p in s.scalars(select(m.MerchantProfileRow))}
    return aliases, profiles


def merchant_of(t: m.Transaction, aliases: dict[str, str], profiles: dict[str, Merchant]
                ) -> Merchant | None:
    key = aliases.get(normalize_name(t.description_clean))
    return profiles.get(key) if key else None


def as_engine(mp: Merchant) -> engine.Merchant:
    return engine.Merchant(mp.key, mp.display_name, mp.reward_category or "other", mp.acceptance)


def anniversary_months(s: Session) -> dict[str, int]:
    return {x.card_id: int(x.value) for x in s.scalars(select(m.CardFact).where(
        m.CardFact.kind == "anniversary_month")) if x.card_id}


def period_window(period: str, today: date, anniversary: int | None = None
                  ) -> tuple[date, date, str, bool]:
    """(start, end inclusive, period key, approximate) of the period containing `today`.
    A cardmember year without my anniversary month falls back to the calendar year."""
    y = today.year
    if period in ("month", "statement"):
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return start, end, f"{y}-{today.month:02d}", period == "statement"
    if period == "quarter":
        q = (today.month - 1) // 3
        start = date(y, 3 * q + 1, 1)
        end = (date(y + (q == 3), (3 * q + 3) % 12 + 1, 1)) - timedelta(days=1)
        return start, end, f"{y}-Q{q + 1}", False
    if period == "half_year":
        h = 0 if today.month <= 6 else 1
        return date(y, 1 + 6 * h, 1), date(y, 6, 30) if h == 0 else date(y, 12, 31), \
            f"{y}-H{h + 1}", False
    if period == "cardmember_year" and anniversary:
        start = date(y if today.month >= anniversary else y - 1, anniversary, 1)
        end = date(start.year + 1, anniversary, 1) - timedelta(days=1)
        return start, end, f"cm{start.isoformat()}", False
    if period == "multi_year":
        return date(y - 3, 1, 1), date(y, 12, 31), f"{y - 3}-{y}", True
    if period == "one_time":
        return date(2000, 1, 1), date(2999, 12, 31), "once", False
    return date(y, 1, 1), date(y, 12, 31), str(y), period == "cardmember_year"


# --------------------------------------------------------------------------- cap use


@dataclass
class CapUse:
    card_id: str
    key: str
    label: str
    cap_cents: int
    used_cents: int
    period: str
    start: date
    end: date
    approximate: bool


def cap_use(s: Session, profiles: dict[str, CardProfile], facts: engine.Facts,
            today: date | None = None) -> list[CapUse]:
    """Spend so far in each capped rule's period, on that card, at merchants the rule covers.
    Rules that share a cap share one row."""
    today = today or date.today()
    aliases, merchants = merchant_index(s)
    anniversary = anniversary_months(s)
    out: dict[tuple[str, str], CapUse] = {}
    counted: dict[tuple[str, str], set[int]] = defaultdict(set)  # a shared cap counts once
    for card_id, p in profiles.items():
        capped = [r for r in p.earn_rules if r.cap]
        if not capped:
            continue
        txns = s.scalars(select(m.Transaction).where(
            m.Transaction.card_id == card_id, m.Transaction.type == PURCHASE)).all()
        for rule in capped:
            key = engine.cap_key(rule)
            start, end, _, approx = period_window(rule.cap.period, today, anniversary.get(card_id))
            row = out.setdefault((card_id, key), CapUse(
                card_id, key, f"{engine.rate_text(rule.rate, p.currency)} on "
                f"{engine.scope_text(rule)}", rule.cap.amount_cents, 0, rule.cap.period, start,
                end, approx))
            seen = counted[(card_id, key)]
            for t in txns:
                if t.id in seen or not start <= _day(t) <= end:
                    continue
                mp = merchant_of(t, aliases, merchants)
                if mp is None or mp.reward_category is None:
                    continue
                opts = engine.Options(t.amount_cents, None, False, _day(t))
                if engine.rule_fits(rule, card_id, as_engine(mp), opts, facts) is not None:
                    row.used_cents += t.amount_cents
                    seen.add(t.id)
    return list(out.values())


# --------------------------------------------------------------------------- credits used

STOP = {"CREDIT", "CREDITS", "STATEMENT", "ANNUAL", "MONTHLY", "CARD", "CARDS", "AMEX",
        "AMERICAN", "EXPRESS", "CHASE", "CITI", "PLATINUM", "GOLD", "PLUS", "WITH", "YOUR",
        "EACH", "THE", "AND", "FOR", "POINTS", "MEMBERSHIP", "REWARDS", "PURCHASES"}


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[A-Z0-9]+", text.upper().replace("'", ""))
            if len(w) >= 4 and w not in STOP}


@dataclass
class CreditUse:
    card_id: str
    product: str
    credit: Credit
    identity: str
    period_key: str
    start: date
    end: date
    used_cents: int
    known: bool
    how: str  # posted | manual | unknown
    matched: list[int] = field(default_factory=list)
    enrolled: bool | None = None
    approximate: bool = False
    as_of: date | None = None  # the card's latest posted transaction
    fresh: bool = False  # data recent enough for "unused" to be believed in the ranking

    def as_dict(self, today: date) -> dict[str, Any]:
        c = self.credit
        return {"card_id": self.card_id, "product": self.product, "name": c.name,
                "identity": self.identity, "amount_cents": c.amount_cents, "period": c.period,
                "period_key": self.period_key, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "days_left": max((self.end - today).days, 0),
                "used_cents": min(self.used_cents, c.amount_cents), "known": self.known,
                "how": self.how, "matched_transactions": len(self.matched),
                "enrollment_required": c.enrollment_required, "enrolled": self.enrolled,
                "enroll_ref": normalize_name(c.name),
                "posts_as": c.posts_as, "approximate": self.approximate,
                "as_of": self.as_of.isoformat() if self.as_of else None, "fresh": self.fresh}


def _facts_by(s: Session, kind: str) -> dict[tuple[str | None, str, str | None], str]:
    return {(x.card_id, x.ref, x.period): x.value
            for x in s.scalars(select(m.CardFact).where(m.CardFact.kind == kind))}


def credit_use(s: Session, profiles: dict[str, CardProfile], cards: CardsConfig,
               facts: engine.Facts, today: date | None = None) -> list[CreditUse]:
    """Each credit's use this period, from posted `credit` transactions on the card matched by
    keywords from the credit's name and merchants (plus my `credit_alias` keywords). A credit
    that doesn't post as a statement credit (an in-app balance) is unknown unless I mark it."""
    today = today or date.today()
    anniversary = anniversary_months(s)
    aliases = defaultdict(set)
    for (card_id, ref, _), value in _facts_by(s, "credit_alias").items():
        aliases[(card_id, ref)] |= tokens(value) | {value.upper()}
    manual = _facts_by(s, "credit_used")
    out: list[CreditUse] = []
    for card in cards.cards:
        p = profiles.get(card.id)
        if p is None or not p.credits:
            continue
        keywords = {c.identity(): tokens(c.name) | tokens(" ".join(c.merchant_names))
                    | aliases.get((card.id, c.identity()), set()) for c in p.credits}
        posted = s.scalars(select(m.Transaction).where(
            m.Transaction.card_id == card.id, m.Transaction.type == "credit")).all()
        latest = s.scalar(select(func.max(m.Transaction.post_date)).where(
            m.Transaction.card_id == card.id))
        as_of = date.fromisoformat(latest) if latest else None
        fresh = as_of is not None and (today - as_of).days <= FRESH_DAYS
        # Each posted credit goes to the credit it shares the most keywords with.
        assigned: dict[str, list[m.Transaction]] = defaultdict(list)
        for t in posted:
            words = tokens(t.description_clean) | {t.description_clean.upper()}
            best = max(((len(words & kw), ident) for ident, kw in keywords.items()),
                       default=(0, ""))
            if best[0] > 0:
                assigned[best[1]].append(t)
        for c in p.credits:
            ident = c.identity()
            start, end, pkey, approx = period_window(c.period, today, anniversary.get(card.id))
            hits = [t for t in assigned.get(ident, []) if start <= _day(t) <= end]
            row = CreditUse(card.id, card.product, c, ident, pkey, start, end,
                            sum(-t.amount_cents for t in hits), c.posts_as == "statement_credit",
                            "posted" if c.posts_as == "statement_credit" else "unknown",
                            [t.id for t in hits], facts.requirement(card.id, "enrollment", c.name),
                            approx, as_of, fresh)
            said = manual.get((card.id, ident, pkey))
            if said is not None:
                row.used_cents = c.amount_cents if said == "full" else (
                    0 if said == "none" else int(said))
                row.known, row.how, row.fresh = True, "manual", True
            out.append(row)
    return out


def credit_used_map(uses: list[CreditUse]) -> dict[tuple[str, str], int]:
    """For the ranking: only credits whose use is known from recent data (or that I marked)."""
    return {(u.card_id, u.identity): u.used_cents for u in uses if u.known and u.fresh}


# --------------------------------------------------------------------------- missed rewards


def earn_only(facts: engine.Facts) -> engine.Facts:
    """The same facts with no credit counted: the look back and the cheat sheet compare earn
    rates, not this period's unused credits."""
    from dataclasses import replace

    return replace(facts, credit_used={})


def tips(s: Session, settings: Settings, cards: CardsConfig, profiles: dict[str, CardProfile],
         facts: engine.Facts, months: int, today: date | None = None) -> dict[str, Any]:
    """For purchases in the last `months`: the card used against the best eligible card at
    current terms, grouped by merchant. An estimate: credits and caps are left out."""
    today = today or date.today()
    facts = earn_only(facts)
    since = today - timedelta(days=round(months * 30.44))
    aliases, merchants = merchant_index(s)
    ecards = [engine.Card(c.id, c.product, c.network) for c in cards.cards]
    by_id = {c.id: c for c in ecards}
    names = {c.id: c.product for c in cards.cards}
    groups: dict[str, dict[str, Any]] = {}
    unknown = Counter()
    low = settings.ai.low_confidence
    for t in s.scalars(select(m.Transaction).where(m.Transaction.type == PURCHASE)):
        day = _day(t)
        if day < since or day > today or t.card_id not in profiles:
            continue
        mp = merchant_of(t, aliases, merchants)
        if mp is None or mp.reward_category is None:
            unknown[t.description_clean] += 1
            continue
        opts = engine.Options(t.amount_cents, None, False, day)
        merchant = as_engine(mp)
        used = engine.evaluate(by_id[t.card_id], profiles[t.card_id], merchant, opts, facts)
        ranking = engine.rank(merchant, ecards, profiles, opts, facts)
        best = ranking.results[0]
        g = groups.setdefault(mp.key, {
            "merchant": mp.display_name, "key": mp.key, "category": mp.reward_category,
            "unsure": mp.confidence < low, "purchases": 0, "spend_cents": 0,
            "cards_used": Counter(), "better": Counter(), "used_cents": 0.0, "best_cents": 0.0,
            "examples": []})
        g["purchases"] += 1
        g["spend_cents"] += t.amount_cents
        g["cards_used"][t.card_id] += 1
        g["better"][best.card.id] += 1
        g["used_cents"] += used.rule_cents - used.fee_cents
        g["best_cents"] += best.rule_cents - best.fee_cents
        if len(g["examples"]) < 5:
            g["examples"].append({"transaction_id": t.id, "date": day.isoformat(),
                                  "amount_cents": t.amount_cents, "card_id": t.card_id,
                                  "used_cents": round(used.rule_cents - used.fee_cents, 2),
                                  "best_card_id": best.card.id,
                                  "best_cents": round(best.rule_cents - best.fee_cents, 2)})
    rows = []
    for g in groups.values():
        better = g["better"].most_common(1)[0][0]
        used_cards = [c for c, _ in g["cards_used"].most_common()]
        diff = g["best_cents"] - g["used_cents"]
        rows.append({
            "merchant": g["merchant"], "key": g["key"], "category": g["category"],
            "unsure": g["unsure"], "purchases": g["purchases"], "spend_cents": g["spend_cents"],
            "cards_used": [{"card_id": c, "product": names[c], "purchases": g["cards_used"][c]}
                           for c in used_cards],
            "better_card_id": better, "better_product": names[better],
            "earned_cents": round(g["used_cents"]), "possible_cents": round(g["best_cents"]),
            "difference_cents": round(diff), "examples": g["examples"],
            "text": _tip_text(names, better, g["merchant"], g["cards_used"]) if diff >= 1
            else None})
    rows.sort(key=lambda r: -r["difference_cents"])
    top = [r for r in rows if r["difference_cents"] >= 1 and not r["unsure"]][:10]
    unsure = [r for r in rows if r["unsure"] and r["difference_cents"] >= 1]
    return {"months": months, "since": since.isoformat(), "until": today.isoformat(),
            "estimate": "estimate, at current terms (credits and caps not counted)",
            "top": top, "unsure": unsure,
            "total_difference_cents": sum(r["difference_cents"] for r in top),
            "merchants": rows, "unknown_descriptors": len(unknown),
            "unknown_purchases": sum(unknown.values())}


def _tip_text(names: dict[str, str], better: str, merchant: str, used: Counter) -> str:
    others = [c for c, _ in used.most_common() if c != better]
    if not others:
        return f"Keep using {names[better]} at {merchant}"
    n_other = sum(used[c] for c in others)
    if better in used:
        on = " / ".join(names[c] for c in others)
        return (f"Use {names[better]} at {merchant} every time ({n_other} of "
                f"{n_other + used[better]} purchases were on {on})")
    return f"Use {names[better]} at {merchant} instead of {' / '.join(names[c] for c in others)}"


# --------------------------------------------------------------------------- cheat sheet

CHEAT_CATEGORIES = ("dining", "supermarkets", "gas_stations", "ev_charging", "airlines", "hotels",
                    "car_rental", "travel_agencies", "drugstores", "food_delivery",
                    "rideshare_taxi", "transit", "streaming_tv", "wholesale_clubs",
                    "discount_stores", "electronics", "home_improvement", "other")
CHEAT_MERCHANTS = ("amazon", "whole_foods", "costco_warehouse", "costco_gas", "costco_online",
                   "uber", "uber_eats", "lyft", "doordash", "target", "walmart")


def cheatsheet(s: Session, cards: CardsConfig, profiles: dict[str, CardProfile],
               facts: engine.Facts, amount_cents: int, today: date | None = None
               ) -> dict[str, Any]:
    today = today or date.today()
    facts = earn_only(facts)
    ecards = [engine.Card(c.id, c.product, c.network) for c in cards.cards]
    names = {c.id: c.name for c in load_reward_categories().categories}
    opts = engine.Options(amount_cents, None, False, today)

    def line(merchant: engine.Merchant, label: str) -> dict[str, Any]:
        r = engine.rank(merchant, ecards, profiles, opts, facts)
        top = r.results[0] if r.results else None
        second = r.results[1] if len(r.results) > 1 else None
        return {"label": label, "key": merchant.key,
                "card_id": top.card.id if top else None,
                "product": top.card.product if top else None,
                "cents_per_dollar": round(top.rule_cents / amount_cents * 100, 2) if top else 0,
                "rule": top.as_dict(amount_cents)["rule"]["text"] if top and top.rule else None,
                "caveats": top.caveats if top else [],
                "runner_up": second.card.product if second else None,
                "runner_up_cents_per_dollar": round(second.rule_cents / amount_cents * 100, 2)
                if second else None}

    categories = [line(engine.Merchant(f"category:{c}", names.get(c, c), c), names.get(c, c))
                  for c in CHEAT_CATEGORIES]
    merchants = []
    for key in CHEAT_MERCHANTS:
        p = s.get(m.MerchantProfileRow, key)
        if p is not None and p.reward_category:
            merchants.append(line(as_engine(Merchant.of(p)), p.display_name))
    return {"amount_cents": amount_cents, "categories": categories, "merchants": merchants}


def mark_credit(s: Session, card_id: str, identity: str, *, period_key: str | None = None,
                used: str | None = None, alias: str | None = None) -> None:
    """My fixes (§10): `used` = full | none | cents for one period (None clears it); `alias` = a
    keyword from the statement line that means this credit (empty clears it)."""
    from fin.cards.best import set_fact

    if used is not None or period_key is not None:
        if used not in (None, "full", "none") and not str(used).isdigit():
            raise ValueError("used is full, none or a number of cents")
        if not period_key:
            raise ValueError("period_key is required to mark a credit used")
        row = s.scalars(select(m.CardFact).where(
            m.CardFact.kind == "credit_used", m.CardFact.card_id == card_id,
            m.CardFact.ref == identity, m.CardFact.period == period_key)).first()
        if used is None:
            if row is not None:
                s.delete(row)
        elif row is None:
            s.add(m.CardFact(kind="credit_used", card_id=card_id, ref=identity,
                             period=period_key, value=str(used)))
        else:
            row.value = str(used)
    if alias is not None:
        set_fact(s, "credit_alias", identity, alias.strip().upper() or None, card_id)
    s.commit()
