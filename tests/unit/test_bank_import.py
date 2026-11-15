# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Checking import, cleaning and classification (docs/SPEC-checking.md §8, §9).
Fictional bank and rows only."""

import json

import pytest

from fin.config import CONFIG_DIR, CardsConfig
from fin.ingest import inbox
from fin.ingest.bank import (
    is_bank_export,
    parse_bank_export,
    parse_bank_statement,
)
from fin.normalize.bank import (
    CleanTerms,
    classify_bank_row,
    clean_bank_description,
    export_type,
    load_bank_rules,
)
from fin.normalize.merchants import clean_description
from fin.normalize.signs import classify
from tests.conftest import FIXTURES

BANK = FIXTURES / "bank"
EXPORT = BANK / "ExportedTransactions.csv"
STATEMENT = BANK / "statement_checking_2026-10-31.txt"


def example_cards(**changes) -> CardsConfig:
    d = json.loads((CONFIG_DIR / "cards.example.json").read_text())
    d.update(changes)
    return CardsConfig.model_validate(d)


TERMS = CleanTerms.from_cards(example_cards(private_words=["Harborview"]))


# ------------------------------------------------------------------ export


def test_export_columns_signs_dates_and_order():
    text = EXPORT.read_text()
    assert is_bank_export(text) and b"\r\n" in EXPORT.read_bytes()
    rows, dropped = parse_bank_export(text, TERMS)
    assert len(rows) == 11 and dropped == 0
    by_desc = {r.description_raw: r for r in rows}
    pay = by_desc["EXAMPLE EMPLOYER INC  - PAYROLL"]
    card = by_desc["CHASE CREDIT CRD  - EPAY"]
    assert pay.amount_cents == -240000 and card.amount_cents == 61245  # §8: + is money out
    assert pay.type == "deposit" and card.type == "purchase"  # provisional, by sign
    assert card.post_date == "2026-10-02" and card.bank_type == "ach|external withdrawal"
    check = by_desc["Returned Check # 00000101"]
    assert check.bank_type == "|returned check"
    assert (check.post_date, check.txn_date) == ("2026-10-14", "2026-10-13")
    assert rows[0].post_date == "2026-10-31" and rows[0].balance_cents == 185083  # newest first


def test_export_drops_rows_that_arent_posted():
    lines = EXPORT.read_text().splitlines(keepends=True)
    lines.insert(1, lines[1].replace('"Posted"', '"Pending"'))
    rows, dropped = parse_bank_export("".join(lines), TERMS)
    assert len(rows) == 11 and dropped == 1


def test_export_never_uses_card_meaning():
    """classify() from signs.py would call a card payment out of checking a purchase and a
    money-in 'PAYMENT' row a payment (§8.1): the export parser doesn't use it."""
    assert classify(-50000, "ACME PAYMENT REFUND")[0] == "payment"
    rows, _ = parse_bank_export(EXPORT.read_text(), TERMS)
    assert {r.type for r in rows} == {"purchase", "deposit"}


def test_detect_recognizes_the_export_by_its_header():
    assert inbox.detect(EXPORT, example_cards()) == ("bank", "transactions")
    two = example_cards()
    d = two.model_dump(by_alias=True)
    d["issuers"].append({**d["issuers"][-1], "id": "bank2"})
    d["bank_accounts"].append({**d["bank_accounts"][0], "id": "checking2", "site": "bank2"})
    assert inbox.detect(EXPORT, CardsConfig.model_validate(d)) is None  # which bank? --card


# ------------------------------------------------------------------ cleaning


@pytest.mark.parametrize("raw, want, gone", [
    ("ACH DEBIT ACME UTILITY INDN:ALEX EXAMPLE CO ID:9876543210 PPD", "ACME UTILITY",
     ["ALEX", "EXAMPLE", "9876543210"]),
    ("Web Xfer To ******0001 Transfer 00000001 at ONLINE BANK, WEB", "WEB XFER TO TRANSFER",
     ["0001", "00000001"]),
    ("Northwind Mortgage 3f9a07c2b1de-0000001 - NorthwindM", "NORTHWIND MORTGAGE NORTHWINDM",
     ["3F9A07C2B1DE", "0000001"]),
    ("HarborviewTaxHelp  - Consulting", "TAXHELP CONSULTING", ["HARBORVIEW"]),
    ("MAIN ST MARKET  SPRINGFIELD  IL                       US", "MAIN ST MARKET SPRINGFIELD",
     ["  US"]),
    ("POS PURCHASE 10/02 CORNER CAFE 0000", "CORNER CAFE", ["10/02"]),
    ("Xfer From ******0002 Sam Q Example car fund", "XFER FROM CAR FUND", ["SAM", "EXAMPLE"]),
])
def test_cleaning_keeps_the_merchant_and_drops_people_and_ids(raw, want, gone):
    out = clean_description(clean_bank_description(raw, TERMS))
    assert out.startswith(want), out
    for g in gone:
        assert g.strip() not in out.upper(), (g, out)


