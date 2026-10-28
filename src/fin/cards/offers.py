# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Card-linked offers, read-only (docs/SPEC-cards.md Phase C4).

The weekly sync saves each card's offers page (manifest kind `offers`: a `browser_snapshot`
file or a `browser_pdf_save` PDF). Import stores a snapshot row; `fin process` sends the page's
text, with names and card endings scrubbed, to `offer_extract` (haiku, no tools). Code checks
each offer's quote against the page and every number against its quote, and parses the expiry
date itself. The engine shows offers; only ones the page says are added count in the value.
Nothing here adds, enrolls in or activates an offer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.ai import claude_cli as cc
from fin.ai.runs import new_run_dir
from fin.cards import engine
from fin.cards.models import normalize_name
from fin.cards.verify import DocText, normalize, numbers_in
from fin.config import CardsConfig, DataPaths, Settings
from fin.db import models as m

TASK = "offer_extract"
OFFER_SITES = ("amex", "chase", "citi")
SUFFIXES = {".pdf", ".md", ".yml", ".yaml", ".txt"}
MIN_TEXT = 40
# A snapshot this old still shows, marked with its date.
STALE_DAYS = 21
ADDED_WORDS = ("added", "enrolled", "activated", "saved to card")

# Accessibility snapshot noise: element refs, cursor hints and link targets.
SNAPSHOT_NOISE = re.compile(r"\s*\[(?:ref|cursor|active|level)=[^\]]*\]|\s*\[(?:checked|"
                            r"disabled|expanded|pressed|selected)[^\]]*\]")
URL_LINE = re.compile(r"^\s*-?\s*/url:.*$", re.M)
PAGE_HEADER = re.compile(r"^\s*(#+ |- Page (URL|Title):)")
# `- paragraph: text`, `- button "Name"`, `- generic:` -> just the words.
SNAPSHOT_ITEM = re.compile(r'^\s*-\s*[a-z]+(?:\s+"((?:[^"\\]|\\.)*)")?\s*:?\s*(.*)$')
ENDING = re.compile(r"(?:ending(?:\s+in)?|[•·*xX]{2,}|-)\s*(\d{4,5})\b")


# --------------------------------------------------------------------------- page text


def page_text(path: Path) -> str:
    """The saved page as text with [[page N]] markers (PDF) or without its snapshot noise."""
    if path.suffix.lower() == ".pdf":
        from fin.ingest.statements import pdf_text

        pages = pdf_text(path).split("\f")
        return "\n".join(f"[[page {i}]]\n{p}" for i, p in enumerate(pages, 1))
    text = SNAPSHOT_NOISE.sub("", URL_LINE.sub("", path.read_text(errors="replace")))
    lines = []
    for line in text.splitlines():
        if line.startswith("```") or PAGE_HEADER.match(line):
            continue
        m_ = SNAPSHOT_ITEM.match(line)
        if m_ is None:
            lines.append(line.strip())
            continue
        name, inline = (m_.group(1) or "").replace('\\"', '"'), (m_.group(2) or "").strip()
        lines.append(" ".join(x for x in (name, inline) if x))
    return "\n".join(x for x in lines if x)


def scrub(text: str, cards: CardsConfig) -> str:
    """Remove what offer_extract must never see (SPEC §7): names and card endings."""
    endings = {h.card_ending for c in cards.cards for h in c.cardholders if h.card_ending}
    text = ENDING.sub("[ending]", text)
    for e in endings:
        text = re.sub(rf"\b{e}\b", "[ending]", text)
    names: set[str] = set()
    for p in cards.people:
        names |= {p.full_name, *p.name_aliases}
        first = p.full_name.split()[0] if p.full_name.split() else ""
        if len(first) >= 3:  # "Welcome back, Alex"; a surname alone is rare and may be a brand
            names.add(first)
    for n in sorted(names, key=len, reverse=True):
        if n.strip():
            text = re.sub(rf"\b{re.escape(n.strip())}\b", "[name]", text, flags=re.I)
    return text


# --------------------------------------------------------------------------- import


