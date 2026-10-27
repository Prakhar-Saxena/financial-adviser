# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify extracted terms against the stored documents (docs/SPEC-cards.md §7.4).

AI reads, code decides: every quote must be found in the cited document (on the cited page or
the one before or after), every number an item asserts must appear in one of its verified
quotes, and the values must be sane. Failed items are dropped from the profile.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any

MARKER = re.compile(r"\[\[(?:page|section) (\d+)\]\]")
NUMBER = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")
WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty-five": 25, "thirty": 30, "fifty": 50,
}
ZERO_WORDS = ("no", "none", "zero")
MAX_RATE, FLAG_RATE = 25, 10

_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "®": None, "™": None, " ": " ", "­": None,
})


def normalize(text: str, keep_hyphens: bool = False) -> str:
    """NFKC (expands ligatures), lower case, ASCII quotes and dashes, ® and ™ dropped,
    line-end hyphenation joined, whitespace collapsed."""
    s = unicodedata.normalize("NFKC", text).translate(_TRANSLATE).lower()
    s = re.sub(r"-[ \t]*\n\s*", "-" if keep_hyphens else "", s)
    return " ".join(s.split())


@dataclass
class DocText:
    """One document's text split by its [[page N]] / [[section N]] markers."""

    pages: dict[int, str]  # page number -> raw text

    @classmethod
    def parse(cls, text: str) -> DocText:
        pages: dict[int, str] = {}
        parts = MARKER.split(text)
        if parts[0].strip():
            pages[1] = parts[0]
        for i in range(1, len(parts), 2):
            n = int(parts[i])
            pages[n] = pages.get(n, "") + "\n" + parts[i + 1]
        return cls(pages or {1: text})

    def window(self, page: int | None) -> str:
        """The cited page and its neighbours, or the whole document."""
        if page is None or page not in self.pages:
            return "\n".join(self.pages.values())
        return "\n".join(self.pages[p] for p in (page - 1, page, page + 1) if p in self.pages)

    def contains(self, quote: str, page: int | None) -> bool:
        window = self.window(page)
        q = normalize(quote)
        if not q:
            return False
        return any(q in normalize(window, keep_hyphens=k) for k in (False, True))


def numbers_in(text: str) -> set[float]:
    """Every number a quote states: digits ("$25,000", "4X", "1.5%") and words ("four"),
    plus 0 for "no"/"none" ("no foreign transaction fees")."""
    s = normalize(text)
    out: set[float] = set()
    for whole, frac in NUMBER.findall(s):
        out.add(float(whole.replace(",", "") + (f".{frac}" if frac else "")))
    tokens = re.findall(r"[a-z]+(?:-[a-z]+)?", s)
    for t in tokens:
        if t in WORDS:
            out.add(float(WORDS[t]))
        if t in ZERO_WORDS:
            out.add(0.0)
    return out


def _has_number(value: float, found: set[float]) -> bool:
    return any(abs(value - f) < 1e-6 for f in found)


@dataclass
class Check:
    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verified_quotes: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.ok = False
        self.reasons.append(reason)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": self.reasons, "warnings": self.warnings}


def check_evidence(evidence: list[dict], docs: dict[str, DocText]) -> Check:
    c = Check()
    for e in evidence:
        doc = docs.get(e.get("doc", ""))
        if doc is None:
            c.fail(f"cites unknown document {e.get('doc')!r}")
        elif doc.contains(e.get("quote", ""), e.get("page")):
            c.verified_quotes.append(e["quote"])
        else:
            c.fail(f"quote not found in {e['doc']}: {e.get('quote', '')[:80]!r}")
    return c


def check_numbers(c: Check, values: list[tuple[str, float]]) -> None:
    found: set[float] = set()
    for q in c.verified_quotes:
        found |= numbers_in(q)
    for name, v in values:
        if not _has_number(v, found):
            c.fail(f"{name} {v:g} is in none of its verified quotes")


def check_dates(c: Check, item: dict) -> None:
    for k in ("valid_from", "valid_to"):
        if item.get(k):
            try:
                date.fromisoformat(item[k])
            except ValueError:
                c.fail(f"{k} is not a date: {item[k]!r}")


