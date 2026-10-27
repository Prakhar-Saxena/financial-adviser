# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`fin cards ...`: the card optimizer's commands (docs/SPEC-cards.md §14)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.orm import Session

from fin.cards.models import DOC_TYPES
from fin.config import data_paths, load_cards, load_settings

cards_app = typer.Typer(no_args_is_help=True, help="Card optimizer: which card to use where.")


@contextmanager
def _session() -> Iterator[Session]:
    from fin.cli import _open_db
    from fin.db import session_factory

    paths = data_paths(load_settings())
    _open_db(paths)
    with session_factory(paths.db_file)() as s:
        yield s


def _card(card_id: str):
    try:
        return load_cards().card(card_id)
    except KeyError:
        typer.echo(f"Unknown card {card_id!r}. Cards: "
                   + ", ".join(c.id for c in load_cards().cards))
        raise typer.Exit(2) from None


@cards_app.command()
def status() -> None:
    """Per card: profile version, pending changes, documents, last checked, stale or not."""
    from fin.cards.status import card_status

    settings = load_settings()
    with _session() as s:
        rows = card_status(s, load_cards(), settings.cards.refresh_days)

    width = max(len(r.card_id) for r in rows)
    typer.echo(f"  {'card':<{width}}  profile   pending  documents          last checked  stale")
    for r in rows:
        active = f"v{r.active_version}" if r.active_version else "none"
        pending = f"v{r.pending_version}" if r.pending_version else "-"
        checked = r.last_checked[:10] if r.last_checked else "never"
        typer.echo(f"  {r.card_id:<{width}}  {active:<8}  {pending:<7}  {r.docs_summary:<17}  "
                   f"{checked:<12}  {'yes' if r.stale else 'no'}")
    if not any(r.active_version or r.pending_version or r.docs for r in rows):
        typer.echo("\nNo card terms yet. `fin cards refresh --card ID` builds one card's.")
    elif any(r.pending_version for r in rows):
        typer.echo("\nPending: review with `fin cards accept CARD` or the dashboard's Card terms.")


@cards_app.command()
def refresh(
    card: Annotated[str | None, typer.Option(help="One card id (default: every card)")] = None,
    discover: Annotated[bool, typer.Option("--discover", help="Look for documents again")] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-extract even if cached")] = False,
    retext: Annotated[bool, typer.Option(
        "--retext", help="Re-read stored files' text (after a fin update); no download")] = False,
    if_due: Annotated[bool, typer.Option("--if-due", help="Skip cards checked recently")] = False,
    trigger: Annotated[str, typer.Option(hidden=True)] = "cli",
) -> None:
    """Discover, download and read each card's documents; propose profile changes (§7)."""
    from fin.cards.refresh import RefreshBusyError
    from fin.cards.refresh import refresh as run_refresh

    settings, cards = load_settings(), load_cards()
    if card:
        _card(card)
    quiet = trigger == "weekly"

    def progress(card_id: str, step: str) -> None:
        if not quiet:
            typer.echo(f"  {card_id}: {step}")

    with _session() as s:
        try:
            res = run_refresh(s, data_paths(settings), settings, cards,
                              card_ids=[card] if card else None, discover=discover, force=force,
                              retext=retext, if_due=if_due, trigger=trigger, progress=progress)
        except RefreshBusyError as e:
            typer.echo(f"Card refresh: {e}.")
            raise typer.Exit(1) from None
    calls = sum(r.ai_calls for r in res.reports)
    if quiet:
        done = [r for r in res.reports if r.outcome != "not due"]
        pending = [r.card_id for r in res.reports if r.outcome == "pending"]
        typer.echo(f"Card terms: {len(done)} checked, {calls} AI calls, status {res.status}"
                   + (f"; waiting for approval: {', '.join(pending)}" if pending else ""))
    else:
        typer.echo("")
        for r in res.reports:
            v = f" v{r.version}" if r.version else ""
            extra = f"  ({r.error})" if r.error else ""
            typer.echo(f"  {r.card_id:<24} {r.outcome}{v}: {r.docs_checked} docs checked, "
                       f"{r.docs_changed} changed, {r.ai_calls} AI calls{extra}")
        typer.echo(f"\nRefresh {res.status}: {calls} AI calls.")
        if any(r.outcome == "pending" for r in res.reports):
            typer.echo("Review the new terms: `fin cards accept CARD` (or the dashboard).")
    if res.message:
        typer.echo(res.message)
    if res.status in ("failed", "limited"):
        raise typer.Exit(1)


