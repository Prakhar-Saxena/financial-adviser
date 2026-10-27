# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Turn stored documents into terms with quotes (docs/SPEC-cards.md §7.3), then verify (§7.4).

Each task is cached on its exact input: `input_key` hashes the text hashes it reads, the prompt
version, the model and the page budget. A refresh where no text changed makes no call.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.ai import claude_cli as cc
from fin.ai.runs import new_run_dir
from fin.cards import review
from fin.cards.docs import BENEFITS_TYPES, TERMS_TYPES, latest_ok_version, read_text
from fin.cards.verify import MARKER, DocText, Verified, verify_benefits, verify_terms
from fin.config import Card, DataPaths, Settings, load_reward_categories
from fin.db import models as m
from fin.db.models import utcnow

TASK_TYPES = {"card_terms": TERMS_TYPES, "card_benefits": BENEFITS_TYPES}
KEYWORDS = {
    "card_terms": ("earn", "point", "mile", "cash back", "%", "annual fee", "foreign transaction",
                   "statement credit", "reward", "per dollar", "per $1", "bonus", "categor",
                   "purchases", "dining", "restaurant", "grocer", "supermarket", "travel", "gas",
                   "cap", "up to $"),
    "card_benefits": ("protection", "warranty", "insurance", "coverage", "rental", "trip",
                      "baggage", "lounge", "claim", "benefit", "roadside", "cell phone",
                      "collision", "accident", "delay"),
}
# A blocked or broken document's last good text still counts until a document of the same
# type replaces it; `needs_domain` and `retired` never do.
USABLE = ("active", "broken", "blocked", "needs_browser")


@dataclass
class Source:
    ref: str
    doc: m.CardDocument
    version: m.CardDocVersion
    text: str


def sources(s: Session, card_id: str, task: str) -> list[Source]:
    """The documents a task reads, in a stable order: by doc type, then document id."""
    types = TASK_TYPES[task]
    docs = s.scalars(select(m.CardDocument).where(
        m.CardDocument.card_id == card_id, m.CardDocument.status.in_(USABLE),
        m.CardDocument.doc_type.in_(types))).all()
    picked: list[tuple[m.CardDocument, m.CardDocVersion]] = []
    for d in docs:
        v = latest_ok_version(s, d.id)
        if v is not None:
            picked.append((d, v))
    healthy = {d.doc_type for d, _ in picked if d.status == "active"}
    picked = [(d, v) for d, v in picked if d.status == "active" or d.doc_type not in healthy]
    picked.sort(key=lambda dv: (types.index(dv[0].doc_type), dv[0].id))
    return [Source(f"D{i}", d, v, read_text(v)) for i, (d, v) in enumerate(picked, 1)]


def split_pages(text: str) -> list[tuple[int, str]]:
    """[(page number, text including its marker)]."""
    parts = MARKER.split(text)
    out: list[tuple[int, str]] = []
    if parts[0].strip():
        out.append((1, parts[0]))
    for i in range(1, len(parts), 2):
        kind = "page" if f"[[page {parts[i]}]]" in text else "section"
        out.append((int(parts[i]), f"[[{kind} {parts[i]}]]{parts[i + 1]}"))
    return out


def budget(srcs: list[Source], task: str, max_chars: int) -> tuple[dict[str, str], dict]:
    """Texts to send per ref, and the pages sent ({} when everything fits).

    Over budget: each document's first page, then the pages with the most task keywords, while
    they fit. A page is sent whole or not at all."""
    total = sum(len(x.text) for x in srcs)
    if total <= max_chars:
        return {x.ref: x.text for x in srcs}, {}
    keys = KEYWORDS[task]
    pages = {x.ref: split_pages(x.text) for x in srcs}
    chosen: dict[str, set[int]] = {x.ref: set() for x in srcs}
    used = 0
    for ref, ps in pages.items():
        if ps and used + len(ps[0][1]) <= max_chars:
            chosen[ref].add(ps[0][0])
            used += len(ps[0][1])
    scored = []
    for order, (ref, ps) in enumerate(pages.items()):
        for n, t in ps[1:]:
            low = t.lower()
            score = sum(low.count(k) for k in keys)
            if score:
                scored.append((-score, order, n, ref, len(t)))
    for _, _, n, ref, size in sorted(scored):
        if used + size <= max_chars:
            chosen[ref].add(n)
            used += size
    texts = {ref: "\n".join(t for n, t in ps if n in chosen[ref]) for ref, ps in pages.items()}
    return texts, {ref: sorted(ns) for ref, ns in chosen.items()}


def input_key(task: cc.Task, model: str, srcs: list[Source], max_chars: int) -> str:
    blob = json.dumps({"task": task.name, "prompt_version": task.prompt_version, "model": model,
                       "max_chars": max_chars,
                       "docs": [[x.ref, x.doc.doc_type, x.version.text_sha256] for x in srcs]})
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class ExtractOutcome:
    task: str
    status: str  # skipped | cached | ok | partial
    extraction_id: int | None = None
    called: bool = False
    problems: list[dict] = field(default_factory=list)


