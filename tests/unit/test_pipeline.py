"""fin process on the real (redacted) fixtures, with a fake classifier."""

import json
import shutil
from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from fin.categorize.allocate import largest_remainder, split_by_category
from fin.cli import app
from fin.config import data_paths, load_cards, load_settings, load_taxonomy
from fin.db import models as m
from fin.db import session_factory
from fin.pipeline import process
from tests.conftest import FIXTURES

RUN_DATE = "2026-10-06"


@dataclass
class FakeResult:
    output: dict


class FakeAI:
    def __init__(self):
        self.calls = []

    def __call__(self, task, payload, run_dir):
        self.calls.append((task.name, len(payload["records"])))
        recs = payload["records"]
        if task.name == "item_categorize":
            return FakeResult({"results": [
                {"key": r["key"], "clean_title": r["title"][:20], "category_id":
                 "home.furniture_and_decor" if "Wall Art" in r["title"] else "personal_care",
                 "confidence": 0.9} for r in recs]})
        return FakeResult({"results": [
            {"key": r["key"], "merchant_name": r["description"].title(),
             "category_id": "shopping_other", "confidence": 0.5} for r in recs]})


def entry(file, kind, card, ps=None, pe=None):
    return {"file": file, "kind": kind, "card_id": card, "period_start": ps, "period_end": pe,
            "note": ""}


@pytest.fixture
def loaded(data_dir, monkeypatch, tmp_path):
    monkeypatch.setattr("fin.cli.REPO_ROOT", tmp_path / "no-repo")
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    runs = {
        "chase": [(FIXTURES / "chase/activity_prime_visa.csv", "transactions", "chase_prime_visa"),
                  (FIXTURES / "chase/statement_prime_visa_2026-09-21.txt", "statement",
                   "chase_prime_visa")],
        "amazon": [(f, "payment_transactions" if f.name.startswith("payments") else
                    "order_invoice", None) for f in sorted((FIXTURES / "amazon").glob("*"))],
    }
    for site, files in runs.items():
        run = data_dir / "inbox" / site / RUN_DATE
        run.mkdir(parents=True)
        for src, _, _ in files:
            shutil.copy(src, run / src.name)
        (run / "manifest.json").write_text(json.dumps({"site": site, "run_date": RUN_DATE,
            "files": [entry(f.name, k, c) for f, k, c in files]}))
        (run / "run_report.json").write_text(json.dumps({"site": site, "run_date": RUN_DATE,
            "collected": [], "gaps": [], "notes": []}))
    assert runner.invoke(app, ["import", "--inbox"]).exit_code == 0
    return data_dir


def run(loaded, ai=None, use_ai=True):
    settings = load_settings()
    paths = data_paths(settings)
    with session_factory(paths.db_file)() as s:
        return process(s, paths, settings, load_cards(), load_taxonomy(), use_ai=use_ai,
                       today=date(2026, 10, 6), runner=ai or FakeAI())


def snapshot(db):
    with session_factory(db)() as s:
        return {
            "txns": s.scalar(select(func.count(m.Transaction.id))),
            "matches": sorted(s.execute(select(m.Match.transaction_id, m.Match.order_id,
                                               m.Match.method)).all()),
            "allocs": sorted(s.execute(select(m.Allocation.transaction_id,
                                              m.Allocation.category_id,
                                              m.Allocation.amount_cents)).all()),
            "reviews": sorted(s.execute(select(m.ReviewItem.kind, m.ReviewItem.ref_id,
                                               m.ReviewItem.status)).all()),
        }


