import json
from datetime import date

import pytest
from sqlalchemy import func, select

from fin.config import CONFIG_DIR, DataPaths, Settings, load_cards, load_taxonomy
from fin.db import models as m
from fin.db import session_factory, upgrade
from fin.db.seed import sync_reference
from fin.ingest import inbox
from fin.ingest.manifest import validate_run
from fin.sync import params

CARDS = load_cards(CONFIG_DIR / "cards.example.json")
TODAY = date(2026, 10, 6)


@pytest.fixture
def Session(tmp_path):
    db = tmp_path / "t.sqlite"
    upgrade(db)
    S = session_factory(db)
    with S() as s:
        sync_reference(s, CARDS, load_taxonomy())
    return S


def test_months_before():
    assert params.months_before(date(2026, 10, 6), 3) == date(2026, 7, 6)
    assert params.months_before(date(2026, 5, 31), 3) == date(2026, 2, 28)
    assert params.months_before(date(2026, 1, 15), 2) == date(2025, 11, 15)


def test_first_run_params(Session):
    with Session() as s:
        p, since, until = params.run_params(s, CARDS, Settings(), "chase", TODAY)
    assert {c["card_id"] for c in p["cards"]} == {
        "chase_sapphire_reserve", "chase_freedom_unlimited", "chase_prime_visa"}
    c = p["cards"][0]
    assert c["first_run"] and c["transactions_from"] == "2026-07-06"
    assert "last 3 closed statements" in c["statements_wanted"]
    assert (since, until) == ("2026-07-06", "2026-10-06")
    assert params.allowed_card_ids(CARDS, "chase") == {c["card_id"] for c in p["cards"]}


def _import_row(s, card="chase_prime_visa"):
    imp = m.Import(source="chase", kind="transactions", card_id=card, file_path="x",
                   sha256="a" * 64, status="ok")
    s.add(imp)
    s.flush()
    return imp


def test_weekly_params_use_overlap(Session):
    with Session() as s:
        imp = _import_row(s)
        s.add(m.Transaction(card_id="chase_prime_visa", post_date="2026-09-30", amount_cents=100,
                            description_raw="X", description_clean="X", type="purchase",
                            dedupe_key="k1", import_id=imp.id))
        s.add(m.Statement(card_id="chase_prime_visa", period_start="2026-08-15",
                          period_end="2026-09-14", previous_balance=0, new_balance=0,
                          pdf_path="p", parse_method="regex"))
        s.commit()
        p, since, _ = params.run_params(s, CARDS, Settings(), "chase", TODAY)
    prime = next(c for c in p["cards"] if c["card_id"] == "chase_prime_visa")
    assert prime["transactions_from"] == "2026-09-16" and not prime["first_run"]
    assert prime["statements_already_imported"] == ["2026-09-14"]
    assert "on or after 2026-09-16" in prime["statements_wanted"]
    assert since == "2026-07-06"  # other cards are still on their first run


def test_amazon_params(Session):
    with Session() as s:
        s.add(m.Order(merchant="amazon", channel="online", external_id="111-1",
                      order_date="2026-09-20", total=100, raw_path="r"))
        s.commit()
        p, since, _ = params.run_params(s, CARDS, Settings(), "amazon", TODAY)
    assert p["orders_from"] == "2026-09-06" and p["orders_already_imported"] == ["111-1"]
    assert params.allowed_card_ids(CARDS, "amazon") == set()


def test_since_override(Session):
    with Session() as s:
        p, since, _ = params.run_params(s, CARDS, Settings(), "chase", TODAY, date(2026, 9, 1))
    assert since == "2026-09-01" and not p["cards"][0]["first_run"]


# ------------------------------------------------------------------ import


def make_run(tmp_path, files):
    paths = DataPaths(tmp_path / "data")
    run = paths.run_dir("chase", "2026-10-06")
    run.mkdir(parents=True)
    entries = []
    for name, content in files.items():
        (run / name).write_bytes(content)
        entries.append({"file": name, "kind": "transactions", "card_id": "chase_prime_visa",
                        "period_start": "2026-07-06", "period_end": "2026-10-06", "note": ""})
    (run / "manifest.json").write_text(json.dumps(
        {"site": "chase", "run_date": "2026-10-06", "files": entries}))
    (run / "run_report.json").write_text(json.dumps(
        {"site": "chase", "run_date": "2026-10-06", "collected": [], "notes": [],
         "gaps": [{"card_id": "chase_prime_visa", "kind": "statement", "description": "x"}]}))
    (run / "guard.log").write_text('{"decision": "allow"}\n')
    (run / "session-1").mkdir()
    (run / "session-1" / "session.md").write_text("steps")
    (run / "page-1.yml").write_text("snapshot")
    return paths, run


