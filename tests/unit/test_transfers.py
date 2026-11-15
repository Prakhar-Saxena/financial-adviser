# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""No double counting and proving the checking ledger (docs/SPEC-checking.md §10, §11).
Fictional rows only; every case in §16's transfer list has a test."""

import itertools
import json
from datetime import date

import pytest
from sqlalchemy import select

from fin.config import CONFIG_DIR, CardsConfig, Settings, load_taxonomy
from fin.db import models as m
from fin.db import session_factory, upgrade
from fin.db.seed import sync_reference
from fin.normalize.bank import load_bank_rules
from fin.reconcile.bank import balance_walk, bank_checks
from fin.reconcile.transfers import check_invariants, classify_checking, link_transfers
from fin.review import ReviewSet

CARDS = CardsConfig.model_validate(json.loads((CONFIG_DIR / "cards.example.json").read_text()))
RULES = load_bank_rules(CARDS)
TODAY = date(2026, 11, 20)
_keys = itertools.count()


@pytest.fixture
def s(tmp_path):
    db = tmp_path / "t.sqlite"
    upgrade(db)
    with session_factory(db)() as session:
        sync_reference(session, CARDS, load_taxonomy())
        session.add(m.Import(id=1, source="x", kind="transactions", file_path="x", sha256="h",
                             status="ok", card_id="checking", period_start="2026-01-01",
                             period_end="2026-12-31"))
        session.commit()
        yield session


def card(s, card_id, day, cents, typ="payment", desc="PAYMENT THANK YOU", source="csv"):
    t = m.Transaction(card_id=card_id, person_id="self", post_date=day, amount_cents=cents,
                      description_raw=desc, description_clean=desc, type=typ,
                      dedupe_key=f"k{next(_keys)}", import_id=1, source=source)
    s.add(t)
    s.flush()
    return t


def bank(s, day, cents, desc, bank_type=None, balance=None):
    t = m.Transaction(card_id="checking", person_id="self", post_date=day, amount_cents=cents,
                      description_raw=desc, description_clean=desc,
                      type="purchase" if cents > 0 else "deposit", dedupe_key=f"k{next(_keys)}",
                      import_id=1, bank_type=bank_type, balance_after_cents=balance)
    s.add(t)
    s.flush()
    return t


def run(s, settings=None, today=TODAY):
    reviews = ReviewSet(s)
    classes = classify_checking(s, CARDS, RULES)
    res = link_transfers(s, CARDS, settings or Settings(), reviews, classes, today)
    reviews.close_stale()
    s.flush()
    return res


def links(s):
    return {(lk.bank_txn_id, lk.card_txn_id, lk.method) for lk in s.scalars(
        select(m.TransferLink))}


def items(s, kind):
    return {int(i.ref_id) for i in s.scalars(select(m.ReviewItem).where(
        m.ReviewItem.kind == kind, m.ReviewItem.status == "open"))}


# ------------------------------------------------------------------ §10.1


@pytest.mark.parametrize("bank_day", ["2026-10-02", "2026-10-08"])
def test_named_payment_links_either_date_order(s, bank_day):
    p = card(s, "chase_prime_visa", "2026-10-05", -61245)
    b = bank(s, bank_day, 61245, "CHASE CREDIT CRD EPAY")
    res = run(s)
    assert links(s) == {(b.id, p.id, "named")} and res.linked["named"] == 1
    assert (b.type, b.flow_kind) == ("transfer", "card_payment")


@pytest.mark.parametrize("days, linked", [(-10, True), (6, True), (-11, False), (7, False)])
def test_window_edges(s, days, linked):
    p = card(s, "chase_prime_visa", "2026-10-15", -10000)
    b = bank(s, date.fromordinal(date(2026, 10, 15).toordinal() + days).isoformat(), 10000,
             "CHASE CREDIT CRD EPAY")
    run(s)
    assert bool(links(s)) is linked
    assert b.type == "transfer"  # a named payment never counts as spending, linked or not
    assert p.type == "payment"


def test_wrong_issuer_is_rejected(s):
    card(s, "amex_gold", "2026-10-05", -61245)
    b = bank(s, "2026-10-05", 61245, "CHASE CREDIT CRD EPAY")
    run(s)
    assert links(s) == set() and b.flow_kind == "card_payment"


