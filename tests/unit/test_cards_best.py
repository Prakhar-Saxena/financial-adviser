# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

# ruff: noqa: F811  (pytest fixtures imported from test_cards_refresh)
"""Card optimizer Phase C2: merchant resolution, lookup, corrections, `fin cards best`, API.

Fictional Example Bank cards (fixtures) with the real seed merchants; the fake `claude` CLI.
"""

import json
import time

from sqlalchemy import select
from typer.testing import CliRunner

from fin.cards import compose
from fin.cards.best import best
from fin.cards.merchants import correct, lookup, resolve, sync_seeds
from fin.db import models as m
from tests.unit.test_cards_refresh import client, env  # noqa: F401  (fixtures)


def ready(env):
    """Both example cards' terms accepted, as after a real first refresh."""
    env.run()
    with env.S() as s:
        compose.accept(s, "example_everyday")
        s.commit()


def merchant_calls(env) -> int:
    return sum("merchant_profile" in json.dumps(c["argv"]) for c in env.calls())


def test_seeds_load_once_and_keep_my_corrections(env):
    with env.S() as s:
        assert sync_seeds(s) is True
        assert sync_seeds(s) is False  # unchanged file: nothing to do
        p = s.get(m.MerchantProfileRow, "costco_warehouse")
        assert p.reward_category == "wholesale_clubs"
        assert json.loads(p.acceptance_json)["amex"] == "no"
        correct(s, "target", category="supermarkets")
        assert sync_seeds(s, force=True) is True
        assert s.get(m.MerchantProfileRow, "target").reward_category == "supermarkets"
        assert s.get(m.MerchantProfileRow, "target").source == "user"


def test_resolution_order(env):
    from fin.categorize.rules import load_seed_rules
    from fin.config import load_taxonomy

    with env.S() as s:
        load_seed_rules(s, load_taxonomy())
        sync_seeds(s)
        assert resolve(s, "Whole Foods!").how == "alias"
        r = resolve(s, "WHOLEFDS MKT #123")  # no alias; the SPEC §10 rule finds the group
        assert (r.how, r.merchant.key) == ("rule", "whole_foods")
        r = resolve(s, "wholefoods")  # close match: offered, never applied
        assert r.merchant is None and r.suggestions[0]["key"] == "whole_foods"
        assert resolve(s, "shell on main street").merchant.key == "gas_station"  # rule
        r = resolve(s, "walmart on main street")  # contains an alias: offered, never applied
        assert r.merchant is None and r.suggestions[0]["key"] == "walmart"
        assert resolve(s, "zzqx corp").needs_lookup


def test_known_merchant_is_fast_and_never_calls_claude(env):
    ready(env)
    n = len(env.calls())
    with env.S() as s:
        best(s, env.settings, env.cards, "warm up")
        t = time.perf_counter()
        out = best(s, env.settings, env.cards, "whole foods")
        elapsed = time.perf_counter() - t
    assert elapsed < 0.2, elapsed
    assert len(env.calls()) == n
    top = out["results"][0]
    assert out["merchant"]["key"] == "whole_foods" and not out["needs_lookup"]
    assert (top["card_id"], top["total_cents"]) == ("example_everyday", 400)
    assert top["rule"]["evidence"]["doc_version_id"]


def test_unknown_merchant_one_lookup_then_none(env):
    ready(env)
    with env.S() as s:
        out = best(s, env.settings, env.cards, "Zzqx Bistro")
        assert out["needs_lookup"] and out["merchant"] is None
        res = lookup(s, env.paths, env.settings, "Zzqx Bistro")
        assert res.merchant.source == "ai"
    assert merchant_calls(env) == 1
    argv = next(c["argv"] for c in env.calls() if "merchant_profile" in json.dumps(c["argv"]))
    assert argv[argv.index("--model") + 1] == "haiku" and "--effort" in argv
    with env.S() as s:
        out = best(s, env.settings, env.cards, "zzqx bistro")
        assert not out["needs_lookup"] and out["resolution"] == "alias"
    assert merchant_calls(env) == 1
    # Only the query went to Claude: no amounts, cards or other personal data.
    sent = json.loads(next(env.paths.ai_runs.glob("*merchant_profile/input.json")).read_text())
    assert sent["records"] == [{"key": "q1", "query": "Zzqx Bistro"}]


