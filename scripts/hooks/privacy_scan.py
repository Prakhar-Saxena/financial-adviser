#!/usr/bin/env python3
# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Keep your own data out of git (SPEC §7). Stdlib only, Python 3.9+.

The repo is public. This checks what is about to be committed or pushed against your own
data: the ledger in the data dir (opened read-only) and config/cards.json. A finding says
where it is and what kind of thing it is, never the value.

    privacy_scan.py staged          pre-commit: the lines being added
    privacy_scan.py message FILE    commit-msg: the commit message
    privacy_scan.py push REMOTE     pre-push: every commit about to leave (refs on stdin)
    privacy_scan.py tree            tracked files and commit messages (fin doctor)
    privacy_scan.py history         every version of every file, and every commit message
    privacy_scan.py history --all-amounts
                                    an audit: also every lone amount of yours, including whole
                                    numbers read as cents, unless chance easily explains it

What counts as yours:
- From config/cards.json: the names of everyone but you (full names, aliases, first names,
  ids), card and account endings next to words like "ending" or "****", and `private_words`.
- From the ledger: order and reference numbers, reference codes and phone or ID numbers in
  descriptions, merchant descriptions that carry a number, item titles, store locations,
  statement points, and amounts.
- Anywhere: a full card number and a bank routing number.

An amount alone is weak evidence: any made-up amount can equal one of yours by chance, and
common shelf prices are in most ledgers. So an amount of yours counts when it is over $1,000,
when it is a statement total over $100, when its own date and merchant are on the same line,
or when one place (a file, or the lines a commit adds to it) holds more of your amounts than
chance explains. Chance is estimated from your own ledger: how many of its amounts sit near
each value with the same cents. Amounts under $10 and whole-dollar amounts are ignored.

When config/cards.json holds real values but the ledger can't be read, the check fails: it
can't tell what is yours. There is no inline way to skip it.

Exit codes: 0 nothing found, 1 something found or the check couldn't run, 2 usage.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

MIN_CENTS = 1000             # under $10: too common to mean anything
STATEMENT_CENTS = 10_000     # a statement total over $100 counts on its own
LARGE_CENTS = 100_000        # so does any amount over $1,000
CLUSTER_MIN = 2             # amounts of yours in one place before chance is even asked
CLUSTER_CHANCE = 1e-6        # flag when this many hits would happen by chance less often
AUDIT_CHANCE = 0.1           # --all-amounts skips amounts more likely than this to match by chance
MIN_POINTS = 1000
MAX_BYTES = 5 * 1024 * 1024
MAX_PRINTED = 100

SKIP_FILES = {"uv.lock", "package-lock.json"}  # generated from public registries
BINARY_OK = ("docs/screenshots/", "web/public/", "web/src/assets/")
GENERIC_NAMES = {
    "self", "me", "myself", "you", "spouse", "wife", "husband", "partner", "father", "dad",
    "daddy", "papa", "mother", "mom", "mum", "mommy", "mama", "child", "kid", "son",
    "daughter", "brother", "sister", "other", "family", "parent", "baby",
}

KINDS = {
    "name": "a name from config/cards.json",
    "word": "a private word from config/cards.json",
    "ending": "a card or account ending of yours",
    "order": "an order or reference number from your ledger",
    "code": "a reference code from your ledger's descriptions",
    "digits": "a phone or ID number from your ledger's descriptions",
    "descriptor": "a merchant description from your ledger",
    "title": "an item title from your orders",
    "location": "a store location from your orders",
    "points": "a points figure from your statements",
    "large": "an amount over $1,000 from your ledger",
    "statement": "a statement total from your ledger",
    "dated": "an amount from your ledger, with its date and merchant on the same line",
    "alone": "an amount from your ledger, on its own and not a common price (it can still be "
             "chance; change it if it came from your data)",
    "cluster": "an amount from your ledger ({n} of the {m} amounts here are yours: too many "
               "for chance)",
    "card": "what looks like a full card number",
    "routing": "what looks like a bank routing number",
    "binary": "a binary file, which can't be checked (binary fixtures need a .src.* text "
              "source next to them; images go in docs/screenshots/ or web/)",
    "large file": "a file too large to check",
}

# ------------------------------------------------------------------- patterns

WORD = re.compile(r"[A-Za-z0-9]+")
ALPHA = re.compile(r"[A-Za-z]{3,}")
NON_ALNUM = re.compile(r"[^0-9a-z]+")
ID_TOKEN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
PHONE = re.compile(r"(?<!\d)\(?(\d{3})\)?[-. ]?(\d{3})[-. ](\d{4})(?!\d)")
DATE_DIGITS = re.compile(r"(?:19|20)\d{6}")
MONEY = re.compile(r"(?<![\w.$])\$?(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?![\w%]|\.\d)")
INT_CENTS = re.compile(r"(?<![\w.$,/:-])(-?)(\d{4,8})(?![\w.%/:-])")
# Whole words, or parts of snake_case names (amount_cents), but not inside other words:
# "taxes_government" or "coffee" are no reason to read a number as cents.
MONEY_WORDS = re.compile(
    r"(?i)(?<![a-z])(?:cents?|amounts?|totals?|balances?|prices?|subtotal|tax|payments?"
    r"|charges?|refunds?|credits?|purchases?|fees?|spend|spent|due)(?![a-z])")
