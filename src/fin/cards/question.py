# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ask about a card's terms in plain words (docs/SPEC-cards.md Phase C5, `card_question`).

Code picks the card's stored public documents (the pages that best match the question when
they don't all fit), `extract_model` answers with no tools, and code checks every quote against
the documents and every number in the answer against the checked quotes. Answers are cached on
the question and the exact documents, so asking again makes no call.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from fin.ai import claude_cli as cc
from fin.ai.runs import new_run_dir
from fin.cards.extract import Source, sources, split_pages
from fin.cards.verify import DocText, check_evidence, normalize, numbers_in
from fin.config import Card, DataPaths, Settings
from fin.db import models as m

TASK = "card_question"
MAX_QUESTION = 500
MAX_CHARS = 80_000
STOP = {"the", "and", "for", "with", "does", "what", "when", "how", "can", "this", "that",
        "card", "cards", "are", "there", "any", "have", "has", "will", "would", "should", "about",
        "from", "into", "your", "you", "much", "many", "get", "use", "using", "used", "my", "is",
        "do", "if", "on", "in", "of", "to", "a", "an", "or", "it", "i", "me", "be", "at", "by"}
# Plain words a question uses -> words the documents use.
SYNONYMS = {
    "insurance": ("coverage", "insured", "protection"), "covered": ("coverage", "insured"),
    "rental": ("rental", "collision", "vehicle", "auto"), "car": ("rental", "vehicle", "auto"),
    "abroad": ("foreign", "international", "outside"), "overseas": ("foreign", "international"),
    "fee": ("fee", "fees", "charge"), "fees": ("fee", "charge"), "lounge": ("lounge", "priority"),
    "phone": ("cellular", "wireless", "phone"), "warranty": ("warranty", "manufacturer"),
    "points": ("points", "rewards", "earn"), "cashback": ("cash", "back", "rewards"),
    "flight": ("airline", "flights", "airfare", "travel"), "hotel": ("hotel", "lodging"),
    "delay": ("delay", "delayed"), "lost": ("lost", "loss", "baggage"),
    "luggage": ("baggage", "luggage"), "groceries": ("supermarkets", "grocery"),
}


class QuestionError(ValueError):
    """A question the app won't send (empty, too long, or no documents for the card)."""


def card_sources(s: Session, card_id: str) -> list[Source]:
    """The card's terms and benefits documents, once each, refs D1.. in a stable order."""
    seen, out = set(), []
    for task in ("card_terms", "card_benefits"):
        for x in sources(s, card_id, task):
            if x.doc.id in seen:
                continue
            seen.add(x.doc.id)
            out.append(Source(f"D{len(out) + 1}", x.doc, x.version, x.text))
    return out


def terms(question: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9$%]+", question.lower())
             if w not in STOP and len(w) >= 3]
    out = list(words)
    for w in words:
        out += SYNONYMS.get(w, ())
    return out


def pick(srcs: list[Source], question: str, max_chars: int = MAX_CHARS) -> dict[str, str]:
    """Each document's text, or, over the budget, the pages that best match the question."""
    if sum(len(x.text) for x in srcs) <= max_chars:
        return {x.ref: x.text for x in srcs}
    keys = terms(question)
    scored = []
    for order, x in enumerate(srcs):
        for n, t in split_pages(x.text):
            low = t.lower()
            score = sum(low.count(k) for k in keys)
            if score:
                scored.append((-score, order, n, x.ref, t))
    chosen: dict[str, list[tuple[int, str]]] = {x.ref: [] for x in srcs}
    used = 0
    for _, _, n, ref, t in sorted(scored):
        if used + len(t) <= max_chars:
            chosen[ref].append((n, t))
            used += len(t)
    return {ref: "\n".join(t for _, t in sorted(ps)) for ref, ps in chosen.items() if ps}


def _key(task: cc.Task, model: str, question: str, srcs: list[Source]) -> str:
    blob = json.dumps({"q": normalize(question), "v": task.prompt_version, "model": model,
                       "docs": [[x.ref, x.version.text_sha256] for x in srcs]})
    return hashlib.sha256(blob.encode()).hexdigest()