def fake_parser(s, path, entry, imp):
    s.add(m.ReviewItem(kind="uncategorized", details_json=json.dumps({"f": path.name})))
    return inbox.ParseOutcome(row_count=7, problems=["date range differs"] if "b" in path.name
                              else [])


def test_import_run_and_reimport(Session, tmp_path):
    paths, run = make_run(tmp_path, {"a.csv": b"1", "b.csv": b"2"})
    check = validate_run(run, "chase", {"chase_prime_visa"})
    assert check.ok, check.errors
    parsers = {("chase", "transactions"): fake_parser}
    with Session() as s:
        res = inbox.import_run(s, paths, run, check, parsers=parsers)
        assert res.count("imported") == 2
        assert s.scalar(select(func.count(m.Import.id))) == 2
        b = s.scalar(select(m.Import).where(m.Import.file_path.like("%b.csv")))
        assert b.status == "partial" and b.row_count == 7
        assert s.scalar(select(func.count()).where(m.ReviewItem.kind == "parse_failure")) == 1
    raw = paths.raw / "chase" / "2026-10-06"
    assert (raw / "a.csv").exists() and (raw / "manifest.json").exists()
    assert not (run / "a.csv").exists()
    logs = paths.raw / "sync-logs" / "chase" / "2026-10-06"
    assert (logs / "guard.log").exists() and (logs / "session-1" / "session.md").exists()
    assert (logs / "page-1.yml").exists()

    # Same folder again (files already moved): no new rows.
    (run / "manifest.json").write_text((raw / "manifest.json").read_text())
    (run / "run_report.json").write_text((raw / "run_report.json").read_text())
    assert not validate_run(run, "chase", {"chase_prime_visa"}).ok
    check = validate_run(run, "chase", {"chase_prime_visa"}, imported_dir=raw)
    with Session() as s:
        res = inbox.import_run(s, paths, run, check, parsers=parsers)
        assert res.count("already_imported") == 2
        assert s.scalar(select(func.count(m.Import.id))) == 2

    # Same content under a new name: duplicate by sha256.
    (run / "a-copy.csv").write_bytes(b"1")
    man = json.loads((raw / "manifest.json").read_text())
    (run / "run_report.json").write_text((raw / "run_report.json").read_text())
    man["files"] = [{**man["files"][0], "file": "a-copy.csv"}]
    (run / "manifest.json").write_text(json.dumps(man))
    check = validate_run(run, "chase", {"chase_prime_visa"})
    with Session() as s:
        res = inbox.import_run(s, paths, run, check, parsers=parsers)
        assert res.count("duplicate") == 1
        assert s.scalar(select(func.count(m.Import.id))) == 2


def test_no_parser_leaves_files(Session, tmp_path):
    paths, run = make_run(tmp_path, {"a.csv": b"1"})
    check = validate_run(run, "chase", {"chase_prime_visa"})
    with Session() as s:
        res = inbox.import_run(s, paths, run, check, parsers={})
        assert res.count("no_parser") == 1
        assert s.scalar(select(func.count(m.Import.id))) == 0
    assert (run / "a.csv").exists() and (run / "manifest.json").exists()
    assert not (run / "guard.log").exists()  # logs still filed


def test_parser_crash_keeps_file(Session, tmp_path):
    paths, run = make_run(tmp_path, {"a.csv": b"1", "c.csv": b"3"})
    check = validate_run(run, "chase", {"chase_prime_visa"})

    def parser(s, path, entry, imp):
        if path.name == "c.csv":
            raise ValueError("unexpected header")
        return inbox.ParseOutcome(row_count=1)

    with Session() as s:
        res = inbox.import_run(s, paths, run, check, parsers={("chase", "transactions"): parser})
        assert {r.file: r.status for r in res.results} == {"a.csv": "imported", "c.csv": "failed"}
        assert s.scalar(select(func.count(m.Import.id))) == 1
    assert (run / "c.csv").exists() and (run / "manifest.json").exists()


def test_refuses_invalid_manifest(Session, tmp_path):
    paths, run = make_run(tmp_path, {"a.csv": b"1"})
    check = validate_run(run, "chase", set())  # card not allowed
    with Session() as s, pytest.raises(ValueError):
        inbox.import_run(s, paths, run, check)


def test_record_gaps(Session):
    with Session() as s:
        n = inbox.record_gaps(s, {"gaps": [{"card_id": None, "kind": "statement",
                                            "description": "x"}]}, 1)
        assert n == 1
        assert s.scalar(select(m.ReviewItem.kind)) == "sync_gap"


