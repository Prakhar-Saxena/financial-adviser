"""`fin backup`: SQLite online backup into backups/ (SPEC §7). Keeps the newest KEEP copies."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

KEEP = 12


def backup(db_file: Path, backups: Path, keep: int = KEEP) -> Path:
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = backups / f"finance-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite"
    src, out = sqlite3.connect(db_file), sqlite3.connect(dest)
    try:
        with out:
            src.backup(out)
    finally:
        src.close()
        out.close()
    dest.chmod(0o600)
    for old in sorted(backups.glob("finance-*.sqlite"))[:-keep]:
        old.unlink()  # pre-migration-*.sqlite copies are never pruned
    return dest
