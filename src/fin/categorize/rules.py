"""Merchant rules and category precedence (SPEC §10, §13).

Precedence: user override (per transaction) > user rule > seed rule > ai_cache > AI batch >
uncategorized + review item. Seed rules are reloaded from config/rules.yaml on every run;
user rules live only in the DB (created from the dashboard) and survive `fin rebuild`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fin.config import CONFIG_DIR, Taxonomy
from fin.db import models as m

RULES_PATH = CONFIG_DIR / "rules.yaml"
# Excluded from spending (§13): they get no category.
NON_SPENDING = {"payment", "credit", "reward", "adjustment"}


@dataclass(frozen=True)
class CompiledRule:
    id: int
    regex: re.Pattern
    merchant_name: str | None
    merchant_group: str | None
    category_id: str | None
    created_by: str


def load_seed_rules(s: Session, taxonomy: Taxonomy, path: Path = RULES_PATH) -> int:
    doc = yaml.safe_load(path.read_text())
    s.execute(delete(m.Rule).where(m.Rule.created_by == "seed"))
    ids = set(taxonomy.ids)
    for i, r in enumerate(doc["rules"]):
        if r.get("category") and r["category"] not in ids:
            raise ValueError(f"rules.yaml rule {i}: unknown category {r['category']!r}")
        re.compile(r["pattern"])
        s.add(m.Rule(priority=1000 + i, match_field="description_clean", pattern=r["pattern"],
                     merchant_name=r.get("merchant"), merchant_group=r.get("group"),
                     category_id=r.get("category"), created_by="seed"))
    s.flush()
    return len(doc["rules"])


def compiled_rules(s: Session) -> list[CompiledRule]:
    rows = s.scalars(select(m.Rule).order_by(
        (m.Rule.created_by != "user"), m.Rule.priority, m.Rule.id)).all()
    return [CompiledRule(r.id, re.compile(r.pattern, re.IGNORECASE), r.merchant_name,
                         r.merchant_group, r.category_id, r.created_by) for r in rows]


def first_rule(rules: list[CompiledRule], description_clean: str) -> CompiledRule | None:
    for r in rules:
        if r.regex.match(description_clean):
            return r
    return None


def merchant_for(s: Session, name: str, group: str | None, category: str | None,
                 cache: dict[str, m.Merchant]) -> m.Merchant:
    if name in cache:
        return cache[name]
    merchant = s.scalar(select(m.Merchant).where(m.Merchant.name == name))
    if merchant is None:
        merchant = m.Merchant(name=name, merchant_group=group, default_category_id=category)
        s.add(merchant)
        s.flush()
    else:
        merchant.merchant_group = group or merchant.merchant_group
        merchant.default_category_id = category or merchant.default_category_id
    cache[name] = merchant
    return merchant


def apply_rules(s: Session) -> tuple[int, list[m.Transaction]]:
    """Set merchant and category from rules. Returns (matched, transactions left for AI)."""
    rules = compiled_rules(s)
    cache: dict[str, m.Merchant] = {}
    matched, leftovers = 0, []
    for t in s.scalars(select(m.Transaction)).all():
        if t.category_source == "user":
            continue  # matching re-applies item splits after this step
        r = first_rule(rules, t.description_clean)
        if r is not None:
            merchant = merchant_for(s, r.merchant_name or t.description_clean, r.merchant_group,
                                    r.category_id, cache)
            t.merchant_id = merchant.id
            if t.type in NON_SPENDING:
                t.category_id, t.category_source = None, None
            else:
                t.category_id, t.category_source = r.category_id, "rule"
            matched += 1
        elif t.type not in NON_SPENDING:
            leftovers.append(t)
        else:
            t.category_id, t.category_source = None, None
    return matched, leftovers


def item_stable_key(order: m.Order, item: m.OrderItem) -> str:
    """merchant|order id|sku (or line number): survives `fin rebuild` (SPEC §8 user_overrides)."""
    return f"{order.merchant}|{order.external_id}|{item.sku or f'line{item.line_no}'}"


def apply_overrides(s: Session) -> int:
    """Per-transaction user corrections, keyed by dedupe_key so they survive rebuilds."""
    n = 0
    for o in s.scalars(select(m.UserOverride).where(m.UserOverride.target_type == "transaction")):
        t = s.scalar(select(m.Transaction).where(m.Transaction.dedupe_key == o.stable_key))
        if t is None:
            continue
        if o.field == "category_id":
            t.category_id, t.category_source = o.value, "user"
        elif o.field == "person_id":
            t.person_id, t.person_source = o.value, "user"
        n += 1
    items = {o.stable_key: o.value for o in s.scalars(select(m.UserOverride).where(
        m.UserOverride.target_type == "order_item", m.UserOverride.field == "category_id"))}
    if items:
        for item, order in s.execute(select(m.OrderItem, m.Order).join(
                m.Order, m.Order.id == m.OrderItem.order_id)):
            value = items.get(item_stable_key(order, item))
            if value:
                item.category_id, item.category_source = value, "user"
                n += 1
    return n
