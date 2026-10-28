# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: F811  (pytest fixtures imported from test_cards_refresh)

"""Card optimizer Phase C4: card-linked offers, read-only.

A fictional offers page (tests/fixtures/cards/offers/everyday_offers.md, an accessibility
snapshot) and the fake claude's canned offer_extract output, which has one wrong number and one
invented offer that verification must reject.
"""

import json
import shutil
from datetime import date

import pytest
from sqlalchemy import select

from fin.cards import compose, engine
from fin.cards.best import active_profiles, best, load_facts
from fin.cards.history import earn_only
from fin.cards.offers import current_offers, extract_offers, parse_expiry
from fin.db import models as m
from fin.ingest import load_parsers
from fin.ingest.inbox import import_file
from tests.conftest import FIXTURES
from tests.unit.test_cards_refresh import client, env  # noqa: F401  (fixtures)

PAGE = FIXTURES / "cards" / "offers" / "everyday_offers.md"


def import_page(env, tmp_path, site="amex", card="example_everyday"):
    load_parsers()
    src = tmp_path / f"offers_{card}.md"
    shutil.copy(PAGE, src)
    with env.S() as s:
        return import_file(s, env.paths, src, site, "offers", card)


def offer_calls(env):
    return [c for c in env.calls() if "offer_extract" in json.dumps(c["argv"])]


def test_import_extract_verify(env, tmp_path):
    assert import_page(env, tmp_path).status == "imported"
    with env.S() as s:
        r = extract_offers(s, env.paths, env.settings, env.cards)
        assert (r.snapshots, r.calls, r.found, r.verified) == (1, 1, 6, 4)
        rows = {o.merchant_name: o for o in s.scalars(select(m.CardOffer))}
        # Code checks the numbers against the quote, and the quote against the page.
        assert not rows["Sample Shoes"].verified
        assert "reward 40 is not in the quote" in rows["Sample Shoes"].reject_reason
        assert not rows["Sample Invented"].verified
        assert rows["Sample Invented"].reject_reason == "quote not found on the page"
        coffee, books, kroger = rows["Sample Coffee"], rows["Sample Books"], rows["Kroger"]
        assert (coffee.reward_value, coffee.min_spend_cents, coffee.expires_on, coffee.added) == \
            (500, 2000, "2099-11-30", False)
        assert (books.reward_kind, books.reward_value, books.max_value, books.added) == \
            ("percent", 10, 1500, True)
        assert (kroger.reward_kind, kroger.max_value, kroger.added) == \
            ("points_per_dollar", 1000, True)
        # Expired offers stay stored but aren't current.
        current = current_offers(s, {"example_everyday": "amex"})["example_everyday"]
        assert {o.merchant_name for o in current} == {"Sample Coffee", "Sample Books", "Kroger"}
        # Read once: a second run asks nothing.
        again = extract_offers(s, env.paths, env.settings, env.cards)
        assert again.snapshots == 0 and len(offer_calls(env)) == 1


def test_offer_extract_never_sees_names_or_endings(env, tmp_path):
    import_page(env, tmp_path)
    with env.S() as s:
        extract_offers(s, env.paths, env.settings, env.cards)
    sent = next(env.paths.ai_runs.glob("*offer_extract/input.json")).read_text()
    assert "Alex" not in sent and "0000" not in sent and "[name]" in sent
    assert set(json.loads(sent)) == {"card", "captured_on", "page"}
    argv = offer_calls(env)[0]["argv"]
    assert argv[argv.index("--model") + 1] == "haiku" and "--bare" not in argv
    assert "WebSearch" not in json.dumps(argv) and "WebFetch" not in json.dumps(argv)


