# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Checking account foundation (docs/SPEC-checking.md Phase B0): bank_rules.yaml, settings,
types. Fictional values only ("Example Bank", bank.example)."""

import pytest
from pydantic import ValidationError

from fin.config import CONFIG_DIR, Settings, load_cards, load_settings
from fin.db.models import FLOW_KINDS
from fin.normalize.bank import MONEY_IN_ONLY, RULE_FLOWS, load_bank_rules

CARDS = load_cards(CONFIG_DIR / "cards.example.json")


def test_bank_rules_load_and_name_only_card_issuers():
    rules = load_bank_rules(CARDS)
    assert len(rules) >= 10
    assert {r.issuer for r in rules if r.flow == "card_payment"} == {"amex", "chase", "citi"}
    assert all(r.issuer is None for r in rules if r.flow != "card_payment")
    assert set(MONEY_IN_ONLY) <= set(RULE_FLOWS) and set(RULE_FLOWS) - {"fee", "refund"} <= set(
        FLOW_KINDS)


@pytest.mark.parametrize("line", [
    "TRANSFER TO SAVINGS",          # must not be a fee (bare NSF would match TRANSFER)
    "MEDICAL TREATMENT CENTER",     # must not be ATM
])
def test_short_patterns_respect_word_boundaries(line):
    flows = [r.flow for r in load_bank_rules(CARDS) if r.regex.search(line)]
    assert "fee" not in flows and "atm" not in flows


@pytest.mark.parametrize(("line", "flow"), [
    ("AMERICAN EXPRESS ACH PMT", "card_payment"),
    ("CHASE CREDIT CRD EPAY", "card_payment"),
    ("CITI CARD ONLINE PAYMENT", "card_payment"),
    ("CAPITAL ONE CRCARDPMT", "card_payment_other"),
    ("ZELLE PAYMENT TO A FRIEND", "p2p"),
    ("ONLINE TRANSFER TO SAV", "own_transfer"),
    ("VANGUARD BUY INVESTMENT", "investment"),
    ("CHECK 1042", "check"),
    ("ATM WITHDRAWAL MAIN ST", "atm"),
    ("MONTHLY FEE", "fee"),
    ("INTEREST PAID", "interest"),
    ("EXAMPLE CORP PAYROLL", "income"),
    ("IRS TREAS 310 TAX REF", "tax_refund"),
])
def test_first_hit_wins(line, flow):
    assert next(r.flow for r in load_bank_rules(CARDS) if r.regex.search(line)) == flow


@pytest.mark.parametrize("rule", [
    "{pattern: 'X', flow: lottery}",
    "{pattern: 'X', flow: card_payment}",                    # no issuer
    "{pattern: 'X', flow: card_payment, issuer: bank}",      # not a card issuer
    "{pattern: 'X', flow: p2p, issuer: amex}",
    "{pattern: '(', flow: p2p}",
    "{pattern: '', flow: p2p}",
    "{pattern: 'X', flow: p2p, category: groceries}",
])
def test_bad_bank_rules_refused(tmp_path, rule):
    p = tmp_path / "bank_rules.yaml"
    p.write_text(f"version: 1\nrules:\n  - {rule}\n")
    with pytest.raises(ValueError, match="rule 0"):
        load_bank_rules(CARDS, p)


def test_transfer_settings():
    m = load_settings().match
    assert m.transfer_window_days == (-10, 6) and m.transfer_grace_days == 10
    with pytest.raises(ValidationError):
        Settings.model_validate({"match": {"transfer_window_days": [6, -10]}})


def test_rebuild_clears_links_and_bank_statements(tmp_path):
    from fin.config import load_taxonomy
    from fin.db import models as m
    from fin.db import session_factory, upgrade
    from fin.db.seed import sync_reference
    from fin.rebuild import DERIVED, rebuild

    assert DERIVED[0] is m.TransferLink and m.BankStatement in DERIVED
    db = tmp_path / "t.sqlite"
    upgrade(db)
    with session_factory(db)() as s:
        sync_reference(s, CARDS, load_taxonomy())
        s.add(m.Import(id=1, source="bank", kind="transactions", file_path=str(tmp_path / "gone"),
                       sha256="h", status="ok"))
        for i, (card, amt, typ) in enumerate([("checking", 5000, "transfer"),
                                              ("amex_gold", -5000, "payment")], 1):
            s.add(m.Transaction(id=i, card_id=card, post_date="2026-09-01", amount_cents=amt,
                                description_raw="x", description_clean="x", type=typ,
                                dedupe_key=f"k{i}", import_id=1))
        s.flush()
        s.add(m.TransferLink(bank_txn_id=1, card_txn_id=2, method="named", days_apart=0,
                             confidence=1.0))
        s.add(m.BankStatement(card_id="checking", period_start="2026-08-02",
                              period_end="2026-09-01", opening_balance=0, closing_balance=0,
                              pdf_path="x", parse_method="regex"))
        s.commit()
        rebuild(s)
        assert s.query(m.TransferLink).count() == s.query(m.BankStatement).count() == 0
