"""Local dashboard API (SPEC §15.1). Bound to 127.0.0.1 only; trusted hosts localhost/127.0.0.1.

No CORS: the Vite dev server proxies /api to this app, and `fin serve` serves the built app.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fin.api.spending import months_with_data, summarize, txn_month
from fin.config import REPO_ROOT, data_paths, load_cards, load_settings, load_taxonomy
from fin.db import models as m
from fin.db import session_factory
from fin.db.models import utcnow
from fin.sync.params import months_before

WEB_DIST = REPO_ROOT / "web" / "dist"


# Request bodies live at module level so FastAPI can resolve them under
# `from __future__ import annotations`.
class TxnPatch(BaseModel):
    category_id: str | None = None
    person_id: str | None = None


class RuleIn(BaseModel):
    pattern: str
    category_id: str
    merchant_name: str | None = None


class Resolve(BaseModel):
    status: str = "resolved"  # resolved | ignored


class BulkResolve(BaseModel):
    ids: list[int]
    status: str = "resolved"


def create_app(db_file: Path | None = None, web_dist: Path | None = WEB_DIST) -> FastAPI:
    db_file = db_file or data_paths(load_settings()).db_file
    Session_ = session_factory(db_file)
    cards = load_cards()
    taxonomy = load_taxonomy()
    cat_names = {c.id: c.name for c in taxonomy.categories}
    app = FastAPI(title="fin", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    def db():
        with Session_() as s:
            yield s

    # ------------------------------------------------------------- meta

    @app.get("/api/meta")
    def meta(s: Session = Depends(db)):
        from fin.api.spending import parent, spending_lines

        # Parent categories by all-time spend: the dashboard gives the top ones fixed colours,
        # so a category keeps its colour across months and chart types.
        totals: dict[str, int] = {}
        for line in spending_lines(s):
            k = parent(line.category_id)
            totals[k] = totals.get(k, 0) + line.amount_cents
        return {
            "months": months_with_data(s),
            "category_rank": [k for k, v in sorted(totals.items(), key=lambda kv: -kv[1])
                              if v > 0 and k != "uncategorized"],
            "people": [{"id": p.id, "name": p.display_name} for p in cards.people],
            "cards": [{"id": c.id, "name": c.product, "issuer": c.issuer} for c in cards.cards],
            "categories": [{"id": c.id, "name": c.name, "parent_id": c.parent_id}
                           for c in taxonomy.categories],
        }

    # ---------------------------------------------------------- summary

    @app.get("/api/summary")
    def summary(month: str | None = None, s: Session = Depends(db)):
        months = months_with_data(s)
        month = month or (months[-1] if months else date.today().strftime("%Y-%m"))
        y, mo = map(int, month.split("-"))
        trend = sorted({months_before(date(y, mo, 1), k).strftime("%Y-%m") for k in range(3)})
        out = summarize(s, month, trend)
        for row in out["by_category"] + out["by_subcategory"]:
            row["name"] = cat_names.get(row["key"], row["key"])
        return out

    # ----------------------------------------------------- transactions

    def txn_row(t: m.Transaction, merchant: m.Merchant | None) -> dict:
        return {
            "id": t.id, "txn_date": t.txn_date, "post_date": t.post_date,
            "card_id": t.card_id, "person_id": t.person_id, "person_source": t.person_source,
            "amount_cents": t.amount_cents, "description": t.description_clean,
            "description_raw": t.description_raw, "type": t.type, "source": t.source,
            "merchant": merchant.name if merchant else None,
            "merchant_group": merchant.merchant_group if merchant else None,
            "category_id": t.category_id, "category_source": t.category_source,
            "review_status": t.review_status, "order_ref": t.order_ref,
        }

    @app.get("/api/transactions")
    def transactions(month: str | None = None, card: str | None = None,
                     person: str | None = None, category: str | None = None,
                     group: str | None = None, type: str | None = None,
                     review: bool = False, s: Session = Depends(db)):
        q = (select(m.Transaction, m.Merchant)
             .outerjoin(m.Merchant, m.Merchant.id == m.Transaction.merchant_id)
             .order_by(func.coalesce(m.Transaction.txn_date, m.Transaction.post_date).desc(),
                       m.Transaction.id.desc()))
        if month:
            q = q.where(txn_month() == month)
        if card:
            q = q.where(m.Transaction.card_id == card)
        if person:
            q = q.where(m.Transaction.person_id == person)
        if category:
            q = q.where((m.Transaction.category_id == category)
                        | m.Transaction.category_id.like(f"{category}.%"))
        if group:
            q = q.where(m.Merchant.merchant_group == group)
        if type:
            q = q.where(m.Transaction.type == type)
        if review:
            open_ids = select(m.ReviewItem.ref_id).where(
                m.ReviewItem.status == "open", m.ReviewItem.ref_table == "transactions")
            q = q.where(func.cast(m.Transaction.id, m.String).in_(open_ids))
        return [txn_row(t, mer) for t, mer in s.execute(q.limit(2000)).all()]

    @app.get("/api/transactions/{txn_id}")
    def transaction(txn_id: int, s: Session = Depends(db)):
        t = s.get(m.Transaction, txn_id)
        if t is None:
            raise HTTPException(404)
        row = txn_row(t, s.get(m.Merchant, t.merchant_id) if t.merchant_id else None)
        row["allocations"] = [{"category_id": a.category_id, "amount_cents": a.amount_cents}
                              for a in s.scalars(select(m.Allocation).where(
                                  m.Allocation.transaction_id == t.id))]
        match = s.scalar(select(m.Match).where(m.Match.transaction_id == t.id))
        row["match"] = None
        if match and match.order_id:
            o = s.get(m.Order, match.order_id)
            items = s.scalars(select(m.OrderItem).where(m.OrderItem.order_id == o.id)
                              .order_by(m.OrderItem.line_no)).all()
            row["match"] = {
                "method": match.method, "confidence": match.confidence,
                "order": {"id": o.id, "merchant": o.merchant, "external_id": o.external_id,
                          "order_date": o.order_date, "total_cents": o.total,
                          "tax_cents": o.tax, "shipping_cents": o.shipping},
                "items": [{"title": i.title_clean or i.title_raw, "title_raw": i.title_raw,
                           "quantity": i.quantity, "line_total_cents": i.line_total_cents,
                           "category_id": i.category_id, "sku": i.sku} for i in items],
            }
        row["reviews"] = [{"id": r.id, "kind": r.kind, "details": json.loads(r.details_json)}
                          for r in s.scalars(select(m.ReviewItem).where(
                              m.ReviewItem.status == "open",
                              m.ReviewItem.ref_table == "transactions",
                              m.ReviewItem.ref_id == str(t.id)))]
        return row

    @app.patch("/api/transactions/{txn_id}")
    def patch_transaction(txn_id: int, body: TxnPatch, s: Session = Depends(db)):
        t = s.get(m.Transaction, txn_id)
        if t is None:
            raise HTTPException(404)
        changes = body.model_dump(exclude_none=True)
        if "category_id" in changes and changes["category_id"] not in cat_names:
            raise HTTPException(422, "unknown category")
        if "person_id" in changes and changes["person_id"] not in {p.id for p in cards.people}:
            raise HTTPException(422, "unknown person")
        for field, value in changes.items():
            existing = s.scalar(select(m.UserOverride).where(
                m.UserOverride.target_type == "transaction",
                m.UserOverride.stable_key == t.dedupe_key, m.UserOverride.field == field))
            if existing:
                existing.value = value
            else:
                s.add(m.UserOverride(target_type="transaction", stable_key=t.dedupe_key,
                                     field=field, value=value))
            if field == "category_id":
                t.category_id, t.category_source = value, "user"
                s.query(m.Allocation).filter(m.Allocation.transaction_id == t.id).delete()
            else:
                t.person_id, t.person_source = value, "user"
        _resolve_for(s, t.id, changes)
        s.commit()
        return transaction(txn_id, s)

    def _resolve_for(s: Session, txn_id: int, changes: dict) -> None:
        kinds = []
        if "category_id" in changes:
            kinds += ["uncategorized", "low_confidence"]
        if "person_id" in changes:
            kinds += ["unknown_person"]
        for r in s.scalars(select(m.ReviewItem).where(
                m.ReviewItem.status == "open", m.ReviewItem.ref_table == "transactions",
                m.ReviewItem.ref_id == str(txn_id), m.ReviewItem.kind.in_(kinds))):
            r.status, r.resolved_at = "resolved", utcnow()

    @app.post("/api/rules")
    def create_rule(body: RuleIn, s: Session = Depends(db)):
        import re

        if body.category_id not in cat_names:
            raise HTTPException(422, "unknown category")
        try:
            re.compile(body.pattern)
        except re.error as e:
            raise HTTPException(422, f"bad pattern: {e}") from e
        rule = m.Rule(priority=100, match_field="description_clean", pattern=body.pattern,
                      merchant_name=body.merchant_name, category_id=body.category_id,
                      created_by="user")
        s.add(rule)
        s.commit()
        return {"id": rule.id, "note": "applies on the next `fin process`"}

    # -------------------------------------------------------------- rules

    @app.get("/api/rules")
    def rules(s: Session = Depends(db)):
        return [{"id": r.id, "pattern": r.pattern, "merchant_name": r.merchant_name,
                 "merchant_group": r.merchant_group, "category_id": r.category_id,
                 "created_by": r.created_by, "priority": r.priority}
                for r in s.scalars(select(m.Rule).order_by(m.Rule.created_by != "user",
                                                           m.Rule.priority, m.Rule.id))]

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: int, s: Session = Depends(db)):
        r = s.get(m.Rule, rule_id)
        if r is None:
            raise HTTPException(404)
        if r.created_by != "user":
            raise HTTPException(422, "seed rules live in config/rules.yaml")
        s.delete(r)
        s.commit()
        return {"deleted": rule_id}

    @app.get("/api/rules/preview")
    def rule_preview(pattern: str, s: Session = Depends(db)):
        import re

        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise HTTPException(422, f"bad pattern: {e}") from e
        hits: dict[str, int] = {}
        for (desc,) in s.execute(select(m.Transaction.description_clean)):
            if rx.match(desc):
                hits[desc] = hits.get(desc, 0) + 1
        return {"transactions": sum(hits.values()),
                "descriptions": sorted(hits.items(), key=lambda kv: -kv[1])[:20]}

    @app.post("/api/process")
    def reprocess(s: Session = Depends(db)):
        """Re-run fin process without AI, so new rules and corrections apply now."""
        from fin.pipeline import process

        settings = load_settings()
        r = process(s, data_paths(settings), settings, cards, taxonomy, use_ai=False)
        return {"rule_matched": r.rule_matched, "ledger": r.ledger,
                "reviews_open": r.reviews_open}

    # ------------------------------------------------------------- orders

    def _orders(s: Session, merchant: str, month: str | None):
        q = select(m.Order).where(m.Order.merchant == merchant)
        if month:
            q = q.where(func.substr(m.Order.order_date, 1, 7) == month)
        return s.scalars(q.order_by(m.Order.order_date.desc())).all()

    @app.get("/api/orders")
    def orders(merchant: str = "amazon", month: str | None = None, s: Session = Depends(db)):
        matched = set(s.scalars(select(m.Match.order_id).where(m.Match.order_id.is_not(None))))
        out = []
        for o in _orders(s, merchant, month):
            items = s.scalars(select(m.OrderItem).where(m.OrderItem.order_id == o.id)
                              .order_by(m.OrderItem.line_no)).all()
            out.append({"id": o.id, "external_id": o.external_id, "order_date": o.order_date,
                        "channel": o.channel, "location": o.location,
                        "total_cents": o.total, "tax_cents": o.tax,
                        "discounts_cents": o.discounts, "payment_method": o.payment_method,
                        "matched": o.id in matched,
                        "items": [{"title": i.title_clean or i.title_raw,
                                   "title_raw": i.title_raw, "sku": i.sku,
                                   "quantity": i.quantity, "category_id": i.category_id,
                                   "line_total_cents": i.line_total_cents,
                                   "discount_cents": i.discount_cents} for i in items]})
        return out

    @app.get("/api/orders/summary")
    def orders_summary(merchant: str = "amazon", month: str | None = None,
                       s: Session = Depends(db)):
        from fin.api.spending import parent

        matched = set(s.scalars(select(m.Match.order_id).where(m.Match.order_id.is_not(None))))
        orders_ = _orders(s, merchant, month)
        by_item: dict[str, dict] = {}
        by_cat: dict[str, int] = {}
        for o in orders_:
            for i in s.scalars(select(m.OrderItem).where(m.OrderItem.order_id == o.id)):
                paid = i.line_total_cents - i.discount_cents
                key = i.sku or (i.title_clean or i.title_raw).lower()
                row = by_item.setdefault(key, {"title": i.title_clean or i.title_raw,
                                               "category_id": i.category_id, "cents": 0,
                                               "count": 0})
                row["cents"] += paid
                row["count"] += i.quantity
                cat = parent(i.category_id or "uncategorized")
                by_cat[cat] = by_cat.get(cat, 0) + paid
        return {
            "orders": len(orders_),
            "matched": sum(o.id in matched for o in orders_),
            "total_cents": sum(o.total for o in orders_),
            "top_items": sorted(by_item.values(), key=lambda r: -r["cents"])[:12],
            "by_category": [{"key": k, "name": cat_names.get(k, k), "cents": v}
                            for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])],
        }

    # ------------------------------------------------------------- review

    @app.get("/api/review")
    def review(s: Session = Depends(db)):
        out = []
        for r in s.scalars(select(m.ReviewItem).where(m.ReviewItem.status == "open")
                           .order_by(m.ReviewItem.kind, m.ReviewItem.id)):
            item = {"id": r.id, "kind": r.kind, "ref_table": r.ref_table, "ref_id": r.ref_id,
                    "details": json.loads(r.details_json or "{}"), "created_at": r.created_at}
            if r.ref_table == "transactions" and r.ref_id:
                t = s.get(m.Transaction, int(r.ref_id))
                if t:
                    item["transaction"] = txn_row(
                        t, s.get(m.Merchant, t.merchant_id) if t.merchant_id else None)
            if r.ref_table == "order_items" and r.ref_id:
                it = s.get(m.OrderItem, int(r.ref_id))
                if it:
                    o = s.get(m.Order, it.order_id)
                    item["item"] = {"id": it.id, "title": it.title_clean or it.title_raw,
                                    "title_raw": it.title_raw, "merchant": o.merchant,
                                    "order_date": o.order_date,
                                    "cents": it.line_total_cents - it.discount_cents,
                                    "category_id": it.category_id}
            if r.ref_table == "orders" and r.ref_id:
                o = s.get(m.Order, int(r.ref_id))
                if o:
                    item["order"] = {"external_id": o.external_id, "order_date": o.order_date,
                                     "total_cents": o.total}
            out.append(item)
        return out

    @app.post("/api/review/{item_id}/resolve")
    def resolve(item_id: int, body: Resolve, s: Session = Depends(db)):
        r = s.get(m.ReviewItem, item_id)
        if r is None:
            raise HTTPException(404)
        if body.status not in ("resolved", "ignored"):
            raise HTTPException(422)
        r.status, r.resolved_at = body.status, utcnow()
        s.commit()
        return {"id": r.id, "status": r.status}

    # ----------------------------------------------------- reconciliation

    @app.patch("/api/order_items/{item_id}")
    def patch_item(item_id: int, body: TxnPatch, s: Session = Depends(db)):
        from fin.categorize.rules import item_stable_key
        from fin.match.amazon import allocate_matched

        it = s.get(m.OrderItem, item_id)
        if it is None:
            raise HTTPException(404)
        if body.category_id not in cat_names:
            raise HTTPException(422, "unknown category")
        key = item_stable_key(s.get(m.Order, it.order_id), it)
        existing = s.scalar(select(m.UserOverride).where(
            m.UserOverride.target_type == "order_item", m.UserOverride.stable_key == key,
            m.UserOverride.field == "category_id"))
        if existing:
            existing.value = body.category_id
        else:
            s.add(m.UserOverride(target_type="order_item", stable_key=key, field="category_id",
                                 value=body.category_id))
        it.category_id, it.category_source = body.category_id, "user"
        for r in s.scalars(select(m.ReviewItem).where(
                m.ReviewItem.status == "open", m.ReviewItem.ref_table == "order_items",
                m.ReviewItem.ref_id == str(item_id))):
            r.status, r.resolved_at = "resolved", utcnow()
        allocate_matched(s)  # the charge's split follows the corrected item
        s.commit()
        return {"id": item_id, "category_id": it.category_id}

    @app.post("/api/review/resolve")
    def resolve_bulk(body: BulkResolve, s: Session = Depends(db)):
        if body.status not in ("resolved", "ignored"):
            raise HTTPException(422)
        n = 0
        for r in s.scalars(select(m.ReviewItem).where(m.ReviewItem.id.in_(body.ids),
                                                     m.ReviewItem.status == "open")):
            r.status, r.resolved_at = body.status, utcnow()
            n += 1
        s.commit()
        return {"updated": n}

    @app.get("/api/reconciliation")
    def reconciliation(s: Session = Depends(db)):
        return [{"id": st.id, "card_id": st.card_id, "period_start": st.period_start,
                 "period_end": st.period_end, "new_balance_cents": st.new_balance,
                 "math_ok": st.math_ok, "ledger_status": st.ledger_status,
                 "ledger_delta_cents": st.ledger_delta_cents}
                for st in s.scalars(select(m.Statement).order_by(m.Statement.card_id,
                                                                 m.Statement.period_end))]

    @app.get("/api/sources/status")
    def sources_status(s: Session = Depends(db)):
        out = []
        for site in [i.id for i in cards.issuers] + [i.id for i in cards.item_sources]:
            run = s.scalar(select(m.SyncRun).where(m.SyncRun.site == site)
                           .order_by(m.SyncRun.id.desc()))
            gaps = s.scalar(select(func.count(m.ReviewItem.id)).where(
                m.ReviewItem.kind == "sync_gap", m.ReviewItem.status == "open",
                m.ReviewItem.ref_table == "sync_runs",
                m.ReviewItem.ref_id == str(run.id))) if run else 0
            out.append({"site": site, "last_run": run.started_at if run else None,
                        "status": run.status if run else None,
                        "files_imported": run.files_imported if run else 0,
                        "guard_blocks": run.guard_blocks if run else 0, "open_gaps": gaps})
        return out

    # --------------------------------------------------------- web app

    if web_dist and web_dist.exists():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            target = web_dist / path
            if path and target.is_file() and web_dist in target.resolve().parents:
                return FileResponse(target)
            return FileResponse(web_dist / "index.html")

    return app
