# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Link ledger rows to utility accounts (docs/SPEC-utilities.md §7).

Runs in `fin process` after `apply_overrides` and recomputes `utility_payments` from scratch.
It never touches a transaction: type, category, amount and review status stay as they are.

Which rows: `purchase` and `refund` rows on any card or bank account. First match wins:
1. my transaction override (`utility_account`): an account id, or `none`
2. my merchant override, by the row's merchant name
3. an account's `payment_patterns` (re.search, case-insensitive, on description_clean, and on
   description_raw too: checking rows are cleaned of private words (SPEC-checking §8.3), which
   may include a provider's short form), within its active dates with 60 days of margin; a
   row matching two accounts goes to step 4
4. otherwise a row in a bills category becomes `utility_unassigned`
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fin.config import CardsConfig, UtilityAccount
from fin.db import models as m
from fin.review import ReviewSet

LINKED_TYPES = ("purchase", "refund")
BILL_CATEGORIES = ("bills.utilities", "bills.internet", "bills.phone")
NOT_A_UTILITY = "none"
ACTIVE_MARGIN_DAYS = 60
UTILITY_KINDS = ("utility_unassigned", "utility_missing")


@dataclass
class LinkResult:
    linked: Counter = field(default_factory=Counter)  # by method
    unassigned: int = 0
    not_utility: int = 0  # my "none" corrections

    @property
    def total(self) -> int:
        return sum(self.linked.values())


def merchant_key(name: str | None) -> str:
    """How a merchant override is keyed: the merchant's name, spaces collapsed, upper case."""
    return " ".join((name or "").split()).upper()


def row_merchant(t: m.Transaction, merchants: dict[int, str]) -> str:
    """The row's merchant name, else its cleaned description."""
    return merchant_key(merchants.get(t.merchant_id) if t.merchant_id else t.description_clean)


def overrides(s: Session) -> tuple[dict[str, str], dict[str, int], dict[str, str]]:
    """(transaction account, transaction covers_months, merchant account)."""
    txn: dict[str, str] = {}
    covers: dict[str, int] = {}
    merch: dict[str, str] = {}
    for o in s.scalars(select(m.UserOverride).where(m.UserOverride.field.in_(
            ("utility_account", "utility_covers_months")))):
        if o.target_type == "transaction" and o.field == "utility_account":
            txn[o.stable_key] = o.value
        elif o.target_type == "transaction":
            covers[o.stable_key] = int(o.value)
        elif o.target_type == "merchant":
            merch[merchant_key(o.stable_key)] = o.value
    return txn, covers, merch


def _in_dates(acct: UtilityAccount, day: date) -> bool:
    margin = timedelta(days=ACTIVE_MARGIN_DAYS)
    return ((acct.active_from is None or day >= acct.active_from - margin)
            and (acct.active_to is None or day <= acct.active_to + margin))


def link_payments(s: Session, cards: CardsConfig, reviews: ReviewSet) -> LinkResult:
    res = LinkResult()
    s.execute(delete(m.UtilityPayment))
    known = {a.id: a for a in s.scalars(select(m.UtilityAccount))}
    txn_over, covers_over, merch_over = overrides(s)
    merchants = dict(s.execute(select(m.Merchant.id, m.Merchant.name)).all())
    patterned = [(u, u.patterns) for u in cards.utility_accounts if u.payment_patterns]

    rows = s.scalars(select(m.Transaction).where(m.Transaction.type.in_(LINKED_TYPES))
                     .order_by(m.Transaction.id)).all()
    for t in rows:
        day = date.fromisoformat(t.txn_date or t.post_date)
        account, method, reason = None, None, None
        chosen = txn_over.get(t.dedupe_key)
        if chosen is not None:
            method = "override"
        else:
            chosen = merch_over.get(row_merchant(t, merchants))
            method = "merchant" if chosen is not None else None
        if chosen == NOT_A_UTILITY:
            res.not_utility += 1
            continue
        if chosen is not None:
            if chosen in known:
                account = chosen
            else:
                reason = f"my correction names {chosen!r}, which isn't a utility account"
        else:
            texts = (t.description_clean or "", t.description_raw or "")
            hits = [u.id for u, pats in patterned if _in_dates(u, day)
                    and any(p.search(x) for p in pats for x in texts)]
            if len(hits) == 1:
                account, method = hits[0], "pattern"
            elif hits:
                reason = "matches " + " and ".join(hits)
        if account:
            months = covers_over.get(t.dedupe_key) or known[account].bill_every_months
            s.add(m.UtilityPayment(transaction_id=t.id, account_id=account, method=method,
                                   covers_months=months))
            res.linked[method] += 1
        elif reason or t.category_id in BILL_CATEGORIES:
            reviews.flag("utility_unassigned", "transactions", t.id,
                         reason=reason or "a bill payment that matches no utility account")
            res.unassigned += 1
    s.flush()
    return res
