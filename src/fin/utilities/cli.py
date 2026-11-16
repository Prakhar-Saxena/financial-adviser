# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`fin utilities ...`: utility bills from the payments in the ledger
(docs/SPEC-utilities.md §11). `fin utilities` alone is `fin utilities status`."""

from __future__ import annotations

import typer

from fin.config import data_paths, load_settings

utilities_app = typer.Typer(help="Utility bills from the payments already in the ledger.")


def _money(cents: int | None) -> str:
    return "" if cents is None else f"${cents / 100:,.2f}"


@utilities_app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        status()


@utilities_app.command()
def status() -> None:
    """Per account: payments linked so far, the last one, open items."""
    from fin.cli import _open_db
    from fin.db import session_factory
    from fin.utilities.status import account_status, unassigned_count

    paths = data_paths(load_settings())
    _open_db(paths)
    with session_factory(paths.db_file)() as s:
        rows = account_status(s)
        unassigned = unassigned_count(s)
    if not rows:
        typer.echo("No utility accounts. Add them to config/cards.json under utility_accounts "
                   "(docs/SPEC-utilities.md §5), then run `fin init`.")
        return

    width = max(7, *(len(r.id) for r in rows))
    pw = max(8, *(len(r.provider) for r in rows))
    typer.echo(f"  {'account':<{width}}  {'service':<8}  {'provider':<{pw}}  payments  "
               f"last payment            open items")
    for r in rows:
        last = f"{r.last_payment} {_money(r.last_amount_cents)}" if r.last_payment else "none yet"
        name = r.id if r.in_config else f"{r.id}*"
        typer.echo(f"  {name:<{width}}  {r.service:<8}  {r.provider:<{pw}}  {r.payments:>8}  "
                   f"{last:<22}  {r.open_items:>10}")
    if any(not r.in_config for r in rows):
        typer.echo("\n* no longer in config/cards.json; its past payments stay.")
    if not any(r.payments for r in rows):
        typer.echo("\nNo payments linked to these accounts yet.")
    if unassigned:
        typer.echo(f"\n{unassigned} payment(s) in a bills category match no account.")
