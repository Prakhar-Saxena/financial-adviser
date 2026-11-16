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
    shutil.copytree(CONFIG_DIR, cfg, ignore=shutil.ignore_patterns("accounts.json"))
    shutil.copy(cfg / "accounts.example.json", cfg / "accounts.json")
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


# ------------------------------------------------------------------ PATCH and fin transfers


@pytest.fixture
def bank_loaded(data_dir, example_config):
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    drop_run(data_dir, "chase", [("activity.csv", CHASE_CSV.encode(), "transactions",
                                  "chase_prime_visa", "2026-10-01", "2026-11-02")])
    drop_run(data_dir, "bank", BANK_FILES, run_date="2026-11-03")
    import_inbox()
    process_all()
    return data_paths(load_settings()).db_file


def api(db):
    from fastapi.testclient import TestClient

    from fin.api.app import create_app

    return TestClient(create_app(db, web_dist=None), base_url="http://127.0.0.1")


def row_id(db, raw):
    with session_factory(db)() as s:
        return s.scalar(select(m.Transaction.id).where(m.Transaction.description_raw == raw))


def test_patch_says_what_a_checking_row_is(bank_loaded):
    db, c = bank_loaded, api(bank_loaded)
    paid = row_id(db, "CHASE CREDIT CRD  - EPAY")
    card_row = row_id(db, "CORNER CAFE")
    assert c.patch(f"/api/transactions/{card_row}", json={"type": "transfer"}).status_code == 422
    assert c.patch(f"/api/transactions/{paid}", json={"type": "payment"}).status_code == 422
    assert c.patch(f"/api/transactions/{paid}",
                   json={"type": "purchase", "flow_kind": "income"}).status_code == 422
    r = c.patch(f"/api/transactions/{paid}", json={"type": "purchase"})
    assert r.status_code == 200 and r.json()["counts_as_spending"]
    process_all()  # my choice holds: no link, still spending
    with session_factory(db)() as s:
        t = s.get(m.Transaction, paid)
        assert (t.type, t.flow_kind) == ("purchase", None)
        assert s.scalar(select(m.TransferLink).where(m.TransferLink.bank_txn_id == paid)) is None


def test_patch_apply_to_all_and_closing_the_review_item(bank_loaded):
    db, c = bank_loaded, api(bank_loaded)
    amex = row_id(db, "AMERICAN EXPRESS ACH PMT  - ACH PMT")
    with session_factory(db)() as s:
        assert s.scalar(select(m.ReviewItem).where(  # no Amex payment synced: unmatched
            m.ReviewItem.kind == "unmatched_transfer", m.ReviewItem.ref_id == str(amex),
            m.ReviewItem.status == "open"))
    r = c.patch(f"/api/transactions/{amex}", json={"type": "transfer",
                                                   "flow_kind": "own_transfer",
                                                   "apply_to_all": True})
    assert r.status_code == 200 and not r.json()["reviews"]
    with session_factory(db)() as s:
        rule = s.scalar(select(m.Rule).where(m.Rule.txn_type.is_not(None)))
        assert (rule.txn_type, rule.flow_kind, rule.category_id) == \
            ("transfer", "own_transfer", None)
        s.add(m.Transaction(card_id="checking", person_id="self", post_date="2026-10-20",
                            amount_cents=500, description_raw="x",
                            description_clean=s.get(m.Transaction, amex).description_clean,
                            type="purchase", dedupe_key="another", import_id=1))
        s.commit()
    process_all()
    with session_factory(db)() as s:
        other = s.scalar(select(m.Transaction).where(m.Transaction.dedupe_key == "another"))
        assert (other.type, other.flow_kind) == ("transfer", "own_transfer")
    assert c.get("/api/rules").status_code == 200


def test_fin_transfers_lists_and_checks(bank_loaded):
    r = CliRunner().invoke(app, ["transfers", "--month", "2026-10"])
    assert r.exit_code == 0, r.output
    assert "Chase Prime Visa" in r.output or "Prime" in r.output
    assert "named" in r.output and "Card payments: 1 of 1 matched" in r.output
    assert "EPAY" not in r.output and "VENMO" not in r.output  # no descriptions
    ok = CliRunner().invoke(app, ["transfers", "--check"])
    assert ok.exit_code == 0, ok.output
    with session_factory(bank_loaded)() as s:  # break invariant 1 behind fin process's back
        t = s.get(m.Transaction, row_id(bank_loaded, "CHASE CREDIT CRD  - EPAY"))
        t.type = "purchase"
        s.commit()
    bad = CliRunner().invoke(app, ["transfers", "--check"])
    assert bad.exit_code == 1 and "FAIL 1:" in bad.output


