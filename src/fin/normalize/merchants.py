"""Descriptor cleaning (SPEC §10). `description_raw` is always kept as-is."""

from __future__ import annotations

import re

PREFIXES = re.compile(r"^(SQ ?\*|TST ?\*|SP |PAYPAL ?\*|PY ?\*|DD ?\*|SPO ?\*|IC ?\*)\s*")
# After a star: "WAYFAIR1234567890" is a name + number (keep WAYFAIR); "123AB45C6" and
# "5Q1P87LA2" are reference codes (drop).
STAR_TOKEN = re.compile(r"\*\s*([A-Z0-9]{4,})\b")
NAME_NUMBER = re.compile(r"([A-Z]{3,})\d+")
GLUED = re.compile(r"^([A-Z]{3,})\d{4,}$")
PHONE = re.compile(r"\b(\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b")
STORE_NO = re.compile(r"(#\s*\d+|\bSTORE\s+\d+|\b\d{3,}\b)")
TRAILING_STATE = re.compile(r"\s+[A-Z]{2}$")
BILLING_URL = re.compile(r"\s+\S+\.(COM|NET|ORG)/\S*$")
DOMAIN = re.compile(r"\S+\.(COM|NET|ORG|IO|CO)")


def normalize_raw(desc: str) -> str:
    """Stable form for dedupe keys: upper case, single spaces. No other changes."""
    return " ".join(desc.upper().split())


def _star_token(m: re.Match) -> str:
    tok = m.group(1)
    if not any(c.isdigit() for c in tok):
        return " " + tok
    name = NAME_NUMBER.fullmatch(tok)
    return " " + name.group(1) if name else " "


def clean_description(desc: str) -> str:
    s = normalize_raw(desc)
    s = PREFIXES.sub("", s)
    s = STAR_TOKEN.sub(_star_token, s)
    s = s.replace("*", " ")
    s = PHONE.sub(" ", s)
    s = " ".join(s.split())
    s = TRAILING_STATE.sub("", s) if len(s.split()) > 1 else s
    s = BILLING_URL.sub("", s)
    s = STORE_NO.sub(" ", s)
    tokens = s.split()
    # Drop bare numbers and trailing domains ("MICROSOFT.COM") when other words remain,
    # and repeated words ("MICROSOFT MICROSOFT").
    # "FEDEX12345678" -> "FEDEX": a name glued to a long number.
    tokens = [GLUED.sub(r"\1", t) for t in tokens]
    kept = [t for t in tokens if not t.isdigit() and re.search(r"[A-Z0-9]", t)]
    if len(kept) > 1:
        kept = [t for i, t in enumerate(kept) if not (i > 0 and DOMAIN.fullmatch(t))] or kept
    out: list[str] = []
    for t in kept:
        if not out or out[-1] != t:
            out.append(t)
    return " ".join(out) or normalize_raw(desc)
