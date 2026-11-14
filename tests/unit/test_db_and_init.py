# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

import stat
import subprocess

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from fin.cli import app
from fin.config import CONFIG_DIR, load_cards, load_taxonomy
from fin.db import make_engine, models, session_factory, upgrade
from fin.db.seed import sync_reference


@pytest.fixture
def db(tmp_path):
    f = tmp_path / "t.sqlite"
    upgrade(f)
    return f


def test_migration_matches_models(db):
    engine = make_engine(db)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), models.Base.metadata)
    assert diff == []
    tables = set(inspect(engine).get_table_names())
    for t in ("transactions", "statements", "orders", "order_items", "order_charges", "matches",
              "allocations", "review_items", "ai_cache", "sync_runs", "user_overrides",
              "card_documents", "card_doc_versions", "card_extractions", "card_profiles",
              "merchant_profiles", "merchant_aliases", "card_facts", "card_refresh_runs",
              "card_discoveries"):
        assert t in tables


def test_seed_is_idempotent(db):
    cards, tax = load_cards(CONFIG_DIR / "cards.example.json"), load_taxonomy()
    Session = session_factory(db)
    for _ in range(2):
        with Session() as s:
            counts = sync_reference(s, cards, tax)
    with Session() as s:
        assert s.execute(text("select count(*) from card_holders")).scalar() == 24
        assert s.execute(text("select count(*) from categories")).scalar() == counts["categories"]


