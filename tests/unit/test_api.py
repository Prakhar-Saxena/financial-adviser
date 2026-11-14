# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

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
    t = next(t for t in c.get("/api/transactions").json() if t["amount_cents"] == 5211)
    d = c.get(f"/api/transactions/{t['id']}").json()
    assert d["match"]["method"] == "exact" and len(d["match"]["items"]) == 3
    assert sum(a["amount_cents"] for a in d["allocations"]) == 5211


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
    assert c.post("/api/process").status_code == 415  # needs JSON (SPEC-cards §12)
    assert c.post("/api/process", json={}).status_code == 200
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
    t = next(t for t in c.get("/api/transactions").json() if t["amount_cents"] == 5211)
    detail = c.get(f"/api/transactions/{t['id']}").json()
    with session_factory(db)() as s:
        item = s.scalar(select(m.OrderItem).where(m.OrderItem.line_total_cents == 749))
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
        item = s.scalar(select(m.OrderItem).where(m.OrderItem.line_total_cents == 749))
        assert (item.category_id, item.category_source) == ("transportation.auto_service", "user")



def test_meta_category_rank(client):
    c, _ = client
    rank = c.get("/api/meta").json()["category_rank"]
    assert rank and "uncategorized" not in rank and len(rank) == len(set(rank))


def test_transactions_filter_uncategorized(client):
    """The charts' "Uncategorized" link lists uncategorized spending, not payments."""
    c, db = client
    with session_factory(db)() as s:
        t = s.scalars(select(m.Transaction).where(m.Transaction.type == "purchase")).first()
        t.category_id, t.category_source = None, None
        s.commit()
        target = t.id
    rows = c.get("/api/transactions", params={"category": "uncategorized"}).json()
    assert target in {r["id"] for r in rows}
    assert all(r["category_id"] in (None, "uncategorized") and r["type"] != "payment" for r in rows)


def test_transactions_filter_several_categories(client):
    """The donut's "Other" links with several categories: the union of each one's rows."""
    c, _ = client
    month = c.get("/api/meta").json()["months"][-1]
    cats = [r["key"] for r in c.get("/api/summary", params={"month": month}).json()["by_category"]]
    assert len(cats) >= 2

    def ids(category: str) -> set[int]:
        rows = c.get("/api/transactions", params={"month": month, "category": category}).json()
        return {r["id"] for r in rows}

    union = set().union(*(ids(k) for k in cats[:2]))
    assert ids(",".join(cats[:2])) == union and union


def test_transactions_filter_matches_split_parts(client):
    """A split charge is listed under every category it is split into, as in the charts."""
    c, db = client
    with session_factory(db)() as s:
        a = s.scalars(select(m.Allocation).join(
            m.Transaction, m.Transaction.id == m.Allocation.transaction_id).where(
            m.Allocation.category_id != m.Transaction.category_id)).first()
        assert a, "fixture has a split charge with a part outside its main category"
        txn_id, part = a.transaction_id, a.category_id
    rows = c.get("/api/transactions", params={"category": part.split(".")[0]}).json()
    assert txn_id in {r["id"] for r in rows}


def test_web_build_caching(tmp_path, client):
    """The page is revalidated on every load; hashed assets are cached forever."""
    _, db = client
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text('<script src="/assets/index-abc123.js"></script>')
    (tmp_path / "assets" / "index-abc123.js").write_text("// build")
    c = TestClient(create_app(db, web_dist=tmp_path), base_url="http://127.0.0.1")
    for path in ("/", "/transactions"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    r = c.get("/assets/index-abc123.js")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert "cache-control" not in c.get("/assets/missing.js").headers


# ------------------------------------------------- checking (docs/SPEC-checking.md Phase B0)


def add_checking_rows(db, rows):
    """Fictional checking rows: (amount, type, flow_kind, description)."""
    with session_factory(db)() as s:
        s.merge(m.Card(id="checking", issuer="bank", product="Example Bank Checking",
                       network="none", card_type="checking"))
        imp = s.scalar(select(m.Import.id))
        for i, (amt, typ, flow, desc) in enumerate(rows):
            s.add(m.Transaction(card_id="checking", person_id="self",  # §5: rows default to self
                                person_source="default_self",
                                post_date="2026-09-10", amount_cents=amt,
                                description_raw=desc, description_clean=desc, type=typ,
                                flow_kind=flow, dedupe_key=f"bank-{i}", import_id=imp))
        s.commit()


@pytest.fixture
def example_cards(monkeypatch):
    """The example config, which has a bank account (the real one may not yet)."""
    from fin.config import CONFIG_DIR, load_cards

    cards = load_cards(CONFIG_DIR / "cards.example.json")
    monkeypatch.setattr("tests.unit.test_pipeline.load_cards", lambda: cards)
    monkeypatch.setattr("fin.api.app.load_cards", lambda: cards)
    return cards


def test_transfers_and_deposits_never_count_as_spending(client, example_cards):
    c, db = client
    before = c.get("/api/summary", params={"month": "2026-09"}).json()
    add_checking_rows(db, [(51290, "transfer", "card_payment", "EXAMPLE CARD AUTOPAY"),
                           (-250000, "deposit", "income", "EXAMPLE CORP PAYROLL"),
                           (-1200, "transfer", "own_transfer", "TRANSFER FROM SAVINGS")])
    run(db.parent.parent)  # fin process, now with checking rows in the ledger
    after = c.get("/api/summary", params={"month": "2026-09"}).json()
    assert after["total_cents"] == before["total_cents"]
    assert after["credits_rewards_cents"] == before["credits_rewards_cents"]
    assert after["by_category"] == before["by_category"]
    rows = c.get("/api/transactions", params={"card": "checking"}).json()
    assert len(rows) == 3
    assert all(not r["counts_as_spending"] and r["category_id"] is None for r in rows)
    assert {r["flow_kind"] for r in rows} == {"card_payment", "income", "own_transfer"}
    refs = {str(r["id"]) for r in rows}
    assert not [i for i in c.get("/api/review").json()
                if i["ref_table"] == "transactions" and i["ref_id"] in refs]
    card_rows = c.get("/api/transactions", params={"month": "2026-09"}).json()
    assert all(r["counts_as_spending"] == (r["type"] in ("purchase", "refund", "fee", "interest"))
               for r in card_rows)


def test_a_debit_amazon_purchase_doesnt_break_matching(client, example_cards):
    """match_amazon looked a row's card up with cards.card(), which raises on an account."""
    c, db = client
    add_checking_rows(db, [(1999, "purchase", None, "AMAZON MKTP US")])
    r = run(db.parent.parent)
    assert r is not None
    (row,) = c.get("/api/transactions", params={"card": "checking"}).json()
    assert row["merchant_group"] == "amazon" and row["counts_as_spending"]


def test_meta_names_accounts_next_to_cards(client, example_cards):
    _, db = client
    meta = TestClient(create_app(db, web_dist=None), base_url="http://127.0.0.1").get(
        "/api/meta").json()
    kinds = {x["id"]: x["kind"] for x in meta["cards"]}
    assert kinds["checking"] == "account" and kinds["amex_gold"] == "card"
    assert next(x for x in meta["cards"] if x["id"] == "checking")["name"] == \
        "Example Bank Checking"