def test_money_in_and_out_reproduces_the_statement(bank_loaded):
    """Phase B3: money in (income only) plus what came in otherwise is the statement's deposits;
    money out is its withdrawals."""
    c = api(bank_loaded)
    flow = c.get("/api/summary", params={"month": "2026-10"}).json()["cash_flow"]
    with session_factory(bank_loaded)() as s:
        st = s.scalar(select(m.BankStatement))
    assert flow["money_in_cents"] + flow["not_money_in_cents"] == st.deposits
    assert flow["money_out_cents"] == st.withdrawals
    assert {r["key"] for r in flow["money_in"]} <= {
        "income", "other_in", "interest", "reward", "tax_refund", "p2p"}
    assert flow["money_in"] and flow["not_money_in"]
    assert all(r["key"] in ("own_transfer", "card_payment", "investment", "refund")
               for r in flow["not_money_in"])
    out = {r["key"]: r for r in flow["money_out"]}
    assert out["card_payment"]["spending"] is False and out["p2p"]["spending"] is True
    # Each line opens its rows: the same count and cents, by posted date and direction.
    for side, direction in (("money_in", "in"), ("not_money_in", "in"), ("money_out", "out")):
        for line in flow[side]:
            rows = c.get("/api/transactions", params={
                "month": "2026-10", "card": "checking", "flow": line["key"],
                "direction": direction, "posted": "true"}).json()
            assert (len(rows), sum(abs(r["amount_cents"]) for r in rows)) == \
                (line["count"], line["cents"]), line["key"]
    every_in = ",".join(r["key"] for r in flow["money_in"])
    assert sum(-r["amount_cents"] for r in c.get("/api/transactions", params={
        "month": "2026-10", "card": "checking", "flow": every_in, "direction": "in",
        "posted": "true"}).json()) == flow["money_in_cents"]
    assert flow["card_payments"]["linked"] == 1
    assert c.get("/api/summary", params={"month": "2026-09"}).json()["cash_flow"][
        "money_in"] == []


def test_a_card_payment_shows_both_sides(bank_loaded):
    db, c = bank_loaded, api(bank_loaded)
    paid = row_id(db, "CHASE CREDIT CRD  - EPAY")
    with session_factory(db)() as s:
        card_row = s.scalar(select(m.Transaction.id).where(
            m.Transaction.card_id == "chase_prime_visa", m.Transaction.type == "payment"))
    there = c.get(f"/api/transactions/{paid}").json()["transfer"]
    assert [(x["txn_id"], x["card_id"], x["method"]) for x in there] == [
        (card_row, "chase_prime_visa", "named")]
    back = c.get(f"/api/transactions/{card_row}").json()["transfer"]
    assert [x["txn_id"] for x in back] == [paid] and back[0]["name"]
    one = c.get("/api/transactions", params={"id": card_row, "month": "2026-01"}).json()
    assert [t["id"] for t in one] == [card_row]  # the link opens it whatever the month
    assert c.get(f"/api/transactions/{row_id(db, 'CORNER CAFE')}").json()["transfer"] == []


def test_bank_reconciliation(bank_loaded):
    r = api(bank_loaded).get("/api/reconciliation/bank").json()
    assert r["accounts"] == ["checking"]
    [st] = r["statements"]
    assert st["math_ok"] and st["ledger_status"] == "match"
    assert st["opening_cents"] + st["deposits_cents"] - st["withdrawals_cents"] \
        == st["closing_cents"]
    assert r["balance"]["days"] > 0 and r["balance"]["gaps"] == []
    assert {"month": "2026-10", "card_payments": 1, "matched": 1, "checking_payments": 3,
            "checking_linked": 1} in r["card_payments"]


def test_a_dashboard_correction_survives_rebuild(bank_loaded):
    """Phase B3 acceptance: "This is spending" on a card payment, then fin rebuild."""
    db, c = bank_loaded, api(bank_loaded)
    paid = row_id(db, "CHASE CREDIT CRD  - EPAY")
    key = c.get(f"/api/transactions/{paid}").json()
    assert not key["counts_as_spending"]
    assert c.patch(f"/api/transactions/{paid}", json={"type": "purchase"}).status_code == 200
    with session_factory(db)() as s:
        dedupe = s.get(m.Transaction, paid).dedupe_key
        rebuild(s)
    process_all()
    with session_factory(db)() as s:
        t = s.scalar(select(m.Transaction).where(m.Transaction.dedupe_key == dedupe))
        assert (t.type, t.flow_kind) == ("purchase", None)
        assert t.id in {ln.txn_id for ln in spending_lines(s)}
        assert s.scalar(select(m.TransferLink).where(m.TransferLink.bank_txn_id == t.id)) is None
