# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the Utilities tab shows (docs/SPEC-utilities.md §9, §10), built from the same engine
result as `fin utilities status` and `trends`, so the two always agree. Reads only."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import String, select
from sqlalchemy.orm import Session

from fin.config import CardsConfig, Settings
from fin.db import models as m
from fin.utilities.engine import (
    FIRST_PAYMENT_SHOWN_WHILE,
    AccountView,
    Insight,
    Result,
    add_months,
    month_of,
    month_range,
)
from fin.utilities.series import utilities

# Services in the order the tab lists and colours them.
SERVICE_ORDER = ("electric", "gas", "water", "trash", "internet", "phone", "other")
FLAT_FOR_BEST_CARD = ("internet", "phone")


def paid_from_name(cards: CardsConfig, card_id: str) -> str:
    try:
        return cards.account_name(card_id)
    except KeyError:
        return card_id


def insight_row(i: Insight) -> dict[str, Any]:
    return asdict(i)


def _sum(series: dict[str, int], months: list[str]) -> int:
    return sum(series.get(m, 0) for m in months)


def _first_month(res: Result) -> str | None:
    return min(res.total) if res.total else None


def _year_ago(series: dict[str, int], first: str | None, until: str) -> dict[str, int | None]:
    """Last 12 months and the 12 before, when the ledger reaches back that far."""
    last = month_range(add_months(until, -11), until)
    prev = month_range(add_months(until, -23), add_months(until, -12))
    have_prev = first is not None and first <= prev[0]
    return {"last_12_cents": _sum(series, last),
            "prev_12_cents": _sum(series, prev) if have_prev else None}


def _average(series: dict[str, int], first: str | None, until: str) -> int | None:
    """Average month over the last 12, counting only months the ledger covers."""
    if first is None or first > until:
        return None
    months = month_range(max(first, add_months(until, -11)), until)
    return round(_sum(series, months) / len(months))


def account_row(v: AccountView, res: Result, cards: CardsConfig, today: date) -> dict[str, Any]:
    a, until = v.account, res.months[-1]
    first = min(v.series) if v.series else None
    kinds = {i.kind for i in res.insights if i.account_id == a.id}
    paid_from = sorted({paid_from_name(cards, p.source) for p in v.payments})
    ended = not a.in_config or (a.active_to is not None and a.active_to < today)
    return {
        "id": a.id, "service": a.service, "provider": a.provider, "in_config": a.in_config,
        "bill_every_months": a.bill_every_months, "paid_in_advance": a.paid_in_advance,
        "active_from": a.active_from.isoformat() if a.active_from else None,
        "active_to": a.active_to.isoformat() if a.active_to else None,
        "ended": ended, "paid_from": paid_from, "payments": len(v.payments),
        "last_payment": {"date": v.last.day.isoformat(), "amount_cents": v.last.amount_cents,
                         "transaction_id": v.last.txn_id} if v.last else None,
        "next_expected": v.next_expected.isoformat() if v.next_expected and not ended else None,
        "cadence_days": v.cadence_days if v.payments else None,
        "missing": v.missing,
        "first_payment": v.first_is_new and len(v.payments) < FIRST_PAYMENT_SHOWN_WHILE,
        "budget_plan": "budget_plan" in kinds,
        **_year_ago(v.series, first, until),
        "average_month_cents": _average(v.series, first, until),
        "sparkline": [{"month": mo, "cents": v.series.get(mo, 0)}
                      for mo in month_range(add_months(until, -11), until)],
        "best_card_query": a.provider if a.service in FLAT_FOR_BEST_CARD else None,
    }