def verify_rule(rule: dict, docs: dict[str, DocText]) -> Check:
    c = check_evidence(rule["evidence"], docs)
    values = [("rate", rule["rate"])]
    if rule.get("cap"):
        cap = rule["cap"]
        values += [("cap", cap["amount_usd"]), ("rate after cap", cap["rate_after"])]
    if c.ok:
        check_numbers(c, values)
    for name, v in values:
        if v < 0 or (name != "cap" and v > MAX_RATE):
            c.fail(f"{name} {v:g} is out of range")
    if 0 <= rule["rate"] <= MAX_RATE and rule["rate"] > FLAG_RATE:
        c.warnings.append(f"rate {rule['rate']:g} is unusually high")
    if not rule["is_base"] and not (rule.get("categories") or rule.get("merchant_names")):
        c.fail("a non-base rule needs a category or a merchant")
    check_dates(c, rule)
    return c


def verify_credit(credit: dict, docs: dict[str, DocText]) -> Check:
    c = check_evidence(credit["evidence"], docs)
    if c.ok:
        check_numbers(c, [("amount", credit["amount_usd"])])
    if credit["amount_usd"] <= 0:
        c.fail("credit amount must be positive")
    check_dates(c, credit)
    return c


def verify_fee(fee: dict, key: str, docs: dict[str, DocText]) -> Check:
    c = check_evidence(fee["evidence"], docs)
    if c.ok:
        check_numbers(c, [(key, fee[key])])
    return c


def verify_benefit(benefit: dict, docs: dict[str, DocText]) -> Check:
    c = check_evidence(benefit["evidence"], docs)
    if c.ok:
        limits = sorted(numbers_in_digits(benefit.get("limits", "")))
        check_numbers(c, [("limit", v) for v in limits])
    return c


def numbers_in_digits(text: str) -> set[float]:
    """Numbers written as digits only (benefit limits); words like "one" are too common."""
    return {float(w.replace(",", "") + (f".{f}" if f else ""))
            for w, f in NUMBER.findall(normalize(text))}


@dataclass
class Verified:
    """An extraction's output annotated in place with `_verify` per item, plus counts."""

    output: dict[str, Any]
    n_items: int = 0
    n_verified: int = 0
    problems: list[dict[str, Any]] = field(default_factory=list)

    @property
    def n_rejected(self) -> int:
        return self.n_items - self.n_verified


def _annotate(v: Verified, kind: str, label: str, item: dict, c: Check) -> None:
    item["_verify"] = c.as_dict()
    v.n_items += 1
    if c.ok:
        v.n_verified += 1
    else:
        v.problems.append({"kind": kind, "item": label, "reasons": c.reasons})


def verify_terms(output: dict[str, Any], docs: dict[str, DocText]) -> Verified:
    v = Verified(output)
    _annotate(v, "annual_fee", "annual fee", output["annual_fee"],
              verify_fee(output["annual_fee"], "usd", docs))
    _annotate(v, "foreign_transaction_fee", "foreign transaction fee",
              output["foreign_transaction_fee"],
              verify_fee(output["foreign_transaction_fee"], "percent", docs))
    for r in output["earn_rules"]:
        label = r.get("description") or f"{r['rate']:g}x"
        _annotate(v, "earn_rule", label, r, verify_rule(r, docs))
    bases = [r for r in output["earn_rules"] if r["is_base"] and r["_verify"]["ok"]]
    for extra in bases[1:]:
        extra["_verify"]["ok"] = False
        extra["_verify"]["reasons"].append("more than one base rule")
        v.n_verified -= 1
        v.problems.append({"kind": "earn_rule", "item": extra.get("description", "base"),
                           "reasons": ["more than one base rule"]})
    for cr in output["credits"]:
        _annotate(v, "credit", cr["name"], cr, verify_credit(cr, docs))
    return v


def verify_benefits(output: dict[str, Any], docs: dict[str, DocText]) -> Verified:
    v = Verified(output)
    for b in output["benefits"]:
        _annotate(v, "benefit", b["name"], b, verify_benefit(b, docs))
    return v
