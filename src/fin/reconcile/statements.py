"""Statement math and ledger checks (SPEC §11)."""

from __future__ import annotations


def math_delta(s: dict[str, int]) -> int:
    """previous − payments − credits + purchases + cash + BT + fees + interest − new_balance."""
    return (
        s["previous_balance"] - s["payments"] - s["credits"] + s["purchases"]
        + s["cash_advances"] + s["balance_transfers"] + s["fees"] + s["interest"]
        - s["new_balance"]
    )


def math_ok(s: dict[str, int]) -> bool:
    return math_delta(s) == 0


# ------------------------------------------------------------------ ledger

MONEY_IN = ("payment", "refund", "credit", "reward")


def ledger_components(txns) -> dict[str, int]:
    """Statement-style totals from transactions (§8 signs; Chase combines payments+credits)."""
    c = {"purchases": 0, "payments_credits": 0, "fees": 0, "interest": 0}
    for t in txns:
        if t.type == "purchase" or (t.type == "adjustment" and t.amount_cents > 0):
            c["purchases"] += t.amount_cents
        elif t.type in MONEY_IN or t.type == "adjustment":
            c["payments_credits"] -= t.amount_cents
        elif t.type == "fee":
            c["fees"] += t.amount_cents
        elif t.type == "interest":
            c["interest"] += t.amount_cents
    return c


def ledger_check(s, st, reviews) -> str:
    """Compare a statement with the ledger. Sets ledger_status/delta. Returns the status."""
    from sqlalchemy import select, true

    from fin.db import models as m

    linked = s.scalars(select(m.Transaction).join(
        m.StatementLine, m.StatementLine.transaction_id == m.Transaction.id).where(
        m.StatementLine.statement_id == st.id)).all()
    unlinked_lines = s.scalars(select(m.StatementLine.id).where(
        m.StatementLine.statement_id == st.id, m.StatementLine.transaction_id.is_(None))).all()
    linked_ids = set(s.scalars(select(m.StatementLine.transaction_id).where(
        m.StatementLine.transaction_id.is_not(None))))
    extras = s.scalars(select(m.Transaction).where(
        m.Transaction.card_id == st.card_id, m.Transaction.source == "csv",
        m.Transaction.post_date > st.period_start, m.Transaction.post_date <= st.period_end,
        m.Transaction.id.not_in(linked_ids) if linked_ids else true())).all()

    got = ledger_components(linked + list(extras))
    want = {"purchases": st.purchases, "payments_credits": st.payments + st.credits,
            "fees": st.fees, "interest": st.interest}
    deltas = {k: got[k] - want[k] for k in want if got[k] != want[k]}
    if st.cash_advances or st.balance_transfers:
        deltas["cash_advances_balance_transfers"] = -(st.cash_advances + st.balance_transfers)
    net = (got["purchases"] - got["payments_credits"] + got["fees"] + got["interest"]) - (
        want["purchases"] - want["payments_credits"] + want["fees"] + want["interest"])

    if not deltas and not extras and not unlinked_lines:
        st.ledger_status, st.ledger_delta_cents = "match", 0
    else:
        st.ledger_status, st.ledger_delta_cents = "mismatch", net
        reviews.flag(
            "statement_mismatch", "statements", st.id,
            deltas_cents=deltas, net_delta_cents=net,
            csv_rows_not_on_statement=[t.id for t in extras][:10],
            unlinked_statement_lines=len(unlinked_lines),
        )
    return st.ledger_status
