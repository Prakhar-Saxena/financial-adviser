"""Golden tests (fixture -> expected JSON) and parser unit tests (SPEC §18)."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from fin.ingest.amazon import parse_invoice, split_links
from fin.ingest.bank_csv import ParsedTxn, coverage_problem, dedupe_keys, parse_chase_csv
from fin.ingest.statements import parse_chase_statement, undouble
from fin.normalize.merchants import clean_description
from fin.normalize.signs import classify, to_cents
from fin.reconcile.statements import math_delta
from tests.conftest import FIXTURES

GOLDEN = Path(__file__).resolve().parents[1] / "golden"


def golden(name):
    return json.loads((GOLDEN / name).read_text())


def test_golden_chase_csv():
    rows, pending = parse_chase_csv((FIXTURES / "chase/activity_prime_visa.csv").read_text())
    assert {"pending_dropped": pending, "rows": [r.as_dict() for r in rows]} == golden(
        "chase_activity_prime_visa.json")


def test_golden_chase_statement():
    st = parse_chase_statement(
        (FIXTURES / "chase/statement_prime_visa_2026-09-21.txt").read_text())
    assert asdict(st) == golden("chase_statement_prime_visa_2026-09-21.json")
    assert math_delta(st.summary()) == 0
    purchases = sum(ln.amount_cents for ln in st.lines if ln.section == "PURCHASE")
    assert purchases == st.purchases  # lines add up to the summary


@pytest.mark.parametrize("name", ["invoice_one_item", "invoice_three_items"])
def test_golden_amazon(name):
    inv = parse_invoice(*split_links((FIXTURES / f"amazon/{name}.txt").read_text()))
    assert asdict(inv) == golden(f"amazon_{name}.json")
    assert inv.check() == []


# ------------------------------------------------------------------ signs


@pytest.mark.parametrize("raw,typ,amount,expect", [
    ("-53.49", "Sale", "AMAZON MKTPL*597SC48P1", ("purchase", 5349)),
    ("567.70", "Payment", "Payment Thank You-Mobile", ("payment", -56770)),
    ("12.00", "Return", "AMAZON MKTPL*ABC123", ("refund", -1200)),
    ("-39.00", "Fee", "LATE FEE", ("fee", 3900)),
    ("-4.12", "Fee", "PURCHASE INTEREST CHARGE", ("interest", 412)),
    ("-95.00", "Fee", "ANNUAL MEMBERSHIP FEE", ("fee", 9500)),
    ("1.00", "Adjustment", "BALANCE ADJUSTMENT", ("adjustment", -100)),
    ("25.00", "Sale", "REDEMPTION CREDIT", ("reward", -2500)),
])
def test_chase_signs_and_types(raw, typ, amount, expect):
    cents = -to_cents(raw)  # Chase: negative = purchase
    assert (classify(cents, amount, typ)[0], cents) == expect


def test_unexpected_money_in_needs_review():
    assert classify(-500, "SOMETHING ODD", "Sale") == ("credit", True)


def test_to_cents():
    assert to_cents("$1,261.84") == 126184 and to_cents("-$567.70") == -56770
    assert to_cents("(12.50)") == -1250
    with pytest.raises(ValueError):
        to_cents("1.234")


# ------------------------------------------------------------------ dedupe


def txn(post, amount, desc="STARBUCKS"):
    return ParsedTxn(post, post, amount, desc, desc, "purchase", False)


def test_dedupe_identical_same_day_rows_survive():
    keys = dedupe_keys("c", [txn("2026-09-01", 450), txn("2026-09-01", 450)])
    assert len(set(keys)) == 2


def test_dedupe_overlap_keeps_max_count():
    first = dedupe_keys("c", [txn("2026-09-01", 450), txn("2026-09-02", 100)])
    second = dedupe_keys("c", [txn("2026-09-02", 100), txn("2026-09-01", 450),
                               txn("2026-09-01", 450)])
    assert set(first) < set(second) and len(set(second) - set(first)) == 1


def test_dedupe_ignores_cleaning_changes():
    a = dedupe_keys("c", [ParsedTxn("2026-09-01", "2026-09-01", 1, "sq *joe", "X", "purchase",
                                    False)])
    b = dedupe_keys("c", [ParsedTxn("2026-09-01", "2026-09-01", 1, "SQ  *JOE", "Y", "purchase",
                                    False)])
    assert a == b


def test_coverage_problem():
    rows = [txn("2026-09-27", 1)]
    assert "2026-09-27" in coverage_problem(rows, "2026-07-06")
    assert coverage_problem(rows, "2026-09-20") is None


def test_pending_rows_dropped():
    csv = ("Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
           "10/05/2026,,PENDING THING,Shopping,Sale,-1.00,\n"
           "10/04/2026,10/05/2026,POSTED THING,Shopping,Sale,-2.00,\n")
    rows, pending = parse_chase_csv(csv)
    assert pending == 1 and [r.description_raw for r in rows] == ["POSTED THING"]


def test_not_chase_csv():
    with pytest.raises(ValueError):
        parse_chase_csv("Date,Description,Amount\n")


# --------------------------------------------------------------- statement


def statement_text(activity: str, closing="01/05/27", opening="12/06/26") -> str:
    return (
        "ACCOUNT SUMMARY\nPrevious Balance $0.00\nPayment, Credits $0.00\nPurchases +$30.00\n"
        "Cash Advances $0.00\nBalance Transfers $0.00\nFees Charged $0.00\n"
        "Interest Charged $0.00\nNew Balance $30.00\n"
        f"Opening/Closing Date {opening} - {closing}\nPayment Due Date: 02/01/27\n"
        f"AACCCCOOUUNNTT AACCTTIIVVIITTYY\n{activity}\nTotals Year-to-Date\n"
    )


def test_sub_dollar_amounts():
    st = parse_chase_statement(statement_text("PURCHASE\n12/28 CITY PARKING SPRINGFIELD IL .60\n"
                                              "12/29 SHOP 29.40"))
    assert [ln.amount_cents for ln in st.lines] == [60, 2940]


def test_year_inference_across_new_year():
    st = parse_chase_statement(statement_text(
        "PURCHASE\n12/28 SHOP A 10.00\n01/03 SHOP B 20.00"))
    assert [ln.txn_date for ln in st.lines] == ["2026-12-28", "2027-01-03"]


def test_cardholder_sections():
    st = parse_chase_statement(statement_text(
        "PURCHASE\n12/28 SHOP A 10.00\n"
        "SAM Q EXAMPLE TRANSACTIONS THIS CYCLE (CARD 0000) $20.00\n01/03 SHOP B 20.00"))
    assert [ln.cardholder_name for ln in st.lines] == [None, "SAM Q EXAMPLE"]


def test_statement_missing_summary_raises():
    with pytest.raises(ValueError):
        parse_chase_statement("ACCOUNT SUMMARY\nPrevious Balance $1.00\n")


def test_undouble():
    assert undouble("AACCCCOOUUNNTT AACCTTIIVVIITTYY") == "ACCOUNT ACTIVITY"
    assert undouble("BOOKKEEPER 2026") == "BOOKKEEPER 2026"


# ------------------------------------------------------------------ amazon


def test_amazon_quantity_and_missing_total():
    text = ("Order placed March 1, 2026 Order # 111-0000000-0000000\n"
            "Visa••••0000 Item(s) Subtotal: $10.00\nGrand Total: $10.00\n"
            "Delivered March 2\nQty: 2 Widget pack\nSold by: Acme\n$10.00\n")
    inv = parse_invoice(text, [])
    (item,) = inv.items
    assert (item.quantity, item.unit_price_cents, item.title) == (2, 500, "Widget pack")
    assert inv.payment_method == "Visa"
    with pytest.raises(ValueError):
        parse_invoice("Order placed March 1, 2026 Order # 111-0000000-0000000\n", [])


def test_amazon_check_flags_mismatch():
    text = ("Order placed March 1, 2026 Order # 111-0000000-0000000\n"
            "Item(s) Subtotal: $10.00\nGrand Total: $12.00\n"
            "Delivered March 2\nWidget\nSold by: Acme\n$10.00\n")
    assert parse_invoice(text, []).check()


# ----------------------------------------------------------------- cleaner


@pytest.mark.parametrize("raw,clean", [
    ("AMAZON MKTPL*597SC48P1", "AMAZON MKTPL"),
    ("Amazon.com*5Q1P87LA2 Amzn.com/bill WA", "AMAZON.COM"),
    ("MICROSOFT*MICROSOFT 36 MICROSOFT.COM WA", "MICROSOFT"),
    ("SQ *BLUE BOTTLE COFFEE 415-555-1212", "BLUE BOTTLE COFFEE"),
    ("TST* JOES PIZZA #123 SEATTLE WA", "JOES PIZZA SEATTLE"),
    ("WHOLEFDS MKT 10234", "WHOLEFDS MKT"),
    ("COSTCO WHSE #0123", "COSTCO WHSE"),
    ("NETFLIX.COM", "NETFLIX.COM"),
    ("WF *WAYFAIR1234567890 866-555-0100 MA", "WF WAYFAIR"),
    ("WF* WAYFAIR9876543210 WAYFAIR.COM MA", "WF WAYFAIR"),
    ("SP TRANSITSHOP 121-12345678 PA", "TRANSITSHOP"),
    ("CBT*CLERK CR6 SHELBYVILLE IL", "CBT CLERK CR6 SHELBYVILLE"),
    ("FEDEX12345678 MEMPHIS TN", "FEDEX MEMPHIS"),
    ("HUDSON ST1234 SPRINGFIELD IL", "HUDSON ST1234 SPRINGFIELD"),
])
def test_clean_description(raw, clean):
    assert clean_description(raw) == clean


def test_amazon_quantity_line_and_derived_discounts():
    text = ("Order placed August 3, 2026 Order # 111-0000000-0000000\n"
            "Visa••••0000 Item(s) Subtotal: $41.26\n"
            "STREET $39.35 (Earns 5% back) Shipping & Handling: $2.99\n"
            "CITY Promotion applied:: -$4.13\nFree Shipping: -$2.99\n"
            "Total before tax: $37.13\nEstimated tax to be $2.22\nGrand Total: $39.35\n"
            "Delivered August 6\nCharger 2-Pack\nSold by: Zhihui1\n"
            "Return window closed on September 5, 2026\n$20.63\n2\nBack to top\n")
    inv = parse_invoice(text, [])
    (item,) = inv.items
    assert (item.quantity, item.unit_price_cents, item.line_total_cents) == (2, 2063, 4126)
    assert inv.promotions == 712 and inv.check() == []


def test_golden_amazon_payments_page():
    from fin.ingest.amazon_payments import parse_payments_page

    es = parse_payments_page((FIXTURES / "amazon/payments_page.txt").read_text())
    assert [asdict(e) for e in es] == golden("amazon_payments_page.json")
    assert len(es) == 20 and all(e.order_ref for e in es)
    assert es[0].charge_date == "2026-10-05" and es[0].amount_cents == 2119
    assert {e.card_name for e in es} == {"Prime Visa"}


def test_payments_pending_skipped_and_refunds():
    from fin.ingest.amazon_payments import parse_payments_page

    text = ("Pending\nOctober 6, 2026\nPrime Visa ****0000 -$5.00\nOrder #114-0000000-0000001\n"
            "Completed\nOctober 5, 2026\nPrime Visa ****0000 +$12.00\n"
            "Order #114-0000000-0000002\nAMZN Mktp US\n$19.99\n"
            "Visa ****1111 -$7.50Order #114-0000000-0000003\n")
    es = parse_payments_page(text)
    assert [(e.amount_cents, e.kind, e.card_name, e.order_ref[-1]) for e in es] == [
        (-1200, "refund", "Prime Visa", "2"), (750, "charge", "Visa", "3")]



# -------------------------------------------------------------------- citi


def test_golden_citi_statement():
    from fin.ingest.statements import parse_citi_statement

    st = parse_citi_statement((FIXTURES / "citi/statement_costco_2026-09-25.txt").read_text())
    assert asdict(st) == golden("citi_statement_costco_2026-09-25.json")
    assert math_delta(st.summary()) == 0
    assert sum(ln.amount_cents for ln in st.lines if ln.section == "PURCHASE") == st.purchases
    holders = {ln.cardholder_name for ln in st.lines if ln.section == "PURCHASE"}
    assert holders == {"ALEX EXAMPLE", "SAM Q EXAMPLE"}  # two cardholders, redacted names


def test_golden_citi_csv():
    from fin.ingest.bank_csv import parse_citi_csv

    text = (FIXTURES / "citi/activity_since_last_statement.csv").read_text()
    rows, pending = parse_citi_csv(text)
    assert {"pending_dropped": pending, "rows": [r.as_dict() for r in rows]} == golden(
        "citi_activity.json")
    assert {r.member_name for r in rows} == {"ALEX EXAMPLE"}


def test_citi_csv_credits_and_pending():
    from fin.ingest.bank_csv import parse_citi_csv

    text = ("Status,Date,Description,Debit,Credit,Member Name\n"
            "Pending,10/01/2026,COSTCO WHSE #0000,10.00,,ALEX EXAMPLE\n"
            "Cleared,09/30/2026,ONLINE PAYMENT THANK YOU,,-991.21,ALEX EXAMPLE\n"
            "Cleared,09/29/2026,COSTCO WHSE #0000,,12.50,SAM Q EXAMPLE\n")
    rows, pending = parse_citi_csv(text)
    assert pending == 1
    assert [(r.amount_cents, r.type) for r in rows] == [(-99121, "payment"), (-1250, "credit")]


# -------------------------------------------------------------------- amex


def test_golden_amex_statement():
    from fin.ingest.statements import parse_amex_statement

    st = parse_amex_statement((FIXTURES / "amex/statement_gold_2026-09-15.txt").read_text())
    assert asdict(st) == golden("amex_statement_gold_2026-09-15.json")
    assert math_delta(st.summary()) == 0
    assert sum(ln.amount_cents for ln in st.lines if ln.section == "PURCHASE") == st.purchases
    money_in = -sum(ln.amount_cents for ln in st.lines
                    if ln.section in ("PAYMENTS AND OTHER CREDITS", "CREDITS"))
    assert money_in == st.payments + st.credits
    assert st.product_markers == ["GOLD CARD"]


def test_golden_amex_csv():
    from fin.ingest.bank_csv import parse_amex_csv

    rows, pending = parse_amex_csv((FIXTURES / "amex/activity_gold.csv").read_text())
    assert {"pending_dropped": pending, "rows": [r.as_dict() for r in rows]} == golden(
        "amex_activity_gold.json")
    assert any(r.type == "payment" and r.amount_cents < 0 for r in rows)


def test_amex_csv_minimal_columns():
    from fin.ingest.bank_csv import is_amex_csv, parse_amex_csv

    text = "Date,Description,Amount\n10/01/2026,UBER EATS help.uber.com CA,2.79\n"
    assert is_amex_csv(text)
    (row,), _ = parse_amex_csv(text)
    assert (row.amount_cents, row.type, row.member_name) == (279, "purchase", None)
    assert not is_amex_csv("Status,Date,Description,Debit,Credit,Member Name\n")
