# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`fin process`: link → checking types and transfers → rules → AI → match → allocate →
reconcile → review items (SPEC §16, docs/SPEC-checking.md §12).

Idempotent: every step recomputes from the stored data, so running it twice changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.cards.history import HistoryResult, resolve_history
from fin.cards.offers import OffersResult, extract_offers
from fin.cards.rewards import fill_statement_rewards
from fin.categorize.ai import AIResult, Runner, categorize_items, categorize_transactions
from fin.categorize.rules import HELD, apply_overrides, apply_rules, load_seed_rules
from fin.config import CardsConfig, DataPaths, Settings, Taxonomy
from fin.db import models as m
from fin.db.seed import sync_categories
from fin.match.amazon import MatchResult, allocate_matched, amazon_on_other_cards, match_amazon
from fin.match.costco import CostcoResult, match_costco
from fin.normalize.bank import load_bank_rules
from fin.reconcile.bank import BankCheckResult, bank_checks
from fin.reconcile.dedicated_cards import unexpected_merchants
from fin.reconcile.link import LinkResult, link_statements
from fin.reconcile.statements import ledger_check
from fin.reconcile.transfers import (
    TransferResult,
    checking_ids,
    classify_checking,
    link_transfers,
)
from fin.review import ReviewSet


@dataclass
class ProcessResult:
    link: LinkResult
    transfers: TransferResult = field(default_factory=TransferResult)
    bank: BankCheckResult = field(default_factory=BankCheckResult)
    rule_matched: int = 0
    ai_merchants: AIResult = field(default_factory=AIResult)
    ai_items: AIResult = field(default_factory=AIResult)
    card_merchants: HistoryResult = field(default_factory=HistoryResult)
    card_offers: OffersResult = field(default_factory=OffersResult)
    statement_rewards: int = 0
    match: MatchResult = field(default_factory=MatchResult)
    costco: CostcoResult = field(default_factory=CostcoResult)
    ledger: dict[str, int] = field(default_factory=dict)
    unexpected: int = 0
    amazon_elsewhere: int = 0
    reviews_open: dict[str, int] = field(default_factory=dict)
    reviews_closed: int = 0

    @property
    def ai_paused(self) -> str | None:
        return (self.ai_merchants.paused or self.ai_items.paused or self.card_merchants.paused
                or self.card_offers.paused)


def process(
    s: Session, paths: DataPaths, settings: Settings, cards: CardsConfig, taxonomy: Taxonomy,
    *, use_ai: bool = True, today: date | None = None, runner: Runner | None = None,
) -> ProcessResult:
    today = today or date.today()
    reviews = ReviewSet(s)

    link = link_statements(s, cards, reviews)
    res = ProcessResult(link=link)
    sync_categories(s, taxonomy)
    load_seed_rules(s, taxonomy)
    # Checking rows get their real types before anything reads them, and card payments out of
    # checking are excluded before rules or AI see them (docs/SPEC-checking.md §9, §10).
    classes = classify_checking(s, cards, load_bank_rules(cards))
    res.transfers = link_transfers(s, cards, settings, reviews, classes, today)
    res.rule_matched, leftovers = apply_rules(s)
    for t in s.scalars(select(m.Transaction).where(
            m.Transaction.card_id.in_(checking_ids(cards)),
            m.Transaction.flow_kind.in_(HELD), m.Transaction.category_id == "uncategorized")):
        reviews.flag("uncategorized", "transactions", t.id,
                     reason="a payment to a person or a check: say what it was")
    s.commit()

    res.ai_merchants = categorize_transactions(s, paths, settings, leftovers, reviews,
                                               use_ai=use_ai, runner=runner)
    res.ai_items = categorize_items(s, paths, settings, reviews, use_ai=use_ai and
                                    not res.ai_merchants.paused, runner=runner)
    apply_overrides(s)
    s.commit()
    # Card optimizer (SPEC-cards §8 History): a merchant profile for every purchase descriptor.
    res.card_merchants = resolve_history(s, paths, settings, use_ai=use_ai and not res.ai_paused,
                                         runner=runner)
    # Phase C4: read the offers pages the sync saved (one haiku call per new page).
    res.card_offers = extract_offers(s, paths, settings, cards,
                                     use_ai=use_ai and not res.ai_paused, runner=runner)
    # Phase C5: each new statement's own rewards summary, for the rewards check.
    res.statement_rewards = fill_statement_rewards(s, cards)

    res.match = match_amazon(s, cards, settings, reviews, today)
    res.costco = match_costco(s, cards, settings, reviews, today)
    allocate_matched(s)  # splits for every matched order, Amazon and Costco
    for st in s.scalars(select(m.Statement)).all():
        status = ledger_check(s, st, reviews)
        res.ledger[status] = res.ledger.get(status, 0) + 1
    res.bank = bank_checks(s, checking_ids(cards), reviews)
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
