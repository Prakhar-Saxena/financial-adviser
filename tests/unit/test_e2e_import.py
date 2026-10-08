"""End to end: fixture run folders -> fin import --inbox (twice) -> DB assertions (SPEC §18)."""

import json
import shutil

from sqlalchemy import func, select
from typer.testing import CliRunner

from fin.cli import app
from fin.db import models as m
from fin.db import session_factory
from tests.conftest import FIXTURES

RUN_DATE = "2026-10-06"


def entry(file, kind, card, ps=None, pe=None, note=""):
    return {"file": file, "kind": kind, "card_id": card, "period_start": ps, "period_end": pe,
            "note": note}


def make_run(data_dir, site, files, entries):
    run = data_dir / "inbox" / site / RUN_DATE
    run.mkdir(parents=True)
    for src in files:
        shutil.copy(src, run / src.name)
    (run / "manifest.json").write_text(json.dumps(
        {"site": site, "run_date": RUN_DATE, "files": entries}))
    (run / "run_report.json").write_text(json.dumps(
        {"site": site, "run_date": RUN_DATE, "collected": [], "gaps": [], "notes": []}))
    return run


def counts(db):
    with session_factory(db)() as s:
        return {t.__tablename__: s.scalar(select(func.count()).select_from(t))
                for t in (m.Import, m.Transaction, m.Statement, m.StatementLine, m.Order,
                          m.OrderItem, m.ReviewItem)}


def test_import_fixtures_twice(data_dir, monkeypatch, tmp_path):
    monkeypatch.setattr("fin.cli.REPO_ROOT", tmp_path / "no-repo")
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    csv = FIXTURES / "chase/activity_prime_visa.csv"
    stmt = FIXTURES / "chase/statement_prime_visa_2026-09-21.txt"
    make_run(data_dir, "chase", [csv, stmt], [
        entry(csv.name, "transactions", "chase_prime_visa", "2026-07-06", RUN_DATE),
        entry(stmt.name, "statement", "chase_prime_visa", None, "2026-09-21"),
    ])
    invoices = sorted((FIXTURES / "amazon").glob("invoice_*.txt"))
    make_run(data_dir, "amazon", invoices,
             [entry(f.name, "order_invoice", None, note="") for f in invoices])

    r = runner.invoke(app, ["import", "--inbox"])
    assert r.exit_code == 0, r.output
    assert "chase/2026-10-06: 2 imported" in r.output
    assert "amazon/2026-10-06: 2 imported" in r.output
    db = data_dir / "db" / "finance.sqlite"
    first = counts(db)
    assert first["transactions"] == 3 and first["statements"] == 1
    assert first["statement_lines"] == 12 and first["orders"] == 2 and first["order_items"] == 4
    # The CSV starts weeks after the requested range: flagged, document over manifest.
    assert "download's date range may not have applied" in r.output

    with session_factory(db)() as s:
        st = s.scalar(select(m.Statement))
        assert st.math_ok and st.purchases == 126184 and st.due_date == "2026-10-18"
        t = s.scalar(select(m.Transaction).where(m.Transaction.amount_cents == 5349))
        assert (t.type, t.person_id, t.person_source) == ("purchase", "self", "default_self")
        o = s.scalar(select(m.Order).where(m.Order.total == 5349))
        assert o.payment_method == "Prime Visa"

    # Second time: everything moved to raw/, so the inbox is empty.
    r = runner.invoke(app, ["import", "--inbox"])
    assert r.exit_code == 0 and "Inbox is empty" in r.output, r.output
    assert counts(db) == first

    # The same run folder put back into the inbox: every file is a duplicate.
    for site in ("chase", "amazon"):
        shutil.copytree(data_dir / "raw" / site / RUN_DATE, data_dir / "inbox" / site / RUN_DATE,
                        dirs_exist_ok=True)
    r = runner.invoke(app, ["import", "--inbox"])
    assert "chase/2026-10-06: 2 duplicate" in r.output, r.output
    assert "amazon/2026-10-06: 2 duplicate" in r.output
    assert counts(db) == first

    # A copy of the same file dropped by hand: duplicate by content.
    r = runner.invoke(app, ["import", str(csv), "--card", "chase_prime_visa"])
    assert "duplicate" in r.output and counts(db) == first


def test_manual_statement_import_detects_card(data_dir, monkeypatch, tmp_path):
    monkeypatch.setattr("fin.cli.REPO_ROOT", tmp_path / "no-repo")
    runner = CliRunner()
    runner.invoke(app, ["init"])
    stmt = FIXTURES / "chase/statement_prime_visa_2026-09-21.txt"
    r = runner.invoke(app, ["import", str(stmt)])
    assert r.exit_code == 0 and "imported" in r.output, r.output
    with session_factory(data_dir / "db" / "finance.sqlite")() as s:
        assert s.scalar(select(m.Statement.card_id)) == "chase_prime_visa"
    r = runner.invoke(app, ["import", str(FIXTURES / "chase/activity_prime_visa.csv")])
    assert r.exit_code == 2 and "--card" in r.output
