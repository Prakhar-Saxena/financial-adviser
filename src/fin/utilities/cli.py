# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`fin utilities ...`: utility bills from the payments in the ledger
(docs/SPEC-utilities.md §11). `fin utilities` alone is `fin utilities status`."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Annotated

import typer
from sqlalchemy.orm import Session

from fin.config import data_paths, load_cards, load_settings

utilities_app = typer.Typer(help="Utility bills from the payments already in the ledger.")
ICONS = {"warning": "!!", "notice": " !", "info": " i"}


@contextmanager
def _session() -> Iterator[Session]:
    from fin.cli import _open_db
    from fin.db import session_factory

    paths = data_paths(load_settings())
    _open_db(paths)
    with session_factory(paths.db_file)() as s:
        yield s


def _money(cents: int | None) -> str:
    from fin.utilities.engine import usd

    return "" if cents is None else usd(cents)


@utilities_app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        status()


@utilities_app.command()
def status() -> None:
    """Per account: last payment, months covered, cadence, next expected, open items."""
    from fin.utilities.series import utilities
    from fin.utilities.status import account_status, unassigned_count

    settings = load_settings()
    with _session() as s:
        rows = account_status(s)
        unassigned = unassigned_count(s)
        views = utilities(s, settings).accounts
    if not rows:
        typer.echo("No utility accounts. Add them to config/cards.json under utility_accounts "
                   "(docs/SPEC-utilities.md §5), then run `fin init`.")
        return

    width = max(7, *(len(r.id) for r in rows))
    pw = max(8, *(len(r.provider) for r in rows))
    typer.echo(f"  {'account':<{width}}  {'service':<8}  {'provider':<{pw}}  payments  "
               f"{'last payment':<22}  months  cadence  next expected  open items")
    for r in rows:
        v = views[r.id]
        last = f"{r.last_payment} {_money(r.last_amount_cents)}" if r.last_payment else "none yet"
        name = r.id if r.in_config else f"{r.id}*"
        cadence = f"{v.cadence_days} d" if v.payments else "-"
        nxt = v.next_expected.isoformat() if v.next_expected and r.in_config else "-"
        if v.missing:
            nxt += " late"
        months = sum(1 for c in v.series.values() if c)
        typer.echo(f"  {name:<{width}}  {r.service:<8}  {r.provider:<{pw}}  {r.payments:>8}  "
                   f"{last:<22}  {months:>6}  {cadence:>7}  {nxt:<13}  {r.open_items:>10}")
    if any(not r.in_config for r in rows):
        typer.echo("\n* no longer in config/cards.json; its past payments stay.")
    if not any(r.payments for r in rows):
        typer.echo("\nNo payments linked to these accounts yet.")
    else:
        typer.echo("\nMonths are payment months: a payment usually pays for the month before.")
    if unassigned:
        typer.echo(f"\n{unassigned} payment(s) in a bills category match no account: "
                   "`fin utilities unassigned`.")


@utilities_app.command()
def trends(
    account: Annotated[str | None, typer.Option(help="One account id")] = None,
    months: Annotated[int | None, typer.Option(help="How many months back (default 24)")] = None,
) -> None:
    """The insights, biggest dollar impact first."""
    from fin.utilities.series import utilities

    settings = load_settings()
    with _session() as s:
        res = utilities(s, settings, months=months)
    if account and account not in res.accounts:
        typer.echo(f"Unknown utility account {account!r}. Accounts: "
                   + ", ".join(res.accounts))
        raise typer.Exit(2)
    insights = res.for_account(account) if account else res.insights
    span = f"{res.months[0]} to {res.months[-1]}"
    if not insights:
        typer.echo(f"No insights for {span}.")
    else:
        typer.echo(f"Insights, {span} (by payment month):")
        for i in insights:
            who = i.account_id or i.service
            typer.echo(f"  {ICONS[i.severity]} {i.severity:<7}  {who:<10}  {i.text}")
    seasonal = [v for v in res.accounts.values() if v.account.seasonal and v.series
                and (not account or v.account.id == account)]
    if seasonal and all(len(v.series) < 13 for v in seasonal):
        from fin.utilities.engine import add_months, month_name

        first = min(min(v.series) for v in seasonal)
        when = add_months(first, 12)
        typer.echo(f"\nYear-over-year comparisons start in {month_name(when)} {when[:4]}, "
                   "once a year of payments is in the ledger.")