ISO_DATE = re.compile(r"(?<!\d)(?:19|20)\d\d-(\d\d)-(\d\d)(?!\d)")
US_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(?:\d{4}|\d{2}))?(?![\d/])")
NAMED_DATE = re.compile(
    r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?!\d)")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
ENDING = re.compile(
    r"(?i)(?:ending(?:\s+in)?|last\s+(?:4|5|four|five)(?:\s+digits)?|acct|account|card|visa"
    r"|mastercard|amex|discover|\*{2,}|•{2,}|x{2,}|(?<![A-Za-z0-9])x|\.{3}|…)"
    r"[\s:*•.-]{0,4}(\d{4,5})(?!\d)")
CARD_NUMBER = re.compile(
    r"(?<![0-9A-Za-z])(\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{3,4}|\d{4}[ -]?\d{6}[ -]?\d{5})"
    r"(?![0-9A-Za-z])")
TEST_CARDS = {
    "4111111111111111", "4242424242424242", "4012888888881881", "4000056655665556",
    "5555555555554444", "5105105105105100", "2223003122003222", "378282246310005",
    "371449635398431", "6011111111111117", "6011000990139424",
}
ROUTING = re.compile(r"(?i)(?:routing|\baba\b|\brtn\b|transit)[^0-9\n]{0,20}(\d{9})(?!\d)")
POINT_WORDS = re.compile(r"(?i)point|reward")
POINTS = re.compile(r"(?<![\w.,$])(\d{1,3}(?:,\d{3})+|\d{4,7})(?![\w.%]|,\d)")
HUNK = re.compile(rb"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# What the ledger contributes, by table. tests/unit/test_privacy_scan.py checks these against
# the models, so a renamed column can't silently drop out.
LEDGER = {
    "transactions": ["amount_cents", "txn_date", "post_date", "description_raw", "external_id",
                     "order_ref", "card_ending"],
    "statement_lines": ["amount_cents", "txn_date", "description", "order_ref",
                        "cardholder_name"],
    "statements": ["previous_balance", "payments", "credits", "purchases", "cash_advances",
                   "balance_transfers", "fees", "interest", "new_balance", "minimum_due",
                   "rewards_json"],
    "orders": ["id", "merchant", "external_id", "order_date", "location", "subtotal", "shipping",
               "tax", "discounts", "gift_card_applied", "total", "payment_card_ending"],
    "order_items": ["title_raw", "unit_price_cents", "line_total_cents", "discount_cents"],
    "order_charges": ["amount_cents", "charge_date", "order_ref", "card_ending", "raw_text"],
    "allocations": ["amount_cents"],
    "emails": ["parsed_total_cents"],
}


# --------------------------------------------------------------------- terms


@dataclass
class Amount:
    statement: bool = False
    # (month, day, merchant words) of each ledger row with this amount
    rows: list[tuple[int, int, frozenset]] = field(default_factory=list)


@dataclass
class Terms:
    """Everything that counts as yours. Built fresh on every run; never written anywhere."""

    real: bool = False                     # config/cards.json holds real values
    problem: str | None = None          # why the check can't run
    ledger: Path | None = None
    names: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    endings: set[str] = field(default_factory=set)
    ids: dict[str, str] = field(default_factory=dict)          # normalized -> kind
    amounts: dict[int, Amount] = field(default_factory=dict)   # cents -> rows
    points: set[int] = field(default_factory=set)
    phrases: dict[tuple[str, str], list[tuple[tuple[str, ...], str]]] = field(
        default_factory=dict)
    names_re: re.Pattern | None = None
    audit: bool = False                    # --all-amounts: report lone amounts too
    words_re: re.Pattern | None = None
    _common: set[int] = field(default_factory=set)       # cents values most of yours end in
    _dollars: dict[int, list[int]] = field(default_factory=dict)  # cents class -> dollars

    def prepare(self) -> None:
        """Index the ledger's amounts by cents class, for `chance`."""
        counts = Counter(v % 100 for v in self.amounts)
        n = len(self.amounts)
        self._common = {c for c, k in counts.items() if n >= 50 and k > 0.02 * n}
        self._dollars = {}
        for v in sorted(self.amounts):
            cls = v % 100 if v % 100 in self._common else -1
            self._dollars.setdefault(cls, []).append(v // 100)

    def chance(self, cents: int) -> float:
        """How likely a made-up amount is to be in the ledger anyway: the share of nearby values
        with the same kind of cents (.99 apart from the rest) that the ledger holds."""
        d, c = divmod(cents, 100)
        cls = c if c in self._common else -1
        w = max(5, d * 3 // 10)
        lo, hi = max(10, d - w), d + w
        got = self._dollars.get(cls, [])
        n = bisect_right(got, hi) - bisect_left(got, lo)
        slots = (hi - lo + 1) * (1 if cls != -1 else 99 - len(self._common))
        return min(0.9, (n + 0.5) / slots)

    def summary(self) -> str:
        n_phrases = sum(len(v) for v in self.phrases.values())
        return (f"{len(self.amounts)} amounts, {len(self.ids)} numbers and codes, {n_phrases} "
                f"titles and descriptions, {len(self.names)} names, {len(self.words)} private "
                f"words, {len(self.endings)} endings")


def norm_words(text: str) -> list[str]:
    return [w for w in NON_ALNUM.split(text.lower()) if w]


def term_regex(terms: Iterable[str], min_len: int) -> re.Pattern | None:
    parts = []
    for t in sorted({t.strip() for t in terms}, key=len, reverse=True):
        if len(t) < min_len:
            continue
        pieces = [p for p in re.split(r"[\s_-]+", t) if p]
        if pieces:
            parts.append(r"[\s_-]+".join(re.escape(p) for p in pieces))
    if not parts:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])",
                      re.IGNORECASE)


