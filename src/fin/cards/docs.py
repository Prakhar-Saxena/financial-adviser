# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The document store in card-docs/ (docs/SPEC-cards.md §5, §7.2, §7.6).

Downloaded and manual documents both end up here: the file under files/<sha[:2]>/<sha>.<ext>,
its text with [[page N]] or [[section N]] markers under text/<text_sha256>.txt, and a
`card_doc_versions` row. Versions are immutable; a new one is stored only when the text changed.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.cards.verify import normalize
from fin.config import DataPaths
from fin.db import models as m
from fin.db.models import utcnow

MIN_TEXT_CHARS = 1500
# Documents about the rewards program or the account in general may name only the issuer.
PROGRAM_LEVEL = {"program_terms", "rewards_terms", "cardmember_agreement", "benefits_guide",
                 "benefit_terms"}
TERMS_TYPES = ("product_page", "rewards_terms", "program_terms", "pricing_terms",
               "cardmember_agreement", "benefit_terms")
BENEFITS_TYPES = ("benefits_guide", "benefit_terms", "product_page")
DROP_TAGS = ["script", "style", "nav", "header", "footer", "form", "noscript", "svg", "iframe",
             "button", "template"]
BANNER = re.compile(r"cookie|consent|onetrust|gdpr", re.I)
BLOCK_TAGS = ["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
              "dt", "dd", "blockquote", "table", "ul", "ol", "main"]


class UnreadableError(ValueError):
    pass


def sniff_kind(data: bytes, content_type: str) -> str | None:
    """"pdf", "html" or None (anything else is refused)."""
    ct = content_type.split(";")[0].strip().lower()
    if data[:5] == b"%PDF-" or ct == "application/pdf":
        return "pdf" if data[:5] == b"%PDF-" else None
    if ct in ("text/html", "application/xhtml+xml") or data.lstrip()[:15].lower().startswith(
            (b"<!doctype html", b"<html")):
        return "html"
    return None


def pdf_text(data: bytes) -> tuple[str, int]:
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
    except Exception as e:  # pdfminer raises many types on a bad file
        raise UnreadableError(f"unreadable PDF: {type(e).__name__}") from None
    return "\n".join(f"[[page {i}]]\n{t}" for i, t in enumerate(pages, 1)), len(pages)


def html_text(data: bytes) -> tuple[str, int]:
    """Visible text without scripts, navigation, forms and cookie banners. Each h1-h3 starts a
    new [[section N]]; table rows become one line with cells joined by " | "."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "lxml")
    for t in soup(DROP_TAGS):
        t.decompose()
    for t in soup.find_all(lambda el: any(BANNER.search(v) for v in
                                          [el.get("id") or "", *(el.get("class") or [])])):
        t.decompose()
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        tr.replace_with("\n" + " | ".join(c for c in cells if c) + "\n")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    n = 1
    for h in soup.find_all(["h1", "h2", "h3"]):
        n += 1
        h.insert_before(f"\n[[section {n}]]\n")
    for el in soup.find_all(BLOCK_TAGS):
        el.append("\n")
    body = soup.body or soup
    lines = [" ".join(line.split()) for line in body.get_text("").splitlines()]
    text = "\n".join(line for line in lines if line)
    if not text.startswith("[[section"):
        text = "[[section 1]]\n" + text
    return text, n


def text_hash(text: str) -> str:
    """Hash of the text with whitespace normalized, so layout noise isn't a change."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def body_chars(text: str) -> int:
    return len(" ".join(re.sub(r"\[\[(?:page|section) \d+\]\]", " ", text).split()))


def mentions(text: str, names: list[str]) -> bool:
    t = normalize(text)
    return any(normalize(n) in t for n in names if n.strip())


def latest_ok_version(s: Session, document_id: int) -> m.CardDocVersion | None:
    return s.scalars(
        select(m.CardDocVersion)
        .where(m.CardDocVersion.document_id == document_id, m.CardDocVersion.status == "ok")
        .order_by(m.CardDocVersion.id.desc()).limit(1)
    ).first()


@dataclass
class StoreOutcome:
    kind: str  # changed | unchanged | rejected
    version_id: int | None = None
    reason: str | None = None


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        path.write_bytes(data)
        path.chmod(0o600)


def store(s: Session, paths: DataPaths, doc: m.CardDocument, data: bytes, content_type: str,
          names: list[str], *, http_status: int | None = None) -> StoreOutcome:
    """Store one fetched or manual document, unless its text is unchanged or it fails the
    checks (§7.2). `names` are the product name and aliases, plus the issuer's name for
    program-level documents."""
    now = utcnow()
    kind = sniff_kind(data, content_type)
    if kind is None:
        return _reject(s, doc, "unsupported_type", content_type, len(data), http_status)
    try:
        text, n_pages = pdf_text(data) if kind == "pdf" else html_text(data)
    except UnreadableError as e:
        return _reject(s, doc, "unreadable", content_type, len(data), http_status, str(e))
    t_sha = text_hash(text)
    prev = latest_ok_version(s, doc.id)
    if prev is not None and prev.text_sha256 == t_sha:
        doc.last_checked_at = now
        return StoreOutcome("unchanged", prev.id)
    named = mentions(text, names)
    if body_chars(text) < MIN_TEXT_CHARS or not named:
        why = "too little text" if named else "doesn't name the card"
        return _reject(s, doc, "thin_or_wrong_page", content_type, len(data), http_status, why,
                       mentions_product=named, n_pages=n_pages)

    sha = hashlib.sha256(data).hexdigest()
    file_path = paths.card_docs / "files" / sha[:2] / f"{sha}.{kind}"
    text_path = paths.card_docs / "text" / f"{t_sha}.txt"
    _write_once(file_path, data)
    _write_once(text_path, text.encode())
    v = m.CardDocVersion(
        document_id=doc.id, fetched_at=now, http_status=http_status,
        content_type="application/pdf" if kind == "pdf" else "text/html", bytes=len(data),
        sha256=sha, text_sha256=t_sha, file_path=str(file_path), text_path=str(text_path),
        n_pages=n_pages, mentions_product=True, status="ok",
    )
    s.add(v)
    s.flush()
    doc.last_checked_at = doc.last_changed_at = now
    doc.failures = 0
    return StoreOutcome("changed", v.id)


def _reject(s: Session, doc: m.CardDocument, reason: str, content_type: str, size: int,
            http_status: int | None, detail: str = "", *, mentions_product: bool | None = None,
            n_pages: int | None = None) -> StoreOutcome:
    """A rejected version keeps the row (for the record) but no file and no text."""
    s.add(m.CardDocVersion(
        document_id=doc.id, http_status=http_status, content_type=content_type[:100],
        bytes=size, n_pages=n_pages, mentions_product=mentions_product, status="rejected",
        reject_reason=f"{reason}: {detail}" if detail else reason,
    ))
    return StoreOutcome("rejected", reason=reason)


def read_text(v: m.CardDocVersion) -> str:
    return Path(v.text_path).read_text() if v.text_path else ""
