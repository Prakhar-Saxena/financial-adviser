# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`fin cards refresh` (docs/SPEC-cards.md §7): discover, fetch, extract, verify, compose.

Card by card in cards.json order, committing after each, so an interruption or a usage limit
loses at most one card's work and the next run picks up where this one stopped. One refresh
at a time (locks/cards-refresh.lock); AI calls one at a time (locks/claude.lock).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from fin.ai import claude_cli as cc
from fin.cards import compose, docs, review
from fin.cards.discover import PROBLEM_STATUSES, discover, due_reason
from fin.cards.extract import TASK_TYPES, USABLE, extract, input_key, latest_good, sources
from fin.cards.fetch import Fetcher, host_allowed
from fin.config import Card, CardsConfig, DataPaths, Settings
from fin.db import models as m
from fin.db.models import utcnow

LIMIT_MESSAGE = "AI paused: subscription limit reached. Rerun `fin cards refresh` later."
STALE_RUN_HOURS = 2
MAX_FAILURES = 3
MANUAL_SUFFIXES = {".pdf": "application/pdf", ".html": "text/html", ".htm": "text/html"}
MANUAL_TYPES = (("benefit", "benefits_guide"), ("guide", "benefits_guide"),
                ("agreement", "cardmember_agreement"), ("pricing", "pricing_terms"),
                ("rates", "pricing_terms"), ("program", "program_terms"),
                ("reward", "rewards_terms"), ("terms", "rewards_terms"))


class RefreshBusyError(RuntimeError):
    pass


