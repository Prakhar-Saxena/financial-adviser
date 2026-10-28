# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Official card pictures for the dashboard (docs/SPEC-cards.md §7.8).

The picture is chosen from the `<img>` tags of the card's own stored documents: the one whose alt
text or file name names the product, with the fewest other words (a cardmember agreements page
shows every card the issuer makes). `[cards.art]` in settings overrides the choice with a URL.
It is downloaded like a document (fetch.py: allowlist on every hop, robots.txt, no cookies,
honest user agent), checked to be a PNG, JPEG or WebP (never SVG, which can carry script) and
kept as card-docs/art/<card_id>.<ext> with a JSON note of where it came from. No AI involved.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from fin.cards.docs import latest_ok_version
from fin.cards.fetch import Fetcher, host_allowed
from fin.config import Card, CardsConfig, DataPaths, Settings
from fin.db import models as m
from fin.db.models import utcnow

MAX_BYTES = 2 << 20
TRIES = 3  # pictures downloaded at most, per card and run
RASTER = re.compile(r"\.(png|jpe?g|webp)$", re.I)
TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
# Words that say nothing about which card a picture shows.
FILLER = {"the", "card", "cards", "credit", "from", "by", "registered", "trademark", "american",
          "express", "chase", "citi", "visa", "mastercard", "amex", "no", "annual", "fee",
          "dagger", "art", "image", "of", "a", "and", "with", "r", "tm"}
# File-name words of a sibling product (a business or premium version of the same card).
SIBLINGS = {"business", "preferred", "goldman", "morgan", "schwab", "charles", "delta", "hilton",
            "marriott", "bonvoy", "family", "promo", "array", "logo", "icon"}


@dataclass
class Candidate:
    url: str
    alt: str
    page: str
    score: int


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("®", " ").replace("™", " "))


def _names(card: Card, settings: Settings) -> list[list[str]]:
    return [_words(n) for n in [card.product, *settings.cards.product_aliases.get(card.id, [])]]


def _score(names: list[list[str]], product: set[str], url: str, alt: str) -> int | None:
    """Higher is better; None when neither the alt text nor the file name names the card."""
    path = urlsplit(url).path
    file_words = _words(re.sub(r"([a-z])([A-Z])", r"\1 \2", "/".join(path.split("/")[-2:])))
    alt_words = _words(alt)
    named_alt = any(n and all(w in alt_words for w in n) for n in names)
    named_file = any(n and all(w in file_words for w in n) for n in names)
    if not (named_alt or named_file):
        return None
    score = 20 if named_alt else 10
    score -= 3 * len({w for w in alt_words if w not in product and w not in FILLER})
    score -= 10 * len({w for w in file_words + alt_words if w in SIBLINGS and w not in product})
    if "annual fee" in " ".join(alt_words).replace("annual fees", "annual fee"):
        score -= 8  # a marketing banner drawn across the card
    if re.search(r"card[-_]?art|cardart", url, re.I):
        score += 3
    return score


def candidates(s: Session, card: Card, settings: Settings, domains: list[str]) -> list[Candidate]:
    """Pictures on this card's stored HTML documents that name it, best first."""
    names = _names(card, settings)
    product = {w for n in names for w in n}
    best: dict[str, Candidate] = {}
    docs = s.scalars(select(m.CardDocument).where(m.CardDocument.card_id == card.id,
                                                  m.CardDocument.status == "active"))
    for d in docs:
        v = latest_ok_version(s, d.id)
        if v is None or not v.file_path or not v.file_path.endswith(".html"):
            continue
        path = Path(v.file_path)
        if not path.is_file():
            continue
        soup = BeautifulSoup(path.read_bytes(), "html.parser")
        for img in soup.find_all(["img", "source"]):
            raw = img.get("src") or img.get("data-src") or (img.get("srcset") or "").split(" ")[0]
            if not raw or raw.startswith("data:"):
                continue
            url = urljoin(d.url, raw.strip())
            parts = urlsplit(url)
            if (parts.scheme != "https" or not RASTER.search(parts.path)
                    or not host_allowed(parts.hostname or "", domains)):
                continue
            alt = " ".join((img.get("alt") or "").split())[:200]
            score = _score(names, product, url, alt)
            if score is not None and (url not in best or best[url].score < score):
                best[url] = Candidate(url, alt, d.url, score)
    return sorted(best.values(), key=lambda c: (-c.score, c.url))


