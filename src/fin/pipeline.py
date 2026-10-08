"""`fin process`: link → rules → AI → match → allocate → reconcile → review items (SPEC §16).

Idempotent: every step recomputes from the stored data, so running it twice changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.categorize.ai import AIResult, Runner, categorize_items, categorize_transactions
from fin.categorize.rules import apply_overrides, apply_rules, load_seed_rules
from fin.config import CardsConfig, DataPaths, Settings, Taxonomy
from fin.db import models as m
from fin.match.amazon import MatchResult, allocate_matched, amazon_on_other_cards, match_amazon
from fin.match.costco import CostcoResult, match_costco
from fin.reconcile.dedicated_cards import unexpected_merchants
from fin.reconcile.link import LinkResult, link_statements
from fin.reconcile.statements import ledger_check
from fin.review import ReviewSet


@dataclass
class ProcessResult:
    link: LinkResult
    rule_matched: int = 0
    ai_merchants: AIResult = field(default_factory=AIResult)
    ai_items: AIResult = field(default_factory=AIResult)
    match: MatchResult = field(default_factory=MatchResult)
    costco: CostcoResult = field(default_factory=CostcoResult)
    ledger: dict[str, int] = field(default_factory=dict)
    unexpected: int = 0
    amazon_elsewhere: int = 0
    reviews_open: dict[str, int] = field(default_factory=dict)
    reviews_closed: int = 0

    @property
    def ai_paused(self) -> str | None:
        return self.ai_merchants.paused or self.ai_items.paused


def process(
    s: Session, paths: DataPaths, settings: Settings, cards: CardsConfig, taxonomy: Taxonomy,
    *, use_ai: bool = True, today: date | None = None, runner: Runner | None = None,
) -> ProcessResult:
    today = today or date.today()
    reviews = ReviewSet(s)

    link = link_statements(s, cards, reviews)
    res = ProcessResult(link=link)
    load_seed_rules(s, taxonomy)
    res.rule_matched, leftovers = apply_rules(s)
    s.commit()

    res.ai_merchants = categorize_transactions(s, paths, settings, leftovers, reviews,
                                               use_ai=use_ai, runner=runner)
    res.ai_items = categorize_items(s, paths, settings, reviews, use_ai=use_ai and
                                    not res.ai_merchants.paused, runner=runner)
    apply_overrides(s)
    s.commit()

    res.match = match_amazon(s, cards, settings, reviews, today)
    res.costco = match_costco(s, cards, settings, reviews, today)
    allocate_matched(s)  # splits for every matched order, Amazon and Costco
    for st in s.scalars(select(m.Statement)).all():
        status = ledger_check(s, st, reviews)
        res.ledger[status] = res.ledger.get(status, 0) + 1
    res.unexpected = unexpected_merchants(s, cards, reviews)
    res.amazon_elsewhere = amazon_on_other_cards(s, cards)
    for t in s.scalars(select(m.Transaction).where(m.Transaction.person_id.is_(None))).all():
        reviews.flag("unknown_person", "transactions", t.id,
                     reason="the cardholder name matches nobody in cards.json")
    for t in s.scalars(select(m.Transaction).where(m.Transaction.review_status ==
                                                   "needs_review")).all():
        reviews.flag("low_confidence", "transactions", t.id,
                     reason=f"unexpected money-in row; typed as {t.type}")
    if not res.ai_paused:
        # Stale AI-dependent items stay open while AI is paused, so nothing looks resolved early.
        res.reviews_closed = reviews.close_stale()
    s.commit()
    res.reviews_open = open_reviews(s)
    return res


def open_reviews(s: Session) -> dict[str, int]:
    from sqlalchemy import func

    rows = s.execute(select(m.ReviewItem.kind, func.count()).where(
        m.ReviewItem.status == "open").group_by(m.ReviewItem.kind)).all()
    return dict(rows)