def _show_pending(s: Session, card_id: str):
    from fin.cards import compose

    pending = compose.current(s, card_id, "pending")
    if pending is None:
        typer.echo(f"{card_id}: nothing pending.")
        raise typer.Exit(1)
    d = json.loads(pending.diff_json or "{}")
    typer.echo(f"{card_id} v{pending.version} ({compose.summarize(d)})")
    if compose.current(s, card_id, "active") is None:
        for line in format_profile(compose.load(pending)):
            typer.echo(f"  {line}")
    else:
        for line in format_diff(d):
            typer.echo(f"  {line}")
    return pending


def format_profile(p) -> list[str]:
    out = [f"currency: {p.currency} ({p.currency_name})",
           f"annual fee: ${p.annual_fee_cents.value / 100:,.2f}; "
           f"foreign transaction fee: {p.foreign_txn_fee_pct.value:g}%"]
    for r in p.earn_rules:
        scope = "all other purchases" if r.is_base else ", ".join(r.categories + r.merchant_names)
        cap = (f", up to ${r.cap.amount_cents / 100:,.0f}/{r.cap.period} then {r.cap.rate_after:g}"
               if r.cap else "")
        cond = "".join(f", {x}" for x in (
            r.channel if r.channel != "any" else "", r.geography if r.geography != "any" else "",
            *(q.kind for q in r.requirements)) if x)
        out.append(f"earn {r.rate:g}x: {scope}{cap}{cond}")
        out.append(f"    \"{r.evidence[0].quote}\"")
    for c in p.credits:
        out.append(f"credit: {c.name}, ${c.amount_cents / 100:,.2f}/{c.period}"
                   + (" (enrollment)" if c.enrollment_required else ""))
    for b in p.benefits:
        out.append(f"benefit: {b.name}" + (f" ({b.limits})" if b.limits else ""))
    return out


def format_diff(d: dict) -> list[str]:
    out = []
    for x in d.get("added", []):
        out.append(f"+ {x['section']}: {x['item']}")
    for x in d.get("removed", []):
        out.append(f"- {x['section']}: {x['item']}")
    for x in d.get("changed", []):
        fields = "; ".join(f"{k}: {v[0]} -> {v[1]}" for k, v in x["fields"].items())
        out.append(f"~ {x['section']}: {x['item']}: {fields}")
    if d.get("reworded"):
        out.append(f"  ({len(d['reworded'])} reworded)")
    return out


@cards_app.command()
def accept(
    card: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask")] = False,
) -> None:
    """Show the pending version's diff, then publish it."""
    from fin.cards import compose

    _card(card)
    with _session() as s:
        pending = _show_pending(s, card)
        if not yes and not typer.confirm("Accept this version?", default=False):
            raise typer.Exit(1)
        compose.accept(s, card, pending.version)
        s.commit()
        typer.echo(f"{card}: v{pending.version} is now active.")


@cards_app.command()
def reject(
    card: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask")] = False,
) -> None:
    """Show the pending version's diff, then reject it. The same terms aren't proposed again."""
    from fin.cards import compose

    _card(card)
    with _session() as s:
        pending = _show_pending(s, card)
        if not yes and not typer.confirm("Reject this version?", default=False):
            raise typer.Exit(1)
        compose.reject(s, card, pending.version)
        s.commit()
        typer.echo(f"{card}: v{pending.version} rejected.")