def git_dir_root(root: Path) -> Path | None:
    """The main worktree, where config/cards.json lives when `root` is a linked worktree."""
    try:
        out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             cwd=root, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip()).parent if out.returncode == 0 and out.stdout.strip() else None


def read_cards(root: Path) -> tuple[dict, bool, str | None]:
    """(cards, real, problem). The example config and a missing file hold nothing of yours."""
    path = root / "config" / "cards.json"
    if not path.exists():
        main = git_dir_root(root)
        if main and (main / "config" / "cards.json").exists():
            root, path = main, main / "config" / "cards.json"
        else:
            return {}, False, None
    raw = path.read_bytes()
    example = root / "config" / "cards.example.json"
    if example.exists() and example.read_bytes() == raw:
        return {}, False, None
    try:
        cards = json.loads(raw)
    except ValueError:
        cards = None
    if not isinstance(cards, dict):
        return {}, True, f"can't read {path} as JSON, so it can't tell what is yours"
    return cards, True, None


def _list(x) -> list:
    return x if isinstance(x, list) else []


def people_terms(cards: dict) -> tuple[list[str], set[str]]:
    """Names of everyone but you, and the words of your own name (public: the copyright line)."""
    people = [p for p in _list(cards.get("people")) if isinstance(p, dict)]
    mine: set[str] = set()
    for p in people:
        if p.get("relationship") == "self":
            for s in [p.get("full_name")] + _list(p.get("name_aliases")):
                mine.update(w.lower() for w in WORD.findall(str(s or "")))
    names: set[str] = set()
    for p in people:
        if p.get("relationship") == "self":
            continue
        full = str(p.get("full_name") or "")
        for s in [full] + [str(a) for a in _list(p.get("name_aliases"))]:
            ws = WORD.findall(s)
            # Whole names only. A middle or last name on its own ("Grace") is too often a word.
            if len(ws) >= 2 and not {w.lower() for w in ws} <= mine:
                names.add(" ".join(ws))
        first = WORD.findall(full)[:1]
        for w in first + [str(p.get("id") or ""), str(p.get("display_name") or "")]:
            w = w.strip()
            if (len(w) >= 3 and " " not in w and w.lower() not in GENERIC_NAMES
                    and w.lower() not in mine):
                names.add(w)
    return sorted(names), mine


def site_terms(cards: dict) -> tuple[list[str], set[str]]:
    """`private_words`, plus everything that names a bank site (docs/SPEC-checking.md §1.1)."""
    words = [str(w) for w in _list(cards.get("private_words"))]
    endings: set[str] = set()
    sites = {s.get("id"): s for s in _list(cards.get("issuers")) if isinstance(s, dict)}
    for acct in _list(cards.get("bank_accounts")):
        if not isinstance(acct, dict):
            continue
        words.append(str(acct.get("product") or ""))
        endings.add(str(acct.get("account_ending") or ""))
        site = sites.get(acct.get("site")) or {}
        words.append(str(site.get("name") or ""))
        words += [str(d) for d in _list(site.get("allowed_domains"))]
        host = re.match(r"https?://([^/:]+)", str(site.get("start_url") or ""))
        if host:
            words.append(host.group(1))
    for card in _list(cards.get("cards")):
        for h in _list(card.get("cardholders") if isinstance(card, dict) else None):
            if isinstance(h, dict):
                endings.add(str(h.get("card_ending") or ""))
    return [w for w in words if w.strip()], {e for e in endings if real_ending(e)}


def real_ending(value) -> bool:
    """4 or 5 digits, not a placeholder like 0000."""
    v = str(value or "")
    return bool(re.fullmatch(r"\d{4,5}", v)) and len(set(v)) > 1


def data_dir(root: Path) -> Path:
    """Same rule as fin.config.data_dir: $FIN_DATA_DIR, settings.toml, the default."""
    raw = os.environ.get("FIN_DATA_DIR")
    if not raw:
        settings = root / "config" / "settings.toml"
        text = settings.read_text(encoding="utf-8") if settings.exists() else ""
        try:
            import tomllib  # Python 3.11+

            raw = tomllib.loads(text).get("general", {}).get("data_dir")
        except ImportError:
            section = re.search(r"(?ms)^\[general\][ \t]*$(.*?)(?=^\[|\Z)", text)
            hit = section and re.search(r'(?m)^\s*data_dir\s*=\s*"([^"]*)"', section.group(1))
            raw = hit.group(1) if hit else None
        except ValueError:
            raw = None
    return Path(raw or "~/FinancialAdviserData").expanduser().resolve()


