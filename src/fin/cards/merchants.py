# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Merchant profiles: seeds, resolution, the Claude lookup and my corrections (SPEC-cards §8).

Resolution order: alias, then the merchant rules (SPEC §10), then close matches as suggestions
only. Claude (`merchant_profile`, haiku, no tools) runs only when I ask for it, once per new
merchant; the web variant only on a second, explicit request.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fin.ai import claude_cli as cc
from fin.ai.runs import new_run_dir
from fin.cards.models import NETWORKS, normalize_name
from fin.config import CONFIG_DIR, DataPaths, Settings, load_reward_categories
from fin.db import models as m
from fin.db.models import utcnow

SEEDS_PATH = CONFIG_DIR / "merchant_profiles.yaml"
SEEDS_REF = "merchant_seeds_sha256"
CLOSE_MATCH = 0.88
LOOKUP_TIMEOUT = 120
KEY = re.compile(r"^[a-z][a-z0-9_]*$")


# --------------------------------------------------------------------------- seeds


def sync_seeds(s: Session, path: Path = SEEDS_PATH, *, force: bool = False) -> bool:
    """Load config/merchant_profiles.yaml when it changed. A profile I corrected (source
    `user`) and my own aliases are never overwritten. Returns True if it loaded."""
    text = path.read_text()
    digest = hashlib.sha256(text.encode()).hexdigest()
    fact = s.scalars(select(m.CardFact).where(m.CardFact.kind == "setting",
                                              m.CardFact.ref == SEEDS_REF)).first()
    if fact is not None and fact.value == digest and not force:
        return False
    doc = yaml.safe_load(text)
    categories = {c.id for c in load_reward_categories().categories}
    s.execute(delete(m.MerchantAlias).where(m.MerchantAlias.source == "seed"))
    s.flush()
    for i, row in enumerate(doc["merchants"]):
        key = row["key"]
        if not KEY.match(key) or row["category"] not in categories:
            raise ValueError(f"merchant_profiles.yaml entry {i} ({key}): bad key or category")
        accepts = {n: row.get("accepts", {}).get(n, "unknown") for n in NETWORKS}
        p = s.get(m.MerchantProfileRow, key)
        if p is None:
            p = m.MerchantProfileRow(key=key, source="seed", display_name=row["name"])
            s.add(p)
        if p.source != "user":
            p.display_name, p.reward_category = row["name"], row["category"]
            p.mcc_guess, p.notes = row.get("mcc"), row.get("notes")
            p.acceptance_json, p.source, p.confidence = json.dumps(accepts), "seed", 1.0
            p.updated_at = utcnow()
        s.flush()
        for alias in {normalize_name(a) for a in [key.replace("_", " "), row["name"],
                                                   *row.get("aliases", [])]}:
            hit = s.get(m.MerchantAlias, alias)
            if hit is None:
                s.add(m.MerchantAlias(alias_norm=alias, profile_key=key, kind="name",
                                      source="seed"))
            elif hit.source in ("kb", "ai", "ai_web"):
                hit.profile_key, hit.source = key, "seed"
    if fact is None:
        s.add(m.CardFact(kind="setting", ref=SEEDS_REF, value=digest))
    else:
        fact.value = digest
    s.flush()
    return True


# --------------------------------------------------------------------------- views


@dataclass
class Merchant:
    key: str
    display_name: str
    reward_category: str | None
    acceptance: dict[str, str]
    source: str
    confidence: float
    mcc_guess: str | None = None
    notes: str | None = None

    @classmethod
    def of(cls, p: m.MerchantProfileRow) -> Merchant:
        acc = json.loads(p.acceptance_json or "{}")
        return cls(p.key, p.display_name, p.reward_category,
                   {n: acc.get(n, "unknown") for n in NETWORKS}, p.source, p.confidence,
                   p.mcc_guess, p.notes)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "display_name": self.display_name,
                "reward_category": self.reward_category, "acceptance": self.acceptance,
                "source": self.source, "confidence": self.confidence, "mcc_guess": self.mcc_guess,
                "notes": self.notes}


@dataclass
class Resolution:
    query: str
    merchant: Merchant | None = None
    how: str | None = None  # alias | rule | lookup | web
    suggestions: list[dict[str, str]] = field(default_factory=list)
    not_a_merchant: bool = False

    @property
    def needs_lookup(self) -> bool:
        return self.merchant is None or self.merchant.reward_category is None