def test_correction_survives_refresh_and_rebuild(env):
    from fin.rebuild import rebuild

    ready(env)
    with env.S() as s:
        sync_seeds(s)
        correct(s, "target", category="supermarkets", acceptance={"amex": "no"})
    env.run(force=True)
    with env.S() as s:
        rebuild(s)
        sync_seeds(s, force=True)
        p = s.get(m.MerchantProfileRow, "target")
        assert (p.reward_category, p.source) == ("supermarkets", "user")
        out = best(s, env.settings, env.cards, "target")
        assert out["results"][0]["total_cents"] == 400  # now ranked as a supermarket


def test_cli_best_and_merchant(env, monkeypatch):
    from fin.cards import cli as cards_cli
    from fin.cli import app

    ready(env)
    monkeypatch.setattr(cards_cli, "load_cards", lambda: env.cards)
    monkeypatch.setattr(cards_cli, "load_settings", lambda: env.settings)
    r = CliRunner()
    out = r.invoke(app, ["cards", "best", "whole foods", "--amount", "50"]).output
    assert "Whole Foods Market · supermarkets" in out and "$2.00" in out
    out = r.invoke(app, ["cards", "best", "zzqx corp"])
    assert out.exit_code == 1 and "--lookup" in out.output
    assert merchant_calls(env) == 0
    out = r.invoke(app, ["cards", "best", "zzqx corp", "--lookup"]).output
    assert merchant_calls(env) == 1 and "Zzqx Corp" in out
    out = r.invoke(app, ["cards", "merchant", "target", "--category", "supermarkets",
                         "--accepts", "amex=no"]).output
    assert "Saved" in out and '"source": "user"' in out


def test_api_best_lookup_patch_settings(env, client):  # noqa: F811
    ready(env)
    n = len(env.calls())
    r = client.get("/api/cards/best", params={"q": "whole foods", "amount_cents": 2500})
    assert r.status_code == 200 and r.json()["results"][0]["total_cents"] == 100
    assert client.get("/api/cards/best", params={"q": "zzqx"}).json()["needs_lookup"]
    assert client.get("/api/cards/best", params={"q": "x", "channel": "moon"}).status_code == 422
    assert client.get("/api/cards/merchants", params={"q": "whole"}).json()[0]["key"] == \
        "whole_foods"
    client.get("/api/cards/settings")
    assert len(env.calls()) == n  # no GET calls Claude

    assert client.post("/api/cards/merchants/lookup", content='{"q": "zzqx"}').status_code == 415
    r = client.post("/api/cards/merchants/lookup", json={"q": "zzqx"})
    assert r.status_code == 200 and r.json()["merchant"]["source"] == "ai"
    key = r.json()["merchant"]["key"]
    assert client.get("/api/cards/best", params={"q": "zzqx"}).json()["merchant"]["key"] == key

    assert client.patch("/api/cards/merchants/target", content="{}").status_code == 415
    r = client.patch("/api/cards/merchants/target", json={"category": "nope"})
    assert r.status_code == 422
    r = client.patch("/api/cards/merchants/target", json={"category": "supermarkets"})
    assert r.json()["source"] == "user"

    r = client.put("/api/cards/settings", json={
        "valuations": [{"currency": "example_points", "cents": 1.5}],
        "count_unused_credits": False,
        "cards": [{"card_id": "example_everyday", "anniversary_month": 3,
                   "requirements": [{"ref": "dining credit", "kind": "enrollment",
                                     "value": "yes"}]}]})
    assert r.status_code == 200, r.text
    got = r.json()
    assert {v["currency"]: v["cents"] for v in got["valuations"]}["example_points"] == 1.5
    assert got["count_unused_credits"] is False
    everyday = next(c for c in got["cards"] if c["card_id"] == "example_everyday")
    assert everyday["anniversary_month"] == "3"
    assert everyday["requirements"][0]["value"] == "yes"
    r = client.get("/api/cards/best", params={"q": "whole foods"})
    assert r.json()["results"][0]["total_cents"] == 600  # 4x at 1.5¢
    bad = client.put("/api/cards/settings", json={"valuations": [{"currency": "x", "cents": 99}]})
    assert bad.status_code == 422
    with env.S() as s:
        assert s.scalars(select(m.CardFact).where(m.CardFact.kind == "valuation")).one().value \
            == "1.5"
