"""Costco warehouse receipts (SPEC §9.4).

Receipts open in a dialog that `browser_pdf_save` doesn't capture (checked on the first
extension-mode run, 2026-10-07: the PDF has the page behind the dialog). So the agent writes a
transcript per receipt (`sync/receipt.schema.json`) plus a screenshot as evidence.

A transcript is accepted only when it adds up to the cent:
    sum(items, including negative instant-savings lines) == subtotal, subtotal + tax == total.
Otherwise nothing is stored and it becomes a parse_failure review item. (Matching then
requires the total to equal a card transaction, the spec's second condition.)
Instant-savings lines become `discount_cents` on the item they apply to (or on the order when
they name no item), so splits use the price actually paid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from fin.config import REPO_ROOT
from fin.normalize.signs import to_cents

SCHEMA = json.loads((REPO_ROOT / "sync" / "receipt.schema.json").read_text())
CARD_TYPES = {"VI": "Visa", "MC": "Mastercard", "AX": "American Express", "DS": "Discover"}


@dataclass
class ReceiptItem:
    line_no: int
    item_number: str | None
    description: str
    amount_cents: int
    taxable: bool
    discount_cents: int = 0


@dataclass
class Receipt:
    date: str
    warehouse: str
    items: list[ReceiptItem]
    order_discount_cents: int
    subtotal: int
    tax: int
    total: int
    card_type: str | None
    card_ending: str | None
    problems: list[str] = field(default_factory=list)

    @property
    def external_id(self) -> str:
        return f"{self.date}|{self.warehouse}|{self.total}"


def parse_transcript(doc: dict) -> Receipt:
    errors = [e.message for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc)]
    if errors:
        raise ValueError(f"receipt transcript doesn't match the schema: {errors[:3]}")
    items: list[ReceiptItem] = []
    savings: list[tuple[str | None, int]] = []
    for raw in doc["items"]:
        cents = to_cents(raw["amount"])
        if raw.get("is_instant_savings") or cents < 0:
            savings.append((raw.get("applies_to_item_number"), -cents if cents < 0 else cents))
            continue
        items.append(ReceiptItem(len(items) + 1, raw.get("item_number"), raw["description"],
                                 cents, raw["taxable"]))
    order_discount = 0
    for target, amount in savings:
        item = next((i for i in items if target and i.item_number == target), None)
        if item:
            item.discount_cents += amount
        else:
            order_discount += amount
    tender = doc["tender"]
    r = Receipt(
        date=doc["date"], warehouse=" ".join(doc["warehouse"].split()), items=items,
        order_discount_cents=order_discount, subtotal=to_cents(doc["subtotal"]),
        tax=to_cents(doc["tax"]), total=to_cents(doc["total"]),
        card_type=CARD_TYPES.get((tender.get("card_type") or "").upper(), tender.get("card_type")),
        card_ending=tender.get("card_ending"),
    )
    lines = sum(i.amount_cents - i.discount_cents for i in items) - order_discount
    if lines != r.subtotal:
        r.problems.append(f"items minus savings {lines} != subtotal {r.subtotal}")
    if r.subtotal + r.tax != r.total:
        r.problems.append(f"subtotal + tax {r.subtotal + r.tax} != total {r.total}")
    return r


def store_receipt(s, r: Receipt, imp, raw_path: str) -> bool:
    from sqlalchemy import select

    from fin.db import models as m

    if s.scalar(select(m.Order.id).where(m.Order.merchant == "costco",
                                         m.Order.external_id == r.external_id)):
        return False
    order = m.Order(
        merchant="costco", channel="warehouse", external_id=r.external_id, import_id=imp.id,
        order_date=r.date, location=r.warehouse, subtotal=r.subtotal, tax=r.tax,
        discounts=r.order_discount_cents + sum(i.discount_cents for i in r.items),
        total=r.total, payment_method=r.card_type, payment_card_ending=r.card_ending,
        raw_path=raw_path,
    )
    s.add(order)
    s.flush()
    for i in r.items:
        s.add(m.OrderItem(order_id=order.id, line_no=i.line_no, sku=i.item_number,
                          title_raw=i.description, quantity=1, unit_price_cents=i.amount_cents,
                          line_total_cents=i.amount_cents, discount_cents=i.discount_cents,
                          taxable=i.taxable))
    return True


def _register() -> None:
    from fin.ingest.inbox import ParseOutcome, register

    @register("costco", "receipt_transcript")
    def import_transcript(s, path: Path, entry, imp):
        r = parse_transcript(json.loads(path.read_text()))
        if r.problems:
            # Not accepted (§9.4): keep the file as evidence, store nothing.
            return ParseOutcome(row_count=0, period_start=r.date, period_end=r.date,
                                problems=["transcript doesn't add up: " + "; ".join(r.problems)])
        stored = store_receipt(s, r, imp, imp.file_path)
        return ParseOutcome(row_count=len(r.items) if stored else 0, period_start=r.date,
                            period_end=r.date)


_register()