def test_process_fixtures(loaded):
    ai = FakeAI()
    r = run(loaded, ai)
    db = loaded / "db" / "finance.sqlite"
    assert (r.link.linked, r.link.created) == (0, 12)  # CSV rows all post after the close
    assert r.ledger == {"match": 1}
    # $53.49 matches its order exactly via Amazon's charge list; the other 12 Amazon charges
    # are confirmed by the list but their invoices aren't in the fixtures.
    assert r.match.by_method == {"exact": 1, "charge_only": 12} and r.match.unmatched == 0
    assert r.unexpected == 1  # Microsoft on the Prime Visa
    assert ("item_categorize", 4) in ai.calls

    with session_factory(db)() as s:
        t = s.scalar(select(m.Transaction).where(m.Transaction.amount_cents == 5349))
        allocs = s.scalars(select(m.Allocation).where(m.Allocation.transaction_id == t.id)).all()
        assert sum(a.amount_cents for a in allocs) == 5349
        assert {a.category_id for a in allocs} == {"home.furniture_and_decor", "personal_care"}
        assert t.category_source == "split"
        ms = s.scalar(select(m.Transaction).where(m.Transaction.description_clean == "MICROSOFT"))
        assert ms.category_id == "digital.software_and_apps" and ms.source == "statement"
        pay = s.scalar(select(m.Transaction).where(m.Transaction.type == "payment"))
        assert pay.category_id is None  # excluded from spending
        refs = s.scalars(select(m.Transaction.order_ref).where(
            m.Transaction.order_ref.is_not(None))).all()
        assert len(refs) == 13  # 10 from statement lines, 3 from Amazon's charge list
        linked = s.scalar(select(func.count(m.OrderCharge.id)).where(
            m.OrderCharge.order_id.is_not(None)))
        assert linked == 2  # the two invoices in the fixtures
    assert r.reviews_open["unmatched_charge"] == 12

    first = snapshot(db)
    again = run(loaded, FakeAI())
    assert snapshot(db) == first  # idempotent
    assert again.ai_items.cached == 4 and again.ai_items.calls == 0  # never classify twice


def test_csv_row_replaces_statement_copy(loaded):
    """A later CSV row for a line already filled from the statement takes its place."""
    run(loaded)
    db = loaded / "db" / "finance.sqlite"
    csv = ("Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
           "09/01/2026,09/02/2026,MICROSOFT*MICROSOFT 36,Shopping,Sale,-140.39,\n")
    p = loaded / "late.csv"
    p.write_text(csv)
    r = CliRunner().invoke(app, ["import", str(p), "--card", "chase_prime_visa"])
    assert "imported" in r.output, r.output
    res = run(loaded)
    assert res.link.replaced == 1 and res.ledger == {"match": 1}
    with session_factory(db)() as s:
        rows = s.scalars(select(m.Transaction).where(
            m.Transaction.description_clean == "MICROSOFT")).all()
        assert [(t.source, t.post_date) for t in rows] == [("csv", "2026-09-02")]


def test_extra_csv_row_breaks_ledger(loaded):
    run(loaded)
    csv = ("Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
           "09/10/2026,09/11/2026,SOMETHING NOT ON THE STATEMENT,Shopping,Sale,-9.99,\n")
    p = loaded / "extra.csv"
    p.write_text(csv)
    CliRunner().invoke(app, ["import", str(p), "--card", "chase_prime_visa"])
    res = run(loaded)
    assert res.ledger == {"mismatch": 1}
    assert res.reviews_open.get("statement_mismatch") == 1


def test_no_ai_keeps_going(loaded):
    r = run(loaded, use_ai=False)
    assert r.ai_items.calls == 0 and r.match.by_method["exact"] == 1


def test_usage_limit_pauses(loaded):
    from fin.ai.claude_cli import UsageLimitError

    def limited(task, payload, run_dir):
        raise UsageLimitError(UsageLimitError.MESSAGE)

    r = run(loaded, limited)
    assert r.ai_paused and r.ai_items.calls == 0


def test_largest_remainder():
    assert largest_remainder(100, [1, 1, 1]) == [34, 33, 33]
    assert sum(largest_remainder(5349, [1449, 2898, 699])) == 5349
    assert sum(largest_remainder(-1001, [3, 3, 3])) == -1001
    assert largest_remainder(7, [0, 0]) == [7, 0]
    assert split_by_category(10, [("a", 1), ("a", 1), ("b", 2)]) == {"a": 5, "b": 5}


def test_weekly_runs_sites_in_order_then_process(loaded, monkeypatch):
    calls = []
    monkeypatch.setattr("fin.cli.sync_site", lambda site, **kw: calls.append(site) or 0)
    r = CliRunner().invoke(app, ["weekly", "--no-ai", "--no-open"])
    assert r.exit_code == 0, r.output
    assert calls == ["chase", "citi", "amex", "amazon", "costco"]
    assert list((loaded / "backups").glob("finance-*.sqlite"))