@contextmanager
def refresh_lock(paths: DataPaths) -> Iterator[None]:
    paths.locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = paths.locks / "cards-refresh.lock"
    with open(path, "a") as f:
        path.chmod(0o600)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RefreshBusyError("a card refresh is already running") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def abort_stale_runs(s: Session, now: datetime | None = None) -> int:
    """`running` rows older than 2 hours are left over from a crash: mark them aborted."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(hours=STALE_RUN_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = s.execute(update(m.CardRefreshRun)
                  .where(m.CardRefreshRun.status == "running",
                         m.CardRefreshRun.started_at < cutoff)
                  .values(status="aborted", ended_at=utcnow()))
    s.commit()
    return r.rowcount or 0


def doc_names(card: Card, cards: CardsConfig, settings: Settings, doc_type: str) -> list[str]:
    """Names a document must mention (§7.2): the product and its aliases, plus the issuer for
    program-level documents."""
    names = [card.product, *settings.cards.product_aliases.get(card.id, [])]
    if doc_type in docs.PROGRAM_LEVEL:
        names += [cards.site(card.issuer).name]
    return names


# --------------------------------------------------------------------------- per card


@dataclass
class CardReport:
    card_id: str
    outcome: str = "checked"
    steps: list[str] = field(default_factory=list)
    docs_checked: int = 0
    docs_changed: int = 0
    ai_calls: int = 0
    version: int | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {"card_id": self.card_id, "outcome": self.outcome, "steps": self.steps,
                "docs_checked": self.docs_checked, "docs_changed": self.docs_changed,
                "ai_calls": self.ai_calls, "version": self.version, "error": self.error}


Progress = Callable[[str, str], None]


class CardRefresher:
    def __init__(self, s: Session, paths: DataPaths, settings: Settings, cards: CardsConfig,
                 fetcher: Fetcher, *, force: bool = False, retext: bool = False,
                 progress: Progress | None = None):
        self.s, self.paths, self.settings, self.cards = s, paths, settings, cards
        self.fetcher, self.force, self.retext = fetcher, force, retext
        self.progress = progress or (lambda card, step: None)

    def run(self, card: Card, rep: CardReport, discover_now: bool) -> None:
        s = self.s
        domains = self.settings.cards.domains_for(card, self.cards.site(card.issuer))
        self._step(card, rep, "manual documents")
        self.scan_manual(card, rep)
        self._reactivate(card, domains)

        discovered = False
        reason = due_reason(s, card.id, self.settings, force=discover_now)
        if reason:
            self._discover(card, rep, domains, reason)
            discovered = True
        if self.retext:
            self._step(card, rep, "re-read stored files")
            self.retext_all(card, rep)
        self._step(card, rep, "fetch")
        self.fetch_all(card, rep, domains)
        if not discovered:
            reason = due_reason(s, card.id, self.settings)
            if reason:
                self._discover(card, rep, domains, reason)
                self.fetch_all(card, rep, domains)
        self._retire_replaced(card)

        ids = []
        for task in ("card_terms", "card_benefits"):
            self._step(card, rep, task)
            try:
                out = extract(s, self.paths, self.settings, card, task, force=self.force)
            except cc.UsageLimitError:
                raise
            except cc.AIError as e:
                rep.ai_calls += 1
                rep.error = f"{task}: {e}"
                good = latest_good(s, card.id, task)
                if good is not None:
                    ids.append(good.id)
                continue
            rep.ai_calls += out.called
            if out.extraction_id is not None:
                ids.append(out.extraction_id)
        self._step(card, rep, "compose")
        self.compose(card, rep, ids)
        self.source_problems(card)

    def _step(self, card: Card, rep: CardReport, step: str) -> None:
        rep.steps.append(step)
        self.progress(card.id, step)

    def _discover(self, card: Card, rep: CardReport, domains: list[str], reason: str) -> None:
        self._step(card, rep, f"discover ({reason})")
        rep.ai_calls += 1
        try:
            discover(self.s, self.paths, self.settings, card, domains, reason)
        except cc.UsageLimitError:
            raise
        except cc.AIError as e:
            rep.error = f"discover: {e}"
        self.s.flush()

    def _reactivate(self, card: Card, domains: list[str]) -> None:
        """A `needs_domain` document whose host I've since allowlisted becomes active."""
        for d in self.s.scalars(select(m.CardDocument).where(
                m.CardDocument.card_id == card.id, m.CardDocument.status == "needs_domain")):
            if host_allowed(urlsplit(d.url).hostname or "", domains):
                d.status, d.note, d.failures = "active", None, 0

    # ------------------------------------------------------------------ fetch

    def fetch_all(self, card: Card, rep: CardReport, domains: list[str]) -> None:
        today = utcnow()[:10]
        for d in self.s.scalars(select(m.CardDocument).where(
                m.CardDocument.card_id == card.id, m.CardDocument.status == "active",
                m.CardDocument.origin != "manual_file").order_by(m.CardDocument.id)).all():
            if d.last_checked_at and d.last_checked_at[:10] == today:
                continue  # at most one fetch per document per day
            rep.docs_checked += 1
            if self.fetch_one(card, d, domains) == "changed":
                rep.docs_changed += 1

    def retext_all(self, card: Card, rep: CardReport) -> None:
        """Extract text again from the stored files (after the text extraction improved), without
        downloading anything. A changed text becomes a new version, so extraction reruns."""
        for d in self.s.scalars(select(m.CardDocument).where(
                m.CardDocument.card_id == card.id).order_by(m.CardDocument.id)).all():
            v = docs.latest_ok_version(self.s, d.id)
            if v is None or not v.file_path or not Path(v.file_path).is_file():
                continue
            checked = d.last_checked_at
            res = docs.store(self.s, self.paths, d, Path(v.file_path).read_bytes(),
                             v.content_type or "", doc_names(card, self.cards, self.settings,
                                                             d.doc_type))
            d.last_checked_at = checked  # not a fetch: keep the date of the last real check
            rep.docs_changed += res.kind == "changed"

    def fetch_one(self, card: Card, d: m.CardDocument, domains: list[str]) -> str:
        prev = docs.latest_ok_version(self.s, d.id)
        got = self.fetcher.fetch(d.url, domains, etag=d.etag if prev else None,
                                 last_modified=d.last_modified if prev else None)
        now = utcnow()
        match got.outcome:
            case "ok":
                res = docs.store(self.s, self.paths, d, got.data, got.content_type,
                                 doc_names(card, self.cards, self.settings, d.doc_type),
                                 http_status=got.status)
                if res.kind == "rejected":
                    if res.reason == "thin_or_wrong_page":
                        d.status = "needs_browser"
                        d.note = "too little text or doesn't name the card (a JavaScript page?)"
                    else:
                        d.status, d.note = "broken", res.reason
                    return "rejected"
                d.etag, d.last_modified, d.failures = got.etag, got.last_modified, 0
                return res.kind
            case "not_modified":
                d.last_checked_at, d.failures = now, 0
                return "unchanged"
            case "off_allowlist":
                d.status = "needs_domain"
                d.note = f"redirects to {got.detail}, which is not on this card's allowlist"
            case "robots" | "blocked":
                d.status, d.note = "blocked", got.detail or f"HTTP {got.status}"
            case "gone":
                d.status, d.note = "broken", f"HTTP {got.status}"
            case "too_large":
                d.status, d.note = "broken", f"file {got.detail}"
            case _:
                d.failures += 1
                d.note = got.detail
                if d.failures >= MAX_FAILURES:
                    d.status = "broken"
        return got.outcome

    def _retire_replaced(self, card: Card) -> None:
        """A broken document is retired once an active one of the same type has text."""
        all_docs = self.s.scalars(select(m.CardDocument).where(
            m.CardDocument.card_id == card.id)).all()
        healthy = {d.doc_type for d in all_docs
                   if d.status == "active" and docs.latest_ok_version(self.s, d.id)}
        for d in all_docs:
            if d.status == "broken" and d.doc_type in healthy:
                d.status = "retired"

    # ------------------------------------------------------------------ manual

    def scan_manual(self, card: Card, rep: CardReport) -> None:
        """Files I dropped into card-docs/manual/<card_id>/ (§7.6)."""
        folder = self.paths.card_docs / "manual" / card.id
        if not folder.is_dir():
            return
        for f in sorted(folder.iterdir()):
            ctype = MANUAL_SUFFIXES.get(f.suffix.lower())
            if ctype is None or not f.is_file():
                continue
            url = f"manual:{f.name}"
            d = self.s.scalars(select(m.CardDocument).where(
                m.CardDocument.card_id == card.id, m.CardDocument.url == url)).first()
            if d is None:
                d = m.CardDocument(card_id=card.id, url=url, doc_type=guess_type(f.name),
                                   title=f.stem[:200], origin="manual_file", status="active")
                self.s.add(d)
                self.s.flush()
            if self.add_file(card, d, f.read_bytes(), ctype) == "changed":
                rep.docs_changed += 1
            rep.docs_checked += 1

    def add_file(self, card: Card, d: m.CardDocument, data: bytes, ctype: str) -> str:
        prev = docs.latest_ok_version(self.s, d.id)
        if prev is not None and prev.sha256 == hashlib.sha256(data).hexdigest():
            d.last_checked_at = utcnow()
            return "unchanged"
        # I chose this file for this card myself, so it needn't name the card (Chase's pricing
        # pages, for one, never do). It still has to be a readable document of real length.
        res = docs.store(self.s, self.paths, d, data, ctype, [])
        if res.kind == "rejected":
            d.status, d.note = "broken", f"manual file rejected: {res.reason}"
        elif d.status != "active":
            d.status, d.note = "active", None
        return res.kind

    # ------------------------------------------------------------------ compose

    def compose(self, card: Card, rep: CardReport, extraction_ids: list[int]) -> None:
        rows = {e.task: e for e in (self.s.get(m.CardExtraction, i) for i in extraction_ids)
                if e is not None}
        terms = rows.get("card_terms")
        if terms is None:
            rep.outcome = "no terms yet"
            return
        benefits = rows.get("card_benefits")
        try:
            profile, dropped = compose.build_profile(
                self.s, card, self.settings, json.loads(terms.output_json),
                json.loads(benefits.output_json) if benefits else None)
        except compose.IncompleteError as e:
            rep.outcome = f"incomplete: {e}"
            review.open_item(self.s, "card_terms_unverified", card.id, "incomplete",
                             {"problem": "incomplete", "message": str(e)})
            # A pending version the current documents no longer support is withdrawn.
            pending = compose.current(self.s, card.id, "pending")
            if pending is not None:
                pending.status, pending.decided_at = "superseded", utcnow()
                review.close_items(self.s, "card_terms_pending", card.id, "pending")
            return
        review.close_items(self.s, "card_terms_unverified", card.id, "incomplete")
        if dropped:
            review.open_item(self.s, "card_terms_unverified", card.id, "compose",
                             {"problem": "invalid", "problems": dropped})
        else:
            review.close_items(self.s, "card_terms_unverified", card.id, "compose")
        out = compose.propose(self.s, card.id, profile, sorted(e.id for e in rows.values()),
                              self.settings.cards.auto_publish)
        rep.outcome, rep.version = out.kind, out.version

    # ------------------------------------------------------------------ problems

    def source_problems(self, card: Card) -> None:
        """card_source_problem items for what still needs me after this run (§6, §7.1)."""
        s = self.s
        all_docs = s.scalars(select(m.CardDocument).where(
            m.CardDocument.card_id == card.id)).all()
        usable = {d.doc_type for d in all_docs
                  if d.status in USABLE and docs.latest_ok_version(s, d.id)}
        healthy = {d.doc_type for d in all_docs if d.status == "active"}
        keep: set[str] = set()

        def flag(key: str, **details: object) -> None:
            keep.add(key)
            review.open_item(s, "card_source_problem", card.id, key, details)

        for d in all_docs:
            if d.status == "needs_domain":
                host = urlsplit(d.url).hostname or ""
                flag(f"domain:{host}", problem="needs_domain", domain=host, url=d.url,
                     doc_type=d.doc_type,
                     message=f"{host} isn't on this card's allowlist. If it really is the "
                             f"issuer's, add it under [cards.doc_domains] {card.id}.")
            elif d.status in PROBLEM_STATUSES and d.doc_type not in healthy:
                flag(f"doc:{d.id}", problem=d.status, url=d.url, doc_type=d.doc_type,
                     note=d.note or "",
                     message=f"The {d.doc_type.replace('_', ' ')} is {d.status.replace('_', ' ')}"
                             f". Save it in your browser into card-docs/manual/{card.id}/.")
        if not usable & {"product_page", "rewards_terms"}:
            flag("missing:terms", problem="missing", doc_type="product_page",
                 message="No product page or rewards terms yet.")
        if "benefits_guide" not in usable:
            flag("missing:benefits_guide", problem="missing", doc_type="benefits_guide",
                 message=f"No guide to benefits yet. If the issuer publishes it elsewhere, add "
                         f"its domain, or save the PDF into card-docs/manual/{card.id}/.")
        review.close_items(s, "card_source_problem", card.id, keep=keep)