def _alias(s: Session, norm: str) -> m.MerchantProfileRow | None:
    hit = s.get(m.MerchantAlias, norm)
    return s.get(m.MerchantProfileRow, hit.profile_key) if hit else None


def _remember(s: Session, norm: str, key: str, kind: str, source: str) -> None:
    hit = s.get(m.MerchantAlias, norm)
    if hit is None:
        s.add(m.MerchantAlias(alias_norm=norm, profile_key=key, kind=kind, source=source))
    elif hit.source != "user" and hit.profile_key != key:
        hit.profile_key, hit.source = key, source


def resolve(s: Session, query: str) -> Resolution:
    """Steps 1-3 of §8: no AI, no network."""
    from fin.categorize.rules import compiled_rules, first_rule
    from fin.normalize.merchants import clean_description

    res = Resolution(query)
    norm = normalize_name(query)
    if not norm:
        return res
    p = _alias(s, norm)
    if p is not None:
        res.merchant, res.how = Merchant.of(p), "alias"
        return res
    rule = first_rule(compiled_rules(s), clean_description(query))
    if rule is not None:
        p = s.get(m.MerchantProfileRow, rule.merchant_group) if rule.merchant_group else None
        if p is None and rule.merchant_name:
            p = _alias(s, normalize_name(rule.merchant_name))
        if p is not None:
            res.merchant, res.how = Merchant.of(p), "rule"
            return res
    aliases = s.scalars(select(m.MerchantAlias.alias_norm)).all()
    for a in difflib.get_close_matches(norm, aliases, n=4, cutoff=CLOSE_MATCH):
        p = _alias(s, a)
        if p is not None and all(x["key"] != p.key for x in res.suggestions):
            res.suggestions.append({"key": p.key, "display_name": p.display_name, "alias": a})
    # Whole-word containment ("shell on main st" -> "shell") is offered, never applied.
    words = f" {norm} "
    for a in sorted(aliases, key=len, reverse=True):
        if len(a) >= 3 and f" {a} " in words and len(res.suggestions) < 4:
            p = _alias(s, a)
            if p is not None and all(x["key"] != p.key for x in res.suggestions):
                res.suggestions.append({"key": p.key, "display_name": p.display_name,
                                        "alias": a})
    return res


def autocomplete(s: Session, q: str, limit: int = 8) -> list[dict[str, str]]:
    norm = normalize_name(q)
    if not norm:
        return []
    out: list[dict[str, str]] = []
    rows = s.execute(select(m.MerchantAlias.alias_norm, m.MerchantProfileRow.key,
                            m.MerchantProfileRow.display_name)
                     .join(m.MerchantProfileRow,
                           m.MerchantProfileRow.key == m.MerchantAlias.profile_key)
                     .where(m.MerchantAlias.alias_norm.like(f"%{norm}%"))
                     .where(m.MerchantProfileRow.reward_category.is_not(None))).all()
    rows = sorted(rows, key=lambda r: (not r[0].startswith(norm), len(r[0])))
    for alias, key, name in rows:
        if all(o["key"] != key for o in out):
            out.append({"key": key, "display_name": name, "alias": alias})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- Claude


class NotAMerchantError(ValueError):
    pass