def open_ledger(path: Path) -> sqlite3.Connection:
    """Read-only, and nothing is created next to the database.

    With no -wal file every row is in the main file, and `immutable` reads it without
    creating -wal/-shm files. With one (fin is running, or crashed), `mode=ro` reads the WAL too.
    """
    uri = path.resolve().as_uri()
    wal = path.with_name(path.name + "-wal").exists()
    for params in (("?mode=ro", "?mode=ro&immutable=1") if wal else
                   ("?mode=ro&immutable=1", "?mode=ro")):
        try:
            con = sqlite3.connect(uri + params, uri=True)
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return con
        except sqlite3.Error:
            continue
    raise sqlite3.OperationalError(f"can't open {path} read-only")


def table_rows(con: sqlite3.Connection, table: str, cols: list[str]) -> list[dict]:
    """Rows of `table` with whichever of `cols` exist (older schemas lack some)."""
    try:
        have = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
        use = [c for c in cols if c in have]
        if not use:
            return []
        # Fixed table and column names, never input.
        cur = con.execute("SELECT " + ", ".join(f'"{c}"' for c in use)  # noqa: S608
                          + f' FROM "{table}"')
        return [dict(dict.fromkeys(cols), **dict(zip(use, r))) for r in cur]  # noqa: B905 (3.9)
    except sqlite3.Error:
        return []


def _md(s) -> tuple[int, int] | None:
    hit = re.match(r"\d{4}-(\d\d)-(\d\d)", str(s or ""))
    return (int(hit.group(1)), int(hit.group(2))) if hit else None


def load_ledger(con: sqlite3.Connection, t: Terms, mine: set[str] | None) -> None:
    def amount(cents, dates=(), words=frozenset(), statement=False) -> None:
        if not isinstance(cents, int) or isinstance(cents, bool):
            return
        c = abs(cents)
        if c < MIN_CENTS or c % 100 == 0:
            return
        a = t.amounts.setdefault(c, Amount())
        a.statement = a.statement or statement
        if words:
            for d in {_md(x) for x in dates} - {None}:
                a.rows.append((d[0], d[1], words))

    def number(value, kind="order") -> None:
        n = re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()
        if len(n) >= 8 and sum(ch.isdigit() for ch in n) >= 4 and len(set(n)) > 1:
            t.ids.setdefault(n, kind)

    def phrase(words: list[str], kind: str) -> None:
        if len(words) >= 2:
            key = (words[0], words[1])
            entry = (tuple(words), kind)
            if entry not in t.phrases.get(key, []):
                t.phrases.setdefault(key, []).append(entry)

    def description(text, whole=True) -> frozenset:
        text = str(text or "")
        for m in ID_TOKEN.finditer(text):
            n = m.group(0).replace("-", "").upper()
            digits = sum(ch.isdigit() for ch in n)
            if len(n) >= 8 and digits >= 2 and digits < len(n) and len(set(n)) > 2:
                t.ids.setdefault(n, "code")
            elif n.isdigit() and len(n) >= 7 and not DATE_DIGITS.fullmatch(n) and len(set(n)) > 1:
                t.ids.setdefault(n, "digits")
        for m in PHONE.finditer(text):
            n = "".join(m.groups())
            if len(set(n)) > 1:
                t.ids.setdefault(n, "digits")
        words = norm_words(text)
        digits = [ch for ch in text if ch.isdigit()]
        # Only descriptions that carry a number (store, terminal, phone), and not a placeholder.
        if whole and len(digits) >= 3 and len(set(digits)) > 1 and len(" ".join(words)) >= 12:
            phrase(words, "descriptor")
        return frozenset(w.upper() for w in ALPHA.findall(text)[:3])

    def ending(value) -> None:
        if real_ending(value):
            t.endings.add(str(value))

    for r in table_rows(con, "transactions", LEDGER["transactions"]):
        words = description(r["description_raw"])
        amount(r["amount_cents"], (r["txn_date"], r["post_date"]), words)
        number(r["external_id"])
        number(r["order_ref"])
        ending(r["card_ending"])
    names: set[str] = set()
    for r in table_rows(con, "statement_lines", LEDGER["statement_lines"]):
        amount(r["amount_cents"], (r["txn_date"],), description(r["description"]))
        number(r["order_ref"])
        ws = WORD.findall(str(r["cardholder_name"] or ""))
        if mine is not None and len(ws) >= 2 and not {w.lower() for w in ws} <= mine:
            names.add(" ".join(ws))
    for r in table_rows(con, "statements", LEDGER["statements"]):
        for k, v in r.items():
            if k != "rewards_json":
                amount(v, statement=True)
        try:
            rewards = json.loads(r["rewards_json"] or "{}")
        except ValueError:
            rewards = {}
        if isinstance(rewards, dict):
            values = [rewards.get("earned")] + [x.get("value") for x in _list(rewards.get("lines"))
                                                if isinstance(x, dict)]
            for v in values:
                if not isinstance(v, int):
                    continue
                if rewards.get("kind") == "cash":
                    amount(v, statement=True)
                elif abs(v) >= MIN_POINTS:
                    t.points.add(abs(v))
    order_dates: dict[object, str] = {}
    for r in table_rows(con, "orders", LEDGER["orders"]):
        order_dates[r["id"]] = r["order_date"]
        words = frozenset({str(r["merchant"] or "").upper()}) - {""}
        for k in ("subtotal", "shipping", "tax", "discounts", "gift_card_applied", "total"):
            amount(r[k], (r["order_date"],), words)
        if "|" not in str(r["external_id"] or ""):
            number(r["external_id"])
        loc = norm_words(str(r["location"] or ""))
        if len(" ".join(loc)) >= 6 and (len(loc) >= 2 or any(ch.isdigit() for ch in "".join(loc))):
            phrase(loc, "location")
        ending(r["payment_card_ending"])
    for r in table_rows(con, "order_items", LEDGER["order_items"]):
        for k in ("unit_price_cents", "line_total_cents", "discount_cents"):
            amount(r[k])
        out: list[str] = []
        for w in norm_words(str(r["title_raw"] or "")):
            out.append(w)
            if len(out) >= 4 and len(" ".join(out)) >= 30:
                phrase(out, "title")
                break
    for r in table_rows(con, "order_charges", LEDGER["order_charges"]):
        amount(r["amount_cents"], (r["charge_date"],), frozenset({"AMAZON"}))
        number(r["order_ref"])
        ending(r["card_ending"])
        description(r["raw_text"], whole=False)
    for r in table_rows(con, "allocations", LEDGER["allocations"]):
        amount(r["amount_cents"])
    for r in table_rows(con, "emails", LEDGER["emails"]):
        amount(r["parsed_total_cents"])
    t.names = sorted(set(t.names) | names)


