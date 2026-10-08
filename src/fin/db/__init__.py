"""Database engine, sessions and migrations."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS = Path(__file__).parent / "migrations"


def make_engine(db_file: Path) -> Engine:
    engine = create_engine(f"sqlite:///{db_file}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    return engine


def alembic_config(db_file: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


def upgrade(db_file: Path) -> None:
    command.upgrade(alembic_config(db_file), "head")


def session_factory(db_file: Path) -> sessionmaker[Session]:
    return sessionmaker(make_engine(db_file))


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config(Path("unused.sqlite"))).get_current_head()


def current_revision(db_file: Path) -> str | None:
    from alembic.migration import MigrationContext

    with make_engine(db_file).connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def ensure_current(db_file: Path, backups: Path) -> str | None:
    """Bring an existing DB up to the latest schema. Backs it up first.

    Returns the revision it upgraded from, or None if it was already current.
    """
    import sqlite3
    from datetime import UTC, datetime

    before = current_revision(db_file)
    if before == head_revision():
        return None
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = backups / f"pre-migration-{before or 'empty'}-{stamp}.sqlite"
    src, out = sqlite3.connect(db_file), sqlite3.connect(dest)
    with out:
        src.backup(out)
    src.close()
    out.close()
    dest.chmod(0o600)
    upgrade(db_file)
    return before or "empty"