def import_offers(s: Session, path: Path, entry: dict[str, Any], imp: m.Import):
    """Parser for (amex|chase|citi, offers): store a snapshot row; extraction runs later."""
    from fin.ingest.inbox import ParseOutcome

    if not entry.get("card_id"):
        raise ValueError("an offers file needs its card_id")
    if path.suffix.lower() not in SUFFIXES:
        raise ValueError(f"offers file type {path.suffix or '(none)'} is not supported")
    text = page_text(path)
    if len(normalize(text)) < MIN_TEXT:
        return ParseOutcome(0, problems=["the saved offers page has no text"])
    captured = entry.get("period_end") or date.today().isoformat()
    digest = hashlib.sha256(normalize(text).encode()).hexdigest()
    snap = s.scalar(select(m.CardOfferSnapshot).where(m.CardOfferSnapshot.import_id == imp.id))
    if snap is None:  # `fin rebuild` re-parses: keep the snapshot and its offers
        s.add(m.CardOfferSnapshot(import_id=imp.id, card_id=entry["card_id"],
                                  captured_on=captured, text_sha256=digest))
    return ParseOutcome(1, period_end=captured)


def _register() -> None:
    from fin.ingest.inbox import register

    for site in OFFER_SITES:
        register(site, "offers")(import_offers)


_register()


# --------------------------------------------------------------------------- verify


DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d %Y", "%B %d %Y")
DATE_IN_TEXT = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b",
    re.I)


DAYS_LEFT = re.compile(r"\b(\d{1,3})\s+days?\s+left\b", re.I)


def parse_expiry(text: str | None, captured_on: str | None = None) -> str | None:
    """The date in the page's own expiry words ("Expires 11/30/26", or Chase's "7 days left"
    counted from the day the page was saved), parsed by code."""
    if not text:
        return None
    left = DAYS_LEFT.search(text)
    if left and captured_on:
        from datetime import timedelta

        return (date.fromisoformat(captured_on) + timedelta(days=int(left.group(1)))).isoformat()
    hit = DATE_IN_TEXT.search(text)
    if not hit:
        return None
    raw = " ".join(hit.group(1).replace(".", "").replace(",", " ").split())
    raw = re.sub(r"(?i)^sept\b", "Sep", raw)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


@dataclass
class OfferCheck:
    ok: bool = True
    reasons: list[str] = field(default_factory=list)

    def fail(self, why: str) -> None:
        self.ok = False
        self.reasons.append(why)


def verify_offer(o: dict[str, Any], doc: DocText) -> OfferCheck:
    """The quote is on the page; the merchant name, reward, minimum and maximum are in the
    quote; the expiry words are on the page; "added" needs the page's own word for it."""
    c = OfferCheck()
    quote = o.get("quote", "")
    if not doc.contains(quote, None):
        c.fail("quote not found on the page")
        return c
    q = normalize(quote)
    if normalize_name(o["merchant"]) not in normalize_name(quote):
        c.fail("merchant name is not in the quote")
    found = numbers_in(quote)
    r = o["reward"]
    checks = [("reward", r["value"])]
    if o.get("min_spend_usd") is not None:
        checks.append(("minimum spend", o["min_spend_usd"]))
    if r.get("max") is not None:
        checks.append(("maximum", r["max"]))
    for name, v in checks:
        if not any(abs(v - f) < 1e-6 for f in found):
            c.fail(f"{name} {v:g} is not in the quote")
    if o.get("expires_text") and not doc.contains(o["expires_text"], None):
        c.fail("expiry words not found on the page")
    if o.get("added") is True and not any(w in q for w in ADDED_WORDS):
        o["added"] = None  # not shown in the quote: unknown, so it doesn't count
    return c


# --------------------------------------------------------------------------- extract


@dataclass
class OffersResult:
    snapshots: int = 0
    found: int = 0
    verified: int = 0
    calls: int = 0
    cached: int = 0
    paused: str | None = None
    failed: list[str] = field(default_factory=list)


