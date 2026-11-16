# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Checking rows (docs/SPEC-checking.md §5, §8.3, §9): `bank_rules.yaml`, cleaning and
classification. Pure: no database, no AI.

Bank descriptions are the most sensitive text in the project: ACH lines can carry names,
employers, IDs and account fragments. `clean_bank_description` runs before
`clean_description`, so the merchant rules still match from the start of the text, and its
output is the only bank text that may reach the classifier (SPEC §7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from fin.config import CONFIG_DIR, CardsConfig
from fin.db.models import FLOW_KINDS
from fin.normalize.merchants import normalize_raw

BANK_RULES_PATH = CONFIG_DIR / "bank_rules.yaml"
# A rule's `flow`: a flow kind, or the plain types `fee` and `refund` (§4's table).
RULE_FLOWS = (*[k for k in FLOW_KINDS if k != "other_in"], "fee", "refund")
# Flows a money-out row never gets (§9 step 3).
MONEY_IN_ONLY = ("interest", "income", "reward", "tax_refund", "refund")
TRANSFER_FLOWS = ("card_payment", "card_payment_other", "own_transfer", "investment")


@dataclass(frozen=True)
class BankRule:
    regex: re.Pattern
    flow: str
    issuer: str | None


def load_bank_rules(cards: CardsConfig, path: Path = BANK_RULES_PATH) -> list[BankRule]:
    """Parse and check bank_rules.yaml. Raises ValueError naming the first bad rule."""
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(
            doc.get("rules"), list):
        raise ValueError("bank_rules.yaml: expected `version: 1` and a `rules` list")
    card_issuers = {c.issuer for c in cards.cards}
    out = []
    for i, r in enumerate(doc["rules"]):
        if not isinstance(r, dict) or set(r) - {"pattern", "flow", "issuer"}:
            raise ValueError(f"bank_rules.yaml rule {i}: only pattern, flow and issuer allowed")
        flow, issuer = r.get("flow"), r.get("issuer")
        if flow not in RULE_FLOWS:
            raise ValueError(f"bank_rules.yaml rule {i}: unknown flow {flow!r}")
        if issuer is not None and flow != "card_payment":
            raise ValueError(f"bank_rules.yaml rule {i}: issuer is for card_payment rules only")
        if flow == "card_payment" and issuer not in card_issuers:
            raise ValueError(f"bank_rules.yaml rule {i}: card_payment needs the issuer id of a "
                             f"card in accounts.json, got {issuer!r}")
        try:
            regex = re.compile(str(r.get("pattern") or ""), re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"bank_rules.yaml rule {i}: bad pattern ({e})") from e
        if not regex.pattern:
            raise ValueError(f"bank_rules.yaml rule {i}: empty pattern")
        out.append(BankRule(regex, flow, issuer))
    return out


def first_bank_rule(rules: list[BankRule], text: str, money_out: bool) -> BankRule | None:
    """§9 step 3: the first rule that matches, skipping money-in flows on a money-out row."""
    upper = normalize_raw(text)
    for r in rules:
        if money_out and r.flow in MONEY_IN_ONLY:
            continue
        if r.regex.search(upper):
            return r
    return None


# ------------------------------------------------------------------ cleaning (§8.3)

# Masked account numbers ("******1234", "XXXXXX1234"), dates, and long digit runs.
ACCOUNT_FRAGMENT = re.compile(r"\*{2,}\s*\d+|\bX{3,}\d+\b", re.IGNORECASE)
DATE = re.compile(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b")
LONG_DIGITS = re.compile(r"\d{6,}")
# An ID: one token of 8+ letters and digits mixed, maybe hyphenated ("3f9a07c2b1de-0000001").
MIXED_ID = re.compile(r"(?<![\w-])(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
                      r"(?![\w-])")
# How the transaction happened, at the start: not part of the merchant.
PREFIX = re.compile(
    r"^(?:(?:POS|DEBIT CARD|DBT CRD|CHECK ?CARD|VISA|MC)\s+(?:PURCHASE|PURCH|DEBIT|WITHDRAWAL)?"
    r"|ACH\s+(?:DEBIT|CREDIT|WITHDRAWAL|DEPOSIT|PMT)?|(?:EXTERNAL\s+)?(?:WITHDRAWAL|DEPOSIT))"
    r"\s*[:-]?\s+", re.IGNORECASE)
# ACH detail fields that hold a person's name or an ID: "INDN:ALEX EXAMPLE CO ID:0000".
ACH_FIELD = re.compile(
    r"\b(?:INDN|IND ?NAME|IND ?ID|NAME|CO ?ID|ID ?NO|TRACE(?: ?NO)?|REF(?:ERENCE)?(?: ?NO)?|"
    r"MEMBER(?: ?NO)?|PPD ?ID|WEB ?ID|CCD ?ID)\s*[:#]\s*.*?(?=\b[A-Z ]{2,12}\s*[:#]|$)",
    re.IGNORECASE)
TRAILING_COUNTRY = re.compile(r"\s{2,}US$")


@dataclass(frozen=True)
class CleanTerms:
    """What must never survive cleaning: names, private words and endings from accounts.json."""

    names: re.Pattern | None
    private: re.Pattern | None
    endings: re.Pattern | None

    @classmethod
    def from_cards(cls, cards: CardsConfig) -> CleanTerms:
        full = [n for p in cards.people for n in (p.full_name, *p.name_aliases) if n.strip()]
        # Whole names first, so a middle initial goes with them; then each name word.
        words = {" ".join(n.split()) for n in full}
        words |= {w for n in full for w in re.findall(r"[A-Za-z][A-Za-z'-]+", n) if len(w) >= 3}
        private = [w for w in cards.private_words if w.strip()]
        endings = {h.card_ending for c in cards.cards for h in c.cardholders if h.card_ending}
        endings |= {a.account_ending for a in cards.bank_accounts if a.account_ending}

        def alternation(items, whole: bool) -> re.Pattern | None:
            if not items:
                return None
            body = "|".join(re.escape(i).replace(r"\ ", r"\s+")
                            for i in sorted(items, key=len, reverse=True))
            return re.compile(rf"\b(?:{body})\b" if whole else f"(?:{body})", re.IGNORECASE)

        # Names are whole words; private words match inside a word too ("<WORD>Tax").
        return cls(alternation(words, True), alternation(private, False),
                   alternation(endings, True))


NO_TERMS = CleanTerms(None, None, None)


def clean_bank_description(desc: str, terms: CleanTerms = NO_TERMS) -> str:
    """The merchant, or the ACH originator and its entry description, with names, private
    words, endings, dates, IDs and account fragments removed (§8.3). Feed the result to
    `clean_description`."""
    s = TRAILING_COUNTRY.sub("", desc.strip())
    s = PREFIX.sub("", s)
    s = ACH_FIELD.sub(" ", s)
    s = ACCOUNT_FRAGMENT.sub(" ", s)
    s = DATE.sub(" ", s)
    s = MIXED_ID.sub(lambda m: " " if len(m.group().replace("-", "")) >= 8 else m.group(), s)
    s = LONG_DIGITS.sub(" ", s)
    for pattern in (terms.private, terms.names, terms.endings):
        if pattern is not None:
            s = pattern.sub(" ", s)
    s = re.sub(r"\s+-\s+", " - ", s)  # "ORIGINATOR  - ENTRY": one space either side
    s = " ".join(s.split()).strip(" -")
    return s or "BANK ROW"


# ------------------------------------------------------------------ classifying (§9)

# The export's own row type (§9 step 4), mapped from the values the first real export showed.
# Keys are "<type column>|<kind column>" with numbers dropped; only types the sign and the
# patterns can't settle are listed.
EXPORT_TYPES: dict[str, tuple[str, str | None]] = {
    "transfer|withdrawal": ("transfer", "own_transfer"),
    "deposit|overdraft protection deposit": ("transfer", "own_transfer"),
    "|returned check": ("purchase", "check"),
    "debit card|point of sale withdrawal": ("purchase", None),
}


@dataclass(frozen=True)
class BankClass:
    type: str
    flow_kind: str | None
    issuer: str | None = None
    # override | user_rule | bank_rule | export | sign
    source: str = "sign"


def flow_to_class(flow: str, money_out: bool, issuer: str | None, source: str) -> BankClass:
    """A rule's flow as (type, flow_kind), per §4's table."""
    if flow in TRANSFER_FLOWS:
        return BankClass("transfer", flow, issuer, source)
    if flow == "fee":
        return BankClass("fee", None, None, source)
    if flow == "refund":
        return BankClass("refund", None, None, source)
    if flow == "p2p":
        return BankClass("purchase" if money_out else "deposit", "p2p", None, source)
    if flow in ("check", "atm"):
        # A check or cash paid in is money in from outside, not spending.
        return BankClass("purchase", flow, None, source) if money_out else \
            BankClass("deposit", "other_in", None, source)
    return BankClass("deposit", flow, None, source)  # income, interest, reward, tax_refund


def classify_bank_row(
    amount_cents: int, description_clean: str, bank_type: str | None, rules: list[BankRule], *,
    override: tuple[str, str | None] | None = None,
    user_rule: tuple[str, str | None] | None = None,
    known_merchant: bool = False,
) -> BankClass:
    """§9: override > my rule > bank_rules.yaml > the export's type > the sign.
    `amount_cents` follows SPEC §8 (positive = money out); the patterns read the cleaned
    description, as bank_rules.yaml says."""
    money_out = amount_cents > 0
    if override is not None:
        return BankClass(override[0], override[1], None, "override")
    if user_rule is not None:
        return BankClass(user_rule[0], user_rule[1], None, "user_rule")
    rule = first_bank_rule(rules, description_clean, money_out)
    if rule is not None:
        return flow_to_class(rule.flow, money_out, rule.issuer, "bank_rule")
    mapped = EXPORT_TYPES.get((bank_type or "").lower())
    if mapped is not None:
        return BankClass(mapped[0], mapped[1], None, "export")
    if money_out:
        return BankClass("purchase", None)
    if known_merchant:
        return BankClass("refund", None)
    return BankClass("deposit", "other_in")


def export_type(type_column: str, kind_column: str) -> str:
    """`bank_type` for a row: "<type>|<kind>", lower case, numbers and "#" dropped."""
    kind = re.sub(r"[#\d]+", " ", kind_column)
    return f"{' '.join(type_column.split())}|{' '.join(kind.split())}".lower()
