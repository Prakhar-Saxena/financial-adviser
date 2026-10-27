# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Card optimizer Phase C0: settings, models, schema, rebuild, argv roles, lock, status."""

import json
import subprocess
import sys
import time

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from fin.ai import claude_cli as cc
from fin.cards import models as cm
from fin.cli import app
from fin.config import (
    CONFIG_DIR,
    Settings,
    data_paths,
    load_cards,
    load_reward_categories,
    load_settings,
    load_taxonomy,
)
from fin.db import models as m
from fin.db import session_factory, upgrade
from fin.db.seed import sync_reference
from fin.doctor import check_cards
from fin.rebuild import DERIVED, rebuild

EXAMPLE = CONFIG_DIR / "cards.example.json"


@pytest.fixture
def Session(tmp_path):
    f = tmp_path / "t.sqlite"
    upgrade(f)
    S = session_factory(f)
    with S() as s:
        sync_reference(s, load_cards(EXAMPLE), load_taxonomy())
    return S


# ------------------------------------------------------------------ settings


def test_cards_settings_load():
    cs = load_settings().cards
    assert cs.research_model == "sonnet" and cs.extract_model == "sonnet"
    assert cs.merchant_model == "haiku" and cs.auto_publish is False
    assert cs.default_amount_cents == 10_000 and cs.refresh_days == 30
    cards = load_cards(EXAMPLE)
    for c in cards.cards:
        assert cs.product_aliases.get(c.id), c.id
    prime = cards.card("chase_prime_visa")
    assert cs.domains_for(prime, cards.site("chase")) == [
        "chase.com", "consumerfinance.gov", "amazon.com"]


@pytest.mark.parametrize("cards", [
    {"merchant_model": "sonnet"},
    {"doc_domains": {"common": ["https://consumerfinance.gov"]}},
    {"doc_domains": {"common": ["*.chase.com"]}},
    {"doc_domains": {"Bad Key": ["chase.com"]}},
    {"user_agent": "Mozilla/5.0 (Macintosh)"},
    {"valuations": {"amex_mr": 0}},
    {"unknown_setting": 1},
])
def test_cards_settings_invalid(cards):
    with pytest.raises(ValidationError):
        Settings.model_validate({"cards": cards})


def test_reward_categories():
    rc = load_reward_categories()
    for cid in ("dining", "supermarkets", "wholesale_clubs", "discount_stores", "gas_stations",
                "airlines", "travel_agencies", "streaming_tv", "other"):
        assert cid in rc.ids
    assert len(rc.ids) == len(set(rc.ids)) == 33
    assert "5411" in next(c for c in rc.categories if c.id == "supermarkets").mcc_hints


# -------------------------------------------------------------------- models


def ev(q="Earn 4X points at U.S. supermarkets"):
    return [cm.Evidence(doc="D1", page=1, quote=q)]


def profile(**kw):
    base = dict(
        card_id="example_visa", currency="cash_back",
        annual_fee_cents=cm.Fee(value=0, evidence=ev("No annual fee")),
        foreign_txn_fee_pct=cm.Fee(value=3, evidence=ev("3% of each transaction")),
        earn_rules=[cm.EarnRule(rate=1, is_base=True, evidence=ev("1% on everything else"))],
    )
    return cm.CardProfile(**{**base, **kw})


def test_profile_needs_exactly_one_base_rule():
    profile()
    rule = cm.EarnRule(rate=4, categories=["supermarkets"], evidence=ev())
    with pytest.raises(ValidationError):
        profile(earn_rules=[rule])
    with pytest.raises(ValidationError):
        profile(earn_rules=[cm.EarnRule(rate=1, is_base=True, evidence=ev())] * 2)


def test_rule_needs_scope_and_evidence():
    with pytest.raises(ValidationError):
        cm.EarnRule(rate=4, evidence=ev())
    with pytest.raises(ValidationError):
        cm.EarnRule(rate=4, categories=["dining"], evidence=[])
    with pytest.raises(ValidationError):
        cm.Evidence(doc="D1", quote="x" * (cm.MAX_QUOTE + 1))


def test_identities_ignore_numbers():
    a = cm.EarnRule(rate=4, categories=["supermarkets", "dining"], geography="us", evidence=ev())
    b = cm.EarnRule(rate=3, categories=["dining", "supermarkets"], geography="us", evidence=ev())
    c = cm.EarnRule(rate=4, categories=["supermarkets", "dining"], evidence=ev())
    assert a.identity() == b.identity() != c.identity()
    d1 = cm.Credit(name="Dining Credit", amount_cents=1000, period="month", evidence=ev())
    d2 = cm.Credit(name="dining  credit", amount_cents=1500, period="month", evidence=ev())
    assert d1.identity() == d2.identity()


