"""Dashboard API on the processed fixtures."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fin.api.app import create_app
from fin.db import models as m
from fin.db import session_factory
from fin.rebuild import rebuild
from tests.unit.test_pipeline import loaded, run  # noqa: F401  (fixture)


@pytest.fixture
def client(loaded):  # noqa: F811
    run(loaded)
    db = loaded / "db" / "finance.sqlite"
    return TestClient(create_app(db, web_dist=None), base_url="http://127.0.0.1"), db


def test_trusted_host_only(client):
    c, _ = client
    assert c.get("/api/meta").status_code == 200
    evil = TestClient(c.app, base_url="http://evil.example")
    assert evil.get("/api/meta").status_code == 400


def test_summary_adds_up(client):
    c, _ = client
    months = c.get("/api/meta").json()["months"]
    assert "2026-09" in months
    s = c.get("/api/summary", params={"month": "2026-09"}).json()
    assert s["total_cents"] == sum(r["cents"] for r in s["by_category"])
    assert s["total_cents"] == sum(r["cents"] for r in s["by_person"])
    # September purchases on the statement (Aug 22-Sep 21 lines dated in Sep) + CSV row 9/27
    assert s["total_cents"] > 0 and len(s["trend"]) == 3
    assert all("name" in r for r in s["by_category"])


def test_payments_excluded_from_spending(client):
    c, _ = client
    s = c.get("/api/summary", params={"month": "2026-09"}).json()
    txns = c.get("/api/transactions", params={"month": "2026-09"}).json()
    spend = sum(t["amount_cents"] for t in txns if t["type"] in ("purchase", "refund"))
    assert s["total_cents"] == spend


def test_transaction_detail_has_split(client):
    c, _ = client
    t = next(t for t in c.get("/api/transactions").json() if t["amount_cents"] == 5349)
    d = c.get(f"/api/transactions/{t['id']}").json()
    assert d["match"]["method"] == "exact" and len(d["match"]["items"]) == 3
    assert sum(a["amount_cents"] for a in d["allocations"]) == 5349


def test_patch_is_an_override_that_survives_rebuild(client):
    c, db = client
    t = next(t for t in c.get("/api/transactions").json() if t["description"] == "MICROSOFT")
    r = c.patch(f"/api/transactions/{t['id']}", json={"category_id": "bills.internet"})
    assert r.status_code == 200 and r.json()["category_source"] == "user"
    assert c.patch(f"/api/transactions/{t['id']}", json={"category_id": "nope"}).status_code == 422
    with session_factory(db)() as s:
        rebuild(s)
    run(db.parents[1])
    with session_factory(db)() as s:
        ms = s.scalar(select(m.Transaction).where(m.Transaction.description_clean == "MICROSOFT"))
        assert (ms.category_id, ms.category_source) == ("bills.internet", "user")


def test_rules_and_review(client):
    c, _ = client
    assert c.post("/api/rules", json={"pattern": "MICROSOFT", "category_id": "bills.internet"}
                  ).status_code == 200
    assert c.post("/api/rules", json={"pattern": "(", "category_id": "bills"}).status_code == 422
    items = c.get("/api/review").json()
    assert {i["kind"] for i in items} >= {"unexpected_merchant"}
    first = items[0]
    assert c.post(f"/api/review/{first['id']}/resolve", json={"status": "ignored"}).json()[
        "status"] == "ignored"
    assert first["id"] not in {i["id"] for i in c.get("/api/review").json()}


def test_reconciliation_and_sources(client):
    c, _ = client
    rec = c.get("/api/reconciliation").json()
    assert rec and rec[0]["math_ok"] and rec[0]["ledger_status"] == "match"
    sites = {x["site"] for x in c.get("/api/sources/status").json()}
    assert {"chase", "amazon"} <= sites


def test_orders_and_summary(client):
    c, _ = client
    orders = c.get("/api/orders", params={"merchant": "amazon"}).json()
    assert len(orders) == 2 and any(o["matched"] for o in orders)
    summ = c.get("/api/orders/summary", params={"merchant": "amazon"}).json()
    assert summ["orders"] == 2 and summ["matched"] == 1
    assert summ["total_cents"] == sum(o["total_cents"] for o in orders)
    assert sum(r["cents"] for r in summ["by_category"]) == sum(
        i["line_total_cents"] - i["discount_cents"] for o in orders for i in o["items"])
    assert summ["top_items"][0]["cents"] >= summ["top_items"][-1]["cents"]


def test_rule_editor_flow(client):
    c, _ = client
    seed = [r for r in c.get("/api/rules").json() if r["created_by"] == "seed"]
    assert seed and c.delete(f"/api/rules/{seed[0]['id']}").status_code == 422
    prev = c.get("/api/rules/preview", params={"pattern": "MICROSOFT"}).json()
    assert prev["transactions"] == 1 and prev["descriptions"][0][0] == "MICROSOFT"
    assert c.get("/api/rules/preview", params={"pattern": "("}).status_code == 422
    rid = c.post("/api/rules", json={"pattern": "MICROSOFT", "category_id": "bills.internet"}
                 ).json()["id"]
    assert c.post("/api/process").status_code == 200
    t = next(t for t in c.get("/api/transactions").json() if t["description"] == "MICROSOFT")
    assert (t["category_id"], t["category_source"]) == ("bills.internet", "rule")
    assert c.delete(f"/api/rules/{rid}").json() == {"deleted": rid}


def test_bulk_resolve(client):
    c, _ = client
    items = c.get("/api/review").json()
    ids = [i["id"] for i in items if i["kind"] == "unmatched_charge"]
    assert ids
    assert c.post("/api/review/resolve", json={"ids": ids, "status": "ignored"}).json()[
        "updated"] == len(ids)
    assert not [i for i in c.get("/api/review").json() if i["kind"] == "unmatched_charge"]



def test_item_correction_survives_rebuild(client):
    c, db = client
    t = next(t for t in c.get("/api/transactions").json() if t["amount_cents"] == 5349)
    detail = c.get(f"/api/transactions/{t['id']}").json()
    with session_factory(db)() as s:
        item = s.scalar(select(m.OrderItem).where(m.OrderItem.line_total_cents == 699))
        item_id = item.id
    r = c.patch(f"/api/order_items/{item_id}", json={"category_id": "transportation.auto_service"})
    assert r.status_code == 200
    after = c.get(f"/api/transactions/{t['id']}").json()
    cats = {a["category_id"] for a in after["allocations"]}
    assert "transportation.auto_service" in cats and cats != {a["category_id"] for a in detail[
        "allocations"]}
    with session_factory(db)() as s:
        rebuild(s)
    run(db.parents[1])
    with session_factory(db)() as s:
        item = s.scalar(select(m.OrderItem).where(m.OrderItem.line_total_cents == 699))
        assert (item.category_id, item.category_source) == ("transportation.auto_service", "user")
