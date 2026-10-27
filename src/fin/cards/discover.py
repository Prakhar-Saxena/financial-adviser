# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Find a card's official documents (docs/SPEC-cards.md §7.1).

`card_discover` (claude -p with WebSearch and allowlisted WebFetch) proposes URLs; code
normalizes them, re-checks the allowlist and stores new ones as `active` documents. A URL off
the allowlist becomes a `needs_domain` document, for me to decide on.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.ai import claude_cli as cc
from fin.ai.runs import new_run_dir
from fin.cards.fetch import host_allowed
from fin.config import Card, DataPaths, Settings
from fin.db import models as m

WANTED = ["product_page", "rewards_terms", "program_terms", "pricing_terms",
          "cardmember_agreement", "benefits_guide", "benefit_terms"]
PROBLEM_STATUSES = ("broken", "blocked", "needs_browser")
# A discovery that found nothing new isn't repeated for a week, unless I ask (--discover).
RETRY_DAYS = 7
TRACKING_PREFIXES = ("utm_", "mc_", "_ga", "_gl", "pk_", "hsa_")
TRACKING_KEYS = {"gclid", "fbclid", "msclkid", "dclid", "yclid", "cmpid", "icid", "srsltid",
                 "jp_cmp_cc", "jp_cmp_ch"}


def normalize_url(url: str) -> str | None:
    """https, lower-case host, no fragment, no tracking parameters. None if unusable."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if parts.port and parts.port != 443:
        host = f"{host}:{parts.port}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not (k.lower().startswith(TRACKING_PREFIXES) or k.lower() in TRACKING_KEYS)]
    return urlunsplit(("https", host, parts.path or "/", urlencode(query), ""))


def last_discovery(s: Session, card_id: str) -> m.CardDiscovery | None:
    return s.scalars(select(m.CardDiscovery).where(
        m.CardDiscovery.card_id == card_id, m.CardDiscovery.status == "ok")
        .order_by(m.CardDiscovery.id.desc()).limit(1)).first()


def _ago(stamp: str, now: datetime) -> timedelta:
    return now - datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def due_reason(s: Session, card_id: str, settings: Settings, *, force: bool = False,
               now: datetime | None = None) -> str | None:
    """Why this card needs discovery now, or None (§7.1)."""
    if force:
        return "requested"
    now = now or datetime.now(UTC)
    docs = s.scalars(select(m.CardDocument).where(m.CardDocument.card_id == card_id)).all()
    last = last_discovery(s, card_id)
    if last is None:
        return "first discovery" if not any(d.status == "active" for d in docs) else None
    since = _ago(last.created_at, now)
    if since >= timedelta(days=settings.cards.rediscover_days):
        return "rediscover_days passed"
    if since < timedelta(days=RETRY_DAYS):
        return None
    if not any(d.status == "active" for d in docs):
        return "no active documents"
    active_types = {d.doc_type for d in docs if d.status == "active"}
    for d in docs:
        if d.status in PROBLEM_STATUSES and d.doc_type not in active_types:
            return f"{d.doc_type} is {d.status}"
    return None


@dataclass
class DiscoverResult:
    added: list[int] = field(default_factory=list)  # new document ids
    off_list: list[str] = field(default_factory=list)  # hosts
    not_found: list[str] = field(default_factory=list)
    run_dir: str | None = None


def discover(s: Session, paths: DataPaths, settings: Settings, card: Card, domains: list[str],
             reason: str) -> DiscoverResult:
    """One card_discover call, then code-side filtering. Raises cc.UsageLimitError and
    cc.AIError after recording the attempt."""
    cs = settings.cards
    task = cc.load_task("card_discover")
    known = s.scalars(select(m.CardDocument).where(m.CardDocument.card_id == card.id)).all()
    payload = {
        "card": {"id": card.id, "product": card.product, "issuer": card.issuer,
                 "network": card.network},
        "allowed_domains": domains, "wanted": WANTED,
        "known_documents": [{"url": d.url, "doc_type": d.doc_type, "status": d.status}
                            for d in known if d.origin != "manual_file"],
        "today": time.strftime("%Y-%m-%d"),
    }
    inv = cc.research_invocation(task, cs.research_model, domains,
                                 max_turns=cs.discover_max_turns,
                                 max_searches=cs.discover_max_searches)
    run = new_run_dir(paths.ai_runs, "card_discover")
    rec = m.CardDiscovery(card_id=card.id, reason=reason, status="failed", run_dir=str(run))
    s.add(rec)
    try:
        res = cc.run_task(task, payload, run, invocation=inv, timeout=cs.timeout_seconds)
    except cc.UsageLimitError:
        rec.status = "limited"
        s.add(m.AIRun(task=task.name, run_dir=str(run), n_items=1, status="limited"))
        raise
    except cc.AIError as e:
        rec.error = str(e)[:500]
        s.add(m.AIRun(task=task.name, run_dir=str(run), n_items=1, status="failed",
                      error=str(e)[:500]))
        raise
    s.add(m.AIRun(task=task.name, run_dir=str(run), n_items=1, status="ok",
                  duration_ms=res.duration_ms))

    out = DiscoverResult(run_dir=str(run), not_found=res.output["not_found"])
    by_url = {d.url: d for d in known}
    seen: set[str] = set()
    for item in res.output["documents"]:
        url = normalize_url(item["url"])
        if url is None or url in seen:
            continue
        seen.add(url)
        host = urlsplit(url).hostname or ""
        allowed = host_allowed(host, domains)
        doc = by_url.get(url)
        if doc is not None:
            if doc.status == "needs_domain" and allowed:
                doc.status, doc.note = "active", None
            continue
        doc = m.CardDocument(
            card_id=card.id, url=url, doc_type=item["doc_type"], title=item["title"][:200],
            origin="discovered", status="active" if allowed else "needs_domain",
            note=None if allowed else f"host {host} is not on this card's allowlist",
        )
        s.add(doc)
        s.flush()
        by_url[url] = doc
        if allowed:
            out.added.append(doc.id)
        else:
            out.off_list.append(host)
    rec.status = "ok"
    rec.n_returned = len(res.output["documents"])
    rec.n_added, rec.n_off_list = len(out.added), len(out.off_list)
    rec.not_found_json = json.dumps(out.not_found)
    rec.notes = res.output.get("notes", "")[:1500]
    return out