def test_constraints(db):
    Session = session_factory(db)
    with Session() as s:
        sync_reference(s, load_cards(CONFIG_DIR / "cards.example.json"), load_taxonomy())
    with Session() as s:
        s.add(models.ReviewItem(kind="not_a_kind"))
        with pytest.raises(IntegrityError):
            s.commit()
    with Session() as s:
        s.add(models.Import(source="chase", kind="transactions", file_path="a", sha256="x" * 64,
                            status="ok"))
        s.add(models.Import(source="chase", kind="transactions", file_path="b", sha256="x" * 64,
                            status="ok"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_fin_init(data_dir, monkeypatch, tmp_path):
    monkeypatch.setattr("fin.cli.REPO_ROOT", tmp_path / "not-a-repo")
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    for sub in ("db", "inbox", "raw/sync-logs", "browser-profiles", "sync-sessions", "ai-runs",
                "secrets", "debug", "logs", "backups"):
        assert (data_dir / sub).is_dir()
    db_file = data_dir / "db" / "finance.sqlite"
    assert stat.S_IMODE(db_file.stat().st_mode) == 0o600
    # Idempotent
    assert CliRunner().invoke(app, ["init"]).exit_code == 0


def test_fin_init_refuses_icloud(monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.setenv("FIN_DATA_DIR", str(Path.home() / "Documents" / "fin-test-never-created"))
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 1 and "iCloud" in result.output
    assert not (Path.home() / "Documents" / "fin-test-never-created").exists()


def test_later_phase_commands_say_so():
    r = CliRunner().invoke(app, ["sync", "gmail"])  # Phase 4
    assert r.exit_code == 2 and "Phase 4" in r.output


def test_git_available():
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0


def test_ensure_current_upgrades_old_db_with_backup(tmp_path):
    from alembic import command
    from sqlalchemy import inspect

    from fin.db import alembic_config, current_revision, ensure_current, head_revision

    db = tmp_path / "old.sqlite"
    command.upgrade(alembic_config(db), "0001")
    eng = make_engine(db)
    with eng.begin() as c:
        c.execute(text("insert into people (id, display_name, full_name, relationship) "
                       "values ('self', 'Me', 'Alex Example', 'self')"))
    assert ensure_current(db, tmp_path / "backups") == "0001"
    assert current_revision(db) == head_revision()
    assert "person_source" in {c["name"] for c in inspect(make_engine(db)).get_columns(
        "transactions")}
    with make_engine(db).connect() as c:
        assert c.execute(text("select count(*) from people")).scalar() == 1
    (backup,) = (tmp_path / "backups").glob("pre-migration-0001-*.sqlite")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert ensure_current(db, tmp_path / "backups") is None  # already current


def test_safe_error_hides_sql_parameters(db):
    from fin.ingest.inbox import safe_error

    engine = make_engine(db)
    try:
        with engine.begin() as c:
            c.execute(text("insert into nope (x) values (:v)"), {"v": "SECRET MERCHANT 12.34"})
    except Exception as e:  # noqa: BLE001
        msg = safe_error(e)
    assert msg.startswith("OperationalError: no such table: nope")
    assert "SECRET" not in msg and "[SQL" not in msg
    assert safe_error(ValueError("bad\nsecond line")) == "ValueError: bad"


def test_backup_keeps_newest(tmp_path, db):
    import time

    from fin.backup import backup

    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "pre-migration-0001-x.sqlite").write_text("keep me")
    made = []
    for _ in range(3):
        made.append(backup(db, tmp_path / "b", keep=2))
        time.sleep(1.1)
    left = sorted(p.name for p in (tmp_path / "b").iterdir())
    assert left == sorted([made[1].name, made[2].name, "pre-migration-0001-x.sqlite"])
    assert stat.S_IMODE(made[2].stat().st_mode) == 0o600
    with make_engine(made[2]).connect() as c:
        assert "transactions" in {r[0] for r in c.execute(text(
            "select name from sqlite_master where type='table'"))}


# ------------------------------------------------- checking (docs/SPEC-checking.md §6)


def test_migration_0009_keeps_every_row_and_widens_the_checks(tmp_path):
    """0009 recreates `transactions`: rows that point at it (allocations, matches) survive,
    the new types and flow kinds are allowed, unknown ones refused, and it downgrades."""
    from alembic import command

    from fin.db import alembic_config

    db = tmp_path / "old.sqlite"
    command.upgrade(alembic_config(db), "0008")
    with make_engine(db).begin() as c:
        c.execute(text("insert into cards values ('visa', 'chase', 'Example Visa', 'visa', "
                       "'credit')"))
        c.execute(text("insert into categories values ('groceries', null, 'Groceries')"))
        c.execute(text("insert into imports (id, source, kind, file_path, sha256, status, "
                       "row_count, imported_at) values (1, 'chase', 'transactions', 'x', 'h', "
                       "'ok', 1, 't')"))
        c.execute(text("insert into transactions (id, card_id, post_date, amount_cents, "
                       "description_raw, description_clean, type, source, dedupe_key, import_id, "
                       "review_status) values (7, 'visa', '2026-09-01', 1234, 'X', 'X', "
                       "'purchase', 'csv', 'k', 1, 'ok')"))
        c.execute(text("insert into allocations (transaction_id, category_id, amount_cents, "
                       "source) values (7, 'groceries', 1234, 'user')"))
        c.execute(text("insert into matches (transaction_id, method, confidence) "
                       "values (7, 'user', 1.0)"))
    command.upgrade(alembic_config(db), "head")
    Session = session_factory(db)
    with Session() as s:
        assert s.execute(text("select count(*) from allocations")).scalar() == 1
        assert s.execute(text("select count(*) from matches")).scalar() == 1
        t = s.get(models.Transaction, 7)
        assert (t.flow_kind, t.bank_type, t.balance_after_cents) == (None, None, None)
        t.type, t.flow_kind = "transfer", "card_payment"
        s.add(models.ReviewItem(kind="uncertain_transfer", ref_table="transactions", ref_id="7"))
        s.commit()
    for field, value in (("type", "wire"), ("flow_kind", "lottery")):
        with Session() as s:
            setattr(s.get(models.Transaction, 7), field, value)
            with pytest.raises(IntegrityError):
                s.commit()
    with make_engine(db).begin() as c:
        c.execute(text("update transactions set type = 'purchase', flow_kind = null"))
        c.execute(text("delete from review_items"))
    command.downgrade(alembic_config(db), "0008")
    with make_engine(db).connect() as c:
        assert c.execute(text("select count(*) from allocations")).scalar() == 1
        assert "flow_kind" not in {x["name"] for x in inspect(c).get_columns("transactions")}


def test_seed_adds_bank_accounts_as_cards_rows(db):
    cards, tax = load_cards(CONFIG_DIR / "cards.example.json"), load_taxonomy()
    with session_factory(db)() as s:
        counts = sync_reference(s, cards, tax)
        row = s.get(models.Card, "checking")
        assert counts["bank_accounts"] == 1
        assert (row.issuer, row.network, row.card_type) == ("bank", "none", "checking")
        assert s.execute(text("select count(*) from card_holders where card_id = 'checking'")
                         ).scalar() == 0


def test_transfer_link_cascades_with_its_transactions(db):
    """`link_statements` deletes a statement-sourced card row when its CSV row arrives; a
    link left from the last run must not block that (SPEC-checking §6)."""
    Session = session_factory(db)
    with Session() as s:
        sync_reference(s, load_cards(CONFIG_DIR / "cards.example.json"), load_taxonomy())
        s.add(models.Import(id=1, source="x", kind="transactions", file_path="x", sha256="h",
                            status="ok"))
        for i, (card, amt, typ) in enumerate([("checking", 5000, "transfer"),
                                              ("amex_gold", -5000, "payment")], 1):
            s.add(models.Transaction(id=i, card_id=card, post_date="2026-09-01",
                                     amount_cents=amt, description_raw="x",
                                     description_clean="x", type=typ, dedupe_key=f"k{i}",
                                     import_id=1))
        s.flush()
        s.add(models.TransferLink(bank_txn_id=1, card_txn_id=2, method="named", days_apart=0,
                                  confidence=1.0))
        s.commit()
        s.delete(s.get(models.Transaction, 2))
        s.commit()
        assert s.execute(text("select count(*) from transfer_links")).scalar() == 0