def summary(s: Session, settings: Settings, cards: CardsConfig, today: date,
            until: str | None = None, months: int | None = None) -> dict[str, Any]:
    res = utilities(s, settings, today, until, months)
    until = res.months[-1]
    first = _first_month(res)
    services = [x for x in SERVICE_ORDER if x in res.by_service]
    window = [mo for mo in res.months if first is None or mo >= first]
    rows = [{"month": mo, "total": res.total.get(mo, 0),
             "by_service": {sv: res.by_service[sv].get(mo, 0) for sv in services},
             "by_account": {a: v.series.get(mo, 0) for a, v in res.accounts.items()}}
            for mo in window]
    # Year over year needs 13 months of a seasonal service's payments (§4); say when.
    seasonal_first = [min(res.by_service[sv]) for sv in services if sv in ("electric", "gas",
                                                                             "water")]
    yoy_from = (add_months(min(seasonal_first), 12) if seasonal_first else None)
    stats = {
        **_year_ago(res.total, first, until),
        "month": until,
        "month_cents": res.total.get(until, 0),
        "month_year_ago_cents": (res.total.get(add_months(until, -12), 0)
                                 if first and first <= add_months(until, -12) else None),
        "biggest_change": res.insights[0].text if res.insights else None,
    }
    accounts = [account_row(v, res, cards, today) for v in res.accounts.values()]
    # A typical month: each current account's own average over the months it covers. Averaging
    # the total would count months before an account's first payment as zero.
    current = [a["average_month_cents"] for a in accounts
               if not a["ended"] and a["average_month_cents"] is not None]
    stats["average_month_cents"] = sum(current) if current else None
    accounts.sort(key=lambda r: (r["ended"], SERVICE_ORDER.index(r["service"]), r["id"]))
    return {
        "until": until, "months": window, "services": services, "data_from": first,
        "yoy_from": yoy_from if yoy_from and yoy_from > until else None,
        "accounts": accounts, "rows": rows,
        "history": {sv: res.by_service[sv] for sv in services},
        "insights": [insight_row(i) for i in res.insights],
        "stats": stats,
    }


def account_detail(s: Session, settings: Settings, cards: CardsConfig, account_id: str,
                   today: date, until: str | None = None,
                   months: int | None = None) -> dict[str, Any] | None:
    res = utilities(s, settings, today, until, months)
    v = res.accounts.get(account_id)
    if v is None:
        return None
    descs = dict(s.execute(select(m.Transaction.id, m.Transaction.description_clean).where(
        m.Transaction.id.in_([pl.payment.txn_id for pl in v.placed]))).all())
    methods = dict(s.execute(select(m.UtilityPayment.transaction_id, m.UtilityPayment.method)
                             .where(m.UtilityPayment.account_id == account_id)).all())
    payments = [{
        "transaction_id": pl.payment.txn_id, "date": pl.payment.day.isoformat(),
        "amount_cents": pl.payment.amount_cents, "paid_from": paid_from_name(cards,
                                                                             pl.payment.source),
        "covers_months": pl.payment.covers_months, "counted_in": pl.counted_in,
        "months": pl.months, "moved": pl.payment.amount_cents > 0 and len(pl.months) == 1
        and pl.months[0] != month_of(pl.payment.day),
        "method": methods.get(pl.payment.txn_id), "description": descs.get(pl.payment.txn_id),
    } for pl in reversed(v.placed)]
    return {
        "account": account_row(v, res, cards, today),
        "payments": payments,
        "series": v.series,
        "insights": [insight_row(i) for i in res.for_account(account_id)],
    }


def unassigned(s: Session, cards: CardsConfig) -> list[dict[str, Any]]:
    rows = s.execute(select(m.Transaction, m.Merchant.name, m.ReviewItem.details_json)
                     .join(m.ReviewItem, m.ReviewItem.ref_id == m.Transaction.id.cast(String))
                     .outerjoin(m.Merchant, m.Merchant.id == m.Transaction.merchant_id)
                     .where(m.ReviewItem.kind == "utility_unassigned",
                            m.ReviewItem.status == "open")
                     .order_by(m.Transaction.txn_date, m.Transaction.id)).all()
    return [{"transaction_id": t.id, "date": t.txn_date or t.post_date,
             "amount_cents": t.amount_cents, "paid_from": paid_from_name(cards, t.card_id),
             "description": t.description_clean, "merchant": name or t.description_clean,
             "category_id": t.category_id,
             "reason": json.loads(details or "{}").get("reason", "")}
            for t, name, details in rows]