def test_normalize_name():
    assert cm.normalize_name("  Barnes & Noble®, Inc.") == "barnes and noble inc"
    assert cm.normalize_name("WHOLEFDS MKT #123") == "wholefds mkt 123"


# -------------------------------------------------------------------- schema


def test_new_review_kinds_allowed(Session):
    with Session() as s:
        for kind in m.CARD_REVIEW_KINDS:
            s.add(m.ReviewItem(kind=kind))
        s.commit()


def test_card_facts_unique_with_nulls(Session):
    with Session() as s:
        s.add(m.CardFact(kind="valuation", ref="amex_mr", value="1.2"))
        s.commit()
        s.add(m.CardFact(kind="valuation", ref="amex_mr", value="1.5"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.add(m.CardFact(kind="enrolled", card_id="amex_platinum", ref="resy", value="yes"))
        s.add(m.CardFact(kind="credit_used", card_id="amex_platinum", ref="resy",
                         period="2026-10", value="1000"))
        s.add(m.CardFact(kind="credit_used", card_id="amex_platinum", ref="resy",
                         period="2026-11", value="0"))
        s.commit()


def test_one_active_and_one_pending_profile_per_card(Session):
    def version(n, status):
        return m.CardProfileVersion(card_id="amex_gold", version=n, status=status,
                                    profile_json="{}", content_hash=str(n) * 64)

    with Session() as s:
        s.add_all([version(1, "superseded"), version(2, "active"), version(3, "pending"),
                   version(4, "rejected")])
        s.commit()
        for status in ("active", "pending"):
            s.add(version(10, status))
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()


CARD_TABLES = [m.CardDocument, m.CardDiscovery, m.CardDocVersion, m.CardExtraction,
               m.CardProfileVersion, m.MerchantProfileRow, m.MerchantAlias, m.CardFact,
               m.CardRefreshRun]


def test_card_tables_never_reference_rebuilt_tables():
    rebuilt = {t.__tablename__ for t in DERIVED} | {"review_items", "rules"}
    for model in CARD_TABLES:
        for fk in model.__table__.foreign_keys:
            assert fk.column.table.name not in rebuilt, (model.__tablename__, fk.target_fullname)


def test_rebuild_keeps_card_tables_and_review_kinds(Session):
    with Session() as s:
        doc = m.CardDocument(card_id="amex_gold", url="https://www.americanexpress.com/x",
                             doc_type="product_page")
        s.add(doc)
        s.flush()
        s.add(m.CardDocVersion(document_id=doc.id, sha256="a" * 64))
        s.add(m.CardExtraction(card_id="amex_gold", task="card_terms", input_key="k",
                               prompt_version="1", model="sonnet", status="ok"))
        s.add(m.CardProfileVersion(card_id="amex_gold", version=1, status="active",
                                   profile_json="{}", content_hash="h" * 64))
        s.add(m.MerchantProfileRow(key="whole_foods", display_name="Whole Foods Market",
                                   source="user"))
        s.flush()
        s.add(m.MerchantAlias(alias_norm="whole foods", profile_key="whole_foods", kind="name",
                              source="user"))
        s.add(m.CardFact(kind="valuation", ref="amex_mr", value="1.3"))
        s.add(m.CardRefreshRun(trigger="cli", status="ok"))
        s.add(m.CardDiscovery(card_id="amex_gold", reason="first discovery", status="ok"))
        for kind in (*m.CARD_REVIEW_KINDS, "sync_gap", "uncategorized"):
            s.add(m.ReviewItem(kind=kind))
        s.commit()
        counts = {t: s.query(t).count() for t in CARD_TABLES}

        rebuild(s)
        assert {t: s.query(t).count() for t in CARD_TABLES} == counts
        kinds = set(s.scalars(select(m.ReviewItem.kind)))
        assert kinds == {*m.CARD_REVIEW_KINDS, "sync_gap"}


# ------------------------------------------------------------- claude argv


def flag(argv, name):
    return argv[argv.index(name) + 1]


def allowed(argv):
    i = argv.index("--allowedTools") + 1
    out = []
    while not argv[i].startswith("--"):
        out.append(argv[i])
        i += 1
    return out


def test_research_argv(fake_claude):
    task = cc.load_task("card_discover")
    cs = load_settings().cards
    inv = cc.research_invocation(task, cs.research_model, ["chase.com", "consumerfinance.gov"],
                                 max_turns=cs.discover_max_turns,
                                 max_searches=cs.discover_max_searches)
    a = inv.argv
    assert "--bare" not in a
    assert flag(a, "--model") == "sonnet"
    assert flag(a, "--tools") == "WebSearch,WebFetch"
    assert flag(a, "--setting-sources") == ""
    assert flag(a, "--permission-mode") == "dontAsk"
    assert flag(a, "--disallowedTools") == "mcp__*"
    assert flag(a, "--max-turns") == "30"
    for f in ("--safe-mode", "--strict-mcp-config", "--no-chrome", "--no-session-persistence"):
        assert f in a
    assert allowed(a) == [
        "WebSearch",
        "WebFetch(domain:chase.com)", "WebFetch(domain:*.chase.com)",
        "WebFetch(domain:consumerfinance.gov)", "WebFetch(domain:*.consumerfinance.gov)",
    ]
    assert not any(r == "WebFetch" or "domain:*)" in r for r in allowed(a))
    assert inv.env_extra == {"CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": "20"}
    assert flag(a, "--system-prompt-file").endswith("prompts/card_discover.md")
    schema = json.loads(flag(a, "--json-schema"))
    assert schema["properties"]["documents"]["maxItems"] == 12


@pytest.mark.parametrize("domains", [[], ["*.chase.com"], ["https://chase.com"], ["chase"]])
def test_research_refuses_bad_domains(fake_claude, domains):
    with pytest.raises(cc.AIError):
        cc.research_invocation(cc.load_task("card_discover"), "sonnet", domains)


@pytest.mark.parametrize("task_name", ["card_terms", "card_benefits"])
def test_extract_argv_has_no_tools(fake_claude, task_name):
    task = cc.load_task(task_name)
    a = cc.extract_invocation(task, load_settings().cards.extract_model).argv
    assert flag(a, "--tools") == "" and flag(a, "--model") == "sonnet"
    assert "--allowedTools" not in a and "--bare" not in a
    for f in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert f in a
    schema = flag(a, "--json-schema")
    assert cc.REWARD_CATEGORY_PLACEHOLDER not in schema
    assert cc.BENEFIT_KINDS_PLACEHOLDER not in schema
    if task_name == "card_terms":
        rule = json.loads(schema)["properties"]["earn_rules"]["items"]["properties"]
        assert rule["categories"]["items"]["enum"] == load_reward_categories().ids
    else:
        kind = json.loads(schema)["properties"]["benefits"]["items"]["properties"]["kind"]
        assert kind["enum"] == list(cm.BENEFIT_KINDS)


def test_extract_refuses_classifier_tasks(fake_claude):
    with pytest.raises(cc.AIError):
        cc.extract_invocation(cc.load_task("merchant_categorize"), "sonnet")


def test_merchant_argv(fake_claude):
    plain = cc.merchant_invocation(cc.load_task("merchant_profile")).argv
    assert flag(plain, "--model") == "haiku" and flag(plain, "--tools") == ""
    assert "--effort" not in plain
    low = cc.merchant_invocation(cc.load_task("merchant_profile"), effort="low").argv
    assert flag(low, "--effort") == "low"
    task = cc.load_task("merchant_profile_web", schema_name="merchant_profile")
    inv = cc.merchant_invocation(task, web=True)
    web = inv.argv
    assert flag(web, "--model") == "haiku" and flag(web, "--tools") == "WebSearch"
    assert allowed(web) == ["WebSearch"] and flag(web, "--permission-mode") == "dontAsk"
    assert flag(web, "--setting-sources") == "" and "--no-chrome" in web
    assert inv.env_extra["CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"] == "3"


def test_classifier_argv_unchanged(fake_claude):
    a = cc.build_argv(cc.load_task("merchant_categorize"))
    assert "--setting-sources" not in a and "--permission-mode" not in a
    assert "--no-chrome" not in a and "--effort" not in a
    with pytest.raises(cc.AIError):
        cc.build_argv(cc.load_task("merchant_categorize"), model="sonnet")


# ------------------------------------------------------- calls with the fake


def test_research_call_env_and_audit(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
    task = cc.load_task("card_discover")
    inv = cc.research_invocation(task, "sonnet", ["example.com"])
    run = tmp_path / "run"
    res = cc.run_task(task, {"card": {"id": "x"}}, run, invocation=inv, timeout=60)
    assert len(res.output["documents"]) == 5
    calls = [json.loads(line) for line in fake_claude.read_text().splitlines()]
    assert all("ANTHROPIC_API_KEY" not in c["env_keys"] for c in calls)
    assert "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION" in calls[-1]["env_keys"]
    meta = json.loads((run / "meta.json").read_text())
    assert meta["role"] == "research" and meta["allowed_domains"] == ["example.com"]
    assert meta["model"] == "sonnet" and meta["permission_denials"] == []


def test_env_extra_cannot_carry_api_keys(fake_claude, tmp_path):
    task = cc.load_task("card_terms")
    inv = cc.Invocation(cc.extract_invocation(task, "sonnet").argv, "sonnet", "extract",
                        {"ANTHROPIC_API_KEY": "sk"})
    with pytest.raises(AssertionError):
        cc.run_task(task, {}, tmp_path / "r", invocation=inv)


@pytest.mark.parametrize("mode", ["bad_quote", "bad_number"])
def test_fake_bad_evidence_modes(fake_claude, tmp_path, monkeypatch, mode):
    good = cc.run_task(cc.load_task("card_terms"), {}, tmp_path / "a",
                       invocation=cc.extract_invocation(cc.load_task("card_terms"), "sonnet"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    bad = cc.run_task(cc.load_task("card_terms"), {}, tmp_path / "b",
                      invocation=cc.extract_invocation(cc.load_task("card_terms"), "sonnet"))
    g, b = good.output["earn_rules"][0], bad.output["earn_rules"][0]
    if mode == "bad_quote":
        assert g["evidence"] != b["evidence"] and g["rate"] == b["rate"]
    else:
        assert g["evidence"] == b["evidence"] and b["rate"] == g["rate"] + 1


@pytest.mark.parametrize("mode", ["schema_invalid", "badjson", "error"])
def test_card_task_errors(fake_claude, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    task = cc.load_task("card_benefits")
    with pytest.raises(cc.AIError):
        cc.run_task(task, {}, tmp_path / "r", invocation=cc.extract_invocation(task, "sonnet"))


def test_merchant_profile_fake_returns_every_key(fake_claude, tmp_path):
    task = cc.load_task("merchant_profile")
    recs = [{"key": "q1", "query": "whole foods"}, {"key": "q2", "query": "doordash"}]
    res = cc.run_task(task, {"records": recs}, tmp_path / "r",
                      invocation=cc.merchant_invocation(task))
    assert cc.check_keys(["q1", "q2"], res.output["results"]) == ([], [])


# ---------------------------------------------------------------------- lock


def test_lock_is_cross_process(tmp_path):
    path = tmp_path / "locks" / "claude.lock"
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, os, sys, time\n"
        f"os.makedirs({str(path.parent)!r}, exist_ok=True)\n"
        f"fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(3)\n")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        with pytest.raises(cc.AIBusyError), cc.claude_lock(path, wait=0.3):
            pass
        assert time.monotonic() - started < 2
    finally:
        holder.kill()
        holder.wait()
    with cc.claude_lock(path, wait=1):
        assert path.stat().st_mode & 0o077 == 0


def test_calls_take_the_lock(fake_claude, tmp_path, data_dir):
    lock = data_dir / "locks" / "claude.lock"
    with cc.claude_lock(lock), pytest.raises(cc.AIBusyError):
        cc.run_task(cc.load_task("merchant_categorize"),
                    {"records": [{"key": "k", "description": "X", "amount": 1, "date": "d"}]},
                    tmp_path / "r", lock_wait=0.2)


# ------------------------------------------------------------- CLI, doctor


def test_init_creates_card_dirs_and_status_is_empty(data_dir, monkeypatch, tmp_path):
    monkeypatch.setattr("fin.cli.REPO_ROOT", tmp_path / "not-a-repo")
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    for sub in ("card-docs", "card-docs/files", "card-docs/text", "card-docs/manual", "locks"):
        assert (data_dir / sub).stat().st_mode & 0o777 == 0o700, sub
    out = runner.invoke(app, ["cards", "status"])
    assert out.exit_code == 0, out.output
    assert "amex_platinum" in out.output and "never" in out.output
    assert "No card terms yet" in out.output

    results = {r.name: r for r in check_cards(data_paths())}
    assert results["card-docs mode"].status == "PASS"
    assert results["Cards settings"].status == "PASS"
    assert results["Card product aliases"].status == "PASS"


def test_run_leftovers_removed(fake_claude, tmp_path, monkeypatch, data_dir):
    projects = tmp_path / "projects"
    monkeypatch.setattr(cc, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr("fin.ai.claude_cli.CLAUDE_PROJECTS", projects)
    run = data_dir / "ai-runs" / "20261026T000000Z_card_discover"
    run.mkdir(parents=True)
    other = projects / "-Users-someone-workspace-repo"
    leftover = projects / cc.project_slug(run) / "session" / "tool-results"
    for d in (other, leftover):
        d.mkdir(parents=True)
    (leftover / "webfetch-1.pdf").write_bytes(b"%PDF")

    from fin.doctor import check_claude_leftovers
    paths = data_paths()
    assert check_claude_leftovers(paths, projects).status == "WARN"

    task = cc.load_task("card_discover")
    cc.run_task(task, {}, run, invocation=cc.research_invocation(task, "sonnet", ["example.com"]))
    assert not (projects / cc.project_slug(run)).exists()
    assert other.exists()
    assert check_claude_leftovers(paths, projects).status == "PASS"
