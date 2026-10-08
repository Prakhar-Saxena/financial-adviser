"""SPEC §14.3 spike: verify the `claude -p` classifier setup on FAKE records only.

Usage: uv run python scripts/ai_spike.py [--sizes 3,50] [--runs-dir DIR]
Checks: ANTHROPIC_API_KEY absent, subscription auth, structured_output with --tools "",
schema-valid output with every key once, no new transcript under ~/.claude/projects/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from fin.ai.claude_cli import (
    AIError,
    check_keys,
    claude_bin,
    clean_env,
    load_task,
    require_subscription_auth,
    run_task,
)
from fin.ai.runs import new_run_dir
from fin.config import data_paths

FAKE_MERCHANTS = [
    "STARBUCKS", "SHELL OIL", "TRADER JOE'S", "NETFLIX.COM", "UBER TRIP", "CVS PHARMACY",
    "HOME DEPOT", "CHIPOTLE", "SPOTIFY USA", "DELTA AIR LINES", "MARRIOTT HOTEL", "WALGREENS",
    "TARGET", "BEST BUY", "PETCO", "VERIZON WIRELESS", "COMCAST XFINITY", "LYFT RIDE",
    "SAFEWAY", "DOORDASH DASHPASS", "APPLE.COM/BILL", "STAPLES", "SEPHORA", "NIKE.COM",
    "IKEA", "CHEVRON", "PANERA BREAD", "HULU", "GEICO AUTO", "AMC THEATRES",
]


def projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def transcript_files() -> set[Path]:
    root = projects_dir()
    return set(root.rglob("*.jsonl")) if root.exists() else set()


def encoded(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path.resolve()))


def fake_records(n: int) -> list[dict]:
    recs = []
    for i in range(n):
        m = FAKE_MERCHANTS[i % len(FAKE_MERCHANTS)]
        recs.append(
            {"key": f"k{i:03d}", "description": m, "amount": round(5 + i * 3.17, 2),
             "date": f"2026-09-{(i % 28) + 1:02d}"}
        )
    return recs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="3,50")
    ap.add_argument("--runs-dir", type=Path, default=None)
    args = ap.parse_args()

    report: dict = {"claude_version": None, "checks": {}, "batches": []}
    report["claude_version"] = subprocess.run(
        [claude_bin(), "--version"], capture_output=True, text=True, env=clean_env(), timeout=30
    ).stdout.strip()
    report["checks"]["api_key_absent_in_parent"] = "ANTHROPIC_API_KEY" not in os.environ
    report["checks"]["api_key_absent_in_child"] = "ANTHROPIC_API_KEY" not in clean_env()
    report["checks"]["auth_method"] = require_subscription_auth()

    runs_dir = args.runs_dir or data_paths().ai_runs
    task = load_task("merchant_categorize")
    ok = True
    for n in [int(s) for s in args.sizes.split(",")]:
        before = transcript_files()
        run_dir = new_run_dir(runs_dir, task.name)
        records = fake_records(n)
        entry: dict = {"n": n, "run_dir": str(run_dir)}
        try:
            res = run_task(task, {"records": records}, run_dir)
            missing, extra = check_keys([r["key"] for r in records], res.output["results"])
            entry.update(
                schema_valid=True, missing=missing, extra=extra, duration_ms=res.duration_ms,
                num_turns=res.meta.get("num_turns"), usage=res.meta.get("usage"),
                cost_estimate_usd=res.meta.get("total_cost_usd_estimate"),
                sample=res.output["results"][:3],
            )
            ok &= not missing and not extra
        except AIError as e:
            entry.update(schema_valid=False, error=str(e))
            ok = False
        new = transcript_files() - before
        entry["new_transcripts_for_run_dir"] = [
            str(p) for p in new if encoded(run_dir) in str(p)
        ]
        entry["new_transcripts_other"] = len(new) - len(entry["new_transcripts_for_run_dir"])
        ok &= not entry["new_transcripts_for_run_dir"]
        report["batches"].append(entry)

    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