def extract_offers(s: Session, paths: DataPaths, settings: Settings, cards: CardsConfig, *,
                   use_ai: bool = True, runner: Any = None) -> OffersResult:
    """`fin process` step: read every new offers snapshot. One call per page, cached on the
    scrubbed text, so a re-run (or a rebuild) asks nothing twice."""
    out = OffersResult()
    task = cc.load_task(TASK)
    todo = s.scalars(select(m.CardOfferSnapshot).where(
        m.CardOfferSnapshot.status.in_(("new", "limited", "failed")))
        .order_by(m.CardOfferSnapshot.id)).all()
    for snap in todo:
        imp = s.get(m.Import, snap.import_id)
        path = Path(imp.file_path)
        if not path.exists():
            snap.status, snap.error = "failed", "file missing"
            continue
        card = cards.card(snap.card_id)
        text = scrub(page_text(path), cards)
        key = hashlib.sha256(text.encode()).hexdigest()
        out.snapshots += 1
        cached = s.get(m.AICache, (TASK, key, task.prompt_version))
        if cached is not None:
            output = json.loads(cached.output_json)
            out.cached += 1
        elif not use_ai or out.paused:
            continue
        else:
            run_dir = new_run_dir(paths.ai_runs, TASK)
            payload = {"card": {"product": card.product, "issuer": card.issuer},
                       "captured_on": snap.captured_on, "page": text}
            try:
                res = (runner or _call(settings))(task, payload, run_dir)
            except cc.UsageLimitError as e:
                out.paused = str(e)
                snap.status = "limited"
                s.add(m.AIRun(task=TASK, run_dir=str(run_dir), n_items=1, status="limited",
                              error=str(e)))
                s.commit()
                continue
            except cc.AIError as e:
                out.calls += 1
                snap.status, snap.error, snap.run_dir = "failed", str(e)[:500], str(run_dir)
                out.failed.append(f"{snap.card_id}: {e}")
                s.add(m.AIRun(task=TASK, run_dir=str(run_dir), n_items=1, status="failed",
                              error=str(e)[:500]))
                s.commit()
                continue
            out.calls += 1
            output = res.output
            snap.run_dir = str(run_dir)
            s.merge(m.AICache(task=TASK, key=key, prompt_version=task.prompt_version,
                              output_json=json.dumps(output), model="haiku"))
            s.add(m.AIRun(task=TASK, run_dir=str(run_dir), n_items=1, status="ok",
                          duration_ms=res.duration_ms))
        _store(s, snap, output, DocText.parse(text))
        out.found += snap.n_found
        out.verified += snap.n_verified
        s.commit()
    s.commit()
    return out


def _call(settings: Settings):
    def call(task, payload, run_dir):
        return cc.run_task(task, payload, run_dir, model="haiku",
                           max_turns=settings.ai.max_turns,
                           timeout=settings.ai.timeout_seconds)
    return call


def _store(s: Session, snap: m.CardOfferSnapshot, output: dict[str, Any], doc: DocText) -> None:
    for old in s.scalars(select(m.CardOffer).where(m.CardOffer.snapshot_id == snap.id)):
        s.delete(old)
    s.flush()
    n_ok = 0
    for o in output.get("offers", []):
        c = verify_offer(o, doc)
        r = o["reward"]
        dollars = r["kind"] in ("cash", "percent")  # value (cash) and max in dollars -> cents
        maximum = r.get("max")
        s.add(m.CardOffer(
            snapshot_id=snap.id, card_id=snap.card_id, merchant_name=o["merchant"][:200],
            merchant_norm=normalize_name(o["merchant"]), reward_kind=r["kind"],
            reward_value=round(r["value"] * 100) if r["kind"] == "cash" else r["value"],
            max_value=None if maximum is None else (round(maximum * 100) if dollars
                                                    else maximum),
            min_spend_cents=None if o.get("min_spend_usd") is None
            else round(o["min_spend_usd"] * 100),
            expires_on=parse_expiry(o.get("expires_text"), snap.captured_on),
            added=o.get("added"),
            channel=o.get("channel"), quote=o.get("quote", "")[:1000], verified=c.ok,
            reject_reason="; ".join(c.reasons)[:500] or None))
        n_ok += c.ok
    snap.status, snap.error = "extracted", None
    snap.n_found, snap.n_verified = len(output.get("offers", [])), n_ok


# --------------------------------------------------------------------------- current offers