def test_cleaning_never_returns_nothing():
    assert clean_bank_description("******0001 12345678", TERMS) == "BANK ROW"


# ------------------------------------------------------------------ classifying


RULES = load_bank_rules(example_cards())


@pytest.mark.parametrize("amount, desc, bank_type, want", [
    # §4's table, one row each.
    (61245, "CHASE CREDIT CRD EPAY", None, ("transfer", "card_payment", "chase")),
    (25000, "DISCOVER E-PAYMENT", None, ("transfer", "card_payment_other", None)),
    (10000, "WEB XFER TO TRANSFER", None, ("transfer", "own_transfer", None)),
    (50000, "VANGUARD BUY INVESTMENT", None, ("transfer", "investment", None)),
    (4218, "MAIN ST MARKET", "debit card|point of sale withdrawal", ("purchase", None, None)),
    (15000, "CHECK", None, ("purchase", "check", None)),
    (15000, "RETURNED CHECK", "|returned check", ("purchase", "check", None)),
    (6000, "ATM WITHDRAWAL", None, ("purchase", "atm", None)),
    (4000, "VENMO PAYMENT", None, ("purchase", "p2p", None)),
    (600, "MONTHLY SERVICE FEE", None, ("fee", None, None)),
    (-1999, "ACME REFUND", None, ("refund", None, None)),
    (-240000, "EXAMPLE EMPLOYER PAYROLL", None, ("deposit", "income", None)),
    (-42, "DIVIDEND EARNED", None, ("deposit", "interest", None)),
    (-500, "CARD REWARDS REWARD", None, ("deposit", "reward", None)),
    (-12000, "STATE INCOME TAX", None, ("deposit", "tax_refund", None)),
    (-4000, "VENMO CASHOUT", None, ("deposit", "p2p", None)),
    (-2500, "REMOTE DEPOSIT CAPTURE", None, ("deposit", "other_in", None)),
    (-2500, "OVERDRAFT PROTECTION DEPOSIT", "deposit|overdraft protection deposit",
     ("transfer", "own_transfer", None)),
    # My own transfer's memo may name a card: still my own money moving, not a card payment.
    (-30000, "XFER FROM CHASE CARD BILL", None, ("transfer", "own_transfer", None)),
    # Money-in flows are skipped on money out: a tax payment is spending, not a refund.
    (30000, "STATE INCOME TAX", None, ("purchase", None, None)),
])
def test_classify_bank_row_per_spec_table(amount, desc, bank_type, want):
    c = classify_bank_row(amount, desc, bank_type, RULES)
    assert (c.type, c.flow_kind, c.issuer) == want


def test_classify_precedence_override_rule_patterns_export_sign():
    args = (61245, "CHASE CREDIT CRD EPAY", "transfer|withdrawal", RULES)
    assert classify_bank_row(*args, override=("purchase", None)).source == "override"
    assert classify_bank_row(*args, user_rule=("purchase", None)).source == "user_rule"
    assert classify_bank_row(*args).source == "bank_rule"
    assert classify_bank_row(100, "MYSTERY", "transfer|withdrawal", RULES).source == "export"
    assert classify_bank_row(100, "MYSTERY", None, RULES).source == "sign"
    assert classify_bank_row(-100, "MYSTERY", None, RULES, known_merchant=True).type == "refund"


def test_export_type_drops_numbers():
    assert export_type("", "Returned Check # 00000101") == "|returned check"
    assert export_type("Debit Card", "Point Of Sale Withdrawal") == \
        "debit card|point of sale withdrawal"


# ------------------------------------------------------------------ statement


def test_statement_reads_only_the_checking_section():
    st = parse_bank_statement(STATEMENT.read_text(), ["Example Bank Checking"])
    assert (st.period_start, st.period_end) == ("2026-10-01", "2026-10-31")
    assert (st.opening, st.closing) == (150000, 185083)
    assert st.deposits == 241734 + 42 and st.withdrawals == 206693
    assert st.math_delta == 0 and st.account_ending == "0003"


def test_statement_sections_and_endings():
    text = STATEMENT.read_text()
    savings = parse_bank_statement(text, ["Example Bank Personal Savings"])
    assert savings.closing == 250000 and savings.deposits == 25 and savings.math_delta == 0
    assert parse_bank_statement(text, ["example  bank checking"], "0003").closing == 185083
    with pytest.raises(ValueError, match="no section"):
        parse_bank_statement(text, ["Example Bank Checking"], "9999")
    with pytest.raises(ValueError, match="no section"):
        parse_bank_statement(text, ["Some Other Account"])


def test_statement_with_a_missing_figure_is_refused():
    text = STATEMENT.read_text().replace("Total Withdrawals", "Total Withdrawn", 1)
    with pytest.raises(ValueError, match="withdrawals"):
        parse_bank_statement(text, ["Example Bank Checking"])
