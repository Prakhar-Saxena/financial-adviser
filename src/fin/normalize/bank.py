# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Checking rows (docs/SPEC-checking.md §5, §9).

Phase B0 has only `config/bank_rules.yaml`: loading and validating it. Cleaning
(`clean_bank_description`) and classification (`classify_bank_row`) arrive in Phase B2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from fin.config import CONFIG_DIR, CardsConfig
from fin.db.models import FLOW_KINDS

BANK_RULES_PATH = CONFIG_DIR / "bank_rules.yaml"
# A rule's `flow`: a flow kind, or the plain types `fee` and `refund` (§4's table).
RULE_FLOWS = (*[k for k in FLOW_KINDS if k != "other_in"], "fee", "refund")
# Flows a money-out row never gets (§9 step 3).
MONEY_IN_ONLY = ("interest", "income", "reward", "tax_refund", "refund")


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
                             f"card in cards.json, got {issuer!r}")
        try:
            regex = re.compile(str(r.get("pattern") or ""), re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"bank_rules.yaml rule {i}: bad pattern ({e})") from e
        if not regex.pattern:
            raise ValueError(f"bank_rules.yaml rule {i}: empty pattern")
        out.append(BankRule(regex, flow, issuer))
    return out
