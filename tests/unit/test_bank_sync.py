# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bank site plumbing (docs/SPEC-checking.md §7.4, §7.5). Fictional bank only."""

import itertools
import json
from datetime import date

import pytest
from typer.testing import CliRunner

from fin import cli
from fin.config import CONFIG_DIR, CardsConfig, DataPaths, Settings
from fin.db import models as m
from fin.db import session_factory, upgrade
from fin.db.seed import sync_reference
from fin.ingest import inbox
from fin.sync import launcher, params

EXAMPLE = json.loads((CONFIG_DIR / "cards.example.json").read_text())


def bank_cards(site_id="mybank", **acct) -> CardsConfig:
    """The example config with its bank under another id: nothing may depend on 'bank'."""
    d = json.loads(json.dumps(EXAMPLE))
    d["issuers"][-1]["id"] = d["bank_accounts"][0]["site"] = site_id
    d["bank_accounts"][0].update(acct)
    return CardsConfig.model_validate(d)


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """CLI reads cards.json from here."""
    d = tmp_path / "config"
    d.mkdir()
    (d / "cards.json").write_text(json.dumps(bank_cards().model_dump(by_alias=True)))
    monkeypatch.setattr(cli, "CONFIG_DIR", d)
    return d


# ------------------------------------------------------------------ launcher


def test_bank_site_gets_the_generic_site_file_and_money_mode(tmp_path, monkeypatch):
    cards = bank_cards(money_url_words=["harborpay"])
    site_file = tmp_path / "bank.md"
    site_file.write_text("# Bank site (fictional)\n")
    monkeypatch.setattr(launcher, "BANK_SITE_FILE", site_file)
    plan = launcher.plan_session(DataPaths(tmp_path / "data"), Settings(), cards.site("mybank"),
                                 cards=cards)
    assert plan.money_site and plan.site_file == site_file and not plan.trusted
    assert plan.money_url_words == ["harborpay"]
    assert "# Bank site (fictional)" in launcher.build_prompt(plan)
    chase = launcher.plan_session(DataPaths(tmp_path / "data"), Settings(), cards.site("chase"),
                                  cards=cards)
    assert not chase.money_site and chase.site_file.name == "chase.md"


@pytest.mark.parametrize("settings", [
    {"sync": {"trusted_sites": ["mybank"]}},
    {"sync": {"site_browser": {"mybank": "extension"}}},
])
def test_bank_site_never_trusted_or_in_everyday_chrome(tmp_path, settings):
    cards = bank_cards()
    with pytest.raises(ValueError, match="approve mode"):
        launcher.plan_session(DataPaths(tmp_path), Settings.model_validate(settings),
                              cards.site("mybank"), cards=cards)
    with pytest.raises(ValueError):
        launcher.plan_session(DataPaths(tmp_path), Settings(), cards.site("mybank"),
                              trusted=True, cards=cards)


# ------------------------------------------------------------------ params


@pytest.fixture
def Session(tmp_path):
    db = tmp_path / "t.sqlite"
    upgrade(db)
    factory = session_factory(db)
    from fin.config import load_taxonomy

    with factory() as s:
        sync_reference(s, bank_cards(), load_taxonomy())
        s.add(m.Import(id=1, source="chase", kind="transactions", file_path="x", sha256="h",
                       status="ok"))
        s.commit()
    return factory


_keys = itertools.count()


def add_txn(s, card, post_date):
    s.add(m.Transaction(card_id=card, post_date=post_date, amount_cents=100, description_raw="x",
                        description_clean="x", type="purchase", dedupe_key=f"k{next(_keys)}",
                        import_id=1))


def test_first_run_reaches_back_to_the_earliest_card_transaction(Session):
    cards = bank_cards()
    with Session() as s:
        add_txn(s, "amex_gold", "2025-03-04")
        add_txn(s, "citi_costco", "2026-01-10")
        s.commit()
        p, since, until = params.run_params(s, cards, Settings(), "mybank", date(2026, 11, 13))
    (acct,) = p["accounts"]
    assert acct["card_id"] == "checking" and acct["first_run"]
    assert acct["transactions_from"] == since == "2025-03-04" and until == "2026-11-13"
    assert acct["statements_already_imported"] == [] and "cards" not in p