def image_kind(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def image_size(data: bytes, kind: str) -> tuple[int, int] | None:
    """Width and height from the file header, without an imaging library."""
    try:
        if kind == "png":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if kind == "webp":
            chunk = data[12:16]
            if chunk == b"VP8 ":
                return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                        int.from_bytes(data[28:30], "little") & 0x3FFF)
            if chunk == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            return None
        i = 2  # JPEG: walk the segments to the first start-of-frame
        while i + 9 < len(data):
            if data[i] != 0xFF:
                return None
            marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (int.from_bytes(data[i + 7:i + 9], "big"),
                        int.from_bytes(data[i + 5:i + 7], "big"))
            i += 2 + length
    except IndexError:
        pass
    return None


def card_shaped(size: tuple[int, int] | None) -> bool:
    """A card is 1.586:1; pictures with a shadow or a little margin stay within 1.3-1.9."""
    return bool(size and size[1] and 1.3 <= size[0] / size[1] <= 1.9 and size[0] >= 120)


def art_dir(paths: DataPaths) -> Path:
    return paths.card_docs / "art"


def stored(paths: DataPaths, card_id: str) -> tuple[Path, dict] | None:
    """The stored picture and its note, or None."""
    note = art_dir(paths) / f"{card_id}.json"
    if not note.is_file():
        return None
    info = json.loads(note.read_text())
    pic = art_dir(paths) / info.get("file", "")
    return (pic, info) if pic.is_file() and pic.parent == art_dir(paths) else None


@dataclass
class ArtResult:
    card_id: str
    outcome: str  # ok | unchanged | none_found | off_allowlist | not_an_image | <fetch outcome>
    url: str | None = None
    detail: str = ""


def fetch_art(s: Session, paths: DataPaths, settings: Settings, cards: CardsConfig, card: Card,
              fetcher: Fetcher, *, force: bool = False) -> ArtResult:
    domains = settings.cards.domains_for(card, cards.site(card.issuer))
    override = settings.cards.art.get(card.id)
    if override:
        picks = [Candidate(override, "", "settings", 0)]
    else:
        picks = candidates(s, card, settings, domains)[:TRIES]
        if not picks:
            return ArtResult(card.id, "none_found",
                             detail="no picture naming the card in its documents")
    have = stored(paths, card.id)
    if have and have[1].get("url") == picks[0].url and not force:
        return ArtResult(card.id, "unchanged", picks[0].url)
    # The best-named picture that is really a card: a page photo can name the card too.
    result = ArtResult(card.id, "none_found")
    for pick in picks:
        got = fetcher.fetch(pick.url, domains)
        kind = image_kind(got.data) if got.outcome == "ok" else None
        if got.outcome != "ok":
            result = ArtResult(card.id, got.outcome, pick.url, got.detail)
        elif len(got.data) > MAX_BYTES:
            result = ArtResult(card.id, "too_large", pick.url)
        elif kind is None:
            result = ArtResult(card.id, "not_an_image", pick.url, got.content_type)
        elif not card_shaped(image_size(got.data, kind)):
            result = ArtResult(card.id, "not_card_shaped", pick.url)
        else:
            break
    else:
        return result
    if have and have[1].get("url") == pick.url and not force:
        return ArtResult(card.id, "unchanged", pick.url)
    folder = art_dir(paths)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for old in folder.glob(f"{card.id}.*"):
        old.unlink()
    _write(folder / f"{card.id}.{kind}", got.data)
    note = {"file": f"{card.id}.{kind}", "url": pick.url, "alt": pick.alt, "page": pick.page,
            "sha256": hashlib.sha256(got.data).hexdigest(), "fetched_at": utcnow()}
    _write(folder / f"{card.id}.json", json.dumps(note, indent=1).encode())
    return ArtResult(card.id, "ok", pick.url)


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def art_versions(paths: DataPaths) -> dict[str, str]:
    """card_id -> a short version for cache-busting, for every card with a stored picture."""
    out = {}
    for note in art_dir(paths).glob("*.json"):
        have = stored(paths, note.stem)
        if have:
            out[note.stem] = have[1].get("sha256", "")[:12]
    return out
