"""`fin rebuild`: regenerate every derived table from raw/ (SPEC §4, §16).

Keeps: imports (the evidence index), user_overrides, user rules, sync_runs and sync_gap
review items. Everything else is deleted and rebuilt by re-running each imported file's
parser in the original import order. `fin process` then redoes matching and categories.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fin.db import models as m
from fin.ingest import load_parsers
from fin.ingest.inbox import PARSERS, safe_error

DERIVED = [m.Allocation, m.Match, m.OrderCharge, m.OrderItem, m.Order, m.StatementLine,
           m.Transaction, m.Statement, m.Merchant, m.AIRun]


@dataclass
class RebuildResult:
    reparsed: int = 0
    failed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def rebuild(s: Session) -> RebuildResult:
    load_parsers()
    s.execute(delete(m.ReviewItem).where(m.ReviewItem.kind != "sync_gap"))
    for table in DERIVED:
        s.execute(delete(table))
    s.execute(delete(m.Rule).where(m.Rule.created_by == "seed"))
    s.commit()

    out = RebuildResult()
    for imp in s.scalars(select(m.Import).order_by(m.Import.id)).all():
        path = Path(imp.file_path)
        parser = PARSERS.get((imp.source, imp.file_kind or imp.kind))
        if not path.exists():
            out.missing.append(path.name)
            continue
        if parser is None:
            continue
        entry = {"file": path.name, "kind": imp.file_kind, "card_id": imp.card_id,
                 "period_start": imp.period_start, "period_end": imp.period_end, "note": ""}
        try:
            with s.begin_nested():
                outcome = parser(s, path, entry, imp)
        except Exception as e:  # keep going; report which files no longer parse
            imp.status, imp.error = "failed", safe_error(e)
            out.failed.append(f"{path.name}: {safe_error(e)}")
            s.commit()
            continue
        imp.row_count = outcome.row_count
        imp.status = "partial" if outcome.problems else "ok"
        imp.error = "; ".join(outcome.problems)[:2000] or None
        for problem in outcome.problems:
            s.add(m.ReviewItem(kind="parse_failure", ref_table="imports", ref_id=str(imp.id),
                               details_json=json.dumps({"file": path.name, "problem": problem})))
        s.commit()
        out.reparsed += 1
    return out
