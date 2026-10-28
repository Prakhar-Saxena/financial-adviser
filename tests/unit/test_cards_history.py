# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: F811  (pytest fixtures imported from test_cards_refresh)

"""Card optimizer Phase C3: history step, missed rewards, cap use, credits, cheat sheet.

Fictional Example Bank cards; every expected value is worked out by hand in the comments.
"""

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from fin.cards import compose
from fin.cards.best import active_profiles, load_facts
from fin.cards.history import (
    cap_use,
    cheatsheet,
    credit_use,
    credit_used_map,
    mark_credit,
    period_window,
    resolve_history,
    tips,
)
from fin.cards.models import CardProfile, EarnRule, Evidence, Fee
from fin.db import models as m
from tests.unit.test_cards_refresh import client, env  # noqa: F401  (fixtures)

TODAY = date.today()
EV = [Evidence(doc="D1", page=1, quote="a quote", doc_version_id=None)]


def setup(env):
    """Everyday Visa from the fixture refresh (accepted), plus a Travel Card earning a flat 2%
    cash back, and a few purchases."""
    env.run()
    with env.S() as s:
        compose.accept(s, "example_everyday")
        travel = CardProfile(card_id="example_travel", currency="cash_back",
                             annual_fee_cents=Fee(value=0, evidence=EV),
                             foreign_txn_fee_pct=Fee(value=0, evidence=EV),
                             earn_rules=[EarnRule(rate=2, is_base=True, evidence=EV)])
        s.add(m.CardProfileVersion(card_id="example_travel", version=1, status="active",
                                   profile_json=travel.model_dump_json(), content_hash="h" * 64))
        imp = m.Import(source="chase", kind="transactions", file_path="/x.csv", sha256="0" * 64,
                       status="ok")
        s.add(imp)
        s.flush()
        rows = [  # card, days ago, cents, type, descriptor
            ("example_travel", 3, 5_000, "purchase", "KROGER"),  # cleaned descriptors
            ("example_everyday", 4, 4_000, "purchase", "CHEVRON"),
            ("example_everyday", 5, 2_500, "purchase", "KROGER"),
            ("example_everyday", 2, -1_000, "credit", "EXAMPLE EATS DINING CREDIT"),
            ("example_everyday", 6, 1_234, "purchase", "ZZQX STORE"),
        ]
        for i, (card, ago, cents, typ, desc) in enumerate(rows):
            day = (TODAY - timedelta(days=ago)).isoformat()
            s.add(m.Transaction(card_id=card, post_date=day, txn_date=day, amount_cents=cents,
                                description_raw=desc, description_clean=desc, type=typ,
                                dedupe_key=f"k{i}", import_id=imp.id))
        s.commit()


def merchant_calls(env):
    return [c for c in env.calls() if "merchant_profile" in json.dumps(c["argv"])]


def test_history_step_sends_only_descriptors_and_runs_once(env):
    setup(env)
    with env.S() as s:
        r = resolve_history(s, env.paths, env.settings)
    assert (r.descriptors, r.asked, r.calls) == (3, 1, 1)  # Kroger, Chevron known; ZZQX asked
    sent = json.loads(next(env.paths.ai_runs.glob("*merchant_profile/input.json")).read_text())
    assert sent["records"] == [{"key": "ZZQX STORE", "query": "ZZQX STORE"}]
    assert set(sent) == {"records", "known", "reward_categories"}
    assert "1234" not in json.dumps(sent) and "12.34" not in json.dumps(sent)
    with env.S() as s:
        again = resolve_history(s, env.paths, env.settings)
        assert again.calls == 0 and again.resolved == 3
        alias = s.get(m.MerchantAlias, "zzqx store")
        assert alias.kind == "descriptor" and alias.source == "ai"
    assert len(merchant_calls(env)) == 1


def test_no_ai_skips_the_lookup(env):
    setup(env)
    with env.S() as s:
        r = resolve_history(s, env.paths, env.settings, use_ai=False)
    assert r.calls == 0 and r.resolved == 2 and not merchant_calls(env)


