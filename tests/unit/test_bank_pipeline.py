# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Checking rows through `fin import`, `fin process` and `fin rebuild`
(docs/SPEC-checking.md §16 "End to end"). Fictional config and files only."""

import json
import re
import shutil
from collections import Counter
from datetime import date

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from fin.api.spending import spending_lines
from fin.cli import app
from fin.config import CONFIG_DIR, data_paths, load_cards, load_settings, load_taxonomy
from fin.db import models as m
from fin.db import session_factory
from fin.pipeline import process
from fin.rebuild import rebuild
from tests.conftest import FIXTURES

TODAY = date(2026, 11, 2)
# A fictional Chase export with the payment the bank fixture's "CHASE CREDIT CRD - EPAY" row
# paid, plus one purchase. Chase exports payments as positive amounts.
CHASE_CSV = (
    "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
    "10/03/2026,10/03/2026,Payment Thank You-Mobile,,Payment,612.45,\n"
    "10/01/2026,10/02/2026,CORNER CAFE,Food & Drink,Sale,-18.40,\n"
)


BANK_FILES = [
    ("ExportedTransactions.csv", (FIXTURES / "bank/ExportedTransactions.csv").read_bytes(),
     "transactions", "checking", "2026-10-01", "2026-10-31"),
    ("statement_checking_2026-10-31.txt",
     (FIXTURES / "bank/statement_checking_2026-10-31.txt").read_bytes(), "statement",
     "checking", None, "2026-10-31"),
]


@pytest.fixture
def example_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, cfg, ignore=shutil.ignore_patterns("cards.json"))
    shutil.copy(cfg / "cards.example.json", cfg / "cards.json")
    monkeypatch.setattr("fin.config.CONFIG_DIR", cfg)
    monkeypatch.setattr("fin.cli.CONFIG_DIR", cfg)
    monkeypatch.setattr("fin.cli.REPO_ROOT", tmp_path / "no-repo")
    return cfg


def drop_run(data_dir, site, files, run_date="2026-11-02"):
    """files: (name, bytes, kind, card_id, period_start, period_end)."""
    run = data_dir / "inbox" / site / run_date
    run.mkdir(parents=True)
    for name, data, *_ in files:
        (run / name).write_bytes(data)
    (run / "manifest.json").write_text(json.dumps({"site": site, "run_date": run_date,
        "files": [{"file": n, "kind": k, "card_id": c, "period_start": ps, "period_end": pe,
                   "note": ""} for n, _, k, c, ps, pe in files]}))
    (run / "run_report.json").write_text(json.dumps({"site": site, "run_date": run_date,
                                                     "collected": [], "gaps": [], "notes": []}))


def import_inbox():
    r = CliRunner().invoke(app, ["import", "--inbox"])
    assert r.exit_code == 0, r.output
    return r.output


def process_all():
    settings = load_settings()
    paths = data_paths(settings)
    with session_factory(paths.db_file)() as s:
        return process(s, paths, settings, load_cards(), load_taxonomy(), use_ai=False,
                       today=TODAY)


def state(db):
    with session_factory(db)() as s:
        lines = spending_lines(s)
        card_lines = [ln for ln in lines if ln.card_id != "checking"]
        by_key: Counter = Counter()
        for ln in card_lines:  # cents by month, card and category
            by_key[(ln.month, ln.card_id, ln.category_id)] += ln.amount_cents
        return {
            "card_spending": sorted(by_key.items()),
            "card_total": sum(ln.amount_cents for ln in card_lines),
            "credits": s.scalar(select(func.coalesce(func.sum(m.Transaction.amount_cents), 0))
                                .where(m.Transaction.type.in_(("credit", "reward")))),
            "types": sorted(s.execute(select(m.Transaction.dedupe_key, m.Transaction.type,
                                             m.Transaction.flow_kind)).all()),
            "links": sorted(s.execute(select(m.TransferLink.method, m.TransferLink.days_apart,
                                             m.TransferLink.confidence)).all()),
            "rows": s.scalar(select(func.count(m.Transaction.id))),
        }


def test_checking_end_to_end(data_dir, example_config):
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    drop_run(data_dir, "chase", [("activity.csv", CHASE_CSV.encode(), "transactions",
                                  "chase_prime_visa", "2026-10-01", "2026-11-02")])
    import_inbox()
    process_all()
    db = data_paths(load_settings()).db_file
    before = state(db)
    assert before["card_total"] == 1840 and before["card_spending"]

    drop_run(data_dir, "bank", BANK_FILES)
    out = import_inbox()
    assert "no parser" not in out
    r = process_all()
    after = state(db)

    # Invariant 5: card spending and card credits don't move, to the cent.
    assert after["card_spending"] == before["card_spending"]
    assert after["card_total"] == before["card_total"]
    assert after["credits"] == before["credits"]
    with session_factory(db)() as s:
        paid = s.scalar(select(m.Transaction).where(
            m.Transaction.description_raw == "CHASE CREDIT CRD  - EPAY"))
        assert (paid.type, paid.flow_kind) == ("transfer", "card_payment")
        assert paid.id not in {ln.txn_id for ln in spending_lines(s)}
        link = s.scalar(select(m.TransferLink))
        assert link.bank_txn_id == paid.id and link.method == "named"
        st = s.scalar(select(m.BankStatement))
        assert st.math_ok and st.ledger_status == "match"
    assert r.transfers.linked["named"] == 1 and r.bank.balance_gaps == []
    assert r.bank.statements == {"match": 1}

    # Again: nothing moves. The same files again, in a later run folder: no new rows.
    process_all()
    assert state(db) == after
    drop_run(data_dir, "bank", BANK_FILES, run_date="2026-11-03")
    import_inbox()
    process_all()
    assert state(db) == after

    # My override survives a rebuild; the totals come back identical.
    with session_factory(db)() as s:
        venmo = s.scalar(select(m.Transaction).where(m.Transaction.flow_kind == "p2p"))
        s.add(m.UserOverride(target_type="transaction", stable_key=venmo.dedupe_key,
                             field="category_id", value="gifts_donations"))
        s.commit()
    process_all()
    kept = state(db)
    with session_factory(db)() as s:
        rebuild(s)
    process_all()
    assert state(db) == kept
    with session_factory(db)() as s:
        venmo = s.scalar(select(m.Transaction).where(m.Transaction.flow_kind == "p2p"))
        assert venmo.category_id == "gifts_donations" and venmo.category_source == "user"


def test_classifier_never_sees_people_transfers_or_deposits(data_dir, example_config):
    """Acceptance 7: what reaches the merchant classifier from checking rows."""
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    drop_run(data_dir, "bank", [
        ("ExportedTransactions.csv", (FIXTURES / "bank/ExportedTransactions.csv").read_bytes(),
         "transactions", "checking", "2026-10-01", "2026-10-31")])
    import_inbox()
    seen = []

    class Recorder:
        def __call__(self, task, payload, run_dir):
            from tests.unit.test_pipeline import FakeAI

            if task.name == "merchant_categorize":
                seen.extend(payload["records"])
            return FakeAI()(task, payload, run_dir)

    settings = load_settings()
    paths = data_paths(settings)
    with session_factory(paths.db_file)() as s:
        process(s, paths, settings, load_cards(), load_taxonomy(), use_ai=True, today=TODAY,
                runner=Recorder())
        held = {t.description_clean for t in s.scalars(select(m.Transaction).where(
            m.Transaction.card_id == "checking",
            (m.Transaction.flow_kind.is_not(None)) | (m.Transaction.type != "purchase")))}
    sent = {r["description"] for r in seen}
    assert sent and not sent & held  # only merchant-like purchases
    text = json.dumps(seen).upper()
    for word in ("ALEX", "EXAMPLE", "VENMO", "XFER", "PAYROLL", "CHECK"):
        assert word not in text, word
    assert not re.search(r"\d{6,}", text)
