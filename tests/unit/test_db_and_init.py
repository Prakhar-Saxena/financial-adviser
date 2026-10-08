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
              "allocations", "review_items", "ai_cache", "sync_runs", "user_overrides"):
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