def check_answer(output: dict[str, Any], srcs: list[Source]) -> dict[str, Any]:
    """Quotes against the full documents; the answer's numbers against the checked quotes."""
    docs = {x.ref: DocText.parse(x.text) for x in srcs}
    by_ref = {x.ref: x for x in srcs}
    cites, found_numbers, warnings = [], set(), []
    for c in output.get("citations", []):
        e = dict(c)
        check_evidence([e], docs)
        src = by_ref.get(c["doc"])
        cites.append({"quote": " ".join(c["quote"].split()), "page": e.get("page"),
                      "ok": bool(e.get("_ok")),
                      "doc_title": (src.doc.title or src.doc.doc_type) if src else c["doc"],
                      "doc_version_id": src.version.id if src else None})
        if e.get("_ok"):
            found_numbers |= numbers_in(c["quote"])
        else:
            warnings.append(f"quote not found in the documents: {c['quote'][:80]!r}")
    unsupported = sorted(v for v in numbers_in(output["answer"])
                         if not any(abs(v - f) < 1e-6 for f in found_numbers)
                         and not 1900 <= v <= 2100)  # years in "as of 2026" aren't terms
    if unsupported:
        warnings.append("numbers not in a checked quote: "
                        + ", ".join(f"{v:g}" for v in unsupported))
    ok = [c for c in cites if c["ok"]]
    if not ok:
        status = "unverified"
    elif unsupported or len(ok) < len(cites):
        status = "partial"
    else:
        status = "verified"
    return {"answer": output["answer"], "found": output["found"], "status": status,
            "citations": cites, "warnings": warnings}


def ask(s: Session, paths: DataPaths, settings: Settings, card: Card, question: str, *,
        runner: Any = None, lock_wait: float | None = None) -> dict[str, Any]:
    """One question about one card. Raises QuestionError, cc.UsageLimitError, cc.AIError."""
    question = " ".join(question.split())
    if not question:
        raise QuestionError("Ask a question.")
    if len(question) > MAX_QUESTION:
        raise QuestionError(f"Keep the question under {MAX_QUESTION} characters.")
    srcs = card_sources(s, card.id)
    if not srcs:
        raise QuestionError(f"No documents for {card.product} yet. Refresh its terms first.")
    task = cc.load_task(TASK)
    model = settings.cards.extract_model
    key = _key(task, model, question, srcs)
    cached = s.get(m.AICache, (TASK, key, task.prompt_version))
    if cached is not None:
        return {**check_answer(json.loads(cached.output_json), srcs), "cached": True,
                "question": question, "card_id": card.id}
    texts = pick(srcs, question)
    payload = {"card": {"product": card.product, "issuer": card.issuer}, "question": question,
               "documents": [{"ref": x.ref, "doc_type": x.doc.doc_type,
                              "title": x.doc.title or "", "text": texts[x.ref]}
                             for x in srcs if x.ref in texts]}
    run = new_run_dir(paths.ai_runs, TASK)
    call = runner or (lambda t, p, d: cc.run_task(
        t, p, d, timeout=settings.cards.timeout_seconds, lock_wait=lock_wait,
        invocation=cc.extract_invocation(t, model)))
    try:
        res = call(task, payload, run)
    except cc.UsageLimitError:
        s.add(m.AIRun(task=TASK, run_dir=str(run), n_items=1, status="limited"))
        s.commit()
        raise
    except cc.AIError as e:
        s.add(m.AIRun(task=TASK, run_dir=str(run), n_items=1, status="failed",
                      error=str(e)[:500]))
        s.commit()
        raise
    s.add(m.AIRun(task=TASK, run_dir=str(run), n_items=1, status="ok",
                  duration_ms=res.duration_ms))
    s.merge(m.AICache(task=TASK, key=key, prompt_version=task.prompt_version,
                      output_json=json.dumps(res.output), model=model))
    s.commit()
    return {**check_answer(res.output, srcs), "cached": False, "question": question,
            "card_id": card.id}