def lookup(s: Session, paths: DataPaths, settings: Settings, query: str, *, web: bool = False,
           lock_wait: float | None = None) -> Resolution:
    """§8 steps 4-5: one merchant_profile call for one query. Only the query goes to Claude."""
    norm = normalize_name(query)
    if not norm:
        raise NotAMerchantError("empty query")
    known = s.scalars(select(m.MerchantProfileRow).where(
        m.MerchantProfileRow.reward_category.is_not(None))).all()
    payload = {
        "records": [{"key": "q1", "query": query[:120]}],
        "known": [{"key": p.key, "name": p.display_name, "reward_category": p.reward_category}
                  for p in known],
        "reward_categories": [{"id": c.id, "description": c.description}
                              for c in load_reward_categories().categories],
    }
    name = "merchant_profile_web" if web else "merchant_profile"
    task = cc.load_task(name, schema_name="merchant_profile")
    run = new_run_dir(paths.ai_runs, name)
    try:
        res = cc.run_task(task, payload, run, timeout=LOOKUP_TIMEOUT, lock_wait=lock_wait,
                          invocation=cc.merchant_invocation(task, web=web, effort="low"))
    except cc.AIError as e:
        status = "limited" if isinstance(e, cc.UsageLimitError) else "failed"
        s.add(m.AIRun(task=name, run_dir=str(run), n_items=1, status=status, error=str(e)[:500]))
        s.commit()
        raise
    s.add(m.AIRun(task=name, run_dir=str(run), n_items=1, status="ok",
                  duration_ms=res.duration_ms))
    results = [r for r in res.output["results"] if r["key"] == "q1"]
    if not results:
        raise cc.AIError("merchant_profile returned no result for the query")
    out = Resolution(query, how="web" if web else "lookup")
    target = apply_result(s, norm, results[0], "ai_web" if web else "ai", task.prompt_version,
                          kind="query")
    if target is None:
        out.not_a_merchant = True
        s.commit()
        return out
    s.commit()
    out.merchant = Merchant.of(target)
    return out


def apply_result(s: Session, norm: str, r: dict[str, Any], source: str, prompt_version: str,
                 *, kind: str) -> m.MerchantProfileRow | None:
    """Store one merchant_profile result: a match adds `norm` as an alias of that profile, a new
    merchant becomes a profile (an uncategorized `kb` one from the documents is filled in).
    Seed and user profiles are never changed. Returns None for "not a merchant"."""
    if not r["is_merchant"]:
        return None
    target = s.get(m.MerchantProfileRow, r["match_key"]) if r["match_key"] else None
    new = r["new"]
    if target is None and new:
        target = s.get(m.MerchantProfileRow, new["key"]) or _alias(s, norm)
        if target is None:
            target = m.MerchantProfileRow(key=new["key"], display_name=new["display_name"],
                                          source=source)
            s.add(target)
        if target.source not in ("user", "seed"):
            target.display_name = new["display_name"] or target.display_name
            target.reward_category, target.mcc_guess = new["reward_category"], new["mcc_guess"]
            target.acceptance_json, target.notes = json.dumps(new["acceptance"]), new["notes"]
            target.source, target.confidence = source, r["confidence"]
            target.prompt_version, target.model = prompt_version, "haiku"
            target.updated_at = utcnow()
        s.flush()
    if target is None:
        return None
    _remember(s, norm, target.key, kind, source)
    return target


# --------------------------------------------------------------------------- corrections


def correct(s: Session, key: str, *, category: str | None = None,
            acceptance: dict[str, str] | None = None, alias: str | None = None) -> Merchant:
    """My correction: the profile becomes source `user`, which nothing overwrites (seeds,
    lookups, refreshes and `fin rebuild` all leave it alone)."""
    p = s.get(m.MerchantProfileRow, key)
    if p is None:
        raise KeyError(key)
    if category is not None:
        if category not in {c.id for c in load_reward_categories().categories}:
            raise ValueError(f"unknown reward category {category!r}")
        p.reward_category = category
    if acceptance:
        acc = json.loads(p.acceptance_json or "{}")
        for net, val in acceptance.items():
            if net not in NETWORKS or val not in ("yes", "no", "unknown"):
                raise ValueError(f"bad acceptance {net}={val}")
            acc[net] = val
        p.acceptance_json = json.dumps(acc)
    p.source, p.confidence, p.updated_at = "user", 1.0, utcnow()
    if alias:
        norm = normalize_name(alias)
        hit = s.get(m.MerchantAlias, norm)
        if hit is None:
            s.add(m.MerchantAlias(alias_norm=norm, profile_key=key, kind="query", source="user"))
        else:
            hit.profile_key, hit.source = key, "user"
    s.commit()
    return Merchant.of(p)


def name_keys(s: Session) -> dict[str, tuple[str, str]]:
    """Normalized alias -> (profile key, profile source), for mapping the merchant names in
    card documents onto merchant profiles at ranking time."""
    rows = s.execute(select(m.MerchantAlias.alias_norm, m.MerchantProfileRow.key,
                            m.MerchantProfileRow.source)
                     .join(m.MerchantProfileRow,
                           m.MerchantProfileRow.key == m.MerchantAlias.profile_key)).all()
    return {a: (k, src) for a, k, src in rows}
