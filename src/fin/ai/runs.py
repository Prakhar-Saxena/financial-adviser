"""Audit-trail folders for classifier calls: ai-runs/<timestamp>_<task>/ (SPEC §6)."""

from __future__ import annotations

import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path


def new_run_dir(ai_runs: Path, task: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run = ai_runs / f"{stamp}_{task}"
    run.mkdir(parents=True, mode=0o700)
    return run


def prune(ai_runs: Path, days: int = 90) -> int:
    """Delete run folders older than `days`. Returns how many were removed."""
    if not ai_runs.exists():
        return 0
    cutoff = time.time() - timedelta(days=days).total_seconds()
    removed = 0
    for d in ai_runs.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d)
            removed += 1
    return removed
