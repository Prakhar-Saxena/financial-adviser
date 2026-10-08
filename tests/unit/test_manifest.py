import json

import pytest

from fin.ingest.manifest import validate_run

CARDS = {"chase_prime_visa", "chase_sapphire_reserve"}


def entry(file, kind="statement", card="chase_prime_visa", ps="2026-08-15", pe="2026-09-14"):
    return {"file": file, "kind": kind, "card_id": card, "period_start": ps, "period_end": pe,
            "note": ""}


def report(**kw):
    base = {"site": "chase", "run_date": "2026-10-06",
            "collected": [{"card_id": "chase_prime_visa", "kind": "statement", "count": 1}],
            "gaps": [], "notes": []}
    return {**base, **kw}


@pytest.fixture
def run(tmp_path):
    d = tmp_path / "2026-10-06"
    d.mkdir()
    (d / "stmt.pdf").write_bytes(b"%PDF-1.4")
    return d


def write(run, files, rep=None, site="chase"):
    (run / "manifest.json").write_text(
        json.dumps({"site": site, "run_date": "2026-10-06", "files": files}))
    (run / "run_report.json").write_text(json.dumps(rep or report(site=site)))


def test_valid(run):
    write(run, [entry("stmt.pdf")])
    (run / "guard.log").write_text("")
    (run / "session-2026.yml").write_text("")  # --save-session output is expected
    c = validate_run(run, "chase", CARDS, "2026-10-06")
    assert c.ok, c.errors
    assert c.unlisted == []


def test_unknown_card_rejected(run):
    write(run, [entry("stmt.pdf", card="amex_gold")])
    c = validate_run(run, "chase", CARDS)
    assert not c.ok and "unknown card_id 'amex_gold'" in c.errors[0]


def test_missing_file_and_unlisted_file(run):
    (run / "extra.csv").write_text("a,b")
    write(run, [entry("nope.pdf")])
    c = validate_run(run, "chase", CARDS)
    assert any("file not found" in e for e in c.errors)
    assert set(c.unlisted) == {"stmt.pdf", "extra.csv"}


@pytest.mark.parametrize("bad", [
    {"file": "../escape.pdf"}, {"file": "sub/dir.pdf"}, {"kind": "money"},
    {"period_start": "09/01/2026"}, {"card_id": "Chase Card"}, {"file": "manifest.json"},
])
def test_schema_rejects(run, bad):
    write(run, [{**entry("stmt.pdf"), **bad}])
    assert not validate_run(run, "chase", CARDS).ok


def test_extra_field_rejected(run):
    write(run, [{**entry("stmt.pdf"), "amount": 1}])
    assert not validate_run(run, "chase", CARDS).ok


def test_site_mismatch_and_period_order(run):
    write(run, [entry("stmt.pdf", ps="2026-09-30", pe="2026-09-01")], site="amex")
    c = validate_run(run, "chase", CARDS)
    assert any("expected 'chase'" in e for e in c.errors)
    assert any("period_start after" in e for e in c.errors)


def test_selftest_kind_only_for_selftest(run):
    write(run, [entry("stmt.pdf", kind="selftest", card=None, ps=None, pe=None)])
    assert not validate_run(run, "chase", CARDS).ok
    rep = report(site="selftest", collected=[{"card_id": None, "kind": "selftest", "count": 1}])
    write(run, [entry("stmt.pdf", kind="selftest", card=None, ps=None, pe=None)], rep, "selftest")
    assert validate_run(run, "selftest", set()).ok


def test_bad_json_and_missing(run):
    (run / "manifest.json").write_text("{not json")
    c = validate_run(run, "chase", CARDS)
    assert any("not valid JSON" in e for e in c.errors)
    assert any("run_report.json is missing" in e for e in c.errors)


def test_report_unknown_card(run):
    rep = report(gaps=[{"card_id": "citi_costco", "kind": "statement", "description": "x"}])
    write(run, [entry("stmt.pdf")], rep)
    assert not validate_run(run, "chase", CARDS).ok