def load_terms(root: Path = ROOT, data: Path | None = None) -> Terms:
    t = Terms()
    cards, t.real, t.problem = read_cards(root)
    if t.problem:
        return t
    t.names, mine = people_terms(cards)
    t.words, t.endings = site_terms(cards)
    t.ledger = (data or data_dir(root)) / "db" / "finance.sqlite"
    if t.ledger.exists():
        try:
            con = open_ledger(t.ledger)
            try:
                load_ledger(con, t, mine if cards else None)
            finally:
                con.close()
        except sqlite3.Error as e:
            t.problem = (f"can't read your ledger at {t.ledger} ({e}), so it can't tell what "
                         "is yours")
    elif t.real:
        t.problem = (f"no ledger at {t.ledger}, so it can't tell what is yours. Set FIN_DATA_DIR, "
                     "or run `uv run fin init`")
    t.names_re = term_regex(t.names, 3)
    t.words_re = term_regex(t.words, 4)
    t.prepare()
    return t


# ------------------------------------------------------------------ matching


@dataclass
class Finding:
    where: str
    kind: str      # what it is, in words
    key: str = ""  # its KINDS key

    def __str__(self) -> str:
        return f"{self.where}: {self.kind}"


def luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def aba(digits: str) -> bool:
    d = [int(c) for c in digits]
    prefix = int(digits[:2])
    if not (prefix <= 12 or 21 <= prefix <= 32 or 61 <= prefix <= 72 or prefix == 80):
        return False
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + d[2] + d[5] + d[8]) % 10 == 0


def at_least(k: int, ps: list[float]) -> float:
    """P(at least k of the events happen), each independently with its own probability."""
    if len(ps) > 400:  # Poisson approximation for big files
        lam = sum(ps)
        below = sum(math.exp(j * math.log(lam) - lam - math.lgamma(j + 1)) for j in range(k))
        return max(0.0, 1.0 - below)
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for j, q in enumerate(dist):
            nxt[j] += q * (1 - p)
            nxt[j + 1] += q * p
        dist = nxt
    return sum(dist[k:])


def money_values(text: str) -> set[int]:
    out = set()
    for m in MONEY.finditer(text):
        out.add(int(m.group(1).replace(",", "")) * 100 + int(m.group(2)))
    if MONEY_WORDS.search(text):
        out.update(int(m.group(2)) for m in INT_CENTS.finditer(text))
    return {c for c in out if c >= MIN_CENTS and c % 100}


def bare_cents(text: str) -> set[int]:
    """Every whole number that could be cents, money word or not (the audit only)."""
    return {c for c in (int(m.group(2)) for m in INT_CENTS.finditer(text))
            if c >= MIN_CENTS and c % 100 and not 1900 <= c <= 2099}  # not a year


def line_dates(text: str) -> set[tuple[int, int]]:
    out = {(int(m.group(1)), int(m.group(2))) for m in ISO_DATE.finditer(text)}
    out |= {(int(m.group(1)), int(m.group(2))) for m in US_DATE.finditer(text)}
    out |= {(MONTHS[m.group(1).lower()], int(m.group(2))) for m in NAMED_DATE.finditer(text)}
    return out


