# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: F811  (pytest fixtures imported from test_cards_refresh)

"""Card optimizer Phase C5: questions about a card's terms, the rewards check against
statements, and the monthly launchd schedule. Fictional cards, statements and documents."""

import json
import plistlib
from datetime import date

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from fin.cards import compose, rewards, schedule
from fin.cards.best import active_profiles, load_facts
from fin.cards.question import QuestionError, ask, pick
from fin.cli import app
from fin.db import models as m
from tests.unit.test_cards_refresh import client, env  # noqa: F401  (fixtures)

Q = "Does it cover rental car damage abroad?"


def question_calls(env):
    return [c for c in env.calls() if "card_question" in json.dumps(c["argv"])]


# --------------------------------------------------------------------------- card_question


def test_answer_is_checked_and_cached(env):
    env.run()
    card = env.cards.card("example_everyday")
    with env.S() as s:
        out = ask(s, env.paths, env.settings, card, Q)
        assert out["status"] == "verified" and out["found"] and not out["cached"]
        assert all(c["ok"] for c in out["citations"])
        assert out["citations"][1]["page"] == 3  # the cited page was wrong; the quote decides
        assert out["citations"][0]["doc_title"] == "Guide to Benefits"
        again = ask(s, env.paths, env.settings, card, "  does it cover rental car damage "
                                                       "abroad? ")
        assert again["cached"] and again["status"] == "verified"
    assert len(question_calls(env)) == 1
    argv = question_calls(env)[0]["argv"]
    assert argv[argv.index("--model") + 1] == env.settings.cards.extract_model
    assert "WebSearch" not in json.dumps(argv) and "--bare" not in argv
    sent = json.loads(next(env.paths.ai_runs.glob("*card_question/input.json")).read_text())
    assert set(sent) == {"card", "question", "documents"} and sent["question"] == Q


@pytest.mark.parametrize("mode,status,warning", [
    ("bad_quote", "partial", "quote not found"),
    ("bad_number", "partial", "numbers not in a checked quote: 99999"),
])
def test_unchecked_quotes_and_numbers_are_flagged(env, monkeypatch, mode, status, warning):
    env.run()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with env.S() as s:
        out = ask(s, env.paths, env.settings, env.cards.card("example_everyday"), Q)
    assert out["status"] == status and any(warning in w for w in out["warnings"])


def test_questions_the_app_wont_send(env):
    card = env.cards.card("example_everyday")
    with env.S() as s:
        with pytest.raises(QuestionError, match="No documents"):
            ask(s, env.paths, env.settings, card, Q)
        env.run()
        for q in ("", "   ", "x" * 501):
            with pytest.raises(QuestionError):
                ask(s, env.paths, env.settings, card, q)
    assert not question_calls(env)


def test_pick_sends_the_matching_pages_when_over_budget(env):
    env.run()
    from fin.cards.question import card_sources

    with env.S() as s:
        srcs = card_sources(s, "example_everyday")
    texts = pick(srcs, "rental car collision coverage", max_chars=600)
    assert "Collision Damage Waiver" in texts["D3"]
    assert "Extended Warranty Protection extends" not in texts["D3"]  # page 2 doesn't match
    assert sum(len(t) for t in texts.values()) <= 600


def test_api_question(env, client):
    env.run()
    body = {"card_id": "example_everyday", "question": Q}
    assert client.post("/api/cards/question", content=json.dumps(body)).status_code == 415
    assert client.post("/api/cards/question",
                       json={**body, "card_id": "nope"}).status_code == 404
    assert client.post("/api/cards/question", json={**body, "question": ""}).status_code == 422
    out = client.post("/api/cards/question", json=body).json()
    assert out["status"] == "verified"


# --------------------------------------------------------------------------- rewards check


CHASE_BOX = """\
Previous points balance 1,000
M$i5nim2u0m. P6a8yment Due + 3% back on dining 300
8 9 10 11 12 13 14 + 1.5% (1.5 Pts)/$1 earned on all purchases 150
22 23 24 25 26 27 28 11/12/26 4x on flights 0
+ Extra 2% Back promotional bonus 20
- Points redeemed this statement period 900
Total points available for
redemption 570
"""
CITI_BOX = """\
Costco Cash Back Rewards balance as of last statement +$10.00
Earned this period +$4.15
5% on gas at Costco +$1.15
2% on all other Costco and Costco.com purchases +$3.00
Total Earned: $4.15
"""


def test_parse_statement_summaries():
    chase = rewards.parse_chase(CHASE_BOX)
    assert chase["earned"] == 470 and chase["kind"] == "points"
    assert [x["value"] for x in chase["lines"]] == [300, 150, 0, 20]  # redemptions left out
    assert chase["lines"][1]["text"] == "1.5% (1.5 Pts)/$1 earned on all purchases"
    citi = rewards.parse_citi(CITI_BOX)
    assert citi == {"kind": "cash", "earned": 415, "lines": [
        {"text": "5% on gas at Costco", "value": 115},
        {"text": "2% on all other Costco and Costco.com purchases", "value": 300}]}
    assert rewards.parse_chase("no rewards here") is None
    assert rewards.parse_citi("no rewards here") is None


