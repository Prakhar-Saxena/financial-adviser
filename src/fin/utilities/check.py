# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`fin utilities check` (docs/SPEC-utilities.md §11): linking changes no transaction, spending
equals the snapshot in debug/, a payment links to at most one account, and each account's
series adds up to its linked payments. Results give counts, never amounts or rows."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fin.api.spending import CREDIT_TYPES, spending_lines, txn_month
from fin.config import CardsConfig, DataPaths, Settings
from fin.db import models as m
from fin.utilities.payments import LINKED_TYPES
from fin.utilities.series import relink, utilities

SNAPSHOT = "utilities-spending.json"


@dataclass
class Check:
    name: str
    ok: bool | None  # None: skipped
    detail: str


def spending_snapshot(s: Session) -> dict:
    """Spending by month, card and category; credits by month; count and sum by type."""
    out: dict[str, dict] = {"by_month_card_category": defaultdict(int),
                            "credits_by_month": {}, "types_by_month": {}}
    for ln in spending_lines(s):
        out["by_month_card_category"][f"{ln.month}|{ln.card_id}|{ln.category_id}"] += \
            ln.amount_cents
    for month, n in s.execute(select(txn_month(), func.sum(m.Transaction.amount_cents))
                              .where(m.Transaction.type.in_(CREDIT_TYPES))
                              .group_by(txn_month())):
        out["credits_by_month"][month] = n
    for month, typ, n, cents in s.execute(
            select(txn_month(), m.Transaction.type, func.count(),
                   func.sum(m.Transaction.amount_cents))
            .group_by(txn_month(), m.Transaction.type)):
        out["types_by_month"][f"{month}|{typ}"] = [n, cents]
    return json.loads(json.dumps(out, sort_keys=True))


def transactions_hash(s: Session) -> str:
    rows = s.execute(select(m.Transaction.__table__).order_by(m.Transaction.id)).all()
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()


def snapshot_path(paths: DataPaths) -> Path:
    return paths.debug / SNAPSHOT


def write_snapshot(s: Session, paths: DataPaths) -> Path:
    p = snapshot_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(spending_snapshot(s), indent=1, sort_keys=True))
    p.chmod(0o600)
    return p


def run_checks(s: Session, cards: CardsConfig, settings: Settings, paths: DataPaths,
               today: date | None = None) -> list[Check]:
    out = []
    before = transactions_hash(s)
    relink(s, cards, settings, today)
    out.append(Check("Linking changes no transaction", transactions_hash(s) == before,
                     "transactions identical before and after linking"))

    p = snapshot_path(paths)
    if p.exists():
        then, now = json.loads(p.read_text()), spending_snapshot(s)
        diff = sum(then[k].get(x) != now[k].get(x) for k in then for x in {*then[k], *now[k]})
        out.append(Check("Spending equals the snapshot", diff == 0,
                         f"{sum(len(v) for v in now.values())} figures by month, card, "
                         f"category and type" + (f"; {diff} differ" if diff else "")))
    else:
        out.append(Check("Spending equals the snapshot", None,
                         f"no snapshot yet; `fin utilities check --snapshot` writes {p.name}"))

    links = s.scalar(select(func.count()).select_from(m.UtilityPayment)) or 0
    distinct = s.scalar(select(func.count(func.distinct(m.UtilityPayment.transaction_id)))) or 0
    wrong_type = s.scalar(select(func.count()).select_from(m.UtilityPayment).join(
        m.Transaction, m.Transaction.id == m.UtilityPayment.transaction_id).where(
        m.Transaction.type.not_in(LINKED_TYPES))) or 0
    out.append(Check("One account per payment", links == distinct and not wrong_type,
                     f"{links} links, {distinct} payments"
                     + (f"; {wrong_type} not purchases or refunds" if wrong_type else "")))

    res = utilities(s, settings, today, months=1)
    linked = Counter(s.scalars(select(m.UtilityPayment.account_id)))
    bad = [a for a, v in res.accounts.items()
           if sum(v.series.values()) != sum(pl.payment.amount_cents for pl in v.placed)
           or len(v.placed) != linked[a]]
    out.append(Check("Each series adds up to its payments", not bad,
                     f"{len(res.accounts)} accounts" + (f"; off: {bad}" if bad else "")))
    return out