@cards_app.command()
def sources(card: Annotated[str | None, typer.Option(help="One card id")] = None) -> None:
    """Each card's documents: type, status, last checked, last changed, URL."""
    from sqlalchemy import select

    from fin.db import models as m

    if card:
        _card(card)
    with _session() as s:
        q = select(m.CardDocument).order_by(m.CardDocument.card_id, m.CardDocument.doc_type)
        if card:
            q = q.where(m.CardDocument.card_id == card)
        docs = s.scalars(q).all()
        if not docs:
            typer.echo("No documents yet.")
            return
        for d in docs:
            checked = (d.last_checked_at or "never")[:10]
            changed = (d.last_changed_at or "-")[:10]
            typer.echo(f"  {d.card_id:<24} {d.doc_type:<21} {d.status:<13} checked {checked:<10} "
                       f"changed {changed:<10} {d.url}")
            if d.note:
                typer.echo(f"  {'':<24} {'':<21} note: {d.note}")


@cards_app.command("add-source")
def add_source(
    card: str,
    url: str,
    type_: Annotated[str, typer.Option("--type", help="|".join(DOC_TYPES))],
) -> None:
    """Add a document URL I found myself. It must be on the card's allowlist."""
    from sqlalchemy import select

    from fin.cards.discover import normalize_url
    from fin.cards.fetch import host_allowed
    from fin.db import models as m

    c = _card(card)
    if type_ not in DOC_TYPES:
        typer.echo(f"--type must be one of {', '.join(DOC_TYPES)}")
        raise typer.Exit(2)
    settings, cards = load_settings(), load_cards()
    norm = normalize_url(url)
    domains = settings.cards.domains_for(c, cards.site(c.issuer))
    if norm is None or not url.lower().startswith("https://"):
        typer.echo("Only https:// URLs.")
        raise typer.Exit(2)
    host = norm.split("/")[2]
    if not host_allowed(host, domains):
        typer.echo(f"{host} isn't on this card's allowlist ({', '.join(domains)}). If it is "
                   f"really the issuer's, add it under [cards.doc_domains] {card} first.")
        raise typer.Exit(2)
    with _session() as s:
        d = s.scalars(select(m.CardDocument).where(m.CardDocument.card_id == card,
                                                   m.CardDocument.url == norm)).first()
        if d is None:
            s.add(m.CardDocument(card_id=card, url=norm, doc_type=type_, origin="user_url",
                                 status="active"))
        else:
            d.doc_type, d.status, d.origin, d.note = type_, "active", "user_url", None
        s.commit()
    typer.echo(f"Added. `fin cards refresh --card {card}` downloads it.")


@cards_app.command("add-doc")
def add_doc(
    card: str,
    file: Path,
    type_: Annotated[str, typer.Option("--type", help="|".join(DOC_TYPES))],
    url: Annotated[str | None, typer.Option(help="Where it came from")] = None,
) -> None:
    """Add a PDF or saved page I downloaded myself (the fallback for blocked pages)."""
    from fin.cards.refresh import add_doc as store_doc

    c = _card(card)
    if type_ not in DOC_TYPES:
        typer.echo(f"--type must be one of {', '.join(DOC_TYPES)}")
        raise typer.Exit(2)
    if not file.is_file():
        typer.echo(f"No such file: {file}")
        raise typer.Exit(2)
    settings, cards = load_settings(), load_cards()
    with _session() as s:
        try:
            d, outcome = store_doc(s, data_paths(settings), settings, cards, c, file, type_, url)
        except ValueError as e:
            typer.echo(str(e))
            raise typer.Exit(2) from None
        typer.echo(f"{file.name}: {outcome}" + (f" ({d.note})" if d.note else "")
                   + f". `fin cards refresh --card {card}` reads it.")
