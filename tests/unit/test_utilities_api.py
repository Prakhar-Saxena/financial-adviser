# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Utilities API (docs/SPEC-utilities.md §9, §14 "API"). Fictional config and fixtures only."""

import socket
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from fin.api.app import create_app
from fin.api.routes.utilities import as_of
from fin.cli import app
from fin.config import data_paths, load_settings
from fin.db import models as m
from fin.db import session_factory
from fin.rebuild import rebuild
from fin.utilities.series import utilities
from tests.unit.test_bank_pipeline import drop_run, example_config, import_inbox  # noqa: F401
from tests.unit.test_utilities_e2e import BANK, CARD, INSIGHTS, run

UNTIL = "2026-10"


@pytest.fixture
def tab(data_dir, example_config):  # noqa: F811
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    drop_run(data_dir, "bank", BANK)
    drop_run(data_dir, "chase", CARD)
    import_inbox()
    db = data_paths(load_settings()).db_file
    with session_factory(db)() as s:
        trash = s.scalar(select(m.Transaction).where(
            m.Transaction.description_raw.like("TOWN TRASH%")))
        s.add(m.UserOverride(target_type="transaction", stable_key=trash.dedupe_key,
                             field="category_id", value="bills.utilities"))
        s.commit()
        trash_id = trash.id
    run()
    client = TestClient(create_app(db, web_dist=None), base_url="http://127.0.0.1")
    return client, db, trash_id


def test_gets_never_call_claude_or_the_network(tab, fake_claude, monkeypatch):
    client, _, _ = tab

    def refuse(*_a, **_k):
        raise AssertionError("a GET opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for path in ("/api/utilities/summary", f"/api/utilities/summary?until={UNTIL}&months=12",
                 "/api/utilities/accounts/electric", "/api/utilities/accounts/water",
                 "/api/utilities/unassigned", "/api/meta"):
        assert client.get(path).status_code == 200, path
    assert not fake_claude.exists() or fake_claude.read_text() == ""
    assert client.get("/api/utilities/accounts/cable").status_code == 404
    assert client.get("/api/utilities/summary?until=2026-13").status_code == 422


def test_writes_need_json(tab):
    client, _, trash_id = tab
    body = f'{{"transaction_id": {trash_id}, "account_id": "none"}}'
    assert client.post("/api/utilities/assign", content=body).status_code == 415
    assert client.patch(f"/api/utilities/payments/{trash_id}",
                        content='{"covers_months": 2}').status_code == 415
    bad = client.post("/api/utilities/assign", json={"transaction_id": trash_id,
                                                      "account_id": "cable"})
    assert bad.status_code == 422


def test_the_tab_matches_the_engine(tab):
    """The tab's figures are the CLI's (both read `utilities`)."""
    client, db, _ = tab
    d = client.get(f"/api/utilities/summary?until={UNTIL}").json()
    with session_factory(db)() as s:
        res = utilities(s, load_settings(), as_of(UNTIL), UNTIL)
    assert [i["text"] for i in d["insights"]] == [i.text for i in res.insights] == INSIGHTS
    assert d["stats"]["month_cents"] == res.total[UNTIL] == 10000 + 6500 + 4000
    assert {r["month"]: r["total"] for r in d["rows"]} == {
        mo: res.total.get(mo, 0) for mo in d["months"]}
    electric = next(a for a in d["accounts"] if a["id"] == "electric")
    assert electric["last_payment"]["date"] == "2026-10-20"
    assert electric["prev_12_cents"] is None  # 13 months of payments: no full year before
    assert d["yoy_from"] is None and d["services"] == ["electric", "water", "internet", "phone"]
    meta = client.get("/api/meta").json()
    assert {u["id"] for u in meta["utility_accounts"]} == {
        "electric", "gas", "water", "internet", "phone"}
    water = client.get(f"/api/utilities/accounts/water?until={UNTIL}").json()
    assert water["payments"][0]["counted_in"] == "2026-07 to 2026-09"
    assert sum(water["series"].values()) == sum(p["amount_cents"] for p in water["payments"])


def test_a_correction_in_the_tab_survives_rebuild(tab):
    client, db, trash_id = tab
    assert client.get("/api/utilities/unassigned").json()[0]["transaction_id"] == trash_id
    r = client.post("/api/utilities/assign", json={"transaction_id": trash_id,
                                                    "account_id": "water"})
    assert r.status_code == 200 and r.json()["unassigned"] == 0
    r = client.patch(f"/api/utilities/payments/{trash_id}", json={"covers_months": 3})
    assert r.status_code == 200
    assert client.get("/api/utilities/unassigned").json() == []

    def link():
        with session_factory(db)() as s:
            t = s.scalar(select(m.Transaction).where(
                m.Transaction.description_raw.like("TOWN TRASH%")))
            p = s.get(m.UtilityPayment, t.id)
            return (p.account_id, p.method, p.covers_months) if p else None

    assert link() == ("water", "override", 3)
    with session_factory(db)() as s:
        rebuild(s)
    run()
    assert link() == ("water", "override", 3)


def test_as_of():
    today = date(2026, 11, 15)
    assert as_of(None, today) == today and as_of("2026-11", today) == today
    assert as_of("2026-09", today) == date(2026, 9, 30)
    assert as_of("2027-01", today) == today