def test_missed_rewards_reproduce_by_hand(env):
    setup(env)
    with env.S() as s:
        resolve_history(s, env.paths, env.settings)
        p, f = active_profiles(s), load_facts(s, env.settings, env.cards)
        out = tips(s, env.settings, env.cards, p, f, months=3)
    rows = {r["key"]: r for r in out["merchants"]}
    # Kroger $50 on the Travel Card: 2% = 100¢. Everyday Visa: 4x U.S. supermarkets at 1¢ = 200¢.
    # Kroger $25 on Everyday: 100¢ either way. Missed: 100¢.
    kroger = rows["kroger"]
    assert (kroger["earned_cents"], kroger["possible_cents"], kroger["difference_cents"]) == \
        (200, 300, 100)
    assert kroger["better_card_id"] == "example_everyday"
    assert kroger["text"].startswith("Use Example Bank Everyday Visa at Kroger every time")
    # Chevron $40 on Everyday: base 1x = 40¢. Travel Card: 2% = 80¢. Missed: 40¢.
    gas = rows["gas_station"]
    assert (gas["earned_cents"], gas["possible_cents"], gas["difference_cents"]) == (40, 80, 40)
    assert gas["text"] == ("Use Example Bank Travel Card at Gas station instead of "
                           "Example Bank Everyday Visa")
    assert [r["key"] for r in out["top"]][:2] == ["kroger", "gas_station"]
    assert out["total_difference_cents"] >= 140


def test_purchase_a_credit_pays_back_is_not_a_miss(env):
    from fin.cards.models import Credit

    setup(env)
    with env.S() as s:
        row = s.scalar(select(m.CardProfileVersion).where(
            m.CardProfileVersion.card_id == "example_travel"))
        travel = CardProfile.model_validate_json(row.profile_json)
        grocery = Credit(name="Grocery credit", amount_cents=10_000, period="month",
                         merchants=["kroger"], evidence=EV)
        in_app = grocery.model_copy(update={"name": "Kroger Cash", "posts_as": "in_app_balance"})
        small = grocery.model_copy(update={"amount_cents": 1_000})
        travel_wide = grocery.model_copy(update={"merchants": [], "categories": ["supermarkets"]})
        resolve_history(s, env.paths, env.settings)
        f = load_facts(s, env.settings, env.cards)
        # Still the 100¢ miss from above: an in-app balance, a credit smaller than the $50, a
        # credit for a whole category.
        for c in (in_app, small, travel_wide):
            row.profile_json = travel.model_copy(update={"credits": [c]}).model_dump_json()
            s.commit()
            got = tips(s, env.settings, env.cards, active_profiles(s), f, 3)["merchants"]
            assert next(r for r in got if r["key"] == "kroger")["difference_cents"] == 100
        row.profile_json = travel.model_copy(update={"credits": [grocery]}).model_dump_json()
        s.commit()
        rows = {r["key"]: r for r in tips(s, env.settings, env.cards, active_profiles(s), f,
                                         3)["merchants"]}
    # The $50 on the Travel Card earns its credit back there: kept, so no difference.
    kroger = rows["kroger"]
    assert kroger["difference_cents"] == 0 and kroger["text"] is None
    assert kroger["kept_for_credit"] == ["Grocery credit"]


def test_cap_use_counts_matching_purchases_on_that_card(env):
    setup(env)
    with env.S() as s:
        p, f = active_profiles(s), load_facts(s, env.settings, env.cards, with_history=False)
        rows = cap_use(s, p, f, TODAY)
    sup = next(r for r in rows if r.card_id == "example_everyday")
    # Only the $25 Kroger on Everyday counts; the $50 was on the Travel Card, Chevron isn't a
    # supermarket.
    assert sup.used_cents == 2_500 and sup.cap_cents == 2_500_000 and sup.period == "calendar_year"


