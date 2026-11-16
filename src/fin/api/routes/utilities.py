# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Utilities API (docs/SPEC-utilities.md §9). GET reads the database only: no Claude, no
network. POST and PATCH need `Content-Type: application/json` (`require_json`), as everywhere,
and write corrections that survive `fin rebuild`; links are recomputed at once."""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable, Iterator
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fin.api.routes.cards import require_json
from fin.config import CardsConfig, Settings
from fin.utilities import views
from fin.utilities.corrections import MAX_COVERS, CorrectionError, assign, set_covers
from fin.utilities.series import relink

MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class AssignIn(BaseModel):
    transaction_id: int
    account_id: str
    all_from_merchant: bool = False


class CoversIn(BaseModel):
    covers_months: int = Field(ge=1, le=MAX_COVERS)


def _month(until: str | None) -> str | None:
    if until is not None and not MONTH.fullmatch(until):
        raise HTTPException(422, "until must be YYYY-MM")
    return until


def as_of(until: str | None, today: date | None = None) -> date:
    """Today, or the end of an earlier month picked in the header: an account isn't "missing"
    in a month that has passed only because the month has passed."""
    today = today or date.today()
    if until is None:
        return today
    y, mo = int(until[:4]), int(until[5:7])
    return min(today, date(y, mo, calendar.monthrange(y, mo)[1]))


def utilities_router(session: Callable[[], Iterator[Session]], settings: Settings,
                     cards: CardsConfig) -> APIRouter:
    router = APIRouter(prefix="/api/utilities")
    db = session

    @router.get("/summary")
    def summary(months: int | None = None, until: str | None = None,
                s: Session = Depends(db)):
        if months is not None and not 1 <= months <= 240:
            raise HTTPException(422, "months must be 1 to 240")
        return views.summary(s, settings, cards, as_of(_month(until)), until, months)

    @router.get("/accounts/{account_id}")
    def account(account_id: str, until: str | None = None, months: int | None = None,
                s: Session = Depends(db)):
        out = views.account_detail(s, settings, cards, account_id, as_of(_month(until)), until,
                                   months)
        if out is None:
            raise HTTPException(404, "no such utility account")
        return out

    @router.get("/unassigned")
    def unassigned(s: Session = Depends(db)):
        return views.unassigned(s, cards)

    def _relinked(s: Session) -> dict:
        res, late = relink(s, cards, settings, date.today())
        return {"linked": res.total, "unassigned": res.unassigned, "missing": late}

    @router.post("/assign", dependencies=[Depends(require_json)])
    def post_assign(body: AssignIn, s: Session = Depends(db)):
        try:
            what = assign(s, body.transaction_id, body.account_id, body.all_from_merchant)
        except CorrectionError as e:
            raise HTTPException(422, str(e)) from None
        s.commit()
        return {"applied_to": what, **_relinked(s)}

    @router.patch("/payments/{transaction_id}", dependencies=[Depends(require_json)])
    def patch_payment(transaction_id: int, body: CoversIn, s: Session = Depends(db)):
        try:
            set_covers(s, transaction_id, body.covers_months)
        except CorrectionError as e:
            raise HTTPException(422, str(e)) from None
        s.commit()
        return _relinked(s)

    return router
