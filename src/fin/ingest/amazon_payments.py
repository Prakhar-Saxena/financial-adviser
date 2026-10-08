"""Amazon "Your Payments → Transactions" pages (decided 2026-10-06; SPEC §9.3 extension).

Documented from the first real page (`cpe/yourpayments/transactions`, saved with
browser_pdf_save, redacted copy in tests/fixtures/amazon/payments_page.txt):
- A date heading ("October 5, 2026") applies to every entry below it until the next one.
- Each entry: "<card name> ****1234 -$21.19", then "Order #114-...", then the merchant
  ("AMZN Mktp US", "Amazon.com"). The amount and "Order #" can share a line. Money in
  (refunds) is "+$...".
- The page also carries noise: the cart sidebar ("$19.99", "Subtotal $67.97"), deal badges,
  nav and footer. Only "card ****NNNN ±$amount" lines count.
- Entries under a "Pending"/"In progress" heading are skipped; "Completed" ones are kept.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from fin.normalize.signs import to_cents

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE = re.compile(rf"^({MONTHS}) (\d{{1,2}}), (\d{{4}})\b")
CHARGE = re.compile(
    r"(?P<card>[A-Za-z][A-Za-z .&'-]*?)\s*\*{4}(?P<ending>\d{4,5})\s+(?P<sign>[-+])\$"
    r"(?P<amount>[\d,]+\.\d{2})"
)
ORDER = re.compile(r"Order #\s*([A-Z0-9]{3}-\d{7}-\d{7})")
PENDING = re.compile(r"^(Pending|In progress)\b", re.IGNORECASE)
COMPLETED = re.compile(r"^Completed\b")


@dataclass(frozen=True)
class PaymentEntry:
    charge_date: str
    card_name: str
    card_ending: str
    amount_cents: int  # charges positive, refunds negative
    order_ref: str | None

    @property
    def kind(self) -> str:
        return "charge" if self.amount_cents > 0 else "refund"


def parse_payments_page(text: str) -> list[PaymentEntry]:
    out: list[PaymentEntry] = []
    current_date: str | None = None
    pending = False
    pending_entry: dict | None = None

    def flush(order_ref: str | None) -> None:
        nonlocal pending_entry
        if pending_entry is not None:
            out.append(PaymentEntry(order_ref=order_ref, **pending_entry))
            pending_entry = None

    for raw in text.replace("\f", "\n").splitlines():
        line = raw.strip()
        if not line:
            continue
        if PENDING.match(line):
            pending = True
            continue
        if COMPLETED.match(line):
            pending = False
            continue
        d = DATE.match(line)
        if d:
            current_date = datetime.strptime(" ".join(d.groups()), "%B %d %Y").date().isoformat()
            continue
        c = CHARGE.search(line)
        if c and current_date and not pending:
            flush(None)
            cents = to_cents(c.group("amount"))
            pending_entry = {
                "charge_date": current_date,
                "card_name": " ".join(c.group("card").split()),
                "card_ending": c.group("ending"),
                # The page shows money out as "-$"; order_charges stores charges positive.
                "amount_cents": cents if c.group("sign") == "-" else -cents,
            }
        o = ORDER.search(line)
        if o and pending_entry is not None:
            flush(o.group(1))
    flush(None)
    return out


def dedupe_keys(entries: list[PaymentEntry]) -> list[str]:
    seen: Counter[tuple] = Counter()
    keys = []
    for e in entries:
        ident = (e.charge_date, e.amount_cents, e.order_ref, e.card_name, e.card_ending)
        idx = seen[ident]
        seen[ident] += 1
        keys.append(hashlib.sha256(f"amazon|{ident}|{idx}".encode()).hexdigest())
    return keys


def _register() -> None:
    from sqlalchemy import select

    from fin.db import models as m
    from fin.ingest.amazon import pdf_text_and_links
    from fin.ingest.inbox import ParseOutcome, register

    @register("amazon", "payment_transactions")
    def import_payments(s, path, entry, imp):
        text = path.read_text() if path.suffix == ".txt" else pdf_text_and_links(path)[0]
        entries = parse_payments_page(text)
        if not entries:
            raise ValueError("no payment transactions found on the page")
        keys = dedupe_keys(entries)
        existing = set(s.scalars(select(m.OrderCharge.dedupe_key).where(
            m.OrderCharge.dedupe_key.in_(keys))))
        orders = {o.external_id: o.id for o in s.scalars(select(m.Order).where(
            m.Order.merchant == "amazon"))}
        added = 0
        for e, key in zip(entries, keys, strict=True):
            if key in existing:
                continue
            s.add(m.OrderCharge(
                merchant="amazon", order_ref=e.order_ref, order_id=orders.get(e.order_ref),
                import_id=imp.id, charge_date=e.charge_date, amount_cents=e.amount_cents,
                card_name=e.card_name, card_ending=e.card_ending, kind=e.kind,
                dedupe_key=key,
            ))
            added += 1
        dates = [e.charge_date for e in entries]
        return ParseOutcome(row_count=added, period_start=min(dates), period_end=max(dates))


_register()
