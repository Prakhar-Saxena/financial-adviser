"""Costco receipt transcripts and Costco ↔ card matching (SPEC §9.4, §12.3). Synthetic data."""

import json
from datetime import date

import pytest
from sqlalchemy import select

from fin.config import CONFIG_DIR, Settings, load_cards, load_taxonomy
from fin.db import models as m
from fin.db import session_factory, upgrade
from fin.db.seed import sync_reference
from fin.ingest.costco import parse_transcript, store_receipt
from fin.match.amazon import allocate_matched
from fin.match.costco import match_costco
from fin.review import ReviewSet

CARDS = load_cards(CONFIG_DIR / "cards.example.json")


def transcript(**over):
    doc = {
        "date": "2026-09-16", "warehouse": "Springfield #000",
        "items": [
            {"item_number": "1234567", "description": "KS ORG EGGS 24CT", "amount": "9.99",
             "taxable": False},
            {"item_number": "7654321", "description": "KS PAPER TOWEL", "amount": "24.99",
             "taxable": True},
            {"item_number": None, "description": "INSTANT SAVINGS", "amount": "-5.00",
             "taxable": False, "is_instant_savings": True, "applies_to_item_number": "7654321"},
        ],
        "subtotal": "29.98", "tax": "1.20", "total": "31.18",
        "tender": {"card_type": "VI", "card_ending": "0000"},
    }
    doc.update(over)
    return doc


def test_transcript_adds_up_with_instant_savings():
    r = parse_transcript(transcript())
    assert r.problems == []
    towel = next(i for i in r.items if i.item_number == "7654321")
    assert towel.discount_cents == 500 and len(r.items) == 2
    assert (r.subtotal, r.tax, r.total, r.card_type) == (2998, 120, 3118, "Visa")


def test_transcript_that_does_not_add_up():
    r = parse_transcript(transcript(total="31.19"))
    assert r.problems and "subtotal + tax" in r.problems[0]
    r = parse_transcript(transcript(subtotal="30.00", total="31.20"))
    assert any("items minus savings" in p for p in r.problems)


def test_transcript_schema_enforced():
    with pytest.raises(ValueError):
        parse_transcript(transcript(total="31.2"))


@pytest.fixture
def S(tmp_path):
    db = tmp_path / "t.sqlite"
    upgrade(db)
    Session = session_factory(db)
    with Session() as s:
        sync_reference(s, CARDS, load_taxonomy())
    return Session


def add_txn(s, imp, merch, amount, post, key):
    t = m.Transaction(card_id="citi_costco", post_date=post, txn_date=post, amount_cents=amount,
                      description_raw=merch.name, description_clean=merch.name.upper(),
                      type="purchase", dedupe_key=key, import_id=imp.id, merchant_id=merch.id,
                      person_id="self")
    s.add(t)
    s.flush()
    return t


def test_matching_window_gas_exemption_and_split(S):
    with S() as s:
        imp = m.Import(source="costco", kind="receipts", file_path="x", sha256="b" * 64,
                       status="ok")
        s.add(imp)
        whse = m.Merchant(name="Costco", merchant_group="costco_warehouse")
        gas = m.Merchant(name="Costco Gas", merchant_group="costco_gas")
        s.add_all([whse, gas])
        s.flush()
        r = parse_transcript(transcript())
        store_receipt(s, r, imp, "raw/x.json")
        for i in s.scalars(select(m.OrderItem)):
            i.category_id = "groceries" if "EGGS" in i.title_raw else "household.cleaning_and_paper"
        matched = add_txn(s, imp, whse, 3118, "2026-09-17", "k1")   # +1 day: in window
        add_txn(s, imp, whse, 3118, "2026-09-25", "k2")             # same total, out of window
        add_txn(s, imp, gas, 4000, "2026-09-18", "k3")              # gas: no receipt expected
        s.commit()

        reviews = ReviewSet(s)
        res = match_costco(s, CARDS, Settings(), reviews, date(2026, 10, 7))
        allocate_matched(s)
        s.commit()
        assert res.matched == 1 and res.unmatched_charges == 1 and res.unmatched_receipts == 0
        mt = s.scalar(select(m.Match).where(m.Match.transaction_id == matched.id))
        assert mt and mt.method == "exact"
        allocs = {a.category_id: a.amount_cents for a in s.scalars(
            select(m.Allocation).where(m.Allocation.transaction_id == matched.id))}
        # Paid prices: eggs 9.99, towels 24.99 - 5.00 = 19.99; tax spread proportionally.
        assert sum(allocs.values()) == 3118
        assert allocs == {"groceries": 1039, "household.cleaning_and_paper": 2079}
        kinds = {(k, ref) for k, ref in s.execute(select(m.ReviewItem.kind, m.ReviewItem.ref_id))}
        assert ("unmatched_charge", str(matched.id)) not in kinds
        assert not any(k == "unmatched_charge" and ref == "3" for k, ref in kinds)  # gas exempt


def test_unmatched_receipt_after_grace(S):
    with S() as s:
        imp = m.Import(source="costco", kind="receipts", file_path="x", sha256="c" * 64,
                       status="ok")
        s.add(imp)
        s.flush()
        store_receipt(s, parse_transcript(transcript()), imp, "raw/x.json")
        s.commit()
        res = match_costco(s, CARDS, Settings(), ReviewSet(s), date(2026, 9, 18))
        assert res.unmatched_receipts == 0  # still within the grace period
        res = match_costco(s, CARDS, Settings(), ReviewSet(s), date(2026, 10, 7))
        assert res.unmatched_receipts == 1


def test_receipt_importer_rejects_bad_math(tmp_path, S):
    from fin.ingest import load_parsers
    from fin.ingest.inbox import PARSERS

    load_parsers()
    parser = PARSERS[("costco", "receipt_transcript")]
    p = tmp_path / "costco_2026-09-16_1.json"
    p.write_text(json.dumps(transcript(total="99.99")))
    with S() as s:
        imp = m.Import(source="costco", kind="receipts", file_path=str(p), sha256="d" * 64,
                       status="ok")
        s.add(imp)
        s.flush()
        out = parser(s, p, {"period_start": None}, imp)
        assert out.row_count == 0 and "doesn't add up" in out.problems[0]
        assert s.scalar(select(m.Order.id)) is None
