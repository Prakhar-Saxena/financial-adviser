# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""My corrections to utility links (docs/SPEC-utilities.md §6, §7), shared by `fin utilities
assign` and the Utilities tab. They live in `user_overrides` under stable keys, so they survive
`fin rebuild`; links are recomputed from them by `relink` or the next `fin process`."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fin.db import models as m
from fin.utilities.payments import LINKED_TYPES, NOT_A_UTILITY, merchant_key, row_merchant

MAX_COVERS = 24


class CorrectionError(ValueError):
    pass


def _txn(s: Session, txn_id: int) -> m.Transaction:
    t = s.get(m.Transaction, txn_id)
    if t is None or t.type not in LINKED_TYPES:
        raise CorrectionError(f"No purchase or refund with id {txn_id}.")
    return t


def _set(s: Session, target: str, key: str, field: str, value: str) -> None:
    row = s.scalar(select(m.UserOverride).where(
        m.UserOverride.target_type == target, m.UserOverride.stable_key == key,
        m.UserOverride.field == field))
    if row:
        row.value = value
    else:
        s.add(m.UserOverride(target_type=target, stable_key=key, field=field, value=value))


def assign(s: Session, txn_id: int, account_id: str, all_from_merchant: bool = False) -> str:
    """Link one payment, or every payment from its merchant, to an account, or to `none`.
    Returns what it applied to, in words. The caller commits and relinks."""
    t = _txn(s, txn_id)
    if account_id != NOT_A_UTILITY and s.get(m.UtilityAccount, account_id) is None:
        raise CorrectionError(f"Unknown utility account {account_id!r}.")
    if not all_from_merchant:
        _set(s, "transaction", t.dedupe_key, "utility_account", account_id)
        return f"transaction {txn_id}"
    names = {t.merchant_id: s.get(m.Merchant, t.merchant_id).name} if t.merchant_id else {}
    key = row_merchant(t, names)
    _set(s, "merchant", key, "utility_account", account_id)
    # The merchant correction is the one that should decide this row from now on.
    s.execute(delete(m.UserOverride).where(
        m.UserOverride.target_type == "transaction", m.UserOverride.stable_key == t.dedupe_key,
        m.UserOverride.field == "utility_account"))
    return f"every payment from {merchant_key(key)}"


def set_covers(s: Session, txn_id: int, months: int) -> None:
    """How many months a payment pays for (a prepaid plan, a catch-up payment)."""
    t = _txn(s, txn_id)
    if not 1 <= months <= MAX_COVERS:
        raise CorrectionError(f"covers_months must be 1 to {MAX_COVERS}.")
    _set(s, "transaction", t.dedupe_key, "utility_covers_months", str(months))