def guess_type(filename: str) -> str:
    low = filename.lower()
    for needle, doc_type in MANUAL_TYPES:
        if needle in low:
            return doc_type
    return "product_page"


# --------------------------------------------------------------------------- whole run


def unfinished(s: Session, card: Card, settings: Settings) -> bool:
    """True when a task's current input has no usable extraction (an earlier run stopped)."""
    for task_name in TASK_TYPES:
        srcs = sources(s, card.id, task_name)
        if not srcs:
            continue
        key = input_key(cc.load_task(task_name), settings.cards.extract_model, srcs,
                        settings.cards.max_extract_chars)
        if s.scalars(select(m.CardExtraction.id).where(
                m.CardExtraction.card_id == card.id, m.CardExtraction.task == task_name,
                m.CardExtraction.input_key == key,
                m.CardExtraction.status.in_(("ok", "partial")))).first() is None:
            return True
    return False


def is_due(s: Session, card: Card, settings: Settings, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=settings.cards.refresh_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    checked = [d.last_checked_at for d in s.scalars(select(m.CardDocument).where(
        m.CardDocument.card_id == card.id, m.CardDocument.status == "active"))
        if d.last_checked_at]
    return not checked or max(checked) < cutoff or unfinished(s, card, settings)


@dataclass
class RefreshResult:
    run_id: int
    status: str
    reports: list[CardReport]
    message: str = ""


def start_run(s: Session, trigger: str, card_ids: list[str]) -> m.CardRefreshRun:
    run = m.CardRefreshRun(trigger=trigger, cards_json=json.dumps(card_ids))
    s.add(run)
    s.commit()
    return run


def refresh(s: Session, paths: DataPaths, settings: Settings, cards: CardsConfig, *,
            card_ids: list[str] | None = None, discover: bool = False, force: bool = False,
            retext: bool = False, if_due: bool = False, trigger: str = "cli",
            transport: httpx.BaseTransport | None = None,
            sleep: Callable[[float], None] = time.sleep, progress: Progress | None = None,
            run: m.CardRefreshRun | None = None) -> RefreshResult:
    """The whole refresh. Holds the refresh lock; raises RefreshBusyError if one is running."""
    wanted = [c for c in cards.cards if card_ids is None or c.id in card_ids]
    if card_ids:
        unknown = set(card_ids) - {c.id for c in cards.cards}
        if unknown:
            raise KeyError(f"unknown card ids: {sorted(unknown)}")
    with refresh_lock(paths):
        abort_stale_runs(s)
        from fin.cards.merchants import sync_seeds

        sync_seeds(s)  # so merchant names in new terms map onto the seed profiles
        if run is None:
            run = start_run(s, trigger, [c.id for c in wanted])
        fetcher = Fetcher(settings.cards, transport, sleep=sleep)
        worker = CardRefresher(s, paths, settings, cards, fetcher, force=force, retext=retext,
                               progress=progress)
        reports: list[CardReport] = []
        errors: list[str] = []
        status, message = "ok", ""
        try:
            for card in wanted:
                rep = CardReport(card.id)
                reports.append(rep)
                if if_due and not is_due(s, card, settings):
                    rep.outcome = "not due"
                    continue
                try:
                    worker.run(card, rep, discover)
                except cc.UsageLimitError:
                    s.rollback()
                    rep.outcome, status, message = "limited", "limited", LIMIT_MESSAGE
                    _count(run, rep)
                    break
                if rep.error:
                    errors.append(f"{card.id}: {rep.error}")
                _count(run, rep)
                run.cards_json = json.dumps([r.as_dict() for r in reports])
                s.commit()
        except BaseException:
            s.rollback()
            run.status, run.ended_at = "failed", utcnow()
            run.cards_json = json.dumps([r.as_dict() for r in reports])
            s.commit()
            raise
        if status == "ok" and errors:
            status = "partial"
        run.status, run.ended_at = status, utcnow()
        run.cards_json = json.dumps([r.as_dict() for r in reports])
        run.errors_json = json.dumps(errors)
        s.commit()
        return RefreshResult(run.id, status, reports, message)


def _count(run: m.CardRefreshRun, rep: CardReport) -> None:
    run.docs_checked += rep.docs_checked
    run.docs_changed += rep.docs_changed
    run.ai_calls += rep.ai_calls
    run.profiles_pending += rep.outcome == "pending"
    run.profiles_published += rep.outcome == "published"


def add_doc(s: Session, paths: DataPaths, settings: Settings, cards: CardsConfig, card: Card,
            file: Path, doc_type: str, url: str | None = None) -> tuple[m.CardDocument, str]:
    """`fin cards add-doc`: a document I downloaded myself, stored like a download."""
    ctype = MANUAL_SUFFIXES.get(file.suffix.lower())
    if ctype is None:
        raise ValueError("only .pdf, .html and .htm files")
    key = url or f"manual:{file.name}"
    d = s.scalars(select(m.CardDocument).where(m.CardDocument.card_id == card.id,
                                               m.CardDocument.url == key)).first()
    if d is None:
        d = m.CardDocument(card_id=card.id, url=key, doc_type=doc_type, title=file.stem[:200],
                           origin="manual_file", status="active")
        s.add(d)
        s.flush()
    else:
        d.doc_type, d.origin = doc_type, "manual_file"
    worker = CardRefresher(s, paths, settings, cards, Fetcher(settings.cards))
    outcome = worker.add_file(card, d, file.read_bytes(), ctype)
    s.commit()
    return d, outcome
