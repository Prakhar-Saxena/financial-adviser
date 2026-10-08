"""Statement PDF parsers (SPEC §11). Text comes from pdfplumber, pages joined by form feeds.

Chase (documented from tests/fixtures/chase/statement_prime_visa_2026-09-21.txt):
- ACCOUNT SUMMARY lines: "Previous Balance $567.70", "Payment, Credits -$567.70",
  "Purchases +$1,261.84", "Cash Advances", "Balance Transfers", "Fees Charged",
  "Interest Charged", "New Balance", "Opening/Closing Date 08/22/26 - 09/21/26".
  Chase combines payments and credits into one figure, stored in `payments`.
- The remit coupon has "Payment Due Date: 10/18/26" and "Minimum Payment Due: $35.00".
- ACCOUNT ACTIVITY: section headers ("PAYMENTS AND OTHER CREDITS", "PURCHASE", ...), then
  "MM/DD <description> <amount>" lines, money in negative. Amazon purchases are followed by
  "Order Number 114-...". Lines show the transaction date without a year.
- Bold text extracts with doubled letters ("AACCCCOOUUNNTT"), and some labels pick up a stray
  backtick ("B`alance Transfers"). Both are undone before matching.
- Per-cardholder sections ("<NAME> TRANSACTIONS THIS CYCLE (CARD 1234) ...") are expected for
  cards that authorized users used. Not seen yet; the pattern is a best guess until a sample
  shows it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from fin.normalize.signs import to_cents

MONEY = r"[-+]?\$?(?:\d[\d,]*)?\.\d{2}"  # Chase prints amounts under $1 as ".60"
SUMMARY_FIELDS = {
    "previous_balance": r"Previous Balance",
    "payments": r"Payment, Credits",
    "purchases": r"Purchases",
    "cash_advances": r"Cash Advances",
    "balance_transfers": r"Balance Transfers",
    "fees": r"Fees Charged",
    "interest": r"Interest Charged",
    "new_balance": r"New Balance",
}
SECTION_HEADERS = {
    "PAYMENTS AND OTHER CREDITS", "PURCHASE", "PURCHASES", "CASH ADVANCES",
    "BALANCE TRANSFERS", "FEES CHARGED", "INTEREST CHARGED", "ADJUSTMENTS",
}
ACTIVITY_LINE = re.compile(rf"^(\d{{2}})/(\d{{2}})\s+(.+?)\s+({MONEY})$")
ORDER_LINE = re.compile(r"^Order Number\s+(\S+)")
CARDHOLDER = re.compile(r"^(?P<name>[A-Z][A-Z .'-]+?)\s+TRANSACTIONS THIS CYCLE\b")
PERIOD = re.compile(
    r"Opening/Closing Date\s+(\d{2}/\d{2}/\d{2,4})\s*-\s*(\d{2}/\d{2}/\d{2,4})"
)
ACTIVITY_END = re.compile(r"Totals Year-to-Date|INTEREST CHARGES")


@dataclass
class ParsedLine:
    line_no: int
    section: str
    txn_date: str
    description: str
    amount_cents: int
    order_ref: str | None = None
    cardholder_name: str | None = None


@dataclass
class ParsedStatement:
    issuer: str
    period_start: str
    period_end: str
    due_date: str | None
    minimum_due: int | None
    previous_balance: int
    payments: int
    credits: int
    purchases: int
    cash_advances: int
    balance_transfers: int
    fees: int
    interest: int
    new_balance: int
    lines: list[ParsedLine] = field(default_factory=list)
    product_markers: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in (
            "previous_balance", "payments", "credits", "purchases", "cash_advances",
            "balance_transfers", "fees", "interest", "new_balance")}


def undouble(line: str) -> str:
    """'AACCCCOOUUNNTT AACCTTIIVVIITTYY' -> 'ACCOUNT ACTIVITY' (bold text in pdfplumber)."""
    out = []
    for tok in line.split(" "):
        if len(tok) >= 4 and len(tok) % 2 == 0 and tok[0::2] == tok[1::2]:
            tok = tok[0::2]
        out.append(tok)
    return " ".join(out)


def clean_lines(text: str) -> list[str]:
    lines = []
    for raw in text.replace("\f", "\n").splitlines():
        line = undouble(raw.replace("`", "").strip())
        if line:
            lines.append(line)
    return lines


def _mdy(s: str) -> str:
    mo, d, y = s.split("/")
    year = int(y) + 2000 if len(y) == 2 else int(y)
    return date(year, int(mo), int(d)).isoformat()


def _magnitude(text: str) -> int:
    return abs(to_cents(text))


def parse_chase_statement(text: str) -> ParsedStatement:
    lines = clean_lines(text)
    found: dict[str, int] = {}
    period = due = minimum = None
    for line in lines:
        for key, label in SUMMARY_FIELDS.items():
            if key not in found:
                m = re.fullmatch(rf"{label}\s+({MONEY})", line)
                if m:
                    found[key] = _magnitude(m.group(1))
        if period is None:
            m = PERIOD.search(line)
            if m:
                period = (_mdy(m.group(1)), _mdy(m.group(2)))
        if due is None:
            m = re.search(r"Payment Due Date:?\s+(\d{2}/\d{2}/\d{2,4})", line)
            if m:
                due = _mdy(m.group(1))
        if minimum is None:
            m = re.search(rf"Minimum Payment Due:?\s+({MONEY})", line)
            if m:
                minimum = _magnitude(m.group(1))
    missing = [k for k in SUMMARY_FIELDS if k not in found]
    if missing or period is None:
        raise ValueError(f"Chase statement summary incomplete: missing {missing or 'period'}")

    st = ParsedStatement(
        issuer="chase", period_start=period[0], period_end=period[1], due_date=due,
        minimum_due=minimum, credits=0, **found,
    )
    st.lines = _activity(lines, period)
    upper = text.upper()
    st.product_markers = [p for p in ("PRIME VISA", "SAPPHIRE RESERVE", "FREEDOM UNLIMITED")
                          if p in upper]
    return st


def _activity(lines: list[str], period: tuple[str, str]) -> list[ParsedLine]:
    closing = date.fromisoformat(period[1])
    out: list[ParsedLine] = []
    in_activity = False
    section = ""
    holder: str | None = None
    for line in lines:
        if line == "ACCOUNT ACTIVITY" or line.startswith("ACCOUNT ACTIVITY "):
            in_activity = True
            continue
        if not in_activity:
            continue
        if ACTIVITY_END.search(line):
            in_activity = False
            continue
        if line.upper() in SECTION_HEADERS:
            section = line.upper()
            continue
        ch = CARDHOLDER.match(line)
        if ch:
            holder = " ".join(ch.group("name").split())
            continue
        om = ORDER_LINE.match(line)
        if om and out:
            out[-1].order_ref = om.group(1)
            continue
        am = ACTIVITY_LINE.match(line)
        if am:
            mo, d = int(am.group(1)), int(am.group(2))
            year = closing.year - 1 if mo > closing.month else closing.year
            out.append(ParsedLine(
                line_no=len(out) + 1, section=section or "UNKNOWN",
                txn_date=date(year, mo, d).isoformat(), description=am.group(3).strip(),
                amount_cents=to_cents(am.group(4)), cardholder_name=holder,
            ))
    return out


def pdf_text(path: Path) -> str:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return "\f".join(page.extract_text() or "" for page in pdf.pages)


# ------------------------------------------------------------------ import


def store_statement(
    s, card_id: str, st: ParsedStatement, imp, pdf_path: str
) -> tuple[object | None, list[str]]:
    """Insert the statement and its lines.

    Returns (statement row, or None if it was already stored or rejected; problems)."""
    import json

    from sqlalchemy import select

    from fin.config import load_cards
    from fin.db import models as m
    from fin.reconcile.statements import math_delta

    problems: list[str] = []
    product = load_cards().card(card_id).product.upper()
    if st.product_markers and not any(mk in product for mk in st.product_markers):
        problems.append(f"statement looks like {st.product_markers}, but the manifest says "
                        f"{card_id}; the document wins, so it was not stored")
        return None, problems
    existing = s.scalar(select(m.Statement).where(
        m.Statement.card_id == card_id, m.Statement.period_end == st.period_end))
    if existing:
        return None, []

    delta = math_delta(st.summary())
    row = m.Statement(
        card_id=card_id, import_id=imp.id, period_start=st.period_start,
        period_end=st.period_end, due_date=st.due_date, minimum_due=st.minimum_due,
        pdf_path=pdf_path, parse_method="regex", math_ok=delta == 0, **st.summary(),
    )
    s.add(row)
    s.flush()
    for line in st.lines:
        s.add(m.StatementLine(
            statement_id=row.id, line_no=line.line_no, section=line.section,
            txn_date=line.txn_date, description=line.description,
            amount_cents=line.amount_cents, order_ref=line.order_ref,
            cardholder_name=line.cardholder_name,
        ))
    if delta:
        s.add(m.ReviewItem(
            kind="parse_failure", ref_table="statements", ref_id=str(row.id),
            details_json=json.dumps({"reason": "statement math does not balance",
                                     "delta_cents": delta}),
        ))
        problems.append(f"statement math off by {delta} cents")
    return row, problems


def _register_chase() -> None:
    from fin.ingest.inbox import ParseOutcome, register

    @register("chase", "statement")
    def import_chase_statement(s, path, entry, imp):
        # .txt = text already extracted by pdfplumber (redacted fixtures)
        st = parse_chase_statement(path.read_text() if path.suffix == ".txt" else pdf_text(path))
        problems = []
        if entry["period_end"] and entry["period_end"] != st.period_end:
            problems.append(f"manifest period_end {entry['period_end']} but the statement "
                            f"closes {st.period_end}; using the statement")
        row, more = store_statement(s, entry["card_id"], st, imp, imp.file_path)
        return ParseOutcome(row_count=len(st.lines) if row else 0,
                            period_start=st.period_start, period_end=st.period_end,
                            problems=problems + more)


_register_chase()


# -------------------------------------------------------------------- Citi
#
# Citi (documented from the Costco Anywhere Visa statements, e.g. "September 25.pdf"):
# - Two-column first page: summary labels share lines with other text, e.g.
#   "...your Minimum Payment Payments -$991.21", so labels are matched anywhere in a line:
#   "Previous balance $991.21", "Payments -$", "Credits -$", "Purchases +$", "Cash advances +$",
#   "Fees +$", "Interest +$", "New balance $". Balance transfers are included in Purchases.
# - "Billing Period: 08/28/26-09/25/26", "Payment due date: 10/23/26",
#   "Minimum payment due: $41.00".
# - Activity after "Date Date Description Amount": sections "Payments, Credits and
#   Adjustments", "Standard Purchases", a cardholder name line (all caps) before each
#   cardholder's purchases ("No Activity" when none), lines "MM/DD [MM/DD] <desc> -?$amount"
#   (sale date, post date; payments show one date). Ends at "Fees Charged".

CITI_SUMMARY = {
    "previous_balance": r"Previous balance\s+\$([\d,]+\.\d{2})",
    "payments": r"Payments\s+-\$([\d,]+\.\d{2})",
    "credits": r"Credits\s+-\$([\d,]+\.\d{2})",
    "purchases": r"Purchases\s+\+\$([\d,]+\.\d{2})",
    "cash_advances": r"Cash advances\s+\+\$([\d,]+\.\d{2})",
    "fees": r"Fees\s+\+\$([\d,]+\.\d{2})",
    "interest": r"Interest\s+\+\$([\d,]+\.\d{2})",
    "new_balance": r"New balance\s+\$([\d,]+\.\d{2})",
}
CITI_LINE = re.compile(r"^(\d{2})/(\d{2})(?:\s+\d{2}/\d{2})?\s+(.+?)\s+(-?\$[\d,]+\.\d{2})$")
CITI_SECTIONS = {"Payments, Credits and Adjustments": "PAYMENTS AND OTHER CREDITS",
                 "Standard Purchases": "PURCHASE"}
CITI_NAME = re.compile(r"^[A-Z][A-Z .'-]{2,60}$")


def parse_citi_statement(text: str) -> ParsedStatement:
    lines = clean_lines(text)
    found: dict[str, int] = {}
    period = due = minimum = None
    for line in lines:
        for key, rx in CITI_SUMMARY.items():
            if key not in found:
                m = re.search(rx, line)
                if m:
                    found[key] = to_cents(m.group(1))
        if period is None:
            m = re.search(r"Billing Period:\s*(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})", line)
            if m:
                period = (_mdy(m.group(1)), _mdy(m.group(2)))
        if due is None:
            m = re.search(r"Payment due date:\s*(\d{2}/\d{2}/\d{2,4})", line)
            if m:
                due = _mdy(m.group(1))
        if minimum is None:
            m = re.search(r"Minimum payment due:\s*\$([\d,]+\.\d{2})", line)
            if m:
                minimum = to_cents(m.group(1))
    missing = [k for k in CITI_SUMMARY if k not in found]
    if missing or period is None:
        raise ValueError(f"Citi statement summary incomplete: missing {missing or 'period'}")
    st = ParsedStatement(issuer="citi", period_start=period[0], period_end=period[1],
                         due_date=due, minimum_due=minimum, balance_transfers=0, **found)
    st.lines = _citi_activity(lines, period)
    upper = text.upper()
    st.product_markers = [p for p in ("COSTCO ANYWHERE",) if p in upper]
    return st


def _citi_activity(lines: list[str], period: tuple[str, str]) -> list[ParsedLine]:
    closing = date.fromisoformat(period[1])
    out: list[ParsedLine] = []
    inside, section, holder = False, "", None
    for line in lines:
        if line.startswith("Date Date Description"):
            inside = True
            continue
        if not inside:
            continue
        if line.startswith(("Fees Charged", "TOTAL FEES", "Interest charge calculation")):
            break
        if line in CITI_SECTIONS:
            section = CITI_SECTIONS[line]
            continue
        if line == "No Activity":
            continue
        am = CITI_LINE.match(line)
        if am:
            mo, d = int(am.group(1)), int(am.group(2))
            year = closing.year - 1 if mo > closing.month else closing.year
            out.append(ParsedLine(
                line_no=len(out) + 1, section=section or "UNKNOWN",
                txn_date=date(year, mo, d).isoformat(), description=am.group(3).strip(),
                amount_cents=to_cents(am.group(4)),
                cardholder_name=holder if section == "PURCHASE" else None,
            ))
            continue
        if CITI_NAME.match(line):
            holder = " ".join(line.split())
    return out


def _register_citi() -> None:
    from fin.ingest.inbox import ParseOutcome, register

    @register("citi", "statement")
    def import_citi_statement(s, path, entry, imp):
        st = parse_citi_statement(path.read_text() if path.suffix == ".txt" else pdf_text(path))
        problems = []
        if entry["period_end"] and entry["period_end"] != st.period_end:
            problems.append(f"manifest period_end {entry['period_end']} but the statement "
                            f"closes {st.period_end}; using the statement")
        row, more = store_statement(s, entry["card_id"], st, imp, imp.file_path)
        return ParseOutcome(row_count=len(st.lines) if row else 0,
                            period_start=st.period_start, period_end=st.period_end,
                            problems=problems + more)


_register_citi()


# -------------------------------------------------------------------- Amex
#
# American Express charge cards (documented from the Platinum statements, e.g. "2026-09-16.pdf"):
# - Several summary blocks (Pay In Full / Pay Over Time); the "Account Total" block is the one that
#   reconciles: "Previous Balance $", "Payments/Credits -$", "New Charges +$", "Fees +$",
#   "Interest Charged +$", "New Balance $". Payments and credits are combined (in `payments`).
# - "Closing Date09/16/26" (no space), "Payment Due Date 10/11/26", "Minimum Payment Due $44.76",
#   "Days in Billing Period:30" (the period start is derived from it).
# - Activity: "Payments", "Credits", "New Charges", "Fees", "Interest Charged" sections. Lines are
#   "MM/DD/YY[*] <description> [-]$amount[⧫]" (* = posting date, ⧫ = Pay Over Time), followed by
#   detail lines without a date. New Charges are grouped under a cardholder name line followed by
#   "Card Ending...".

AMEX_SUMMARY = {
    "previous_balance": r"Previous Balance\s+\$([\d,]+\.\d{2})",
    "payments": r"Payments/Credits\s+-\$([\d,]+\.\d{2})",
    "purchases": r"New Charges\s+\+\$([\d,]+\.\d{2})",
    "fees": r"Fees\s+\+\$([\d,]+\.\d{2})",
    "interest": r"Interest Charged\s+\+\$([\d,]+\.\d{2})",
    "new_balance": r"New Balance\s+=?\s*\$([\d,]+\.\d{2})",
}
AMEX_LINE = re.compile(r"^(\d{2})/(\d{2})/(\d{2})\*?\s+(.+?)\s+(-?\$[\d,]+\.\d{2})⧫?$")
AMEX_SECTIONS = {"Payments": "PAYMENTS AND OTHER CREDITS", "Credits": "CREDITS",
                 "New Charges": "PURCHASE", "Fees": "FEES CHARGED",
                 "Interest Charged": "INTEREST CHARGED"}
AMEX_END = re.compile(r"^(\d{4} Fees and Interest Totals|Interest Charge Calculation)")


def parse_amex_statement(text: str) -> ParsedStatement:
    from datetime import timedelta

    lines = clean_lines(text)
    start = next((i for i, ln in enumerate(lines) if "Account Total" in ln), 0)
    found: dict[str, int] = {}
    for line in lines[start:]:
        for key, rx in AMEX_SUMMARY.items():
            if key not in found:
                m = re.search(rx, line)
                if m:
                    found[key] = to_cents(m.group(1))
    joined = "\n".join(lines)
    closing = re.search(r"Closing Date\s*(\d{2}/\d{2}/\d{2})", joined)
    days = re.search(r"Days in Billing Period:\s*(\d+)", joined)
    due = re.search(r"Payment Due Date\s*(\d{2}/\d{2}/\d{2})", joined)
    minimum = re.search(r"Minimum Payment Due\s*\$([\d,]+\.\d{2})", joined)
    missing = [k for k in AMEX_SUMMARY if k not in found]
    if missing or not closing or not days:
        raise ValueError(f"Amex statement summary incomplete: missing {missing or 'dates'}")
    end = date.fromisoformat(_mdy(closing.group(1)))
    period = ((end - timedelta(days=int(days.group(1)) - 1)).isoformat(), end.isoformat())
    st = ParsedStatement(
        issuer="amex", period_start=period[0], period_end=period[1],
        due_date=_mdy(due.group(1)) if due else None,
        minimum_due=to_cents(minimum.group(1)) if minimum else None,
        credits=0, cash_advances=0, balance_transfers=0, **found,
    )
    st.lines = _amex_activity(lines)
    upper = text.upper()
    st.product_markers = [p for p in ("PLATINUM CARD", "GOLD CARD") if p in upper]
    return st


def _amex_activity(lines: list[str]) -> list[ParsedLine]:
    out: list[ParsedLine] = []
    section, holder, inside = "", None, False
    prev = ""
    for line in lines:
        if line.startswith("Detail") and ("posting date" in line or "Pay Over Time" in line):
            inside = True
        if AMEX_END.match(line):
            break
        header = line.removesuffix(" Amount").strip()
        if header in AMEX_SECTIONS and (line.endswith("Amount") or header in ("New Charges",
                                                                             "Fees",
                                                                             "Interest Charged")):
            section = AMEX_SECTIONS[header]
            prev = line
            continue
        if line.startswith("Card Ending") and CITI_NAME.match(prev):
            holder = " ".join(prev.split())
        am = AMEX_LINE.match(line)
        if am and inside and section:
            mo, d, yy = int(am.group(1)), int(am.group(2)), int(am.group(3))
            out.append(ParsedLine(
                line_no=len(out) + 1, section=section,
                txn_date=date(2000 + yy, mo, d).isoformat(), description=am.group(4).strip(),
                amount_cents=to_cents(am.group(5)),
                cardholder_name=holder if section == "PURCHASE" else None,
            ))
        prev = line
    return out


def _register_amex() -> None:
    from fin.ingest.inbox import ParseOutcome, register

    @register("amex", "statement")
    def import_amex_statement(s, path, entry, imp):
        st = parse_amex_statement(path.read_text() if path.suffix == ".txt" else pdf_text(path))
        problems = []
        if entry["period_end"] and entry["period_end"] != st.period_end:
            problems.append(f"manifest period_end {entry['period_end']} but the statement "
                            f"closes {st.period_end}; using the statement")
        row, more = store_statement(s, entry["card_id"], st, imp, imp.file_path)
        return ParseOutcome(row_count=len(st.lines) if row else 0,
                            period_start=st.period_start, period_end=st.period_end,
                            problems=problems + more)


_register_amex()