@utilities_app.command()
def unassigned() -> None:
    """Payments in a bills category that match no account, with their transaction ids."""
    from sqlalchemy import String, select

    from fin.db import models as m

    cards = load_cards()
    with _session() as s:
        rows = s.execute(select(m.Transaction, m.ReviewItem.details_json).join(
            m.ReviewItem, m.ReviewItem.ref_id == m.Transaction.id.cast(String)).where(
            m.ReviewItem.kind == "utility_unassigned", m.ReviewItem.status == "open")
            .order_by(m.Transaction.txn_date, m.Transaction.id)).all()
        accounts = ", ".join(s.scalars(select(m.UtilityAccount.id).where(
            m.UtilityAccount.in_config)))
    if not rows:
        typer.echo("No unassigned utility payments.")
        return
    for t, details in rows:
        reason = json.loads(details or "{}").get("reason", "")
        paid_from = _name(cards, t.card_id)
        typer.echo(f"  {t.id:>6}  {t.txn_date or t.post_date}  {_money(t.amount_cents):>10}  "
                   f"{paid_from:<28.28}  {t.description_clean:<40.40}  {reason}")
    typer.echo(f"\nAssign each: `fin utilities assign ID ACCOUNT` (accounts: {accounts}), "
               "or `none` if it isn't a utility. Add --all-from-merchant for every payment "
               "like it.")


def _name(cards, card_id: str) -> str:
    try:
        return cards.account_name(card_id)
    except KeyError:
        return card_id


@utilities_app.command()
def assign(
    txn_id: Annotated[int, typer.Argument(help="Transaction id")],
    account: Annotated[str, typer.Argument(help="Utility account id, or `none`")],
    all_from_merchant: Annotated[bool, typer.Option(
        "--all-from-merchant", help="Every payment from this merchant")] = False,
    covers: Annotated[int | None, typer.Option(
        help="Months this payment pays for (prepaid or catch-up payments)")] = None,
) -> None:
    """A correction (docs/SPEC-utilities.md §7). It survives `fin rebuild`."""
    from fin.db import models as m
    from fin.utilities.payments import (
        LINKED_TYPES,
        NOT_A_UTILITY,
        merchant_key,
        row_merchant,
    )
    from fin.utilities.series import relink

    settings = load_settings()
    with _session() as s:
        t = s.get(m.Transaction, txn_id)
        if t is None or t.type not in LINKED_TYPES:
            typer.echo(f"No purchase or refund with id {txn_id}.")
            raise typer.Exit(2)
        if account != NOT_A_UTILITY and s.get(m.UtilityAccount, account) is None:
            typer.echo(f"Unknown utility account {account!r}.")
            raise typer.Exit(2)
        if covers is not None and not 1 <= covers <= 24:
            typer.echo("--covers must be 1 to 24 months.")
            raise typer.Exit(2)
        if all_from_merchant:
            merchants = {t.merchant_id: s.get(m.Merchant, t.merchant_id).name} \
                if t.merchant_id else {}
            key = row_merchant(t, merchants)
            _set(s, "merchant", key, "utility_account", account)
            _clear(s, "transaction", t.dedupe_key, "utility_account")
            what = f"every payment from {merchant_key(key)}"
        else:
            _set(s, "transaction", t.dedupe_key, "utility_account", account)
            what = f"transaction {txn_id}"
        if covers is not None:
            _set(s, "transaction", t.dedupe_key, "utility_covers_months", str(covers))
        s.commit()
        res, _ = relink(s, load_cards(), settings)
        link = s.get(m.UtilityPayment, txn_id)
    if account == NOT_A_UTILITY:
        typer.echo(f"Marked {what} as not a utility payment.")
    else:
        typer.echo(f"Assigned {what} to {account}"
                   + (f", covering {link.covers_months} month(s)." if link else "."))
    typer.echo(f"Utilities: {res.total} payments linked, {res.unassigned} unassigned.")


def _set(s: Session, target: str, key: str, field: str, value: str) -> None:
    from sqlalchemy import select

    from fin.db import models as m

    row = s.scalar(select(m.UserOverride).where(
        m.UserOverride.target_type == target, m.UserOverride.stable_key == key,
        m.UserOverride.field == field))
    if row:
        row.value = value
    else:
        s.add(m.UserOverride(target_type=target, stable_key=key, field=field, value=value))


def _clear(s: Session, target: str, key: str, field: str) -> None:
    from sqlalchemy import delete

    from fin.db import models as m

    s.execute(delete(m.UserOverride).where(
        m.UserOverride.target_type == target, m.UserOverride.stable_key == key,
        m.UserOverride.field == field))


@utilities_app.command()
def check(
    snapshot: Annotated[bool, typer.Option(
        "--snapshot", help="Write the spending snapshot to compare against")] = False,
) -> None:
    """Invariants: linking changes no transaction, spending matches the snapshot, one account
    per payment, each series adds up. Exits non-zero on a failure."""
    from fin.utilities.check import run_checks, write_snapshot

    settings = load_settings()
    paths = data_paths(settings)
    with _session() as s:
        if snapshot:
            typer.echo(f"Wrote {write_snapshot(s, paths)}")
        results = run_checks(s, load_cards(), settings, paths, date.today())
    for c in results:
        mark = "SKIP" if c.ok is None else "PASS" if c.ok else "FAIL"
        typer.echo(f"  {mark}  {c.name}: {c.detail}")
    if any(c.ok is False for c in results):
        raise typer.Exit(1)