def add_statement(env, s, summary, rows):
    imp = m.Import(source="example", kind="statement", file_path="/x.pdf", sha256="1" * 64,
                   status="ok")
    s.add(imp)
    s.flush()
    st = m.Statement(card_id="example_everyday", import_id=imp.id, period_start="2026-09-01",
                     period_end="2026-09-30", previous_balance=0, new_balance=0,
                     pdf_path="/x.pdf", parse_method="regex", rewards_json=json.dumps(summary))
    s.add(st)
    s.flush()
    for i, (cents, typ, desc, linked) in enumerate(rows):
        s.add(m.Transaction(card_id="example_everyday", post_date="2026-09-10",
                            txn_date="2026-09-09", amount_cents=cents, description_raw=desc,
                            description_clean=desc, type=typ, dedupe_key=f"r{i}",
                            import_id=imp.id, statement_id=st.id if linked else None))
    s.commit()
    return st


def test_rewards_check_reproduces_by_hand(env):
    from fin.cards.history import resolve_history

    env.run()
    with env.S() as s:
        compose.accept(s, "example_everyday")
        # Everyday Visa: 4x U.S. supermarkets, 1x base. Kroger $100 = 400; Chevron $50 = 50;
        # a Kroger refund typed "credit" -$20 = -80; a statement credit (no merchant) ignored;
        # an unlinked purchase in the same dates isn't on this statement. Predicted 370.
        add_statement(env, s, {"kind": "points", "earned": 370, "lines": []}, [
            (10_000, "purchase", "KROGER", True), (5_000, "purchase", "CHEVRON", True),
            (-2_000, "credit", "KROGER", True),
            (-1_000, "credit", "EXAMPLE EATS DINING CREDIT", True),
            (7_000, "purchase", "KROGER", False)])
        resolve_history(s, env.paths, env.settings, use_ai=False)
        out = rewards.rewards_check(s, env.cards, active_profiles(s),
                                    load_facts(s, env.settings, env.cards), 24,
                                    today=date(2026, 10, 27))
    row = out["statements"][0]
    assert (row["stated"], row["predicted"], row["difference"], row["flagged"]) == \
        (370, 370, 0, False)
    assert (row["purchases"], row["refunds"]) == (2, 1)
    assert out["without_summary"] == []


def test_large_gap_is_flagged(env):
    env.run()
    with env.S() as s:
        compose.accept(s, "example_everyday")
        add_statement(env, s, {"kind": "points", "earned": 5_000, "lines": []},
                      [(10_000, "purchase", "ZZQX STORE", True)])
        out = rewards.rewards_check(s, env.cards, active_profiles(s),
                                    load_facts(s, env.settings, env.cards), 24,
                                    today=date(2026, 10, 27))
    row = out["statements"][0]
    # Unknown merchant at the base 1x: $100 = 100 points against 5,000 on the statement.
    assert row["predicted"] == 100 and row["flagged"] and row["unknown_merchants"] == 1


def test_fill_statement_rewards_once(env, monkeypatch):
    calls = []
    monkeypatch.setattr(rewards, "statement_rewards",
                        lambda path, issuer: calls.append(issuer) or {"kind": "cash",
                                                                      "earned": 1, "lines": []})
    with env.S() as s:
        add_statement(env, s, {}, [])
        s.execute(m.Statement.__table__.update().values(rewards_json=None))
        s.commit()
        assert rewards.fill_statement_rewards(s, env.cards) == 1
        assert rewards.fill_statement_rewards(s, env.cards) == 0
    assert calls == ["example"]


def test_api_rewards_check(env, client):
    n = len(env.calls())
    assert client.get("/api/cards/rewards-check").status_code == 200
    assert client.get("/api/cards/rewards-check", params={"months": 99}).status_code == 422
    assert len(env.calls()) == n


# --------------------------------------------------------------------------- schedule


def test_plist_runs_the_monthly_refresh_without_credentials(env, tmp_path):
    p = schedule.plist(env.paths, "/opt/x/bin/uv", ["/opt/x/bin", "/opt/y/bin"],
                       repo=tmp_path, home=tmp_path)
    assert p["ProgramArguments"] == ["/opt/x/bin/uv", "run", "--project", str(tmp_path), "fin",
                                     "cards", "refresh", "--if-due", "--trigger", "schedule"]
    assert p["StartCalendarInterval"] == {"Day": 1, "Hour": 9, "Minute": 30}
    assert set(p["EnvironmentVariables"]) == {"PATH", "HOME"}
    assert p["EnvironmentVariables"]["PATH"].startswith("/opt/x/bin:/opt/y/bin:/usr/bin")
    assert p["StandardOutPath"] == str(env.paths.logs / "cards-schedule.log")
    plistlib.loads(plistlib.dumps(p))  # a valid plist


def test_scheduled_run_records_a_missing_login(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "api_key")
    r = CliRunner().invoke(app, ["cards", "refresh", "--if-due", "--trigger", "schedule"])
    assert r.exit_code == 1 and "Subscription login: NOT available" in r.output
    with env.S() as s:
        run = s.scalar(select(m.CardRefreshRun).where(m.CardRefreshRun.trigger == "schedule"))
        assert run.status == "failed" and "auth" in run.errors_json
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "claude.ai")
    r = CliRunner().invoke(app, ["cards", "refresh", "--if-due", "--trigger", "schedule"])
    assert "Subscription login: claude.ai" in r.output
