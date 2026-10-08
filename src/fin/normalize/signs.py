"""Amount signs and transaction types (SPEC §8, §9.2).

§8 convention: positive = money out (purchase, fee, interest); negative = money in.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

PAYMENT = re.compile(r"\b(PAYMENT|AUTOPAY|AUTO PAY|THANK YOU)\b")
INTEREST = re.compile(r"\bINTEREST\b")
FEE = re.compile(r"\b(ANNUAL (MEMBERSHIP )?FEE|LATE FEE|FOREIGN TRANSACTION FEE|FOREIGN TRANS FEE|"
                 r"RETURNED PAYMENT FEE|BALANCE TRANSFER FEE|CASH ADVANCE FEE)\b")
REWARD = re.compile(r"\b(REDEMPTION|REWARDS?|CASH BACK|POINTS)\b")
CREDIT = re.compile(r"\b(STATEMENT CREDIT|CREDIT)\b")


def to_cents(text: str) -> int:
    t = text.strip().replace("$", "").replace(",", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        value = Decimal(t)
    except InvalidOperation as e:
        raise ValueError(f"not an amount: {text!r}") from e
    cents = int((value * 100).to_integral_value())
    if value * 100 != cents:
        raise ValueError(f"amount has more than 2 decimals: {text!r}")
    return -cents if neg else cents


def classify(
    amount_cents: int, description: str, issuer_type: str | None = None
) -> tuple[str, bool]:
    """Return (type, needs_review). `amount_cents` already follows the §8 sign."""
    d = description.upper()
    it = (issuer_type or "").strip().lower()
    if amount_cents > 0:
        if INTEREST.search(d) and (it in ("fee", "") or "CHARGE" in d):
            return "interest", False
        if it == "fee" or FEE.search(d):
            return "fee", False
        if it == "adjustment":
            return "adjustment", False
        return "purchase", False
    # Money in.
    if it == "payment" or PAYMENT.search(d):
        return "payment", False
    if it == "return":
        return "refund", False
    if REWARD.search(d):
        return "reward", False
    if it == "adjustment":
        return "adjustment", False
    if CREDIT.search(d):
        return "credit", False
    # Any other money-in row: credit, and a human looks at it (§9.2).
    return "credit", True
