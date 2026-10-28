# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Golden tests for the recommendation engine (docs/SPEC-cards.md §9, Phase C2 acceptance 1).

Four fictional cards and fictional merchants. Every scenario states the expected ranking and the
value of each card to the cent.
"""

from datetime import date

import pytest

from fin.cards import engine as e
from fin.cards.models import (
    Benefit,
    Cap,
    CardProfile,
    Credit,
    EarnRule,
    Evidence,
    Fee,
    Requirement,
)

EV = [Evidence(doc="D1", page=1, quote="a quote from the terms", doc_version_id=1)]


def rule(rate, cats=(), names=(), **kw):
    return EarnRule(rate=rate, categories=list(cats), merchant_names=list(names), evidence=EV,
                    **kw)


def base(rate):
    return EarnRule(rate=rate, is_base=True, evidence=EV)


def fee(v):
    return Fee(value=v, evidence=EV)


CARDS = [e.Card("everyday", "Everyday Visa", "visa"), e.Card("travel", "Travel Amex", "amex"),
         e.Card("cash", "Cash Visa", "visa"), e.Card("club", "Club Visa", "visa")]

PROFILES = {
    "everyday": CardProfile(
        card_id="everyday", currency="ex_points", currency_name="Everyday Points",
        annual_fee_cents=fee(9500), foreign_txn_fee_pct=fee(0),
        earn_rules=[
            base(1),
            rule(4, ["supermarkets"], geography="us",
                 cap=Cap(amount_cents=2_500_000, period="calendar_year", rate_after=1),
                 excluded_categories=["wholesale_clubs", "discount_stores"]),
            rule(3, ["dining"]),
        ],
        benefits=[Benefit(kind="extended_warranty", name="Extended Warranty", evidence=EV)]),
    "travel": CardProfile(
        card_id="travel", currency="ex_mr", currency_name="Example MR points",
        annual_fee_cents=fee(25000), foreign_txn_fee_pct=fee(0),
        earn_rules=[
            base(1),
            rule(5, ["airlines"], channel="direct"),
            rule(10, ["travel_agencies"], ["Example Travel"], channel="issuer_portal",
                 portal="Example Travel"),
            rule(2, ["drugstores"]),
        ],
        credits=[Credit(name="Eats credit", amount_cents=1000, period="month",
                        merchant_names=["Example Eats"], enrollment_required=True,
                        evidence=EV)],
        benefits=[Benefit(kind="trip_delay", name="Trip Delay", evidence=EV)]),
    "cash": CardProfile(
        card_id="cash", currency="cash_back", currency_name="cash back",
        annual_fee_cents=fee(0), foreign_txn_fee_pct=fee(3),
        earn_rules=[
            base(1.5),
            rule(5, ["other"], ["Shopmart"]),
            rule(2, ["rideshare_taxi"], ["Ridely"], channel="in_app"),
            rule(3, ["drugstores"], valid_to="2026-11-15"),
        ]),
    "club": CardProfile(
        card_id="club", currency="cash_back", currency_name="cash back",
        annual_fee_cents=fee(0), foreign_txn_fee_pct=fee(0),
        earn_rules=[
            base(1),
            rule(5, ["gas_stations"], ["Bigbox"],
                 requirements=[Requirement(kind="membership", name="Bigbox membership")],
                 cap=Cap(amount_cents=700_000, period="calendar_year", rate_after=1,
                         shared_id="gas")),
            rule(4, ["gas_stations", "ev_charging"],
                 cap=Cap(amount_cents=700_000, period="calendar_year", rate_after=1,
                         shared_id="gas")),
            rule(2, ["wholesale_clubs"], ["Bigbox"]),
        ],
        benefits=[Benefit(kind="purchase_protection", name="Purchase Protection",
                          evidence=EV)]),
}

NAMES = {"shopmart": ("shopmart", "seed"), "bigbox": ("bigbox_warehouse", "seed"),
         "ridely": ("ridely", "seed"), "example eats": ("example_eats", "kb"),
         "example travel": ("example_travel", "kb")}

VISA_ONLY = {"visa": "yes", "amex": "no", "mastercard": "no", "discover": "no"}
M = {
    "freshmart": e.Merchant("freshmart", "Freshmart", "supermarkets"),
    "bigbox_warehouse": e.Merchant("bigbox_warehouse", "Bigbox", "wholesale_clubs", VISA_ONLY),
    "bigbox_gas": e.Merchant("bigbox_gas", "Bigbox gas", "gas_stations", VISA_ONLY),
    "fuelco": e.Merchant("fuelco", "Fuelco", "gas_stations"),
    "bistro": e.Merchant("bistro", "Bistro", "dining"),
    "deliverfast": e.Merchant("deliverfast", "Deliverfast", "food_delivery"),
    "ridely": e.Merchant("ridely", "Ridely", "rideshare_taxi"),
    "zoomcab": e.Merchant("zoomcab", "Zoomcab", "rideshare_taxi"),
    "skyair": e.Merchant("skyair", "Skyair", "airlines"),
    "shopmart": e.Merchant("shopmart", "Shopmart", "other"),
    "pharmco": e.Merchant("pharmco", "Pharmco", "drugstores"),
    "example_eats": e.Merchant("example_eats", "Example Eats", "dining"),
    "gadgetco": e.Merchant("gadgetco", "Gadgetco", "electronics"),
    "corner": e.Merchant("corner", "Corner shop", "other"),
}
TODAY = date(2026, 11, 1)


def run(merchant, amount=10_000, channel=None, abroad=False, on=TODAY, **facts):
    f = e.Facts(names=NAMES, **facts)
    return e.rank(M[merchant], CARDS, PROFILES, e.Options(amount, channel, abroad, on), f,
                  {"ex_mr": "Example MR points", "ex_points": "Everyday Points"})


def totals(r):
    return [(x.card.id, round(x.total_cents)) for x in r.results]


# (id, merchant, run kwargs, expected [(card, cents)] in rank order)
GOLDEN = [
    ("supermarket category rule", "freshmart", {},
     [("everyday", 400), ("cash", 150), ("travel", 100), ("club", 100)]),
    ("wholesale club: excluded from supermarkets, Amex not accepted", "bigbox_warehouse", {},
     [("club", 200), ("cash", 150), ("everyday", 100)]),
    ("merchant-specific gas: brand match", "bigbox_gas", {},
     [("club", 500), ("cash", 150), ("everyday", 100)]),
    ("other gas: the merchant rule doesn't apply", "fuelco", {},
     [("club", 400), ("cash", 150), ("everyday", 100), ("travel", 100)]),
    ("dining", "bistro", {},
     [("everyday", 300), ("cash", 150), ("travel", 100), ("club", 100)]),
    ("delivery counts as dining", "deliverfast", {},
     [("everyday", 300), ("cash", 150), ("travel", 100), ("club", 100)]),
    ("merchant-specific rideshare, in-app caveat", "ridely", {},
     [("cash", 200), ("everyday", 100), ("travel", 100), ("club", 100)]),
    ("another rideshare doesn't get it", "zoomcab", {},
     [("cash", 150), ("everyday", 100), ("travel", 100), ("club", 100)]),
    ("in store: the in-app rule doesn't apply", "ridely", {"channel": "in_store"},
     [("cash", 150), ("everyday", 100), ("travel", 100), ("club", 100)]),
    ("airline booked direct", "skyair", {},
     [("travel", 500), ("cash", 150), ("everyday", 100), ("club", 100)]),
    ("airline abroad: foreign transaction fee", "skyair", {"abroad": True},
     [("travel", 500), ("everyday", 100), ("club", 100), ("cash", -150)]),
    ("U.S.-only supermarkets abroad", "freshmart", {"abroad": True},
     [("everyday", 100), ("travel", 100), ("club", 100), ("cash", -150)]),
    ("amount crosses the cap", "freshmart", {"amount": 3_000_000},
     [("everyday", 105_000), ("cash", 45_000), ("travel", 30_000), ("club", 30_000)]),
    ("partly used cap", "freshmart",
     {"amount": 100_000, "cap_used": {("everyday", "supermarkets||any|us|"): 2_450_000}},
     [("everyday", 2_500), ("cash", 1_500), ("travel", 1_000), ("club", 1_000)]),
    ("unused credit counts when its use is known", "example_eats",
     {"amount": 800, "credit_used": {("travel", "eats credit|month"): 0}},
     [("travel", 808), ("everyday", 24), ("cash", 12), ("club", 8)]),
    ("credit with unknown use is shown, not counted", "example_eats", {"amount": 800},
     [("everyday", 24), ("cash", 12), ("travel", 8), ("club", 8)]),
    ("a used-up credit adds nothing", "example_eats",
     {"amount": 800, "credit_used": {("travel", "eats credit|month"): 1000}},
     [("everyday", 24), ("cash", 12), ("travel", 8), ("club", 8)]),
    ("break-even valuation", "pharmco", {},
     [("cash", 300), ("travel", 200), ("everyday", 100), ("club", 100)]),
    ("a higher valuation flips it", "pharmco", {"valuations": {"ex_mr": 2.0}},
     [("travel", 400), ("cash", 300), ("everyday", 100), ("club", 100)]),
    ("pooling: cash card valued as Example MR", "corner",
     {"pools": {"cash": "ex_mr"}, "valuations": {"ex_mr": 1.5}},
     [("cash", 225), ("travel", 150), ("everyday", 100), ("club", 100)]),
    ("membership declined: the merchant rule drops", "bigbox_gas",
     {"requirements": {("club", "bigbox membership"): False}},
     [("club", 400), ("cash", 150), ("everyday", 100)]),
    ("expired rule falls back to base", "pharmco", {"on": date(2026, 12, 1)},
     [("travel", 200), ("cash", 150), ("everyday", 100), ("club", 100)]),
    ("ties: protections, then cards.json order", "gadgetco", {},
     [("cash", 150), ("everyday", 100), ("club", 100), ("travel", 100)]),
    ("merchant-specific bonus on 'other'", "shopmart", {},
     [("cash", 500), ("everyday", 100), ("travel", 100), ("club", 100)]),
]


@pytest.mark.parametrize("name,merchant,kw,expected", GOLDEN, ids=[g[0] for g in GOLDEN])
def test_golden(name, merchant, kw, expected):
    assert totals(run(merchant, **kw)) == expected


def test_scenario_count():
    assert len(GOLDEN) >= 20


def test_explanations_and_caveats():
    r = run("freshmart")
    top = r.results[0]
    assert top.as_dict(10_000)["rule"]["text"] == "4x on supermarkets"
    assert any("up to $25,000 per calendar year" in c for c in top.caveats)
    assert top.as_dict(10_000)["cents_per_dollar"] == 4.0
    assert any("Amex acceptance" in c for c in r.results[2].caveats)  # travel: amex unknown
    assert run("ridely").results[0].caveats == ["in the app only"]
    assert run("ridely").asks_channel
    gas = run("bigbox_gas")
    assert [x["card_id"] for x in gas.not_accepted] == ["travel"]
    assert "needs Bigbox membership" in gas.results[0].caveats
    assert any("ends 2026-11-15" in c for c in run("pharmco").results[0].caveats)


def test_alternative_portal_line():
    travel = next(x for x in run("skyair").results if x.card.id == "travel")
    assert travel.alternatives == [{
        "text": "Book through Example Travel: 10x", "cents": 1000, "caveats": [],
        "evidence": {"quote": "a quote from the terms", "page": 1, "doc_version_id": 1}}]
    assert travel.total_cents == 500  # never mixed into the ranking


def test_break_even_text():
    assert run("pharmco").break_even == (
        "Cash Visa wins unless you value Example MR points above 1.50¢ (now 1¢).")
    # Points over cash back: how low the winner's points may go before cash wins.
    assert run("freshmart").break_even == (
        "Everyday Visa wins as long as you value Everyday Points above 0.38¢ (now 1¢); below "
        "that, Cash Visa.")
    assert run("shopmart").break_even is None  # cash back wins: nothing to say


def test_credit_line_and_enrollment():
    r = next(x for x in run("example_eats", amount=800).results if x.card.id == "travel")
    assert r.credit["counted"] is False and r.credit["needs_enrollment"]
    assert "if not used yet" in r.credit["text"]
    r = next(x for x in run("example_eats", amount=800,
                            requirements={("travel", "eats credit"): False}).results
             if x.card.id == "travel")
    assert r.credit is None  # I said I'm not enrolled


def test_stale_caveat_and_benefits():
    r = run("gadgetco", stale_days={"cash": 45})
    assert "terms last checked 45 days ago" in r.results[0].caveats
    club = next(x for x in r.results if x.card.id == "club")
    assert [b["kind"] for b in club.benefits] == ["purchase_protection"]
    sky = next(x for x in run("skyair").results if x.card.id == "travel")
    assert [b["kind"] for b in sky.benefits] == ["trip_delay"]