def test_later_runs_overlap_and_list_imported_statements(Session):
    cards = bank_cards()
    with Session() as s:
        add_txn(s, "amex_gold", "2025-03-04")
        add_txn(s, "checking", "2026-11-01")
        s.add(m.BankStatement(card_id="checking", period_start="2026-09-02",
                              period_end="2026-10-01", opening_balance=0, closing_balance=0,
                              pdf_path="x", parse_method="regex"))
        s.commit()
        p, since, _ = params.run_params(s, cards, Settings(), "mybank", date(2026, 11, 13))
    (acct,) = p["accounts"]
    assert not acct["first_run"] and since == "2026-10-18"  # 14 days' overlap
    assert acct["statements_already_imported"] == ["2026-10-01"]


def test_site_with_no_cards_and_no_history(Session):
    d = json.loads(json.dumps(EXAMPLE))
    d["cards"], d["issuers"] = [], [d["issuers"][-1]]
    cards = CardsConfig.model_validate(d)
    with Session() as s:
        p, since, _ = params.run_params(s, cards, Settings(), "bank", date(2026, 11, 13))
    assert p["accounts"][0]["first_run"] and since == "2026-08-13"  # history_months
    with Session() as s:  # an issuer with nothing to sync doesn't fail on min([])
        assert params.run_params(s, cards, Settings(), "nothing", date(2026, 11, 13))[1] \
            == "2026-11-13"


def test_allowed_card_ids_include_accounts():
    cards = bank_cards()
    assert params.allowed_card_ids(cards, "mybank") == {"checking"}
    assert "checking" not in params.allowed_card_ids(cards, "chase")


# ------------------------------------------------------------------ CLI


@pytest.mark.parametrize("flag", ["--driven", "--trusted"])
def test_sync_bank_refuses_driven_and_trusted(config_dir, flag):
    r = CliRunner().invoke(cli.app, ["sync", "mybank", flag])
    assert r.exit_code == 2 and "approve mode" in r.output


def test_sync_bank_waits_for_its_site_file(config_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "BANK_SITE_FILE", tmp_path / "missing.md")
    r = CliRunner().invoke(cli.app, ["sync", "mybank"])
    assert r.exit_code == 2 and "Phase B1" in r.output


def test_weekly_puts_bank_sites_after_amex(config_dir):
    assert cli.weekly_order() == ["chase", "citi", "amex", "mybank", "amazon", "costco"]


def test_no_cards_json_means_no_bank_sites(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    assert cli.weekly_order() == list(cli.WEEKLY_ORDER)


# ------------------------------------------------------------------ inbox


def test_bank_statement_detected_by_its_markers(tmp_path):
    cards = bank_cards(statement_markers=["EXAMPLE BANK CHECKING SUMMARY"])
    stmt = tmp_path / "stmt.txt"
    stmt.write_text("Example Bank\nExample Bank Checking Summary\nOpening balance 0.00\n")
    assert inbox.detect(stmt, cards) == ("mybank", "statement")
    assert inbox.statement_card(stmt, "mybank", cards) == "checking"
    stmt.write_text("Nothing to see\n")
    assert inbox.detect(stmt, cards) is None
    assert inbox.detect(stmt, bank_cards()) is None  # no markers: no guess


def test_statement_card_picks_an_account_by_its_ending(tmp_path):
    d = json.loads(json.dumps(EXAMPLE))
    d["bank_accounts"] = [{**d["bank_accounts"][0], "account_ending": "0042"},
                          {**d["bank_accounts"][0], "id": "checking2", "account_ending": "0043"}]
    cards = CardsConfig.model_validate(d)
    stmt = tmp_path / "stmt.txt"
    stmt.write_text("Checking account ending 0043\n")
    assert inbox.statement_card(stmt, "bank", cards) == "checking2"
    stmt.write_text("No ending printed\n")
    assert inbox.statement_card(stmt, "bank", cards) is None
