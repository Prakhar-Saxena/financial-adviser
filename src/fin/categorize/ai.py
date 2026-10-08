"""AI categorization for what rules don't cover (SPEC §14). Cached forever per prompt version.

The classifier only sees cleaned descriptors, amounts and dates (merchants) or item titles
(items): never names, card numbers, addresses or order numbers (§7).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.ai.claude_cli import AIError, Task, UsageLimitError, check_keys, load_task, run_task
from fin.ai.runs import new_run_dir
from fin.config import DataPaths, Settings
from fin.db import models as m
from fin.review import ReviewSet

Runner = Callable[[Task, dict[str, Any], Any], Any]


@dataclass
class AIResult:
    cached: int = 0
    classified: int = 0
    calls: int = 0
    failed_keys: list[str] = field(default_factory=list)
    paused: str | None = None  # set when the subscription limit was hit


def item_key(item: m.OrderItem, source: str) -> str:
    if item.sku:
        return f"{source}:{item.sku}"
    return f"{source}:title:" + re.sub(r"[^a-z0-9]+", " ", item.title_raw.lower()).strip()[:120]


def _cache_get(s: Session, task: Task, keys: list[str]) -> dict[str, dict]:
    rows = s.scalars(select(m.AICache).where(
        m.AICache.task == task.name, m.AICache.prompt_version == task.prompt_version,
        m.AICache.key.in_(keys))).all()
    return {r.key: json.loads(r.output_json) for r in rows}


def classify_keys(
    s: Session, paths: DataPaths, settings: Settings, task_name: str,
    records: dict[str, dict[str, Any]], *, use_ai: bool, result: AIResult,
    runner: Runner | None = None,
) -> dict[str, dict]:
    """Return {key: output} from the cache, calling the classifier for the rest."""
    task = load_task(task_name)
    keys = sorted(records)
    out = _cache_get(s, task, keys)
    result.cached += len(out)
    todo = [k for k in keys if k not in out]
    if not todo or not use_ai or result.paused:
        return out
    runner = runner or (lambda t, payload, run_dir: run_task(
        t, payload, run_dir, model=settings.ai.classify_model,
        max_turns=settings.ai.max_turns, timeout=settings.ai.timeout_seconds))
    size = settings.ai.batch_size
    for i in range(0, len(todo), size):
        batch = todo[i:i + size]
        for attempt in (1, 2):  # missing keys are retried once (§14.4)
            run_dir = new_run_dir(paths.ai_runs, task.name)
            started = time.monotonic()
            status, error = "ok", None
            try:
                res = runner(task, {"records": [records[k] for k in batch]}, run_dir)
            except UsageLimitError as e:
                result.paused = str(e)
                _log_run(s, task, run_dir, len(batch), "limited", started, str(e))
                return out
            except AIError as e:
                status, error = "failed", str(e)
                _log_run(s, task, run_dir, len(batch), status, started, error)
                result.calls += 1
                if attempt == 2:
                    result.failed_keys += batch
                continue
            result.calls += 1
            got = {r["key"]: r for r in res.output["results"] if r["key"] in batch}
            missing, _ = check_keys(batch, res.output["results"])
            for k, r in got.items():
                out[k] = r
                s.merge(m.AICache(task=task.name, key=k, prompt_version=task.prompt_version,
                                  output_json=json.dumps(r), model=settings.ai.classify_model))
                result.classified += 1
            _log_run(s, task, run_dir, len(batch), "partial" if missing else status, started,
                     error)
            s.commit()
            if not missing:
                break
            batch = missing
            if attempt == 2:
                result.failed_keys += missing
    return out


def _log_run(s, task, run_dir, n, status, started, error) -> None:
    s.add(m.AIRun(task=task.name, run_dir=str(run_dir), n_items=n, status=status,
                  duration_ms=int((time.monotonic() - started) * 1000), error=error))
    s.flush()


def categorize_transactions(
    s: Session, paths: DataPaths, settings: Settings, txns: list[m.Transaction],
    reviews: ReviewSet, *, use_ai: bool, runner: Runner | None = None,
) -> AIResult:
    from fin.categorize.rules import merchant_for

    result = AIResult()
    by_key: dict[str, list[m.Transaction]] = {}
    for t in txns:
        by_key.setdefault(t.description_clean, []).append(t)
    records = {
        k: {"key": k, "description": k, "amount": round(ts[0].amount_cents / 100, 2),
            "date": ts[0].txn_date or ts[0].post_date}
        for k, ts in by_key.items()
    }
    outputs = classify_keys(s, paths, settings, "merchant_categorize", records, use_ai=use_ai,
                            result=result, runner=runner)
    cache: dict[str, m.Merchant] = {}
    for k, ts in by_key.items():
        o = outputs.get(k)
        for t in ts:
            if o is None or o["category_id"] == "uncategorized":
                t.category_id, t.category_source = "uncategorized", None
                reviews.flag("uncategorized", "transactions", t.id,
                             reason="no rule or AI category yet")
                continue
            merchant = merchant_for(s, o["merchant_name"], None, o["category_id"], cache)
            t.merchant_id, t.category_id, t.category_source = merchant.id, o["category_id"], "ai"
            if o["confidence"] < settings.ai.low_confidence:
                reviews.flag("low_confidence", "transactions", t.id,
                             confidence=o["confidence"], category_id=o["category_id"])
    return result


def categorize_items(
    s: Session, paths: DataPaths, settings: Settings, reviews: ReviewSet, *, use_ai: bool,
    runner: Runner | None = None,
) -> AIResult:
    result = AIResult()
    rows = s.execute(select(m.OrderItem, m.Order.merchant).join(
        m.Order, m.Order.id == m.OrderItem.order_id)).all()
    by_key: dict[str, list[m.OrderItem]] = {}
    records: dict[str, dict] = {}
    for item, merchant in rows:
        if item.category_source == "user":
            continue
        k = item_key(item, merchant)
        by_key.setdefault(k, []).append(item)
        records[k] = {"key": k, "source": merchant, "title": item.title_raw[:300]}
    outputs = classify_keys(s, paths, settings, "item_categorize", records, use_ai=use_ai,
                            result=result, runner=runner)
    for k, items in by_key.items():
        o = outputs.get(k)
        for item in items:
            if o is None:
                item.category_id, item.category_source = None, None
                continue
            item.title_clean = o["clean_title"]
            item.category_id, item.category_source = o["category_id"], "ai"
            if o["confidence"] < settings.ai.low_confidence:
                reviews.flag("low_confidence", "order_items", item.id,
                             confidence=o["confidence"], category_id=o["category_id"])
    return result
