# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Card optimizer API (docs/SPEC-cards.md §12), Phase C1: profiles, approval, refresh, documents.

GET never calls Claude or the network. POST needs `Content-Type: application/json`
(`require_json`), so a page on another site can't start AI or network work with a simple
cross-site form post.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.cards import compose
from fin.cards.status import card_status
from fin.config import CardsConfig, DataPaths, Settings
from fin.db import models as m

NOSNIFF = {"X-Content-Type-Options": "nosniff"}


QUESTION_WAIT = 60  # seconds to wait for another Claude call to finish


def require_json(request: Request) -> None:
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, "Content-Type must be application/json")


class QuestionIn(BaseModel):
    card_id: str
    question: str


class Decide(BaseModel):
    version: int


class LookupIn(BaseModel):
    q: str
    web: bool = False


class MerchantPatch(BaseModel):
    category: str | None = None
    acceptance: dict[str, str] | None = None
    alias: str | None = None


class CreditMark(BaseModel):
    card_id: str
    identity: str
    period_key: str | None = None
    used: str | None = None  # full | none | cents; null clears my mark
    alias: str | None = None  # a statement keyword for this credit; "" clears it
    clear: bool = False


class RefreshIn(BaseModel):
    card_id: str | None = None
    discover: bool = False


# Progress of refreshes started from the dashboard: run id -> {card, step}.
PROGRESS: dict[int, dict[str, str]] = {}


def _doc_row(s: Session, d: m.CardDocument) -> dict[str, Any]:
    from fin.cards.docs import latest_ok_version

    v = latest_ok_version(s, d.id)
    return {"id": d.id, "url": d.url, "doc_type": d.doc_type, "title": d.title,
            "origin": d.origin, "status": d.status, "note": d.note,
            "last_checked_at": d.last_checked_at, "last_changed_at": d.last_changed_at,
            "version_id": v.id if v else None,
            "content_type": v.content_type if v else None}


def _version_row(v: m.CardProfileVersion) -> dict[str, Any]:
    d = json.loads(v.diff_json or "{}")
    return {"version": v.version, "status": v.status, "material": v.material,
            "created_at": v.created_at, "decided_at": v.decided_at,
            "summary": compose.summarize(d) if d else "", "diff": d}