def scan_unit(lines: list[tuple[int, str]], t: Terms, where: Callable[[int], str]
              ) -> list[Finding]:
    """One place: a file, or the lines a commit adds to it, or a commit message."""
    found: dict[tuple[int, str], str] = {}

    def add(ln: int, kind: str, text: str | None = None) -> None:
        found.setdefault((ln, kind), text or KINDS[kind])

    every: set[int] = set()
    mine: set[int] = set()
    weak: list[tuple[int, int]] = []
    seq: list[tuple[str, int]] = []
    for ln, text in lines:
        if not text.strip():
            continue
        if t.names_re and t.names_re.search(text):
            add(ln, "name")
        if t.words_re and t.words_re.search(text):
            add(ln, "word")
        if t.endings and any(m.group(1) in t.endings for m in ENDING.finditer(text)):
            add(ln, "ending")
        if t.ids:
            for m in ID_TOKEN.finditer(text):
                kind = t.ids.get(m.group(0).replace("-", "").upper())
                if kind:
                    add(ln, kind)
            for m in PHONE.finditer(text):
                kind = t.ids.get("".join(m.groups()))
                if kind:
                    add(ln, kind)
        for m in CARD_NUMBER.finditer(text):
            d = re.sub(r"\D", "", m.group(1))
            if (len(d) in (15, 16) and d[0] in "3456" and len(set(d)) > 2
                    and d not in TEST_CARDS and luhn(d)):
                add(ln, "card")
        for m in ROUTING.finditer(text):
            if len(set(m.group(1))) > 2 and aba(m.group(1)):
                add(ln, "routing")
        if (t.points and POINT_WORDS.search(text)
                and any(int(m.group(1).replace(",", "")) in t.points
                        for m in POINTS.finditer(text))):
            add(ln, "points")
        values = money_values(text)
        every |= values
        dates = words = None
        for v in values:
            a = t.amounts.get(v)
            if a is None:
                continue
            mine.add(v)
            if v >= LARGE_CENTS:
                add(ln, "large")
            elif a.statement and v >= STATEMENT_CENTS:
                add(ln, "statement")
            else:
                if dates is None:
                    dates = line_dates(text)
                    words = {w.upper() for w in ALPHA.findall(text)}
                if any((m, d) in dates and ws & words for m, d, ws in a.rows):
                    add(ln, "dated")
                else:
                    weak.append((ln, v))
                    if t.audit and t.chance(v) < AUDIT_CHANCE:
                        add(ln, "alone")
        if t.audit:
            for v in bare_cents(text) - values:
                if v in t.amounts and t.chance(v) < AUDIT_CHANCE:
                    add(ln, "alone")
        if t.phrases:
            seq.extend((w, ln) for w in norm_words(text))
    if weak and len(mine) >= CLUSTER_MIN and (
            at_least(len(mine), [t.chance(v) for v in every]) < CLUSTER_CHANCE):
        text = KINDS["cluster"].format(n=len(mine), m=len(every))
        for ln, _ in weak:
            add(ln, "cluster", text)
    if t.phrases:
        words_only = [w for w, _ in seq]
        for i in range(len(words_only) - 1):
            for phrase, kind in t.phrases.get((words_only[i], words_only[i + 1]), ()):
                if tuple(words_only[i:i + len(phrase)]) == phrase:
                    add(seq[i][1], kind)
    return [Finding(where(ln), text, key) for (ln, key), text in sorted(found.items())]


