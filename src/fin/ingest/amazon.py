"""Amazon printable invoice parser (SPEC §9.3).

Documented from tests/fixtures/amazon/invoice_*.txt
(`gp/css/summary/print.html?orderID=...`, saved with browser_pdf_save):
- "Order placed October 3, 2026 Order # 114-...".
- Three columns flattened into lines: Ship to | Payment method | Order Summary. Summary figures
  ("Item(s) Subtotal:", "Shipping & Handling:", "Total before tax:", "Estimated tax to be
  collected:", "Grand Total:") are found by label anywhere in the text. The tax label can wrap.
- Payment method: "<Card name>••••1234" (one card seen so far). The invoice does NOT list
  individual charges (dates and amounts); it links to "View related transactions" instead.
  Charges come from the card statement's "Order Number" lines.
- Items: shipment status lines ("Delivered ...", "Your package ..."), then the title (may
  wrap), "Sold by: <seller>", "Return or replace items: ..." / "Return window closed ...",
  then the unit "$price". When the quantity is more than 1 it follows on its own line ("4").
- Discounts come under many labels ("Free Shipping: -$2.99", "Promotion applied:: -$4.13",
  "Exclusive Promotion -$3.20"), so they are derived: subtotal + shipping − total before tax.
- ASINs: link targets `/dp/<ASIN>?ref_=ppx_printOD_..._asin_title_<item index>_0`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fin.normalize.signs import to_cents

ORDER = re.compile(r"Order placed\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s+Order #\s*(\d{3}-\d{7}-\d{7})")
# Anchored on card names, because the ship-to name from the next column shares the line.
PAYMENT = re.compile(
    r"(?P<name>(?:(?:Prime|Amazon|Chase|Citi|Costco|Capital One|Apple)\s+)?"
    r"(?:Visa|Mastercard|MasterCard|American Express|Amex|Discover|Store Card|Card))"
    r"\s*(?:••••|\*{4}|ending in\s*)(?P<ending>\d{4,5})"
)
MONEY = r"-?\$[\d,]+\.\d{2}"
SUMMARY = {
    "subtotal": r"Item\(s\) Subtotal:",
    "shipping": r"Shipping & Handling:",
    "total_before_tax": r"Total before tax:",
    "tax": r"Estimated tax to be\s+(?:collected:)?",
    "grand_total": r"Grand Total:",
    "promotions": r"(?:Promotion|Your Coupon Savings|Discount)s?(?: Applied)?:",
    "gift_card": r"Gift Card Amount:",
    "rewards": r"(?:Rewards Points|Amazon Points|Reward Points)(?: Applied)?:",
}
STATUS_LINE = re.compile(
    r"^(Delivered\b|Arriving\b|Shipped\b|Not yet shipped|Your package\b|Package was\b|"
    r"Out for delivery|Return or replace items|Return items|Return window|Back to top$|"
    r"Order Summary$|View related transactions|United States$|collected:$|Ship to\b|"
    r"Refunded$|Refund issued|Replacement ordered)",
    re.IGNORECASE,
)
SOLD_BY = re.compile(r"^Sold by:\s*(.+)$")
PRICE_LINE = re.compile(rf"^({MONEY})$")
QTY = re.compile(r"^(?:Qty:\s*(\d+)\s*|(\d+)\s+of\s+)", re.IGNORECASE)
ASIN_LINK = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})\b[^\s]*?asin_title_(\d+)_\d+")
ASIN_ANY = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})\b")


@dataclass
class InvoiceItem:
    line_no: int
    title: str
    seller: str | None
    quantity: int
    line_total_cents: int
    unit_price_cents: int
    asin: str | None = None


@dataclass
class Invoice:
    order_id: str
    order_date: str
    payment_method: str | None
    payment_card_ending: str | None
    subtotal: int | None
    shipping: int
    tax: int
    promotions: int
    gift_card: int
    rewards: int
    grand_total: int
    items: list[InvoiceItem] = field(default_factory=list)

    def check(self) -> list[str]:
        """Internal consistency: items add up to the subtotal, the subtotal to the total."""
        problems = []
        items = sum(i.line_total_cents for i in self.items)
        if self.subtotal is not None and items != self.subtotal:
            problems.append(f"items sum {items} != subtotal {self.subtotal}")
        if self.subtotal is not None:
            expect = (self.subtotal + self.shipping + self.tax - self.promotions
                      - self.gift_card - self.rewards)
            if expect != self.grand_total:
                problems.append(f"subtotal+shipping+tax-discounts {expect} != grand total "
                                f"{self.grand_total}")
        return problems


def split_links(text: str) -> tuple[str, list[str]]:
    """Fixtures (redact.py) append "\\f[LINKS]" with one "page N: <uri>" per line."""
    if "[LINKS]" not in text:
        return text, []
    body, links = text.split("[LINKS]", 1)
    return body, [ln.split(": ", 1)[-1] for ln in links.splitlines() if ln.strip()]


def _amount(text: str, label: str) -> int | None:
    m = re.search(rf"{label}\s*({MONEY})", text)
    return abs(to_cents(m.group(1))) if m else None


def parse_invoice(text: str, links: list[str]) -> Invoice:
    m = ORDER.search(text)
    if not m:
        raise ValueError("not an Amazon invoice: no 'Order placed ... Order #' line")
    order_date = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    pm = PAYMENT.search(text)
    amounts = {k: _amount(text, v) for k, v in SUMMARY.items()}
    if amounts["grand_total"] is None:
        raise ValueError("Amazon invoice without a Grand Total")
    promotions = amounts["promotions"] or 0
    if amounts["subtotal"] is not None and amounts["total_before_tax"] is not None:
        promotions = (amounts["subtotal"] + (amounts["shipping"] or 0)
                      - amounts["total_before_tax"])
    inv = Invoice(
        order_id=m.group(2), order_date=order_date,
        payment_method=" ".join(pm.group("name").split()) if pm else None,
        payment_card_ending=pm.group("ending") if pm else None,
        subtotal=amounts["subtotal"], shipping=amounts["shipping"] or 0,
        tax=amounts["tax"] or 0, promotions=promotions,
        gift_card=amounts["gift_card"] or 0, rewards=amounts["rewards"] or 0,
        grand_total=amounts["grand_total"],
    )
    inv.items = _items(text)
    _attach_asins(inv.items, links)
    return inv


def _items(text: str) -> list[InvoiceItem]:
    lines = [ln.strip() for ln in text.replace("\f", "\n").splitlines() if ln.strip()]
    # Items start after the summary block, at the first shipment status line.
    start = next((i for i, ln in enumerate(lines) if ln.startswith("Grand Total")), 0) + 1
    items: list[InvoiceItem] = []
    title: list[str] = []
    seller: str | None = None
    awaiting_qty = False
    for ln in lines[start:]:
        if awaiting_qty and re.fullmatch(r"\d{1,3}", ln):
            item = items[-1]
            item.quantity = int(ln)
            item.unit_price_cents = item.line_total_cents
            item.line_total_cents *= item.quantity
            awaiting_qty = False
            continue
        awaiting_qty = False
        sold = SOLD_BY.match(ln)
        if sold:
            seller = sold.group(1).strip()
            continue
        price = PRICE_LINE.match(ln)
        if price and title:
            text_title = " ".join(title)
            qty = 1
            q = QTY.match(text_title)
            if q:
                qty = int(q.group(1) or q.group(2))
                text_title = text_title[q.end():]
            total = to_cents(price.group(1))
            items.append(InvoiceItem(
                line_no=len(items) + 1, title=text_title.strip(), seller=seller,
                quantity=qty, line_total_cents=total,
                unit_price_cents=total // qty if total % qty == 0 else total,
            ))
            title, seller = [], None
            awaiting_qty = qty == 1
            continue
        if STATUS_LINE.match(ln) or ln.startswith(("Conditions of Use", "©")):
            continue
        if seller is None:
            title.append(ln)
    return items


def _attach_asins(items: list[InvoiceItem], links: list[str]) -> None:
    by_index: dict[int, str] = {}
    ordered: list[str] = []
    for uri in links:
        m = ASIN_LINK.search(uri)
        if m:
            by_index.setdefault(int(m.group(2)), m.group(1))
        a = ASIN_ANY.search(uri)
        if a and a.group(1) not in ordered:
            ordered.append(a.group(1))
    for i, item in enumerate(items):
        item.asin = by_index.get(i) or (ordered[i] if len(ordered) == len(items) else None)


def pdf_text_and_links(path: Path) -> tuple[str, list[str]]:
    import pdfplumber

    pages, links = [], []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
            links += [h["uri"] for h in page.hyperlinks or [] if h.get("uri")]
    return "\f".join(pages), links


# ------------------------------------------------------------------ import


def store_invoice(s, inv: Invoice, imp, raw_path: str) -> tuple[bool, list[str]]:
    """Insert the order and its items. Returns (stored, problems). Idempotent per order id."""
    from sqlalchemy import select

    from fin.db import models as m

    if s.scalar(select(m.Order.id).where(m.Order.merchant == "amazon",
                                         m.Order.external_id == inv.order_id)):
        return False, []
    problems = inv.check()
    order = m.Order(
        merchant="amazon", channel="online", external_id=inv.order_id, import_id=imp.id,
        order_date=inv.order_date, subtotal=inv.subtotal, shipping=inv.shipping, tax=inv.tax,
        discounts=inv.promotions, gift_card_applied=inv.gift_card + inv.rewards,
        total=inv.grand_total, payment_method=inv.payment_method,
        payment_card_ending=inv.payment_card_ending, raw_path=raw_path,
    )
    s.add(order)
    s.flush()
    for it in inv.items:
        s.add(m.OrderItem(
            order_id=order.id, line_no=it.line_no, sku=it.asin, seller=it.seller,
            title_raw=it.title, quantity=it.quantity, unit_price_cents=it.unit_price_cents,
            line_total_cents=it.line_total_cents,
        ))
    return True, problems


def _register() -> None:
    from fin.ingest.inbox import ParseOutcome, register

    @register("amazon", "order_invoice")
    def import_amazon_invoice(s, path, entry, imp):
        text, links = (split_links(path.read_text()) if path.suffix == ".txt"
                       else pdf_text_and_links(path))
        inv = parse_invoice(text, links)
        problems = []
        if entry.get("note") and re.search(r"\d{3}-\d{7}-\d{7}", entry["note"]) and \
                inv.order_id not in entry["note"]:
            problems.append(f"manifest note names a different order than the invoice "
                            f"({inv.order_id}); using the invoice")
        stored, more = store_invoice(s, inv, imp, imp.file_path)
        return ParseOutcome(row_count=len(inv.items) if stored else 0,
                            period_start=inv.order_date, period_end=inv.order_date,
                            problems=problems + more)


_register()
