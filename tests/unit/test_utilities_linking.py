# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Linking payments to utility accounts (docs/SPEC-utilities.md §7, §14 "Linking"). Fictional
providers only."""

import copy
import json

import pytest
from sqlalchemy import select

from fin.config import CONFIG_DIR, CardsConfig, load_taxonomy
from fin.db import models as m
from fin.db import session_factory, upgrade
from fin.db.seed import sync_reference
from fin.review import ReviewSet
from fin.utilities.check import transactions_hash
from fin.utilities.payments import link_payments

EXAMPLE = json.loads((CONFIG_DIR / "accounts.example.json").read_text())


def cards_with(fn=lambda d: None) -> CardsConfig:
    doc = copy.deepcopy(EXAMPLE)
    fn(doc)
    return CardsConfig.model_validate(doc)


def accounts(d):
    return {u["id"]: u for u in d["utility_accounts"]}


def setup(d):
    """Gas also matches "POWER AND GAS"; electric ended in January."""
    acc = accounts(d)
    acc["gas"]["payment_patterns"].append("POWER AND GAS")
    acc["electric"]["active_to"] = "2026-01-31"


CARDS = cards_with(setup)


def txn(s, i, desc, day="2026-01-10", cents=5000, typ="purchase", category=None,
        card="checking"):
    if s.get(m.Import, 1) is None:
        s.add(m.Import(id=1, source="bank", kind="transactions", file_path="/nowhere/x.csv",
                       sha256="h", status="ok"))
    s.add(m.Transaction(id=i, card_id=card, txn_date=day, post_date=day, amount_cents=cents,
                        description_raw=desc, description_clean=desc, type=typ,
                        category_id=category, dedupe_key=f"k{i}", import_id=1))


def override(s, target, key, field, value):
    s.add(m.UserOverride(target_type=target, stable_key=key, field=field, value=value))


@pytest.fixture
def s(tmp_path):
    db = tmp_path / "t.sqlite"
    upgrade(db)
    with session_factory(db)() as s:
        sync_reference(s, CARDS, load_taxonomy())
        txn(s, 1, "EXAMPLE POWER AUTOPAY")                                  # pattern
        txn(s, 2, "EXAMPLE GAS REFUND", cents=-500, typ="refund")           # a refund
        txn(s, 3, "EXAMPLE FIBER", typ="payment")                           # not a purchase
        txn(s, 4, "TOWN UTILITIES", category="bills.utilities")             # unassigned
        txn(s, 5, "CORNER CAFE", category="dining")                         # nothing
        txn(s, 6, "EXAMPLE POWER AND GAS", day="2025-12-01")                # two accounts
        txn(s, 7, "MYSTERY BILLER", category="bills.internet")              # my override
        txn(s, 8, "EXAMPLE WIRELESS", category="bills.phone")               # merchant: none
        txn(s, 9, "EXAMPLE WIRELESS", day="2026-02-10", category="bills.phone")
        txn(s, 10, "EXAMPLE POWER", day="2026-03-30", category="bills.utilities")  # margin
        txn(s, 11, "EXAMPLE POWER", day="2026-04-02", category="bills.utilities")  # too late
        txn(s, 12, "EXAMPLE WATER", typ="transfer")                         # never
        override(s, "transaction", "k7", "utility_account", "internet")
        override(s, "transaction", "k7", "utility_covers_months", "3")
        override(s, "merchant", "example  wireless", "utility_account", "none")
        override(s, "transaction", "k9", "utility_account", "phone")
        s.commit()
        yield s


def link(s):
    reviews = ReviewSet(s)
    res = link_payments(s, CARDS, reviews)
    s.commit()
    links = {p.transaction_id: (p.account_id, p.method, p.covers_months)
             for p in s.scalars(select(m.UtilityPayment))}
    flagged = {int(k[2]): k for k in reviews.seen}
    return res, links, flagged


def test_precedence_and_rules(s):
    before = transactions_hash(s)
    res, links, flagged = link(s)
    assert links == {
        1: ("electric", "pattern", 1),
        2: ("gas", "pattern", 1),
        7: ("internet", "override", 3),     # the transaction override and its covers
        9: ("phone", "override", 1),        # the transaction override beats the merchant's
        10: ("electric", "pattern", 1),     # 58 days past active_to: inside the margin
    }
    assert set(flagged) == {4, 6, 11}       # bills rows and the double match
    assert res.linked == {"pattern": 3, "override": 2} and res.unassigned == 3
    assert res.not_utility == 1             # row 8: my merchant correction says none
    item = s.scalar(select(m.ReviewItem).where(m.ReviewItem.ref_id == "6"))
    assert json.loads(item.details_json)["reason"] == "matches electric and gas"
    assert transactions_hash(s) == before   # linking never changes a transaction


def test_idempotent_and_merchant_override(s):
    first = link(s)[1]
    assert link(s)[1] == first
    # "Every payment from this merchant": the merchant override links what no pattern does.
    override(s, "merchant", "TOWN UTILITIES", "utility_account", "water")
    s.commit()
    res, links, flagged = link(s)
    assert links[4] == ("water", "merchant", 3) and 4 not in flagged


def test_an_override_to_an_unknown_account_is_flagged(s):
    override(s, "transaction", "k5", "utility_account", "cable")
    s.commit()
    _, links, flagged = link(s)
    assert 5 in flagged and 5 not in links


def test_patterns_also_read_the_raw_description(s):
    """A private word cleaned out of a checking row (SPEC-checking §8.3) still links."""
    txn(s, 21, "EXAMPLE FIBER  - WEBPAY 0000", category="bills.internet")
    s.get(m.Transaction, 21).description_clean = "FIBER WEBPAY"
    s.commit()
    assert link(s)[1][21] == ("internet", "pattern", 1)


def test_rows_from_cards_link_too(s):
    txn(s, 20, "EXAMPLE FIBER ONLINE", card="amex_gold", category="bills.internet")
    s.commit()
    assert link(s)[1][20] == ("internet", "pattern", 1)


def test_missing_opens_and_closes_a_review_item(s):
    from datetime import date

    from fin.config import Settings
    from fin.utilities.series import relink

    for i, day in enumerate(("2025-10-08", "2025-11-08", "2025-12-08"), 30):
        txn(s, i, "EXAMPLE GAS", day=day, cents=5000 + i)
    s.commit()
    open_items = lambda: [r.ref_id for r in s.scalars(select(m.ReviewItem).where(  # noqa: E731
        m.ReviewItem.kind == "utility_missing", m.ReviewItem.status == "open"))]
    _, late = relink(s, CARDS, Settings(), date(2026, 1, 25))
    # Gas is late; electric ended in January, so it isn't missing.
    assert late == 1 and open_items() == ["gas"]
    txn(s, 40, "EXAMPLE GAS", day="2026-01-20", cents=5100)
    s.commit()
    _, late = relink(s, CARDS, Settings(), date(2026, 1, 25))
    assert late == 0 and open_items() == []