def numbered(text: str) -> list[tuple[int, str]]:
    return list(enumerate(text.split("\n"), 1))


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def skipped(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in SKIP_FILES


def binary_allowed(path: str, paths: Iterable[str]) -> bool:
    if path.startswith(BINARY_OK):
        return True
    if path.startswith("tests/fixtures/") and "." in path.rsplit("/", 1)[-1]:
        stem = path.rsplit(".", 1)[0] + ".src."
        return any(p.startswith(stem) for p in paths)
    return False


# ----------------------------------------------------------------------- git


class GitError(Exception):
    pass


def git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=root,
                              input=stdin, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(str(e)) from e
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return proc.stdout


DIFF = ("-p", "-U0", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv",
        "--src-prefix=a/", "--dst-prefix=b/", "--diff-filter=ACM")


def unquote(path: bytes) -> str:
    """Git's C-style quoting of unusual paths ("a\\tb", octal escapes)."""
    if not (path.startswith(b'"') and path.endswith(b'"')):
        return path.decode("utf-8", "replace")
    out, i, s = bytearray(), 0, path[1:-1]
    simple = {ord("n"): 10, ord("t"): 9, ord("r"): 13, ord('"'): 34, ord("\\"): 92,
              ord("a"): 7, ord("b"): 8, ord("f"): 12, ord("v"): 11}
    while i < len(s):
        c = s[i]
        if c == 92 and i + 1 < len(s):
            nxt = s[i + 1]
            if 48 <= nxt <= 55 and i + 3 < len(s):
                out.append(int(s[i + 1:i + 4], 8))
                i += 4
                continue
            out.append(simple.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return out.decode("utf-8", "replace")


def parse_diff(data: bytes) -> dict[str, list[tuple[int, str]]]:
    """Added lines per file, with their line numbers in the new version."""
    files: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    old_left = new_left = 0
    ln = 0
    for raw in data.split(b"\n"):
        if old_left > 0 or new_left > 0:
            if raw.startswith(b"+"):
                if path is not None:
                    files[path].append((ln, raw[1:].decode("utf-8", "replace")))
                ln += 1
                new_left -= 1
            elif raw.startswith(b"-"):
                old_left -= 1
            elif raw.startswith(b" "):
                ln += 1
                old_left -= 1
                new_left -= 1
            continue
        if raw.startswith(b"diff --git "):
            path = None
        elif raw.startswith(b"+++ "):
            name = raw[4:].rstrip(b"\r")
            name = name.split(b"\t", 1)[0] if not name.startswith(b'"') else name
            p = unquote(name)
            path = p[2:] if p.startswith("b/") else None
            if path is not None:
                files.setdefault(path, [])
        elif raw.startswith(b"@@ "):
            m = HUNK.match(raw)
            if m:
                old_left = int(m.group(1)) if m.group(1) is not None else 1
                ln = int(m.group(2))
                new_left = int(m.group(3)) if m.group(3) is not None else 1
    return files


def binaries(numstat: bytes) -> list[str]:
    out = []
    for entry in numstat.split(b"\0"):
        parts = entry.split(b"\t", 2)
        if len(parts) == 3 and parts[0] == b"-":
            out.append(parts[2].decode("utf-8", "replace"))
    return out


def scan_files(diff: bytes, numstat: bytes, listing: Callable[[], list[str]], t: Terms,
               where: Callable[[str, int], str]) -> list[Finding]:
    findings = []
    bins = binaries(numstat)
    if bins:
        paths = listing()
        findings += [Finding(where(p, 0).rsplit(":", 1)[0], KINDS["binary"], "binary")
                     for p in bins if not binary_allowed(p, paths)]
    for path, lines in parse_diff(diff).items():
        if not skipped(path):
            findings += scan_unit(lines, t, lambda ln, p=path: where(p, ln))
    return findings


def scan_staged(root: Path, t: Terms) -> list[Finding]:
    return scan_files(
        git(root, "diff", "--cached", *DIFF),
        git(root, "diff", "--cached", "--numstat", "-z", "--no-renames", "--diff-filter=ACM"),
        lambda: git(root, "ls-files", "-z").decode("utf-8", "replace").split("\0"),
        t, lambda p, ln: f"{p}:{ln}")


def strip_message(text: str, comment: str = "#") -> str:
    out = []
    for line in text.split("\n"):
        if line.startswith(comment + " ") and line.strip(comment + " -").startswith(">8"):
            break  # `git commit -v`: everything below the scissors line is the diff
        out.append("" if line.startswith(comment) else line)
    return "\n".join(out)


def scan_message(text: str, t: Terms, label: str) -> list[Finding]:
    return scan_unit(numbered(text), t, lambda ln: f"{label}, line {ln}")


def object_body(root: Path, sha: str, kind: str) -> str:
    raw = git(root, "cat-file", kind, sha).decode("utf-8", "replace")
    return raw.split("\n\n", 1)[1] if "\n\n" in raw else ""


def scan_commit(root: Path, sha: str, t: Terms) -> list[Finding]:
    short = sha[:7]
    findings = scan_message(object_body(root, sha, "commit"), t, f"commit {short} message")
    parents = git(root, "rev-list", "--parents", "-n", "1", sha).decode().split()[1:]
    base = [parents[0], sha] if parents else ["--root", sha]
    findings += scan_files(
        git(root, "diff-tree", "-r", "--no-commit-id", *DIFF, *base),
        git(root, "diff-tree", "-r", "--no-commit-id", "--numstat", "-z", "--no-renames",
            "--diff-filter=ACM", *base),
        lambda: git(root, "ls-tree", "-r", "--name-only", "-z", sha).decode(
            "utf-8", "replace").split("\0"),
        t, lambda p, ln: f"commit {short} {p}:{ln}")
    return findings


def is_zero(sha: str) -> bool:
    return set(sha) == {"0"}


def scan_push(root: Path, t: Terms, remote: str, refs: str) -> list[Finding]:
    """Every commit the push would publish, whatever the remote already has."""
    commits: list[str] = []
    seen: set[str] = set()
    findings: list[Finding] = []
    for line in refs.splitlines():
        parts = line.split()
        if len(parts) < 4 or is_zero(parts[1]):
            continue  # a delete sends nothing
        local, theirs = parts[1], parts[3]
        if git(root, "cat-file", "-t", local).strip() == b"tag":
            findings += scan_message(object_body(root, local, "tag"), t, f"tag {local[:7]} message")
        exclude = []
        if not is_zero(theirs):
            try:
                git(root, "cat-file", "-e", theirs + "^{commit}")
                exclude.append(theirs)
            except GitError:
                pass  # not here: check everything the remote-tracking refs don't have
        if remote:
            exclude.append(f"--remotes={remote}")
        for sha in git(root, "rev-list", "--reverse", local, "--not", *exclude).decode().split():
            if sha not in seen:
                seen.add(sha)
                commits.append(sha)
    for sha in commits:
        findings += scan_commit(root, sha, t)
    return findings


def all_messages(root: Path) -> list[tuple[str, str]]:
    try:
        out = git(root, "log", "-z", "--branches", "--tags", "--format=%H%n%B")
    except GitError:
        return []  # no commits yet
    msgs = []
    for rec in out.decode("utf-8", "replace").split("\0"):
        rec = rec.lstrip("\n")
        if rec:
            sha, _, body = rec.partition("\n")
            msgs.append((sha, body))
    return msgs


def scan_tree(root: Path, t: Terms) -> tuple[list[Finding], list[Finding]]:
    """(tracked files as they are on disk, every commit message on a branch or tag)."""
    files: list[Finding] = []
    for raw in git(root, "ls-files", "-z").split(b"\0"):
        path = raw.decode("utf-8", "replace")
        f = root / path
        if not path or skipped(path) or f.is_symlink() or not f.is_file():
            continue
        if f.stat().st_size > MAX_BYTES:
            files.append(Finding(path, KINDS["large file"], "large file"))
            continue
        data = f.read_bytes()
        if not is_binary(data):
            files += scan_unit(numbered(data.decode("utf-8", "replace")), t,
                               lambda ln, p=path: f"{p}:{ln}")
    messages: list[Finding] = []
    for sha, body in all_messages(root):
        messages += scan_message(body, t, f"commit {sha[:7]} message")
    return files, messages


def scan_history(root: Path, t: Terms) -> list[Finding]:
    """Every file version and message reachable from a branch or tag."""
    try:
        listing = git(root, "rev-list", "--objects", "--branches", "--tags")
    except GitError:
        return []
    paths: dict[str, str] = {}
    for line in listing.decode("utf-8", "replace").splitlines():
        sha, _, path = line.partition(" ")
        if path and sha not in paths:
            paths[sha] = path
    findings: list[Finding] = []
    if paths:
        check = git(root, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                    stdin=("\n".join(paths) + "\n").encode())
        blobs = []
        for line in check.decode().splitlines():
            sha, kind, size = line.split()
            if kind != "blob" or skipped(paths[sha]):
                continue
            if int(size) > MAX_BYTES:
                findings.append(Finding(f"{paths[sha]} (blob {sha[:7]})", KINDS["large file"],
                                        "large file"))
            else:
                blobs.append(sha)
        data = git(root, "cat-file", "--batch", stdin=("\n".join(blobs) + "\n").encode())
        pos = 0
        while pos < len(data):
            end = data.index(b"\n", pos)
            sha, _, size = data[pos:end].decode().split()
            body = data[end + 1:end + 1 + int(size)]
            pos = end + 1 + int(size) + 1
            if not is_binary(body):
                label = f"{paths[sha]} (blob {sha[:7]})"
                findings += scan_unit(numbered(body.decode("utf-8", "replace")), t,
                                      lambda ln, lb=label: f"{lb}:{ln}")
    for sha, body in all_messages(root):
        findings += scan_message(body, t, f"commit {sha[:7]} message")
    return findings


# ---------------------------------------------------------------------- main


def toplevel() -> Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=20)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ROOT


def report(findings: list[Finding], limit: int = MAX_PRINTED) -> None:
    for f in findings[:limit]:
        print(f"privacy: {f}", file=sys.stderr)
    if len(findings) > limit:
        print(f"privacy: ... and {len(findings) - limit} more", file=sys.stderr)
    if findings:
        print("privacy: the check never prints what it found. Open each place above, and "
              "replace what's yours with made-up values.", file=sys.stderr)


def main(argv: list[str]) -> int:
    modes = {"staged": 2, "message": 3, "push": 2, "tree": 2, "history": 2}
    if len(argv) < 2 or argv[1] not in modes or len(argv) < modes[argv[1]]:
        print(__doc__, file=sys.stderr)
        return 2
    mode, root = argv[1], toplevel()
    t = load_terms(root)
    if t.problem:
        print(f"privacy: {t.problem}.", file=sys.stderr)
        return 1
    t.audit = mode in ("tree", "history") and "--all-amounts" in argv[2:]
    try:
        if mode == "staged":
            findings = scan_staged(root, t)
        elif mode == "message":
            try:
                comment = git(root, "config", "core.commentChar").decode().strip() or "#"
            except GitError:
                comment = "#"  # not set
            text = Path(argv[2]).read_text(encoding="utf-8", errors="replace")
            findings = scan_message(strip_message(text, "#" if comment == "auto" else comment),
                                    t, "commit message")
        elif mode == "push":
            findings = scan_push(root, t, argv[2] if len(argv) > 2 else "", sys.stdin.read())
        elif mode == "tree":
            files, messages = scan_tree(root, t)
            findings = files + messages
        else:
            findings = scan_history(root, t)
    except GitError as e:
        print(f"privacy: git failed, so the check couldn't run: {e}", file=sys.stderr)
        return 1
    report(findings, len(findings) if t.audit else MAX_PRINTED)
    if mode in ("tree", "history") and not findings:
        what = "every version of every file" if mode == "history" else "tracked files"
        print(f"privacy: clean: {what} and commit messages, against {t.summary()}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
