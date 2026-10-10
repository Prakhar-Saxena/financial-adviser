"""Mock dashboard screenshots for the README.

Builds a throwaway database of fictional data (the Example household from
config/cards.example.json, made-up merchants and products), serves the dashboard from it
on 127.0.0.1, screenshots each page with headless Chrome, then stops. Nothing touches the
real data dir or config/cards.json.

    uv run python scripts/make_screenshots.py          # writes docs/screenshots/*.png
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "screenshots"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PORT = 8767
MONTHS = ["2026-07", "2026-08", "2026-09"]
TAX = 0.06

# name, merchant group, category, cards, people, (min, max) dollars, times per month
MERCHANTS = [
    (
        "Maple Street Grocers",
        None,
        "groceries.fresh",
        ["chase_freedom_unlimited", "amex_gold"],
        ["self", "spouse"],
        (38, 145),
        4,
    ),
    (
        "Whole Foods Market",
        "whole_foods",
        "groceries.fresh",
        ["chase_prime_visa"],
        ["self"],
        (28, 95),
        2,
    ),
    ("Bluebird Coffee", None, "dining.coffee", ["amex_gold"], ["self", "spouse"], (4, 9), 6),
    (
        "Corner Bistro",
        None,
        "dining.restaurants",
        ["chase_sapphire_reserve", "amex_gold"],
        ["self", "spouse"],
        (42, 128),
        3,
    ),
    ("Golden Wok", None, "dining.restaurants", ["amex_gold"], ["spouse"], (24, 58), 2),
    (
        "QuickBite Delivery",
        None,
        "dining.delivery",
        ["chase_freedom_unlimited"],
        ["self"],
        (18, 46),
        2,
    ),
    (
        "Northside Fuel",
        None,
        "transportation.fuel",
        ["chase_freedom_unlimited", "chase_sapphire_reserve"],
        ["self", "spouse"],
        (34, 62),
        3,
    ),
    (
        "City Parking",
        None,
        "transportation.parking_and_tolls",
        ["chase_sapphire_reserve"],
        ["self"],
        (8, 26),
        2,
    ),
    (
        "Zoom Rides",
        None,
        "travel.rideshare_and_taxi",
        ["chase_sapphire_reserve"],
        ["self"],
        (12, 36),
        2,
    ),
    (
        "Riverside Pharmacy",
        None,
        "health.pharmacy_and_otc",
        ["chase_freedom_unlimited"],
        ["father", "mother"],
        (12, 64),
        2,
    ),
    (
        "Lakeside Dental",
        None,
        "health.medical_and_dental",
        ["chase_freedom_unlimited"],
        ["mother"],
        (150, 320),
        1,
    ),
    ("Threadline Apparel", None, "clothing.clothing", ["amex_gold"], ["spouse"], (38, 115), 1),
    ("Paws & Claws", None, "pets", ["chase_freedom_unlimited"], ["self"], (28, 72), 1),
    (
        "Hometown Hardware",
        None,
        "home.improvement_and_tools",
        ["chase_freedom_unlimited"],
        ["father"],
        (18, 92),
        1,
    ),
    ("Cinema 8", None, "entertainment.events", ["chase_sapphire_reserve"], ["self"], (24, 46), 1),
]
FIXED = [  # monthly bills: name, category, card, dollars
    ("StreamBox", "entertainment.streaming", "amex_platinum", 15.99),
    ("TuneCloud", "entertainment.streaming", "amex_platinum", 10.99),
    ("Metro Wireless", "bills.phone", "chase_freedom_unlimited", 85.00),
    ("Brightline Internet", "bills.internet", "chase_freedom_unlimited", 70.00),
    ("Harbor Electric", "bills.utilities", "chase_freedom_unlimited", None),
]
TRIP = [("Skyway Airlines", "travel.air", 389, 612), ("Seaside Inn", "travel.lodging", 412, 540)]

AMAZON = [  # title, category, dollars
    ("Paper towels, 12 rolls", "household.cleaning_and_paper", 24.99),
    ("USB-C charging cable, 2-pack", "electronics.accessories", 12.99),
    ("Wireless earbuds", "electronics.devices", 49.99),
    ("Insulated water bottle", "household.kitchen", 19.99),
    ("Dog chew toys, 3-pack", "pets", 14.49),
    ("Paperback novel", "digital.books_and_media", 13.99),
    ("Vitamin D3 gummies", "health.vitamins_and_supplements", 15.99),
    ("LED desk lamp", "home.furniture_and_decor", 29.99),
    ("Shampoo, 2-pack", "personal_care.hair", 17.98),
    ("Storage bins, set of 4", "household.storage", 27.99),
    ("Kids' sneakers", "clothing.shoes", 34.99),
    ("Cooperative board game", "entertainment.games_and_hobbies", 24.99),
    ("Printer paper, 500 sheets", "office_school", 9.99),
    ("Whole bean coffee, 2 lb", "groceries.beverages", 21.99),
]
COSTCO = [  # title, category, dollars, taxable
    ("Rotisserie chicken", "groceries.fresh", 4.99, False),
    ("Organic eggs, 24 ct", "groceries.fresh", 8.99, False),
    ("Strawberries, 2 lb", "groceries.fresh", 6.99, False),
    ("Sparkling water, 35 pk", "groceries.beverages", 13.99, False),
    ("Mixed nuts, 2.5 lb", "groceries.snacks", 18.99, False),
    ("Frozen berry blend, 4 lb", "groceries.frozen", 12.99, False),
    ("Olive oil, 2 L", "groceries.pantry", 21.99, False),
    ("Paper towels, 12 rolls", "household.cleaning_and_paper", 22.99, True),
    ("Laundry detergent", "household.cleaning_and_paper", 24.99, True),
    ("Toothpaste, 5-pack", "personal_care.toiletries", 14.99, True),
    ("Multivitamin, 365 ct", "health.vitamins_and_supplements", 16.99, True),
    ("Athletic socks, 6 pairs", "clothing.clothing", 15.99, True),
    ("Dog food, 40 lb", "pets", 44.99, True),
    ("Coffee pods, 100 ct", "groceries.beverages", 39.99, False),
]


def cents(d: float) -> int:
    return round(d * 100)


def split(total: int, weights: list[int]) -> list[int]:
    """Largest-remainder allocation of `total` in proportion to `weights`."""
    w = sum(weights)
    raw = [total * x / w for x in weights]
    out = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)[
        : total - sum(out)
    ]:
        out[i] += 1
    return out


def build(db_file: Path) -> None:
    from sqlalchemy import select

    from fin.config import CONFIG_DIR, load_cards, load_taxonomy
    from fin.db import models as m
    from fin.db import session_factory, upgrade
    from fin.db.seed import sync_reference

    rng = random.Random(7)  # noqa: S311 - mock data, not security
    upgrade(db_file)
    s = session_factory(db_file)()
    sync_reference(s, load_cards(CONFIG_DIR / "cards.example.json"), load_taxonomy())

    imports: dict[str, m.Import] = {}

    def imp(source: str, kind: str) -> m.Import:
        if (source, kind) not in imports:
            i = m.Import(
                source=source,
                kind=kind,
                file_path="demo",
                status="ok",
                sha256=hashlib.sha256(f"{source}/{kind}".encode()).hexdigest(),
            )
            s.add(i)
            s.flush()
            imports[source, kind] = i
        return imports[source, kind]

    merchants: dict[str, m.Merchant] = {}

    def merchant(name: str, group: str | None, category: str | None) -> m.Merchant:
        if name not in merchants:
            merchants[name] = m.Merchant(
                name=name, merchant_group=group, default_category_id=category
            )
            s.add(merchants[name])
            s.flush()
        return merchants[name]

    issuer = {"amex_platinum": "amex", "amex_gold": "amex", "citi_costco": "citi"}
    seq = iter(range(1, 10**6))

    def txn(
        card: str,
        date: str,
        amount: int,
        name: str | None,
        category: str | None,
        *,
        type_: str = "purchase",
        person: str = "self",
        group: str | None = None,
        raw: str | None = None,
        source: str = "rule",
    ) -> m.Transaction:
        mer = merchant(name, group, category) if name else None
        t = m.Transaction(
            card_id=card,
            person_id=person,
            person_source="name" if person != "self" else "default_self",
            txn_date=date,
            post_date=date,
            amount_cents=amount,
            description_raw=raw or (name or "").upper(),
            description_clean=name or raw or "",
            merchant_id=mer.id if mer else None,
            type=type_,
            category_id=category,
            category_source=source if category else None,
            dedupe_key=f"demo-{next(seq)}",
            import_id=imp(issuer.get(card, "chase"), "transactions").id,
        )
        s.add(t)
        s.flush()
        return t

    def day(month: str, lo: int = 1, hi: int | None = None) -> str:
        y, mo = map(int, month.split("-"))
        hi = hi or calendar.monthrange(y, mo)[1]
        return f"{month}-{rng.randint(lo, hi):02d}"

    def order(
        merchant_: str,
        channel: str,
        date: str,
        card: str,
        catalog,
        n: int,
        ref: str,
        location: str | None,
        payment: str,
    ) -> m.Transaction:
        picks = rng.sample(catalog, n)
        items, sub, tax = [], 0, 0
        for line_no, p in enumerate(picks, 1):
            qty = 2 if rng.random() < 0.15 else 1
            line = cents(p[2]) * qty
            sub += line
            if merchant_ == "amazon" or p[3]:
                tax += round(line * TAX)
            items.append(
                m.OrderItem(
                    line_no=line_no,
                    title_raw=p[0],
                    title_clean=p[0],
                    quantity=qty,
                    unit_price_cents=cents(p[2]),
                    line_total_cents=line,
                    category_id=p[1],
                    category_source="rule",
                    sku=f"demo-{p[0]}",
                )
            )
        o = m.Order(
            merchant=merchant_,
            channel=channel,
            external_id=ref,
            order_date=date,
            location=location,
            subtotal=sub,
            tax=tax,
            total=sub + tax,
            payment_method=payment,
            raw_path="demo",
            import_id=imp(merchant_, "orders" if merchant_ == "amazon" else "receipts").id,
        )
        s.add(o)
        s.flush()
        for i in items:
            i.order_id = o.id
            s.add(i)
        name = "Amazon" if merchant_ == "amazon" else "Costco"
        main = max(items, key=lambda i: i.line_total_cents).category_id
        t = txn(
            card,
            date,
            o.total,
            name,
            main,
            group=merchant_,
            source="split",
            person=rng.choice(["father", "mother", "self"]) if merchant_ == "costco" else "self",
            raw="AMAZON MKTPL*DEMO" if merchant_ == "amazon" else "COSTCO WHSE #0000",
        )
        t.order_ref = ref if merchant_ == "amazon" else None
        s.add(
            m.Match(
                transaction_id=t.id,
                order_id=o.id,
                method="exact" if merchant_ == "amazon" else "window",
                confidence=1.0,
            )
        )
        for i, part in zip(items, split(o.total, [i.line_total_cents for i in items]), strict=True):
            s.add(
                m.Allocation(
                    transaction_id=t.id,
                    category_id=i.category_id,
                    amount_cents=part,
                    source="order_items",
                )
            )
        return t

    for month in MONTHS:
        for name, group, cat, cards, people, (lo, hi), times in MERCHANTS:
            for _ in range(times):
                txn(
                    rng.choice(cards),
                    day(month),
                    cents(rng.uniform(lo, hi)),
                    name,
                    cat,
                    person=rng.choice(people),
                    group=group,
                )
        for name, cat, card, dollars in FIXED:
            txn(
                card,
                f"{month}-{rng.randint(3, 9):02d}",
                cents(dollars or rng.uniform(92, 168)),
                name,
                cat,
            )
        if month == "2026-08":
            for name, cat, lo, hi in TRIP:
                txn(
                    "chase_sapphire_reserve",
                    day(month, 10, 14),
                    cents(rng.uniform(lo, hi)),
                    name,
                    cat,
                )
        for _ in range(rng.randint(4, 6)):
            ref = f"113-{rng.randint(1000000, 9999999)}-{rng.randint(1000000, 9999999)}"
            order(
                "amazon",
                "online",
                day(month),
                "chase_prime_visa",
                AMAZON,
                rng.randint(1, 3),
                ref,
                None,
                "Prime Visa",
            )
        for k in range(rng.randint(2, 3)):
            order(
                "costco",
                "warehouse",
                day(month),
                "citi_costco",
                COSTCO,
                rng.randint(6, 9),
                f"DEMO-{month}-{k}",
                "Demo Warehouse #0000",
                "Costco Anywhere Visa",
            )
        txn(
            "citi_costco",
            day(month),
            cents(rng.uniform(48, 66)),
            "Costco Gas",
            "transportation.fuel",
            group="costco_gas",
            raw="COSTCO GAS #0000",
        )
        txn("amex_gold", f"{month}-15", -1000, None, None, type_="credit", raw="DINING CREDIT")

    # Monthly statements that balance, with each card paid in full the next month.
    from fin.db.models import Statement, Transaction

    for card in [c.id for c in s.scalars(select(m.Card))]:
        prev = cents(rng.uniform(250, 900))
        for month in MONTHS:
            y, mo = map(int, month.split("-"))
            end = f"{month}-{calendar.monthrange(y, mo)[1]:02d}"
            pay = (
                txn(
                    card,
                    f"{month}-22",
                    -prev,
                    None,
                    None,
                    type_="payment",
                    raw="AUTOPAY PAYMENT - THANK YOU",
                )
                if prev
                else None
            )
            rows = s.scalars(
                select(Transaction).where(
                    Transaction.card_id == card, Transaction.post_date.between(f"{month}-01", end)
                )
            ).all()
            purchases = sum(t.amount_cents for t in rows if t.type == "purchase")
            credits = -sum(t.amount_cents for t in rows if t.type == "credit")
            st = Statement(
                card_id=card,
                period_start=f"{month}-01",
                period_end=end,
                previous_balance=prev,
                payments=prev if pay else 0,
                credits=credits,
                purchases=purchases,
                new_balance=prev - (prev if pay else 0) - credits + purchases,
                pdf_path="demo",
                parse_method="regex",
                math_ok=True,
                ledger_status="match",
                ledger_delta_cents=0,
            )
            s.add(st)
            s.flush()
            for t in rows:
                t.statement_id = st.id
            prev = st.new_balance

    # A few things for the Review page.
    last = MONTHS[-1]
    odd = txn(
        "chase_freedom_unlimited",
        f"{last}-18",
        2350,
        None,
        None,
        raw="SQ *FARMSTAND 0000",
        source="ai",
    )
    s.add(
        m.ReviewItem(
            kind="uncategorized",
            ref_table="transactions",
            ref_id=str(odd.id),
            details_json='{"reason": "No rule or confident AI category"}',
        )
    )
    guess = txn(
        "amex_gold", f"{last}-20", 6420, "Lantern Hall", "entertainment.events", source="ai"
    )
    s.add(
        m.ReviewItem(
            kind="low_confidence",
            ref_table="transactions",
            ref_id=str(guess.id),
            details_json='{"category_id": "entertainment.events", "reason": "AI confidence 0.55"}',
        )
    )
    stray = txn("chase_prime_visa", f"{last}-12", 645, "Bluebird Coffee", "dining.coffee")
    s.add(
        m.ReviewItem(
            kind="unexpected_merchant",
            ref_table="transactions",
            ref_id=str(stray.id),
            details_json='{"reason": "Prime Visa is reserved for Amazon and Whole Foods"}',
        )
    )

    for site, files in [("chase", 9), ("amex", 6), ("citi", 3), ("amazon", 16), ("costco", 8)]:
        s.add(
            m.SyncRun(
                site=site,
                started_at="2026-10-04T09:30:00Z",
                ended_at="2026-10-04T09:41:00Z",
                run_dir="demo",
                model="sonnet",
                mode="trusted",
                status="ok",
                files_imported=files,
            )
        )
    s.commit()
    s.close()


def serve(db_file: Path):
    import uvicorn

    import fin.api.app as app_mod
    from fin.config import CONFIG_DIR, load_cards

    real = app_mod.load_cards
    app_mod.load_cards = lambda path=None: load_cards(CONFIG_DIR / "cards.example.json")
    try:
        app = app_mod.create_app(db_file=db_file)
    finally:
        app_mod.load_cards = real
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/meta", timeout=1)
            return server
        except OSError:
            time.sleep(0.25)
    raise SystemExit("dashboard did not start")


SHOTS = [  # file, path, theme, height
    ("overview.png", "/", "light", 1510),
    ("overview-dark.png", "/", "dark", 1510),
    ("transactions.png", "/transactions", "light", 860),
    ("orders.png", "/orders?merchant=costco", "light", 740),
    ("reconciliation.png", "/reconciliation", "light", 860),
]


def shoot(tmp: Path, only: set[str] | None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, path, theme, height in SHOTS:
        if only and name not in only:
            continue
        theme_flags = (
            ["--force-dark-mode", "--blink-settings=preferredColorScheme=0"]
            if theme == "dark"
            else ["--blink-settings=preferredColorScheme=1"]
        )
        subprocess.run(
            [
                CHROME,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                f"--user-data-dir={tmp / ('chrome-' + name)}",
                "--force-device-scale-factor=2",
                f"--window-size=1280,{height}",
                "--virtual-time-budget=6000",
                f"--screenshot={OUT / name}",
                *theme_flags,
                f"http://127.0.0.1:{PORT}{path}",
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        print(f"{OUT / name}  {(OUT / name).stat().st_size // 1024} KB")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--only", nargs="*", help="file names to regenerate")
    args = ap.parse_args()
    if not Path(CHROME).exists():
        sys.exit(f"Chrome not found at {CHROME} (set CHROME=...)")
    if not (REPO / "web" / "dist" / "index.html").exists():
        sys.exit("Build the dashboard first: make web")
    with tempfile.TemporaryDirectory(prefix="fin-demo-") as d:
        tmp = Path(d)
        os.environ["FIN_DATA_DIR"] = str(tmp / "data")  # never the real data dir
        db_file = tmp / "demo.sqlite"
        build(db_file)
        server = serve(db_file)
        try:
            shoot(tmp, set(args.only) if args.only else None)
        finally:
            server.should_exit = True
            time.sleep(0.5)


if __name__ == "__main__":
    main()