def test_amazon_params_reach_back_for_unmatched_charges(Session):
    with Session() as s:
        imp = _import_row(s)
        merchant = m.Merchant(name="Amazon", merchant_group="amazon")
        s.add(merchant)
        s.flush()
        s.add(m.Transaction(card_id="chase_prime_visa", post_date="2026-06-26",
                            txn_date="2026-06-26", amount_cents=7419, description_raw="AMAZON",
                            description_clean="AMAZON", type="purchase", dedupe_key="u1",
                            import_id=imp.id, merchant_id=merchant.id))
        s.commit()
        p, since, _ = params.run_params(s, CARDS, Settings(), "amazon", TODAY)
    assert since == "2026-06-12" == p["orders_from"]


def test_same_day_rerun_keeps_both_manifests(Session, tmp_path):
    parsers = {("chase", "transactions"): fake_parser}
    paths, run = make_run(tmp_path, {"a.csv": b"1"})
    with Session() as s:
        inbox.import_run(s, paths, run, validate_run(run, "chase", {"chase_prime_visa"}),
                         parsers=parsers)
    (run / "a2.csv").write_bytes(b"2")
    man = {"site": "chase", "run_date": "2026-10-06", "files": [
        {"file": "a2.csv", "kind": "transactions", "card_id": "chase_prime_visa",
         "period_start": None, "period_end": None, "note": ""}]}
    (run / "manifest.json").write_text(json.dumps(man))
    (run / "run_report.json").write_text(json.dumps(
        {"site": "chase", "run_date": "2026-10-06", "collected": [], "gaps": [], "notes": []}))
    with Session() as s:
        inbox.import_run(s, paths, run, validate_run(run, "chase", {"chase_prime_visa"}),
                         parsers=parsers)
    raw = paths.raw / "chase" / "2026-10-06"
    assert len(list(raw.glob("manifest*.json"))) == 2
    assert not (run / "manifest.json").exists()


def test_costco_params_list_receipts_by_date_and_total(Session):
    with Session() as s:
        s.add(m.Order(merchant="costco", channel="warehouse", external_id="r1",
                      order_date="2026-09-20", total=12345, raw_path="r"))
        s.add(m.Order(merchant="costco", channel="online", external_id="ONL-9",
                      order_date="2026-09-21", total=500, raw_path="r"))
        s.commit()
        p, _, _ = params.run_params(s, CARDS, Settings(), "costco", TODAY)
    assert p["orders_already_imported"] == ["2026-09-20_123.45", "ONL-9"]


def test_costco_params_reach_back_for_unmatched_warehouse_charges(Session):
    with Session() as s:
        imp = _import_row(s)
        merchant = m.Merchant(name="Costco", merchant_group="costco_warehouse")
        s.add(merchant)
        s.flush()
        s.add(m.Transaction(card_id="citi_costco", post_date="2026-07-02", txn_date="2026-07-02",
                            amount_cents=13536, description_raw="COSTCO WHSE",
                            description_clean="COSTCO WHSE", type="purchase", dedupe_key="c1",
                            import_id=imp.id, merchant_id=merchant.id))
        s.commit()
        p, since, _ = params.run_params(s, CARDS, Settings(), "costco", TODAY)
    assert since == "2026-06-18"


def test_transcript_screenshot_moves_with_it(Session, tmp_path):
    paths = DataPaths(tmp_path / "data")
    run = paths.run_dir("costco", "2026-10-06")
    run.mkdir(parents=True)
    (run / "costco_2026-09-28_1.json").write_text("{}")
    (run / "costco_2026-09-28_1.png").write_bytes(b"png")
    (run / "manifest.json").write_text(json.dumps({"site": "costco", "run_date": "2026-10-06",
        "files": [{"file": "costco_2026-09-28_1.json", "kind": "receipt_transcript",
                   "card_id": None, "period_start": None, "period_end": None, "note": ""}]}))
    (run / "run_report.json").write_text(json.dumps({"site": "costco", "run_date": "2026-10-06",
        "collected": [], "gaps": [], "notes": []}))
    check = validate_run(run, "costco", set())
    assert check.ok and check.unlisted == []
    with Session() as s:
        inbox.import_run(s, paths, run, check, parsers={
            ("costco", "receipt_transcript"): lambda *a: inbox.ParseOutcome(row_count=1)})
    raw = paths.raw / "costco" / "2026-10-06"
    assert (raw / "costco_2026-09-28_1.png").exists()
    assert not (run / "costco_2026-09-28_1.png").exists()