def test_two_payments_with_the_same_amount_pair_off(s):
    p1 = card(s, "chase_prime_visa", "2026-10-03", -5000)
    p2 = card(s, "chase_freedom_unlimited", "2026-10-04", -5000)
    b1 = bank(s, "2026-10-03", 5000, "CHASE CREDIT CRD EPAY")
    b2 = bank(s, "2026-10-04", 5000, "CHASE CREDIT CRD EPAY")
    run(s)
    assert links(s) == {(b1.id, p1.id, "named"), (b2.id, p2.id, "named")}
    assert items(s, "uncertain_transfer") == set()


# ------------------------------------------------------------------ §10.2


@pytest.mark.parametrize("parts", [[3000, 2000], [1000, 2500, 1500]])
def test_group_of_two_and_three(s, parts):
    ids = ["chase_prime_visa", "chase_freedom_unlimited", "chase_sapphire_reserve"]
    ps = [card(s, c, "2026-10-05", -a) for c, a in zip(ids[:len(parts)], parts, strict=True)]
    b = bank(s, "2026-10-04", sum(parts), "CHASE CREDIT CRD EPAY")
    res = run(s)
    assert links(s) == {(b.id, p.id, "group") for p in ps} and res.linked["group"] == 1


# ------------------------------------------------------------------ §10.3


def test_amount_only_match_and_my_override(s):
    p = card(s, "citi_costco", "2026-10-10", -23456)
    b = bank(s, "2026-10-09", 23456, "ONLINE PMT 0000")
    res = run(s)
    assert links(s) == {(b.id, p.id, "amount_only")} and res.uncertain == 1
    assert (b.type, b.flow_kind) == ("transfer", "card_payment")
    assert items(s, "uncertain_transfer") == {b.id}
    # "This is spending": my override wins and the link goes.
    s.add(m.UserOverride(target_type="transaction", stable_key=b.dedupe_key, field="type",
                         value="purchase"))
    s.flush()
    run(s)
    assert links(s) == set() and (b.type, b.flow_kind) == ("purchase", None)
    assert items(s, "uncertain_transfer") == set()


def test_amount_only_never_takes_p2p_or_atm(s):
    card(s, "citi_costco", "2026-10-10", -4000)
    b = bank(s, "2026-10-10", 4000, "VENMO PAYMENT")
    run(s)
    assert links(s) == set() and b.flow_kind == "p2p"


# ------------------------------------------------------------------ §10.4


@pytest.mark.parametrize("bank_day, today, raised", [
    ("2026-10-15", date(2026, 10, 20), False),   # inside the grace days
    ("2026-10-15", date(2026, 11, 20), True),    # after them
    ("2026-01-15", date(2026, 11, 20), False),   # older than the card ledger
])
def test_no_other_side(s, bank_day, today, raised):
    card(s, "amex_gold", "2026-03-01", 1234, typ="purchase", desc="SHOP")
    b = bank(s, bank_day, 50000, "AMEX EPAYMENT ACH PMT")
    run(s, today=today)
    assert b.type == "transfer"
    assert (items(s, "unmatched_transfer") == {b.id}) is raised


def test_card_not_tracked_raises_at_once(s):
    b = bank(s, "2026-11-18", 25000, "DISCOVER E-PAYMENT")
    res = run(s)
    assert (b.type, b.flow_kind) == ("transfer", "card_payment_other")
    assert items(s, "unmatched_transfer") == {b.id} and res.unmatched == 1
    s.add(m.UserOverride(target_type="transaction", stable_key=b.dedupe_key, field="type",
                         value="transfer"))
    s.add(m.UserOverride(target_type="transaction", stable_key=b.dedupe_key, field="flow_kind",
                         value="card_payment_other"))
    s.flush()
    run(s)
    assert items(s, "unmatched_transfer") == set()  # I said "Not spending"


# ------------------------------------------------------------------ §10.5


def test_returned_payment_links_the_money_coming_back(s):
    card(s, "chase_prime_visa", "2026-10-05", -40000)
    back = card(s, "chase_prime_visa", "2026-10-09", 40000, desc="PAYMENT REVERSAL")
    b_in = bank(s, "2026-10-10", -40000, "CHASE CREDIT CRD PMT RFND")
    run(s)
    assert (b_in.id, back.id, "named") in links(s)
    assert (b_in.type, b_in.flow_kind) == ("transfer", "card_payment")


def test_credit_balance_refund_is_a_payment_and_links(s):
    from fin.normalize.signs import classify

    assert classify(1500, "CREDIT BALANCE REFUND") == ("payment", False)
    back = card(s, "citi_costco", "2026-10-09", 1500, desc="CREDIT BALANCE REFUND")
    b_in = bank(s, "2026-10-12", -1500, "MISC DEPOSIT")
    run(s)
    assert links(s) == {(b_in.id, back.id, "amount_only")}