def current_offers(s: Session, issuers: dict[str, str], today: date | None = None
                   ) -> dict[str, list[engine.Offer]]:
    """Each card's verified, unexpired offers from its latest read snapshot. `issuers` maps
    card id to issuer id."""
    today = today or date.today()
    out: dict[str, list[engine.Offer]] = {}
    for card_id, issuer in issuers.items():
        snap = s.scalar(select(m.CardOfferSnapshot).where(
            m.CardOfferSnapshot.card_id == card_id, m.CardOfferSnapshot.status == "extracted")
            .order_by(m.CardOfferSnapshot.captured_on.desc(), m.CardOfferSnapshot.id.desc())
            .limit(1))
        if snap is None:
            continue
        rows = s.scalars(select(m.CardOffer).where(
            m.CardOffer.snapshot_id == snap.id, m.CardOffer.verified.is_(True))).all()
        out[card_id] = [
            engine.Offer(issuer=issuer, merchant_name=r.merchant_name,
                         merchant_norm=r.merchant_norm, reward_kind=r.reward_kind,
                         reward_value=r.reward_value, max_value=r.max_value,
                         min_spend_cents=r.min_spend_cents, expires_on=r.expires_on,
                         added=r.added, channel=r.channel, captured_on=snap.captured_on,
                         stale=(today - date.fromisoformat(snap.captured_on)).days > STALE_DAYS)
            for r in rows if not r.expires_on or r.expires_on >= today.isoformat()]
    return out


def offers_at_my_merchants(s: Session, cards: CardsConfig, currencies: dict[str, str],
                           facts: engine.Facts, today: date | None = None, days: int = 90
                           ) -> list[dict[str, Any]]:
    """Offers at merchants I've bought from in the last `days`, best value first, valued at
    my typical purchase there (or the offer's minimum, if higher). `currencies`: card id ->
    the currency its profile earns."""
    from datetime import timedelta

    from fin.cards.history import PURCHASE, _day, merchant_index, merchant_of

    today = today or date.today()
    aliases, merchants = merchant_index(s)
    spend: dict[str, list[int]] = {}
    names: dict[str, str] = {}
    since = today - timedelta(days=days)
    for t in s.scalars(select(m.Transaction).where(m.Transaction.type == PURCHASE)):
        if _day(t) < since:
            continue
        mp = merchant_of(t, aliases, merchants)
        if mp is None:
            continue
        spend.setdefault(mp.key, []).append(t.amount_cents)
        names[mp.key] = mp.display_name
    products = {c.id: c.product for c in cards.cards}
    rows = []
    for card_id, offers in facts.offers.items():
        for o in offers:
            for key, amounts in spend.items():
                merchant = engine.Merchant(key, names[key], "other")
                if not engine.offer_fits(o, merchant, facts):
                    continue
                typical = sorted(amounts)[len(amounts) // 2]
                currency = facts.pools.get(card_id, currencies.get(card_id, "cash_back"))
                amount = max(typical, o.min_spend_cents or 0)
                rows.append({"card_id": card_id, "product": products.get(card_id, card_id),
                             "merchant_key": key, "merchant": names[key],
                             "purchases": len(amounts), "typical_cents": typical,
                             "amount_cents": amount,
                             **engine.offer_line(o, amount, facts.valuation(currency))})
    rows.sort(key=lambda r: -r["value_cents"])
    return rows


def offers_summary(s: Session, cards: CardsConfig, currencies: dict[str, str],
                   facts: engine.Facts, today: date | None = None) -> dict[str, Any]:
    """Per card: the latest offers page read and its counts; plus offers at my merchants."""
    today = today or date.today()
    rows = []
    for c in cards.cards:
        snap = s.scalar(select(m.CardOfferSnapshot).where(
            m.CardOfferSnapshot.card_id == c.id)
            .order_by(m.CardOfferSnapshot.captured_on.desc(), m.CardOfferSnapshot.id.desc())
            .limit(1))
        if snap is None:
            continue
        current = facts.offers.get(c.id, [])
        rows.append({"card_id": c.id, "product": c.product, "captured_on": snap.captured_on,
                     "status": snap.status, "found": snap.n_found, "verified": snap.n_verified,
                     "current": len(current), "added": sum(1 for o in current if o.added),
                     "stale": (today - date.fromisoformat(snap.captured_on)).days > STALE_DAYS})
    return {"cards": rows,
            "at_my_merchants": offers_at_my_merchants(s, cards, currencies, facts, today)}
