# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Proving the checking ledger (docs/SPEC-checking.md §11).

- Statement math (`opening + deposits − withdrawals == closing`) is checked at import.
- Ledger check: the rows posted in a statement's period add up to its money in and money out.
  Transfers count here, because the statement sees all the money.
- Running balance: walking the account by day, each day's closing balance must appear among
  that day's `balance_after_cents`. By day, not by row: same-day order can change between
  exports.

`ledger_check` and `link_statements` stay card-only: they read `statements`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fin.db import models as m
from fin.review import ReviewSet


@dataclass
class BankCheckResult:
    statements: dict[str, int] = field(default_factory=dict)  # ledger status -> count
    balance_days: int = 0
    balance_gaps: list[str] = field(default_factory=list)  # "<account> <day>"


def export_coverage(s: Session, card_id: str) -> tuple[str | None, str | None]:
    """The span the imported exports cover for an account (their periods, joined)."""
    return s.execute(select(func.min(m.Import.period_start), func.max(m.Import.period_end))
                     .where(m.Import.card_id == card_id, m.Import.kind == "transactions",
                            m.Import.status == "ok")).one()


def statement_ledger_check(s: Session, st: m.BankStatement, reviews: ReviewSet) -> str:
    """match | mismatch | incomplete (the exports don't cover the whole period)."""
    start, end = export_coverage(s, st.card_id)
    if not start or not end or start > st.period_start or end < st.period_end:
        st.ledger_status, st.ledger_delta_cents = "incomplete", None
        return "incomplete"
    amounts = s.scalars(select(m.Transaction.amount_cents).where(
        m.Transaction.card_id == st.card_id, m.Transaction.post_date >= st.period_start,
        m.Transaction.post_date <= st.period_end)).all()
    money_in = -sum(a for a in amounts if a < 0)
    money_out = sum(a for a in amounts if a > 0)
    deltas = {k: v for k, v in {"deposits": money_in - st.deposits,
                                "withdrawals": money_out - st.withdrawals}.items() if v}
    if not deltas:
        st.ledger_status, st.ledger_delta_cents = "match", 0
        return "match"
    net = deltas.get("deposits", 0) - deltas.get("withdrawals", 0)
    st.ledger_status, st.ledger_delta_cents = "mismatch", net
    reviews.flag("statement_mismatch", "bank_statements", st.id, deltas_cents=deltas,
                 net_delta_cents=net,
                 hint="a positive delta means more rows than the statement: a double import; "
                      "a negative one means rows missing from the exports")
    return "mismatch"


def balance_walk(days: list[tuple[str, int, set[int]]]) -> list[str]:
    """Days where the running balance breaks. `days` is sorted (day, sum of §8 amounts,
    balances seen that day). The first day's close is the candidate that the most following
    days agree with; after a break the walk re-anchors the same way."""

    def run(i: int, close: int) -> int:
        """How many days after day i agree, starting from `close` on day i."""
        n = 0
        for _, total, balances in days[i + 1:]:
            close -= total
            if close not in balances:
                break
            n += 1
        return n

    def anchor(i: int) -> int:
        return max(sorted(days[i][2]), key=lambda c: run(i, c))

    gaps: list[str] = []
    if not days:
        return gaps
    close = anchor(0)
    for i in range(1, len(days)):
        day, total, balances = days[i]
        close -= total
        if close not in balances:
            gaps.append(day)
            close = anchor(i)
    return gaps


def account_balance_check(s: Session, card_id: str, reviews: ReviewSet) -> tuple[int, list[str]]:
    per_day: dict[str, list[m.Transaction]] = defaultdict(list)
    for t in s.scalars(select(m.Transaction).where(
            m.Transaction.card_id == card_id, m.Transaction.balance_after_cents.is_not(None))):
        per_day[t.post_date].append(t)
    days = [(d, sum(t.amount_cents for t in ts), {t.balance_after_cents for t in ts})
            for d, ts in sorted(per_day.items())]
    gaps = balance_walk(days)
    for day in gaps:
        first = min(t.id for t in per_day[day])
        reviews.flag("bank_balance_gap", "transactions", first, day=day,
                     reason="the running balance doesn't follow from the day before: a row is "
                            "missing or doubled around this day")
    return len(days), gaps


def bank_checks(s: Session, account_ids: list[str], reviews: ReviewSet) -> BankCheckResult:
    res = BankCheckResult()
    for st in s.scalars(select(m.BankStatement).where(m.BankStatement.card_id.in_(account_ids))):
        status = statement_ledger_check(s, st, reviews)
        res.statements[status] = res.statements.get(status, 0) + 1
    for acct in account_ids:
        n, gaps = account_balance_check(s, acct, reviews)
        res.balance_days += n
        res.balance_gaps += [f"{acct} {d}" for d in gaps]
    return res