def test_limit_pauses_then_resumes(env, tmp_path, monkeypatch):
    import_page(env, tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    with env.S() as s:
        r = extract_offers(s, env.paths, env.settings, env.cards)
        assert r.paused and s.scalar(select(m.CardOfferSnapshot.status)) == "limited"
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    with env.S() as s:
        assert extract_offers(s, env.paths, env.settings, env.cards).verified == 4


def test_added_offer_counts_in_the_ranking(env, tmp_path):
    env.run()
    import_page(env, tmp_path, site="amex")
    with env.S() as s:
        compose.accept(s, "example_everyday")
        extract_offers(s, env.paths, env.settings, env.cards)
        out = best(s, env.settings, env.cards, "kroger", amount_cents=5_000)
        everyday = next(r for r in out["results"] if r["card_id"] == "example_everyday")
        # +2 points per dollar on $50 = 100 points, added on the page: counted.
        offer = everyday["offer"]
        assert offer["counted"] and offer["text"].startswith("Card offer: +2 points per dollar")
        assert everyday["total_cents"] == everyday["rule_cents"] + everyday["offer_cents"]
        assert everyday["offer_cents"] == round(100 * load_facts(s, env.settings, env.cards)
                                                .valuation(everyday["currency"]))
        # The look back and the cheat sheet compare earn rates only.
        assert earn_only(load_facts(s, env.settings, env.cards)).offers == {}
        # Not added: shown, never counted.
        profile = active_profiles(s)["example_everyday"]
        facts = load_facts(s, env.settings, env.cards)
        r = engine.evaluate(engine.Card("example_everyday", "Everyday", "visa"), profile,
                            engine.Merchant("sample_coffee", "Sample Coffee", "dining"),
                            engine.Options(2_500), facts)
        assert r.offer["text"] == ("Card offer: $5 back on $20 or more, not added yet. Add it "
                                   "in the issuer's app before you pay.")
        assert not r.offer["counted"] and r.offer_cents == 0 and r.offer["value_cents"] == 500


def test_rebuild_keeps_offers(env, tmp_path):
    from fin.rebuild import rebuild

    import_page(env, tmp_path)
    with env.S() as s:
        extract_offers(s, env.paths, env.settings, env.cards)
        rebuild(s)
        assert s.scalar(select(m.CardOfferSnapshot.status)) == "extracted"
        assert len(s.scalars(select(m.CardOffer)).all()) == 6
        assert extract_offers(s, env.paths, env.settings, env.cards).snapshots == 0


def test_api_offers_calls_no_ai(env, client, tmp_path):
    import_page(env, tmp_path)
    with env.S() as s:
        extract_offers(s, env.paths, env.settings, env.cards)
    n = len(env.calls())
    out = client.get("/api/cards/offers").json()
    assert out["cards"][0]["current"] == 3 and out["cards"][0]["added"] == 2
    assert len(env.calls()) == n


# --------------------------------------------------------------------------- pure


def amex(kind, value, **kw):
    return engine.Offer(issuer="amex", merchant_name="Sample Coffee",
                        merchant_norm="sample coffee", reward_kind=kind, reward_value=value, **kw)


def test_offer_text_as_the_spec_words_it():
    o = amex("cash", 1_000, min_spend_cents=5_000, added=False)
    line = engine.offer_line(o, 6_000, 1.0)
    assert line["text"] == ("Amex Offer: $10 back on $50 or more, not added yet. Add it in the "
                            "Amex app before you pay.")
    assert line["value_cents"] == 1_000 and not line["counted"]
    below = engine.offer_line(o, 4_000, 1.0)
    assert below["value_cents"] == 0 and "spend $50 or more to get it" in below["notes"]


@pytest.mark.parametrize("offer,amount,valuation,cents", [
    (amex("percent", 10, max_value=1_500), 20_000, 1.0, 1_500),  # 10% of $200 capped at $15
    (amex("percent", 10, max_value=1_500), 5_000, 1.0, 500),
    (amex("points", 2_000), 100, 1.6, 3_200),
    (amex("points_per_dollar", 3, max_value=3_000), 200_000, 1.6, 4_800),  # cap 3,000 points
    (amex("points_per_dollar", 3, max_value=3_000), 10_000, 1.6, 480),
])
def test_offer_values(offer, amount, valuation, cents):
    assert engine.offer_value(offer, amount, valuation) == pytest.approx(cents)


def test_offer_matching_by_alias_name_and_channel():
    facts = engine.Facts(names={"whole foods market": ("whole_foods", "seed")})
    wf = engine.Merchant("whole_foods", "Whole Foods Market", "supermarkets")
    o = engine.Offer("chase", "Whole Foods Market", "whole foods market", "cash", 500)
    assert engine.offer_fits(o, wf, facts)
    assert not engine.offer_fits(o, engine.Merchant("kroger", "Kroger", "supermarkets"), facts)
    online = engine.Offer("chase", "Sample Coffee", "sample coffee", "cash", 500,
                          channel="online")
    coffee = engine.Merchant("sample_coffee", "Sample Coffee", "dining")
    assert engine.offer_fits(online, coffee, facts)
    assert not engine.offer_fits(online, coffee, facts, channel="in_store")


@pytest.mark.parametrize("text,expect", [
    ("Expires 11/30/26", "2026-11-30"), ("Exp 11/30/2026", "2026-11-30"),
    ("Expires Nov 30, 2026", "2026-11-30"), ("Ends Sept. 3, 2026", "2026-09-03"),
    ("7 days left", "2026-11-03"), ("soon", None), (None, None),
])
def test_parse_expiry(text, expect):
    assert parse_expiry(text, "2026-10-27") == expect


def test_sync_params_collect_offers(env):
    from fin.sync.params import issuer_params

    with env.S() as s:
        today = date(2026, 10, 27)
        p = issuer_params(s, env.cards, env.settings, "example", today)
        assert p["collect_offers"] is False
        on = env.settings.model_copy(update={"sync": env.settings.sync.model_copy(
            update={"offers_sites": ["amex"]})})
        assert issuer_params(s, env.cards, on, "amex", today)["collect_offers"] is True
        assert issuer_params(s, env.cards, on, "amex", today, offers=False)["collect_offers"] \
            is False


def test_manifest_accepts_offers_files(tmp_path):
    from fin.ingest.manifest import validate_run

    run = tmp_path / "run"
    run.mkdir()
    shutil.copy(PAGE, run / "offers_amex_gold.md")
    entry = {"file": "offers_amex_gold.md", "kind": "offers", "card_id": "amex_gold",
             "period_start": None, "period_end": "2026-10-27", "note": "6 offers shown"}
    (run / "manifest.json").write_text(json.dumps(
        {"site": "amex", "run_date": "2026-10-27", "files": [entry]}))
    (run / "run_report.json").write_text(json.dumps(
        {"site": "amex", "run_date": "2026-10-27", "notes": [], "gaps": [],
         "collected": [{"card_id": "amex_gold", "kind": "offers", "count": 1}]}))
    check = validate_run(run, "amex", {"amex_gold"}, "2026-10-27")
    assert check.ok, check.errors