def extract(s: Session, paths: DataPaths, settings: Settings, card: Card, task_name: str, *,
            force: bool = False) -> ExtractOutcome:
    """Run one task for one card if its input changed. Raises cc.UsageLimitError (nothing is
    stored) and cc.AIError (a `failed` row is kept, and retried next time)."""
    cs = settings.cards
    srcs = sources(s, card.id, task_name)
    if not srcs:
        return ExtractOutcome(task_name, "skipped")
    task = cc.load_task(task_name)
    key = input_key(task, cs.extract_model, srcs, cs.max_extract_chars)
    row = s.scalars(select(m.CardExtraction).where(
        m.CardExtraction.card_id == card.id, m.CardExtraction.task == task_name,
        m.CardExtraction.input_key == key)).first()
    if row is not None and row.status in ("ok", "partial") and not force:
        # No call: but check the stored output again, so verifier fixes apply without AI.
        problems = _verify_and_store(s, row, card, task_name, json.loads(row.output_json), srcs)
        return ExtractOutcome(task_name, "cached", row.id, problems=problems)

    texts, pages_sent = budget(srcs, task_name, cs.max_extract_chars)
    payload: dict = {
        "card": {"product": card.product, "issuer": card.issuer, "network": card.network},
        "documents": [{"ref": x.ref, "doc_type": x.doc.doc_type, "title": x.doc.title or "",
                       "url": x.doc.url, "text": texts[x.ref]} for x in srcs],
        "today": time.strftime("%Y-%m-%d"),
    }
    if task_name == "card_terms":
        payload["reward_categories"] = [{"id": c.id, "description": c.description}
                                        for c in load_reward_categories().categories]
    run = new_run_dir(paths.ai_runs, task_name)
    if row is None:
        row = m.CardExtraction(card_id=card.id, task=task_name, input_key=key)
        s.add(row)
    row.prompt_version, row.model, row.run_dir = task.prompt_version, cs.extract_model, str(run)
    row.pages_sent_json = json.dumps(pages_sent)
    row.created_at = utcnow()
    try:
        res = cc.run_task(task, payload, run, timeout=cs.timeout_seconds,
                          invocation=cc.extract_invocation(task, cs.extract_model))
    except cc.UsageLimitError:
        if row.id is None:
            s.expunge(row)  # nothing to keep: the same input is tried again next run
        s.add(m.AIRun(task=task_name, run_dir=str(run), n_items=len(srcs), status="limited"))
        raise
    except cc.AIError as e:
        row.status, row.output_json = "failed", json.dumps({"error": str(e)[:500]})
        row.n_items = row.n_verified = row.n_rejected = 0
        s.add(m.AIRun(task=task_name, run_dir=str(run), n_items=len(srcs), status="failed",
                      error=str(e)[:500]))
        raise

    s.add(m.AIRun(task=task_name, run_dir=str(run), n_items=len(srcs), status="ok",
                  duration_ms=res.duration_ms))
    problems = _verify_and_store(s, row, card, task_name, res.output, srcs)
    return ExtractOutcome(task_name, row.status, row.id, called=True, problems=problems)


def _strip(node):
    """The model's output without our annotations (`_verify`, `_ok`, `_refs`, ...)."""
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if not k.startswith("_")}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _verify_and_store(s: Session, row: m.CardExtraction, card: Card, task_name: str,
                      output: dict, srcs: list[Source]) -> list[dict]:
    docs = {x.ref: DocText.parse(x.text) for x in srcs}
    check = verify_terms if task_name == "card_terms" else verify_benefits
    v: Verified = check(_strip(output), docs)
    out = {**v.output, "_refs": {x.ref: x.version.id for x in srcs}, "_problems": v.problems}
    row.status = "partial" if v.problems else "ok"
    row.output_json = json.dumps(out, ensure_ascii=False)
    row.n_items, row.n_verified, row.n_rejected = v.n_items, v.n_verified, v.n_rejected
    s.flush()
    if v.problems:
        review.open_item(s, "card_terms_unverified", card.id, task_name,
                         {"task": task_name, "extraction_id": row.id, "problems": v.problems})
    else:
        review.close_items(s, "card_terms_unverified", card.id, task_name)
    return v.problems


def latest_good(s: Session, card_id: str, task: str) -> m.CardExtraction | None:
    """The newest usable extraction for a card and task."""
    return s.scalars(select(m.CardExtraction).where(
        m.CardExtraction.card_id == card_id, m.CardExtraction.task == task,
        m.CardExtraction.status.in_(("ok", "partial")))
        .order_by(m.CardExtraction.created_at.desc(), m.CardExtraction.id.desc()).limit(1)).first()