def cards_router(session: Callable[[], Iterator[Session]], settings: Settings,
                 cards: CardsConfig, paths: DataPaths) -> APIRouter:
    router = APIRouter(prefix="/api/cards")
    db = session

    @router.get("/profiles")
    def profiles(s: Session = Depends(db)):
        rows = card_status(s, cards, settings.cards.refresh_days)
        problems: dict[str, int] = {}
        for item in s.scalars(select(m.ReviewItem).where(
                m.ReviewItem.kind.in_(m.CARD_REVIEW_KINDS), m.ReviewItem.status == "open")):
            problems[item.ref_id or ""] = problems.get(item.ref_id or "", 0) + 1
        out = []
        for r in rows:
            active = compose.load(compose.current(s, r.card_id, "active"))
            out.append({
                "card_id": r.card_id, "product": r.product,
                "active_version": r.active_version, "pending_version": r.pending_version,
                "docs": dict(r.docs), "last_checked": r.last_checked,
                "last_changed": r.last_changed, "stale": r.stale,
                "open_items": problems.get(r.card_id, 0),
                "currency": active.currency if active else None,
                "annual_fee_cents": int(active.annual_fee_cents.value) if active else None,
            })
        return out

    @router.get("/profiles/{card_id}")
    def profile(card_id: str, s: Session = Depends(db)):
        try:
            card = cards.card(card_id)
        except KeyError:
            raise HTTPException(404, "unknown card") from None
        active = compose.current(s, card_id, "active")
        pending = compose.current(s, card_id, "pending")
        versions = s.scalars(select(m.CardProfileVersion).where(
            m.CardProfileVersion.card_id == card_id)
            .order_by(m.CardProfileVersion.version.desc())).all()
        documents = s.scalars(select(m.CardDocument).where(
            m.CardDocument.card_id == card_id).order_by(m.CardDocument.doc_type)).all()
        items = s.scalars(select(m.ReviewItem).where(
            m.ReviewItem.kind.in_(m.CARD_REVIEW_KINDS), m.ReviewItem.status == "open",
            m.ReviewItem.ref_table == "cards", m.ReviewItem.ref_id == card_id)).all()
        active_p = compose.load(active)
        currency = active_p.currency if active_p else None
        return {
            "card_id": card_id, "product": card.product, "issuer": card.issuer,
            "network": card.network,
            "active": ({"version": active.version, "decided_at": active.decided_at,
                        "profile": json.loads(active.profile_json)} if active else None),
            "pending": ({"version": pending.version, "created_at": pending.created_at,
                         "first": active is None, "profile": json.loads(pending.profile_json),
                         "diff": json.loads(pending.diff_json or "{}")} if pending else None),
            "valuation_cents": settings.cards.valuations.get(currency, 1.0) if currency else None,
            "documents": [_doc_row(s, d) for d in documents],
            "changelog": [_version_row(v) for v in versions],
            "review": [{"id": i.id, "kind": i.kind, "details": json.loads(i.details_json)}
                       for i in items],
        }

    def _decide(card_id: str, body: Decide, s: Session, fn) -> dict:
        try:
            v = fn(s, card_id, body.version)
        except compose.NothingPendingError as e:
            raise HTTPException(409, str(e)) from None
        s.commit()
        return {"card_id": card_id, "version": v.version, "status": v.status}

    @router.post("/profiles/{card_id}/accept", dependencies=[Depends(require_json)])
    def accept(card_id: str, body: Decide, s: Session = Depends(db)):
        return _decide(card_id, body, s, compose.accept)

    @router.post("/profiles/{card_id}/reject", dependencies=[Depends(require_json)])
    def reject(card_id: str, body: Decide, s: Session = Depends(db)):
        return _decide(card_id, body, s, compose.reject)

    @router.post("/refresh", dependencies=[Depends(require_json)])
    def start_refresh(body: RefreshIn, s: Session = Depends(db)):
        from fin.cards.refresh import abort_stale_runs, start_run

        if body.card_id is not None:
            try:
                cards.card(body.card_id)
            except KeyError:
                raise HTTPException(404, "unknown card") from None
        abort_stale_runs(s)
        if s.scalars(select(m.CardRefreshRun.id).where(
                m.CardRefreshRun.status == "running")).first() is not None:
            raise HTTPException(409, "a card refresh is already running")
        ids = [body.card_id] if body.card_id else [c.id for c in cards.cards]
        run = start_run(s, "dashboard", ids)
        PROGRESS[run.id] = {"card": "", "step": "starting"}
        threading.Thread(target=_background, args=(session, run.id, body), daemon=True).start()
        return {"run_id": run.id}

    def _background(session_gen, run_id: int, body: RefreshIn) -> None:
        from fin.cards.refresh import RefreshBusyError, refresh

        def progress(card_id: str, step: str) -> None:
            PROGRESS[run_id] = {"card": card_id, "step": step}

        gen = session_gen()
        s = next(gen)
        try:
            run = s.get(m.CardRefreshRun, run_id)
            refresh(s, paths, settings, cards,
                    card_ids=[body.card_id] if body.card_id else None,
                    discover=body.discover, trigger="dashboard", progress=progress, run=run)
        except RefreshBusyError:
            run = s.get(m.CardRefreshRun, run_id)
            run.status = "aborted"
            run.errors_json = json.dumps(["another refresh was running"])
            s.commit()
        except Exception as e:  # keep the server up; the run row records what happened
            s.rollback()
            run = s.get(m.CardRefreshRun, run_id)
            if run is not None and run.status == "running":
                run.status = "failed"
            if run is not None:
                run.errors_json = json.dumps([*json.loads(run.errors_json),
                                              f"{type(e).__name__}: {e}"[:300]])
                s.commit()
        finally:
            PROGRESS.pop(run_id, None)
            gen.close()

    # ------------------------------------------------------------- best card (§12)

    @router.get("/best")
    def best(q: str = "", amount_cents: int | None = None, channel: str | None = None,
             abroad: bool = False, key: str | None = None, s: Session = Depends(db)):
        from fin.cards.best import best as rank

        if channel not in (None, "", "in_store", "online", "in_app"):
            raise HTTPException(422, "channel is in_store, online or in_app")
        if amount_cents is not None and not 0 < amount_cents <= 10_000_000:
            raise HTTPException(422, "amount_cents out of range")
        if not q.strip() and not key:
            raise HTTPException(422, "q is required")
        try:
            return rank(s, settings, cards, q, amount_cents=amount_cents, channel=channel or None,
                        abroad=abroad, merchant_key=key,
                        low_confidence=settings.ai.low_confidence)
        except KeyError:
            raise HTTPException(404, "unknown merchant") from None

    @router.post("/merchants/lookup", dependencies=[Depends(require_json)])
    def merchant_lookup(body: LookupIn, s: Session = Depends(db)):
        from fin.ai import claude_cli as cc
        from fin.cards.merchants import LOOKUP_TIMEOUT, NotAMerchantError, lookup

        if not body.q.strip() or len(body.q) > 120:
            raise HTTPException(422, "q must be 1-120 characters")
        try:
            res = lookup(s, paths, settings, body.q, web=body.web, lock_wait=LOOKUP_TIMEOUT)
        except NotAMerchantError as e:
            raise HTTPException(422, str(e)) from None
        except cc.AIBusyError:
            raise HTTPException(409, "Claude is busy with another task; try again soon") from None
        except cc.UsageLimitError:
            raise HTTPException(429, "AI paused: subscription limit reached. Try later.") from None
        except cc.AIError as e:
            raise HTTPException(502, f"lookup failed: {e}"[:300]) from None
        return {"not_a_merchant": res.not_a_merchant,
                "merchant": res.merchant.as_dict() if res.merchant else None}

    @router.post("/question", dependencies=[Depends(require_json)])
    def question(body: QuestionIn, s: Session = Depends(db)):
        from fin.ai import claude_cli as cc
        from fin.cards.question import QuestionError, ask

        try:
            card = cards.card(body.card_id)
        except KeyError:
            raise HTTPException(404, "unknown card") from None
        try:
            return ask(s, paths, settings, card, body.question, lock_wait=QUESTION_WAIT)
        except QuestionError as e:
            raise HTTPException(422, str(e)) from None
        except cc.AIBusyError:
            raise HTTPException(409, "Claude is busy with another task; try again soon") from None
        except cc.UsageLimitError:
            raise HTTPException(429, "AI paused: subscription limit reached. Try later.") from None
        except cc.AIError as e:
            raise HTTPException(502, f"the question failed: {e}"[:300]) from None

    @router.get("/merchants")
    def merchants(q: str = "", s: Session = Depends(db)):
        from fin.cards.merchants import autocomplete

        return autocomplete(s, q)

    @router.patch("/merchants/{key}", dependencies=[Depends(require_json)])
    def patch_merchant(key: str, body: MerchantPatch, s: Session = Depends(db)):
        from fin.cards.merchants import correct

        try:
            return correct(s, key, category=body.category, acceptance=body.acceptance,
                           alias=body.alias).as_dict()
        except KeyError:
            raise HTTPException(404, "unknown merchant") from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @router.get("/settings")
    def get_settings(s: Session = Depends(db)):
        from fin.cards.best import get_settings as read

        return read(s, settings, cards)

    @router.put("/settings", dependencies=[Depends(require_json)])
    def put_settings(body: dict, s: Session = Depends(db)):
        from fin.cards.best import get_settings as read
        from fin.cards.best import put_settings as write

        try:
            write(s, cards, body)
        except (ValueError, KeyError, TypeError) as e:
            s.rollback()
            raise HTTPException(422, f"bad settings: {e}") from None
        return read(s, settings, cards)

    # ------------------------------------------------------------- my data (§10)

    def _inputs(s: Session):
        from fin.cards.best import active_profiles, load_facts

        return active_profiles(s), load_facts(s, settings, cards)

    @router.get("/tips")
    def tips(months: int | None = None, s: Session = Depends(db)):
        from fin.cards.history import tips as missed

        if months is not None and not 1 <= months <= 24:
            raise HTTPException(422, "months is 1-24")
        profiles, facts = _inputs(s)
        return missed(s, settings, cards, profiles, facts, months or settings.cards.tips_months)

    @router.get("/credits")
    def credits(s: Session = Depends(db)):
        from datetime import date

        from fin.cards.history import credit_use

        profiles, facts = _inputs(s)
        today = date.today()
        return [u.as_dict(today) for u in credit_use(s, profiles, cards, facts, today)]

    @router.put("/credits", dependencies=[Depends(require_json)])
    def mark_credit(body: CreditMark, s: Session = Depends(db)):
        from fin.cards.history import mark_credit as mark

        if body.card_id not in {c.id for c in cards.cards}:
            raise HTTPException(404, "unknown card")
        try:
            mark(s, body.card_id, body.identity, period_key=body.period_key,
                 used=None if body.clear else body.used, alias=body.alias)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return credits(s)

    @router.get("/cheatsheet")
    def cheatsheet(amount_cents: int | None = None, s: Session = Depends(db)):
        from fin.cards.history import cheatsheet as sheet

        profiles, facts = _inputs(s)
        return sheet(s, cards, profiles, facts,
                     amount_cents or settings.cards.default_amount_cents)

    @router.get("/rewards-check")
    def rewards_check(months: int = 6, s: Session = Depends(db)):
        from fin.cards.rewards import rewards_check as check

        if not 1 <= months <= 24:
            raise HTTPException(422, "months is 1-24")
        profiles, facts = _inputs(s)
        return check(s, cards, profiles, facts, months)

    @router.get("/offers")
    def offers(s: Session = Depends(db)):
        from fin.cards.offers import offers_summary

        profiles, facts = _inputs(s)
        return offers_summary(s, cards, {k: p.currency for k, p in profiles.items()}, facts)

    @router.get("/refresh/{run_id}")
    def refresh_status(run_id: int, s: Session = Depends(db)):
        run = s.get(m.CardRefreshRun, run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        p = PROGRESS.get(run_id, {})
        return {"run_id": run.id, "status": run.status, "card": p.get("card"),
                "step": p.get("step"), "started_at": run.started_at, "ended_at": run.ended_at,
                "docs_checked": run.docs_checked, "docs_changed": run.docs_changed,
                "ai_calls": run.ai_calls, "profiles_pending": run.profiles_pending,
                "profiles_published": run.profiles_published,
                "cards": json.loads(run.cards_json), "errors": json.loads(run.errors_json)}

    @router.get("/documents/{version_id}")
    def document(version_id: int, s: Session = Depends(db)):
        v = s.get(m.CardDocVersion, version_id)
        if v is None or v.status != "ok" or not v.file_path:
            raise HTTPException(404, "unknown document version")
        root = (paths.card_docs / "files").resolve()
        target = Path(v.file_path).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(404, "document not in card-docs")
        if v.content_type == "application/pdf":
            return FileResponse(target, media_type="application/pdf", headers={
                **NOSNIFF, "Content-Disposition": "inline"})
        # HTML only as its extracted text, never as a live page on the dashboard's origin.
        text_root = (paths.card_docs / "text").resolve()
        text_path = Path(v.text_path or "").resolve()
        if text_root not in text_path.parents or not text_path.is_file():
            raise HTTPException(404, "text not in card-docs")
        return PlainTextResponse(text_path.read_text(), headers=NOSNIFF)

    return router