# ------------------------------------------------------------------ CASCADE


def test_statement_row_replaced_by_its_csv_row_drops_the_link(s):
    st_row = card(s, "chase_prime_visa", "2026-10-05", -61245, source="statement")
    b = bank(s, "2026-10-05", 61245, "CHASE CREDIT CRD EPAY")
    run(s)
    assert links(s) == {(b.id, st_row.id, "named")}
    s.delete(st_row)  # what link_statements does when the CSV row arrives
    s.flush()
    s.expire_all()
    assert links(s) == set()
    csv_row = card(s, "chase_prime_visa", "2026-10-05", -61245)
    run(s)
    assert links(s) == {(b.id, csv_row.id, "named")}


# ------------------------------------------------------------------ §10.6


def test_invariants_hold_and_catch_a_breach(s):
    card(s, "chase_prime_visa", "2026-10-05", -61245)
    bank(s, "2026-10-05", 61245, "CHASE CREDIT CRD EPAY")
    card(s, "citi_costco", "2026-10-10", -999)
    bank(s, "2026-10-10", 4000, "VENMO PAYMENT")
    run(s)
    assert check_invariants(s, CARDS, Settings()) == []
    stray = bank(s, "2026-10-11", 999, "HAND TYPED SHOP")
    s.add(m.UserOverride(target_type="transaction", stable_key=stray.dedupe_key, field="type",
                         value="purchase"))
    wrong = bank(s, "2026-10-12", 100, "X")
    wrong.type = "credit"
    s.flush()
    problems = check_invariants(s, CARDS, Settings())
    assert len(problems) == 1 and problems[0].startswith("4:")  # hand-typed rows are mine


def test_every_type_is_spending_or_not_exactly_once():
    from fin.api.spending import SPENDING_TYPES
    from fin.categorize.rules import NON_SPENDING

    assert set(SPENDING_TYPES) | NON_SPENDING == set(m.TXN_TYPES)
    assert not set(SPENDING_TYPES) & NON_SPENDING


# ------------------------------------------------------------------ §11


def statement(s, deposits, withdrawals, opening=100000):
    st = m.BankStatement(card_id="checking", period_start="2026-10-01", period_end="2026-10-31",
                         opening_balance=opening, deposits=deposits, withdrawals=withdrawals,
                         closing_balance=opening + deposits - withdrawals, pdf_path="x",
                         parse_method="regex", math_ok=True)
    s.add(st)
    s.flush()
    return st


def walk_rows(s, rows, start=100000):
    bal = start
    for day, cents in rows:
        bal -= cents
        bank(s, day, cents, "ROW", balance=bal)


ROWS = [("2026-10-02", -50000), ("2026-10-02", 2000), ("2026-10-05", 30000),
        ("2026-10-20", 1500)]


def test_ledger_and_balance_match(s):
    walk_rows(s, ROWS)
    st = statement(s, 50000, 33500)
    res = bank_checks(s, ["checking"], ReviewSet(s))
    assert res.statements == {"match": 1} and res.balance_gaps == []
    assert st.ledger_status == "match" and res.balance_days == 3


def test_doubled_row_fails_both_checks(s):
    walk_rows(s, ROWS)
    bank(s, "2026-10-05", 30000, "ROW", balance=48000 - 30000)  # imported twice
    statement(s, 50000, 33500)
    reviews = ReviewSet(s)
    res = bank_checks(s, ["checking"], reviews)
    assert res.statements == {"mismatch": 1}
    assert res.balance_gaps == ["checking 2026-10-05"]


def test_missing_row_fails_both_checks(s):
    bal = 100000
    for i, (day, cents) in enumerate(ROWS):
        bal -= cents
        if i != 2:  # the 2026-10-05 row never made it into the export
            bank(s, day, cents, "ROW", balance=bal)
    statement(s, 50000, 33500)
    res = bank_checks(s, ["checking"], ReviewSet(s))
    assert res.statements == {"mismatch": 1} and res.balance_gaps == ["checking 2026-10-20"]


def test_statement_outside_the_exports_is_incomplete(s):
    s.get(m.Import, 1).period_start = "2026-10-15"
    statement(s, 0, 0)
    assert bank_checks(s, ["checking"], ReviewSet(s)).statements == {"incomplete": 1}


def test_balance_walk_ignores_same_day_order():
    days = [("d1", 100, {900, 950}), ("d2", -50, {950, 1000, 980})]
    assert balance_walk(days) == []
