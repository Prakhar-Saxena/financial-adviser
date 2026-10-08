"""What one browsing run should collect (SPEC §9.1 step 1).

- Date range: from the latest imported date minus `sync_overlap_days` to today.
  On the first run, the last `history_months`.
- Statements: closed statements in range that aren't imported yet. On the first run,
  the last `statements_per_card` per card.
- Amazon / Costco: orders in range that aren't imported yet.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fin.config import CardsConfig, Settings
from fin.db import models as m

ITEM_SOURCES = {"amazon", "costco"}
UNMATCHED_LOOKBACK = 14  # days before the earliest unmatched charge


def months_before(d: date, months: int) -> date:
    y, mo = divmod(d.year * 12 + (d.month - 1) - months, 12)
    mo += 1
    return date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


def _start(latest: str | None, today: date, settings: Settings) -> tuple[date, bool]:
    if latest:
        return date.fromisoformat(latest) - timedelta(days=settings.sync.sync_overlap_days), False
    return months_before(today, settings.general.history_months), True


def issuer_params(
    s: Session, cards: CardsConfig, settings: Settings, issuer: str, today: date,
    since: date | None = None,
) -> dict[str, Any]:
    out = []
    for card in cards.cards_for_issuer(issuer):
        latest = s.scalar(
            select(func.max(m.Transaction.post_date)).where(m.Transaction.card_id == card.id)
        )
        start, first = _start(latest, today, settings)
        if since:
            start, first = since, False
        imported = sorted(
            s.scalars(select(m.Statement.period_end).where(m.Statement.card_id == card.id))
        )
        if first:
            wanted = (
                f"The last {settings.general.statements_per_card} closed statements "
                "(first run)."
            )
        else:
            wanted = (
                f"Every closed statement with a closing date on or after {start.isoformat()} "
                "whose closing date is not in statements_already_imported."
            )
        out.append({
            "card_id": card.id,
            "product": card.product,
            "card_endings": sorted({h.card_ending for h in card.cardholders if h.card_ending}),
            "transactions_from": start.isoformat(),
            "transactions_to": today.isoformat(),
            "first_run": first,
            "statements_wanted": wanted,
            "statements_already_imported": imported,
        })
    return {"cards": out}


def source_params(
    s: Session, settings: Settings, source: str, today: date, since: date | None = None,
) -> dict[str, Any]:
    latest = s.scalar(select(func.max(m.Order.order_date)).where(m.Order.merchant == source))
    start, first = _start(latest, today, settings)
    if since:
        start, first = since, False
    # Reach back far enough for card charges that still have no order (e.g. older statement
    # lines carrying order numbers from before the first Amazon sync).
    unmatched = s.scalar(
        select(func.min(func.coalesce(m.Transaction.txn_date, m.Transaction.post_date)))
        .join(m.Merchant, m.Merchant.id == m.Transaction.merchant_id)
        .outerjoin(m.Match, m.Match.transaction_id == m.Transaction.id)
        .where((m.Merchant.merchant_group == source)
               | m.Merchant.merchant_group.like(f"{source}\\_%", escape="\\"),
               m.Match.id.is_(None),
               m.Transaction.type == "purchase"))
    if unmatched and not since:
        start = min(start, date.fromisoformat(unmatched) - timedelta(days=UNMATCHED_LOOKBACK))
    rows = s.execute(select(m.Order.external_id, m.Order.order_date, m.Order.total,
                            m.Order.channel).where(
        m.Order.merchant == source, m.Order.order_date >= start.isoformat())).all()
    if source == "costco":
        # The agent sees receipts by date and total, not by our ids.
        imported = sorted(f"{d}_{t / 100:.2f}" if ch == "warehouse" else ext
                          for ext, d, t, ch in rows)
    else:
        imported = sorted(ext for ext, *_ in rows)
    return {
        "orders_from": start.isoformat(),
        "orders_to": today.isoformat(),
        "first_run": first,
        "orders_already_imported": imported,
    }


def run_params(
    s: Session, cards: CardsConfig, settings: Settings, site: str, today: date,
    since: date | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Return (params for the prompt, since_date, until_date)."""
    if site in ITEM_SOURCES:
        p = source_params(s, settings, site, today, since)
        return p, p["orders_from"], p["orders_to"]
    p = issuer_params(s, cards, settings, site, today, since)
    froms = [c["transactions_from"] for c in p["cards"]]
    return p, min(froms), today.isoformat()


def allowed_card_ids(cards: CardsConfig, site: str) -> set[str]:
    if site in ITEM_SOURCES:
        return set()
    return {c.id for c in cards.cards_for_issuer(site)}