def test_credits_matched_marked_and_freshness(env):
    setup(env)
    with env.S() as s:
        p, f = active_profiles(s), load_facts(s, env.settings, env.cards, with_history=False)
        uses = credit_use(s, p, env.cards, f, TODAY)
        dining = next(u for u in uses if u.credit.name == "Dining credit")
        # "EXAMPLE EATS DINING CREDIT" shares DINING, EXAMPLE, EATS with the credit: $10 used.
        assert (dining.used_cents, dining.known, dining.fresh, dining.how) == \
            (1000, True, True, "posted")
        assert credit_used_map(uses)[("example_everyday", dining.identity)] == 1000
        mark_credit(s, "example_everyday", dining.identity, period_key=dining.period_key,
                    used="none")
        dining = next(u for u in credit_use(s, p, env.cards, f, TODAY)
                      if u.credit.name == "Dining credit")
        assert (dining.used_cents, dining.how) == (0, "manual")
        # Seen from 30 days later, with no newer data: "nothing posted" isn't believed.
        later = TODAY + timedelta(days=30)
        stale = credit_use(s, p, env.cards, f, later)
        assert not any(u.fresh for u in stale if u.how != "manual")
        with pytest.raises(ValueError):
            mark_credit(s, "example_everyday", dining.identity, period_key="x", used="lots")


def test_used_credit_stops_counting_in_the_ranking(env):
    from fin.cards.best import best
    from fin.cards.merchants import correct

    setup(env)
    with env.S() as s:
        correct(s, "example_eats", category="dining")  # a `kb` profile from the documents
        out = best(s, env.settings, env.cards, "example eats", amount_cents=2_000)
        everyday = next(r for r in out["results"] if r["card_id"] == "example_everyday")
        assert everyday["credit"] is None  # the $10 this month is used up
        dining = active_profiles(s)["example_everyday"].credits[0]
        mark_credit(s, "example_everyday", dining.identity(),
                    period_key=period_window("month", date.today())[2], used="none")
        out = best(s, env.settings, env.cards, "example eats", amount_cents=2_000)
        everyday = next(r for r in out["results"] if r["card_id"] == "example_everyday")
        # 3x dining on $20 = 60¢, plus the unused $10 credit, now known: 1060¢.
        assert everyday["credit"]["counted"] and everyday["total_cents"] == 1_060


def test_period_windows():
    d = date(2026, 10, 27)
    assert period_window("month", d)[:3] == (date(2026, 10, 1), date(2026, 10, 31), "2026-10")
    assert period_window("quarter", d)[:3] == (date(2026, 10, 1), date(2026, 12, 31), "2026-Q4")
    assert period_window("quarter", date(2026, 2, 3))[1] == date(2026, 3, 31)
    assert period_window("half_year", d)[:3] == (date(2026, 7, 1), date(2026, 12, 31), "2026-H2")
    assert period_window("cardmember_year", d, 3)[:2] == (date(2026, 3, 1), date(2027, 2, 28))
    assert period_window("cardmember_year", d)[3] is True  # no anniversary: approximate


def test_cheatsheet_ranks_on_earn_rates(env):
    setup(env)
    with env.S() as s:
        p, f = active_profiles(s), load_facts(s, env.settings, env.cards)
        out = cheatsheet(s, env.cards, p, f, 10_000)
    cats = {r["key"]: r for r in out["categories"]}
    assert cats["category:supermarkets"]["card_id"] == "example_everyday"  # 4x
    assert cats["category:dining"]["cents_per_dollar"] == 3.0
    assert cats["category:other"]["card_id"] == "example_travel"  # 2% beats 1x
    assert {r["key"] for r in out["merchants"]} >= {"amazon", "costco_gas"}


def test_api_tips_credits_cheatsheet(env, client):
    setup(env)
    n = len(env.calls())
    assert client.get("/api/cards/tips", params={"months": 3}).status_code == 200
    assert client.get("/api/cards/tips", params={"months": 99}).status_code == 422
    rows = client.get("/api/cards/credits").json()
    assert client.get("/api/cards/cheatsheet").json()["categories"]
    assert len(env.calls()) == n  # no GET calls Claude
    dining = next(r for r in rows if r["name"] == "Dining credit")
    body = {"card_id": "example_everyday", "identity": dining["identity"],
            "period_key": dining["period_key"], "used": "full"}
    assert client.put("/api/cards/credits", content=json.dumps(body)).status_code == 415
    got = client.put("/api/cards/credits", json=body).json()
    assert next(r for r in got if r["name"] == "Dining credit")["how"] == "manual"
    got = client.put("/api/cards/credits", json={**body, "clear": True}).json()
    assert next(r for r in got if r["name"] == "Dining credit")["how"] == "posted"
    with env.S() as s:
        assert not s.scalars(select(m.CardFact).where(m.CardFact.kind == "credit_used")).all()
