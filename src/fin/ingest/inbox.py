"""`fin import` mechanics (SPEC §9.6).

A browsing-run folder `inbox/<site>/<date>/` is imported from its manifest. Each listed file
goes to the parser registered for (site, kind). After a successful parse the file moves to
`raw/<site>/<date>/` (immutable evidence) and an `imports` row records its sha256, so
importing the same file again is a no-op. Session logs and guard.log move to
`raw/sync-logs/<site>/<date>/`. Files without a parser yet stay in the inbox.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.config import DataPaths
from fin.db import models as m
from fin.ingest.manifest import ManifestCheck, is_session_log


@dataclass
class ParseOutcome:
    row_count: int = 0
    period_start: str | None = None
    period_end: str | None = None
    # Document disagrees with the manifest, or a partial parse: each becomes a parse_failure.
    problems: list[str] = field(default_factory=list)


# parser(session, path, manifest_entry, import_row) -> ParseOutcome
Parser = Callable[[Session, Path, dict[str, Any], m.Import], ParseOutcome]
KIND_TO_IMPORT_KIND = {
    "transactions": "transactions", "statement": "statement", "order_invoice": "orders",
    "order_details": "orders", "payment_transactions": "orders", "receipt": "receipts",
    "receipt_transcript": "receipts",
}
PARSERS: dict[tuple[str, str], Parser] = {}


def register(site: str, kind: str) -> Callable[[Parser], Parser]:
    def deco(fn: Parser) -> Parser:
        PARSERS[(site, kind)] = fn
        return fn
    return deco


def safe_error(e: Exception) -> str:
    """Error text for terminal and logs: never SQL parameters or row data (SPEC §7)."""
    orig = getattr(e, "orig", None)  # SQLAlchemy wraps the driver error; its str() has params
    base = orig if orig is not None else e
    msg = str(base).split("[SQL:")[0].split("\n")[0].strip()
    return f"{type(base).__name__}: {msg[:200]}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class FileResult:
    file: str
    status: str  # imported | duplicate | already_imported | no_parser | failed | missing
    detail: str = ""


@dataclass
class RunImport:
    site: str
    run_date: str
    results: list[FileResult] = field(default_factory=list)
    logs_moved: list[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(r.status == status for r in self.results)

    def summary(self) -> str:
        parts = [f"{self.count(s)} {s.replace('_', ' ')}" for s in
                 ("imported", "duplicate", "already_imported", "no_parser", "failed", "missing")
                 if self.count(s)]
        return ", ".join(parts) or "nothing to import"


def _private_copy_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)


def _move(src: Path, dest_dir: Path) -> Path:
    _private_copy_dir(dest_dir)
    dest = dest_dir / src.name
    if dest.exists():
        raise FileExistsError(f"{dest} already exists; refusing to overwrite raw evidence")
    shutil.move(str(src), dest)
    if dest.is_file():
        dest.chmod(0o600)
    return dest


def move_session_logs(paths: DataPaths, run_dir: Path, site: str, run_date: str) -> list[str]:
    dest = paths.raw / "sync-logs" / site / run_date
    moved = []
    for p in sorted(run_dir.iterdir()):
        log_file = p.name == "guard.log" or (is_session_log(p.name) and p.suffix != ".json")
        is_log = (p.is_dir() and p.name.startswith("session")) or (p.is_file() and log_file)
        if is_log:
            target = dest / p.name
            if target.exists() and p.name == "guard.log":
                with target.open("a") as out:
                    out.write(p.read_text())
                p.unlink()
            elif not target.exists():
                _move(p, dest)
            moved.append(p.name)
    return moved


def import_run(
    session: Session,
    paths: DataPaths,
    run_dir: Path,
    check: ManifestCheck,
    sync_run_id: int | None = None,
    parsers: dict[tuple[str, str], Parser] | None = None,
) -> RunImport:
    """Import one validated browsing-run folder. Idempotent."""
    if not check.ok or check.manifest is None:
        raise ValueError("refusing to import a run folder whose manifest didn't validate")
    parsers = PARSERS if parsers is None else parsers
    site, run_date = check.manifest["site"], check.manifest["run_date"]
    raw_dir = paths.raw / site / run_date
    out = RunImport(site, run_date)

    for entry in check.manifest["files"]:
        name = entry["file"]
        src = run_dir / name
        if not src.exists():
            done = (raw_dir / name).exists() and session.scalar(
                select(m.Import.id).where(m.Import.file_path == str(raw_dir / name))
            )
            out.results.append(FileResult(name, "already_imported" if done else "missing"))
            continue
        digest = sha256_file(src)
        if session.scalar(select(m.Import.id).where(m.Import.sha256 == digest)):
            out.results.append(FileResult(name, "duplicate", "same content imported before"))
            continue
        parser = parsers.get((site, entry["kind"]))
        if parser is None:
            out.results.append(FileResult(name, "no_parser", f"{site}/{entry['kind']}"))
            continue

        imp = m.Import(
            sync_run_id=sync_run_id, source=site, kind=KIND_TO_IMPORT_KIND[entry["kind"]],
            file_kind=entry["kind"], card_id=entry["card_id"], file_path=str(raw_dir / name),
            sha256=digest,
            period_start=entry["period_start"], period_end=entry["period_end"], status="ok",
        )
        session.add(imp)
        session.flush()
        try:
            with session.begin_nested():
                outcome = parser(session, src, entry, imp)
        except Exception as e:  # parser bug or unreadable file: keep the file, record why
            session.rollback()  # drops this file's imports row; earlier files are committed
            out.results.append(FileResult(name, "failed", safe_error(e)))
            continue
        imp.row_count = outcome.row_count
        imp.period_start = outcome.period_start or imp.period_start
        imp.period_end = outcome.period_end or imp.period_end
        if outcome.problems:
            imp.status = "partial"
            imp.error = "; ".join(outcome.problems)[:2000]
            for problem in outcome.problems:
                session.add(m.ReviewItem(
                    kind="parse_failure", ref_table="imports", ref_id=str(imp.id),
                    details_json=json.dumps({"file": name, "problem": problem}),
                ))
        _move(src, raw_dir)
        evidence = src.with_suffix(".png")  # a transcript's screenshot travels with it
        if src.suffix == ".json" and evidence.exists():
            _move(evidence, raw_dir)
        session.commit()
        out.results.append(FileResult(name, "imported", f"{outcome.row_count} rows"))

    # Keep the manifest and report with the evidence once nothing is left to import.
    remaining = [r for r in out.results if r.status in ("no_parser", "failed")]
    if not remaining:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for name in ("manifest.json", "run_report.json", "playbook_proposed.md"):
            p = run_dir / name
            if not p.exists():
                continue
            if (raw_dir / name).exists():  # a same-day rerun: keep both
                n, target = 1, f"{p.stem}.{stamp}{p.suffix}"
                while (raw_dir / target).exists():
                    n += 1
                    target = f"{p.stem}.{stamp}-{n}{p.suffix}"
                p = p.rename(run_dir / target)
            _move(p, raw_dir)
    out.logs_moved = move_session_logs(paths, run_dir, site, run_date)
    return out


def record_gaps(session: Session, report: dict[str, Any], sync_run_id: int) -> int:
    n = 0
    for gap in report.get("gaps", []):
        session.add(m.ReviewItem(
            kind="sync_gap", ref_table="sync_runs", ref_id=str(sync_run_id),
            details_json=json.dumps(gap),
        ))
        n += 1
    session.commit()
    return n


# ------------------------------------------------------------ manual files


def detect(path: Path) -> tuple[str, str] | None:
    """Content-based type detection for manual drops (§9.6). Returns (site, kind)."""
    from fin.ingest.bank_csv import is_amex_csv, is_chase_csv, is_citi_csv

    suffix = path.suffix.lower()
    if suffix == ".csv":
        text = path.read_text(errors="replace")
        if is_chase_csv(text):
            return ("chase", "transactions")
        if is_citi_csv(text):
            return ("citi", "transactions")
        return ("amex", "transactions") if is_amex_csv(text) else None
    if suffix in (".pdf", ".txt"):
        if suffix == ".pdf":
            from fin.ingest.statements import pdf_text
            text = pdf_text(path)
        else:
            text = path.read_text(errors="replace")
        if "Order placed" in text and "Order #" in text:
            return ("amazon", "order_invoice")
        if "Opening/Closing Date" in text and "chase.com" in text.lower():
            return ("chase", "statement")
        if "Billing Period:" in text and "citi" in text.lower():
            return ("citi", "statement")
        if "Account Ending" in text and "americanexpress.com" in text.lower():
            return ("amex", "statement")
    return None


def statement_card(path: Path, site: str, cards) -> str | None:
    """Which configured card a statement belongs to, from its product markers."""
    from fin.ingest.statements import (
        parse_amex_statement,
        parse_chase_statement,
        parse_citi_statement,
        pdf_text,
    )

    text = path.read_text() if path.suffix == ".txt" else pdf_text(path)
    parse = {"citi": parse_citi_statement, "amex": parse_amex_statement}.get(
        site, parse_chase_statement)
    markers = parse(text).product_markers
    matches = [c.id for c in cards.cards_for_issuer(site)
               if any(mk in c.product.upper() for mk in markers)]
    return matches[0] if len(matches) == 1 else None


def import_file(
    session: Session, paths: DataPaths, path: Path, site: str, kind: str,
    card_id: str | None, parsers: dict[tuple[str, str], Parser] | None = None,
) -> FileResult:
    """Import one manually downloaded file through the same parsers; file goes to raw/manual/."""
    from datetime import date

    parsers = PARSERS if parsers is None else parsers
    digest = sha256_file(path)
    if session.scalar(select(m.Import.id).where(m.Import.sha256 == digest)):
        return FileResult(path.name, "duplicate", "same content imported before")
    parser = parsers.get((site, kind))
    if parser is None:
        return FileResult(path.name, "no_parser", f"{site}/{kind}")
    raw_dir = paths.raw / "manual" / date.today().isoformat()
    entry = {"file": path.name, "kind": kind, "card_id": card_id, "period_start": None,
             "period_end": None, "note": "manual import"}
    imp = m.Import(source=site, kind=KIND_TO_IMPORT_KIND[kind], file_kind=kind, card_id=card_id,
                   file_path=str(raw_dir / path.name), sha256=digest, status="ok")
    session.add(imp)
    session.flush()
    try:
        with session.begin_nested():
            outcome = parser(session, path, entry, imp)
    except Exception as e:
        session.rollback()
        return FileResult(path.name, "failed", safe_error(e))
    imp.row_count = outcome.row_count
    imp.period_start, imp.period_end = outcome.period_start, outcome.period_end
    if outcome.problems:
        imp.status, imp.error = "partial", "; ".join(outcome.problems)[:2000]
        for problem in outcome.problems:
            session.add(m.ReviewItem(kind="parse_failure", ref_table="imports",
                                     ref_id=str(imp.id),
                                     details_json=json.dumps({"file": path.name,
                                                              "problem": problem})))
    _private_copy_dir(raw_dir)
    shutil.copy2(path, raw_dir / path.name)
    (raw_dir / path.name).chmod(0o600)
    session.commit()
    return FileResult(path.name, "imported", f"{outcome.row_count} rows")


def inbox_run_dirs(paths: DataPaths) -> list[Path]:
    if not paths.inbox.exists():
        return []
    return sorted(p.parent for p in paths.inbox.glob("*/*/manifest.json")
                  if p.parent.parent.name != "selftest")
