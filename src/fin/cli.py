"""`fin` command line (SPEC §16)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from fin import doctor as doctor_mod
from fin.ai.claude_cli import AIError, require_subscription_auth
from fin.config import (
    CONFIG_DIR,
    REPO_ROOT,
    data_paths,
    icloud_risk,
    load_cards,
    load_settings,
    load_taxonomy,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Local expense tracker.")

BROWSING_SITES = ("chase", "amex", "citi", "amazon", "costco")
READY_SITES = ("chase", "amazon", "citi", "costco", "amex")
SITE_PHASE = {"chase": 1, "amazon": 1, "citi": 2, "costco": 2, "amex": 3, "gmail": 4}
MARK = {"PASS": "✓", "FAIL": "✗", "WARN": "!", "INFO": "i"}


def _private_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.chmod(0o700)


def _open_db(paths) -> None:
    """Exit if there's no DB; otherwise bring it to the latest schema (backup first)."""
    from fin.db import ensure_current

    if not paths.db_file.exists():
        typer.echo("Run `fin init` first.")
        raise typer.Exit(1)
    upgraded_from = ensure_current(paths.db_file, paths.backups)
    if upgraded_from:
        typer.echo(f"Database schema upgraded (was {upgraded_from}; backup in {paths.backups}).")


def _not_yet(what: str, phase: int) -> None:
    typer.echo(f"{what} arrives in Phase {phase}.")
    raise typer.Exit(2)


# --------------------------------------------------------------------- init


@app.command()
def init() -> None:
    """Create the data dir (700), DB and migrations; copy config templates; check gitignore."""
    from fin.db import session_factory, upgrade
    from fin.db.seed import sync_reference

    paths = data_paths()
    risk = icloud_risk(paths.root)
    if risk:
        typer.echo(f"Refusing: {paths.root} is under {risk}, which may sync to iCloud.")
        raise typer.Exit(1)

    for d in paths.all_dirs:
        _private_dir(d)
    for f in paths.root.rglob("*"):
        if f.is_file() and "browser-profiles" not in f.parts:
            f.chmod(0o600)
    typer.echo(f"Data dir: {paths.root} (700)")

    cards_path = CONFIG_DIR / "cards.json"
    if not cards_path.exists():
        shutil.copy(CONFIG_DIR / "cards.example.json", cards_path)
        cards_path.chmod(0o600)
        typer.echo("Copied config/cards.example.json -> config/cards.json. Fill in real values.")

    upgrade(paths.db_file)
    paths.db_file.chmod(0o600)
    with session_factory(paths.db_file)() as s:
        counts = sync_reference(s, load_cards(), load_taxonomy())
    typer.echo(f"Database: {paths.db_file} at head; reference rows {counts}")

    if (REPO_ROOT / ".git").exists():
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "config", "core.hooksPath", "scripts/hooks"], check=True
        )
        typer.echo("Pre-commit guard: core.hooksPath=scripts/hooks")
    gi = [r for r in doctor_mod.check_git_hygiene() if r.name == "Gitignore rules"][0]
    typer.echo(f"Gitignore: {gi.detail}")


# ------------------------------------------------------------------- doctor


@app.command()
def doctor() -> None:
    """Security and setup checks (§7): FileVault, data dir, Claude auth, Chrome, Node, git."""
    results = doctor_mod.run_all(data_paths())
    width = max(len(r.name) for r in results)
    for r in results:
        typer.echo(f" {MARK[r.status]} {r.status:<4}  {r.name:<{width}}  {r.detail}")
    fails = [r for r in results if r.status == "FAIL"]
    typer.echo("")
    typer.echo(f"{len(fails)} failing check(s)." if fails else "All checks passed.")
    raise typer.Exit(1 if fails else 0)


# --------------------------------------------------------------------- sync


@app.command()
def sync(
    site: Annotated[str, typer.Argument(help="chase|amex|citi|amazon|costco|selftest|gmail")],
    trusted: Annotated[bool, typer.Option(help="Allow action tools without prompts")] = False,
    since: Annotated[str | None, typer.Option(help="Override start date YYYY-MM-DD")] = None,
    keep_transcript: Annotated[bool, typer.Option(help="Keep this session's transcript")] = False,
    driven: Annotated[bool, typer.Option(
        help="Run from a script (trusted mode; messages via `fin sync-send`)")] = False,
    hold: Annotated[bool, typer.Option(
        help="Driven only: open the site, report sign-in, then wait for 'continue'")] = False,
) -> None:
    """One browsing session for one site (§9.1), then import. You log in; Claude drives Chrome."""
    if driven and site in READY_SITES:
        raise typer.Exit(sync_driven(site, since=since, hold=hold))
    if site == "selftest":
        _sync_selftest(trusted=trusted, keep_transcript=keep_transcript)
        return
    if site in READY_SITES:
        code = sync_site(site, trusted=trusted, since=since, keep_transcript=keep_transcript)
        raise typer.Exit(code)
    if site in BROWSING_SITES or site == "gmail":
        _not_yet(f"`fin sync {site}`", SITE_PHASE[site])
    typer.echo(f"Unknown site {site!r}.")
    raise typer.Exit(2)


def _ready_paths():
    settings = load_settings()
    paths = data_paths(settings)
    _open_db(paths)
    try:
        method = require_subscription_auth()
    except AIError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from e
    return settings, paths, method


def _run_session(
    plan, settings, method: str, moved: list[str], steps: list[str]
) -> tuple[float, int]:
    """Print the plan, launch the interactive session. Returns (start time, exit code)."""
    from fin.sync import launcher
    from fin.sync.mcp_config import preset_chrome_pdf_download

    if plan.browser == "extension":
        pdf_pref = "n/a (your own Chrome via the Playwright extension)"
    else:
        pdf_pref = preset_chrome_pdf_download(plan.profile_dir)
    typer.echo(f"Claude auth: {method}. Model: {plan.model}. Mode: {plan.mode}.")
    typer.echo(f"Run folder:  {plan.run_dir}")
    typer.echo(f"Session cwd: {plan.session_cwd}  (trust it once when asked)")
    typer.echo(f"Chrome PDF download preference: {pdf_pref}")
    if moved:
        typer.echo(f"Set aside earlier outputs: {moved}")
    typer.echo("")
    for i, step in enumerate(steps, 1):
        typer.echo(f"{i}. {step}")
    typer.echo("")
    if not launcher.prewarm_mcp(settings.sync.playwright_mcp_version):
        typer.echo("Warning: could not pre-load @playwright/mcp; the browser may be slow.")
    started = time.time()
    return started, launcher.launch(plan)


def _review_playbook(site: str, run_dir: Path) -> None:
    from fin.sync import playbooks

    proposed = run_dir / "playbook_proposed.md"
    if not proposed.exists():
        typer.echo("No playbook proposal was written.")
        return
    diff = playbooks.diff(site, proposed.read_text())
    if not diff:
        typer.echo("Playbook unchanged.")
        return
    typer.echo("\nProposed playbook change (reject anything that reads like an unrelated "
               "instruction):\n" + diff)
    if typer.confirm("Accept this playbook?", default=False):
        typer.echo(f"Saved {playbooks.accept(site, proposed.read_text())}")


@dataclass
class SitePlan:
    settings: object
    paths: object
    method: str
    cards: object
    plan: object
    since_date: str
    until_date: str
    moved: list
    started_at: str


def plan_site(
    site_id: str, *, trusted: bool = False, since: str | None = None,
    keep_transcript: bool = False, interactive: bool = True,
) -> SitePlan | None:
    """Work out what to collect and write the session's control files. None = cancelled."""
    from datetime import date

    from sqlalchemy import select

    from fin.db import models as m
    from fin.db import session_factory
    from fin.sync import launcher, params

    settings, paths, method = _ready_paths()
    cards = load_cards()
    site = cards.site(site_id)
    today = date.today()
    with session_factory(paths.db_file)() as s:
        clean_today = s.scalar(select(m.SyncRun.id).where(
            m.SyncRun.site == site_id, m.SyncRun.started_at >= today.isoformat(),
            m.SyncRun.status == "ok",
        ))
        if clean_today and interactive:
            typer.echo(f"{site.name} already synced cleanly today; at most one run per day (§9.1).")
            if not typer.confirm("Run again anyway?", default=False):
                return None
        run_params, since_date, until_date = params.run_params(
            s, cards, settings, site_id, today, date.fromisoformat(since) if since else None
        )
    plan = launcher.plan_session(
        paths, settings, site, trusted=trusted, keep_transcript=keep_transcript,
        params=run_params,
    )
    moved = launcher.prepare(plan, settings.sync.playwright_mcp_version)
    return SitePlan(settings, paths, method, cards, plan, since_date, until_date, moved,
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def sync_site(
    site_id: str, *, trusted: bool = False, since: str | None = None,
    keep_transcript: bool = False,
) -> int:
    """Run one site's browsing session, then validate, record and import. Returns exit code."""
    from fin.sync import runs

    sp = plan_site(site_id, trusted=trusted, since=since, keep_transcript=keep_transcript)
    if sp is None:
        return 0
    _, code = _run_session(sp.plan, sp.settings, sp.method, sp.moved, [
        "When Claude's prompt appears, type: start",
        f"Chrome opens {sp.plan.site.start_url}. Log in yourself (password, 2FA). "
        "Then type: logged in",
        "Approve or deny each click the agent asks about. Deny anything that pays, buys or "
        "changes your account.",
        "When the agent says it's done, end the session with /exit.",
    ])
    runs.wait_for_quiet_transcripts()
    return finish_site(sp, code, review_playbook=True)


def sync_driven(site_id: str, *, since: str | None = None, hold: bool = False) -> int:
    from fin.sync.driver import DRIVEN_HOLD, DRIVEN_START, Driver

    sp = plan_site(site_id, trusted=True, since=since, interactive=False)
    typer.echo(f"Driven {site_id} session (trusted mode; the guard checks every action).")
    if sp.plan.browser == "extension":
        typer.echo("Uses your own Chrome via the Playwright extension: approve the connection "
                   "when Chrome asks.")
    typer.echo(f"Control: {sp.plan.control_dir / 'driver'}  (status, driver.log, inbox/)")
    typer.echo(f"Send messages with: fin sync-send {site_id} 'logged in'   (or '/exit')")
    code = Driver(sp.plan, DRIVEN_HOLD if hold else DRIVEN_START).run()
    return finish_site(sp, code, review_playbook=False)


def finish_site(sp: SitePlan, code: int, *, review_playbook: bool) -> int:
    """After the session: validate the manifest, record the run, import, show the playbook."""
    from fin.db import models as m
    from fin.db import session_factory
    from fin.ingest import inbox, load_parsers
    from fin.ingest.manifest import validate_run
    from fin.sync import params, playbooks, runs

    plan, paths, site_id = sp.plan, sp.paths, sp.plan.site.id
    log = runs.read_guard_log(plan.guard_log)
    check = validate_run(plan.run_dir, site_id, params.allowed_card_ids(sp.cards, site_id),
                         plan.run_date, imported_dir=paths.raw / site_id / plan.run_date)
    typer.echo(f"\nSession ended (claude exited {code}). "
               f"guard.log: {len(log)} decisions, {runs.guard_blocks(log)} blocks.")
    if check.ok:
        typer.echo(f"Manifest: valid, {len(check.manifest['files'])} files.")
    else:
        typer.echo("Manifest problems (nothing imported):")
        for e in check.errors:
            typer.echo(f"  - {e}")
    if check.unlisted:
        typer.echo(f"Files not in the manifest (left in place): {check.unlisted}")
    proposed = plan.run_dir / "playbook_proposed.md"
    proposal = proposed.read_text() if proposed.exists() else None
    Session = session_factory(paths.db_file)
    with Session() as s:
        run = m.SyncRun(
            site=site_id, started_at=sp.started_at, since_date=sp.since_date,
            until_date=sp.until_date, run_dir=str(plan.run_dir), model=plan.model,
            mode=plan.mode, status="running", guard_blocks=runs.guard_blocks(log),
            report_json=json.dumps(check.report) if check.report else None,
        )
        s.add(run)
        s.commit()
        gaps = inbox.record_gaps(s, check.report, run.id) if check.report else 0
        result = None
        if check.ok:
            load_parsers()
            result = inbox.import_run(s, paths, plan.run_dir, check, sync_run_id=run.id)
        run = s.get(m.SyncRun, run.id)
        run.ended_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        run.files_imported = result.count("imported") if result else 0
        if not check.ok:
            run.status = "failed"
        elif gaps or check.unlisted or (result and result.count("failed")):
            run.status = "partial"
        else:
            run.status = "ok"
        s.commit()
        status = run.status

    if check.report:
        for c in check.report["collected"]:
            typer.echo(f"  collected {c['count']:>3} {c['kind']:<20} {c['card_id'] or ''}")
        for g in check.report["gaps"]:
            typer.echo(f"  GAP {g['kind']} {g['card_id'] or ''}: {g['description']}")
    if result:
        typer.echo(f"Import: {result.summary()}.")
        waiting = [r.file for r in result.results if r.status == "no_parser"]
        if waiting:
            typer.echo(f"  {len(waiting)} files wait in {plan.run_dir} for their parser.")
        for r in result.results:
            if r.status == "failed":
                typer.echo(f"  FAILED {r.file}: {r.detail}")
    typer.echo(f"Sync run: {status}. Gaps recorded for review: {gaps}.")
    if review_playbook:
        _review_playbook(site_id, plan.run_dir)
    elif proposal is not None:
        pending = sp.plan.control_dir / "playbook_proposed.md"
        pending.write_text(proposal)
        diff = playbooks.diff(site_id, proposal)
        typer.echo("\nPlaybook change awaiting your approval (`fin playbook accept "
                   f"{site_id}`):\n" + (diff or "(no change)"))
    return 0 if status in ("ok", "partial") else 1


@app.command("sync-send")
def sync_send(site: str, text: str) -> None:
    """Send a message ('logged in', '/exit', ...) to the running driven session for SITE."""
    from fin.sync.driver import live_session, send

    control = live_session(data_paths(load_settings()).sync_sessions / site)
    if control is None:
        typer.echo(f"No running driven {site} session.")
        raise typer.Exit(1)
    send(control, text)
    typer.echo(f"Queued for {site}: {text}")


playbook_app = typer.Typer(help="Playbook proposals from driven syncs.")
app.add_typer(playbook_app, name="playbook")


@playbook_app.command("accept")
def playbook_accept(site: str) -> None:
    """Accept the newest pending playbook proposal for SITE (after reading its diff)."""
    from fin.sync import playbooks

    root = data_paths(load_settings()).sync_sessions / site
    pending = sorted(root.glob("*/playbook_proposed.md")) if root.exists() else []
    if not pending:
        typer.echo(f"No pending playbook proposal for {site}.")
        raise typer.Exit(1)
    saved = playbooks.accept(site, pending[-1].read_text())
    pending[-1].unlink()
    typer.echo(f"Saved {saved}")


def _sync_selftest(*, trusted: bool, keep_transcript: bool) -> None:
    from fin.sync import launcher, runs

    settings, paths, method = _ready_paths()
    plan = launcher.plan_session(
        paths, settings, launcher.SELFTEST_SITE, trusted=trusted, keep_transcript=keep_transcript
    )
    moved = launcher.prepare(plan, settings.sync.playwright_mcp_version)
    started, code = _run_session(plan, settings, method, moved, [
        "When Claude's prompt appears, type: start",
        "Chrome opens example.com. When you can see it, type: logged in",
        "When the agent says it's done, end the session with /exit.",
    ])
    runs.wait_for_quiet_transcripts()

    checks, mcheck, session_files = runs.selftest_checks(plan, started)
    typer.echo("")
    typer.echo(f"Selftest results (claude exited {code}):")
    for c in checks:
        typer.echo(f" {'✓' if c.ok else '✗'} {c.name}: {c.detail}")
    typer.echo(f" i Playwright session files (--save-session/--codegen): {session_files or 'none'}")
    if mcheck.unlisted:
        typer.echo(f" i Files not in the manifest: {mcheck.unlisted}")
    log = runs.read_guard_log(plan.guard_log)
    typer.echo(f" i guard.log: {len(log)} decisions, {runs.guard_blocks(log)} blocks")
    _record_selftest_run(paths, plan, checks, log)
    _review_playbook("selftest", plan.run_dir)
    raise typer.Exit(0 if all(c.ok for c in checks) else 1)


def _record_selftest_run(paths, plan, checks, log) -> None:
    from fin.db import models as m
    from fin.db import session_factory
    from fin.sync.runs import guard_blocks

    ok = all(c.ok for c in checks)
    report = {"checks": [{"name": c.name, "ok": c.ok} for c in checks]}
    with session_factory(paths.db_file)() as s:
        s.add(m.SyncRun(
            site=plan.site.id, ended_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            run_dir=str(plan.run_dir), model=plan.model, mode=plan.mode,
            status="ok" if ok else "partial", files_imported=0,
            guard_blocks=guard_blocks(log), report_json=json.dumps(report),
        ))
        s.commit()


# ------------------------------------------------------------ later phases


WEEKLY_ORDER = ("chase", "citi", "amex", "amazon", "costco")


@app.command()
def weekly(
    only: Annotated[str | None, typer.Option(help="e.g. chase,amazon")] = None,
    no_ai: Annotated[bool, typer.Option("--no-ai")] = False,
    open_dashboard: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Sync each site in turn (you log in), then process, back up and open the dashboard."""
    import subprocess as sp
    import sys

    wanted = [x.strip() for x in only.split(",")] if only else list(WEEKLY_ORDER)
    unknown = [x for x in wanted if x not in WEEKLY_ORDER]
    if unknown:
        typer.echo(f"Unknown sites: {unknown}")
        raise typer.Exit(2)
    results: dict[str, str] = {}
    for site in WEEKLY_ORDER:
        if site not in wanted:
            continue
        if site not in READY_SITES:
            results[site] = f"skipped (arrives in Phase {SITE_PHASE[site]})"
            continue
        typer.echo(f"\n=== {site} ===")
        try:
            code = sync_site(site)
        except typer.Exit as e:
            code = e.exit_code
        results[site] = "ok" if code == 0 else f"exit {code}"
        if code != 0 and not typer.confirm("Continue with the next site?", default=True):
            break

    typer.echo("\n=== process ===")
    me = [sys.executable, "-m", "fin.cli"]
    sp.run([*me, "process", *(["--no-ai"] if no_ai else [])], check=False)
    typer.echo("\n=== backup ===")
    sp.run([*me, "backup"], check=False)
    typer.echo("\nWeekly summary:")
    for site, r in results.items():
        typer.echo(f"  {site:<8} {r}")
    if open_dashboard:
        typer.echo("\nStarting the dashboard (Ctrl+C to stop)...")
        sp.run([*me, "serve", "--open"], check=False)


@app.command("import")
def import_(
    file: Annotated[Path | None, typer.Argument(help="A manually downloaded file")] = None,
    inbox: Annotated[bool, typer.Option("--inbox", help="Import every run folder")] = False,
    card: Annotated[str | None, typer.Option(help="Card id, for a CSV")] = None,
) -> None:
    """Import a file or the inbox (§9.6). Re-importing never creates duplicates."""
    from fin.db import session_factory
    from fin.ingest import inbox as ib
    from fin.ingest import load_parsers
    from fin.ingest.manifest import validate_run
    from fin.sync.params import allowed_card_ids

    load_parsers()
    settings = load_settings()
    paths = data_paths(settings)
    _open_db(paths)
    cards = load_cards()
    Session = session_factory(paths.db_file)
    if bool(file) == inbox:
        typer.echo("Give either a FILE or --inbox.")
        raise typer.Exit(2)

    if inbox:
        dirs = ib.inbox_run_dirs(paths)
        if not dirs:
            typer.echo("Inbox is empty.")
        for run_dir in dirs:
            site, run_date = run_dir.parent.name, run_dir.name
            check = validate_run(run_dir, site, allowed_card_ids(cards, site), run_date,
                                 imported_dir=paths.raw / site / run_date)
            if not check.ok:
                typer.echo(f"{site}/{run_date}: manifest problems, skipped:")
                for e in check.errors:
                    typer.echo(f"  - {e}")
                continue
            with Session() as s:
                res = ib.import_run(s, paths, run_dir, check)
            typer.echo(f"{site}/{run_date}: {res.summary()}")
            for r in res.results:
                if r.status in ("failed", "missing"):
                    typer.echo(f"  {r.status.upper()} {r.file}: {r.detail}")
        _print_import_problems(Session)
        return

    path = file.expanduser().resolve()
    found = ib.detect(path)
    if not found:
        typer.echo(f"Can't tell what {path.name} is. Supported: Chase activity CSV, Chase "
                   "statement PDF, Amazon invoice PDF.")
        raise typer.Exit(1)
    site, kind = found
    card_id = card
    if kind == "statement" and not card_id:
        card_id = ib.statement_card(path, site, cards)
    if kind in ("transactions", "statement") and not card_id:
        ids = ", ".join(c.id for c in cards.cards_for_issuer(site))
        typer.echo(f"Which card is {path.name}? Pass --card (one of: {ids}).")
        raise typer.Exit(2)
    with Session() as s:
        r = ib.import_file(s, paths, path, site, kind, card_id)
    typer.echo(f"{path.name}: {r.status} {r.detail}")
    _print_import_problems(Session)


def _print_import_problems(Session) -> None:
    from sqlalchemy import select

    from fin.db import models as m

    with Session() as s:
        partial = s.scalars(select(m.Import).where(m.Import.status == "partial")
                            .order_by(m.Import.id.desc()).limit(20)).all()
        for imp in partial:
            typer.echo(f"  check {Path(imp.file_path).name}: {imp.error}")


@app.command()
def process(no_ai: Annotated[bool, typer.Option("--no-ai", help="Cache only, no AI")] = False
            ) -> None:
    """Link, categorize, match, allocate, reconcile, review items (§16)."""
    from fin.db import session_factory
    from fin.pipeline import process as run

    settings = load_settings()
    paths = data_paths(settings)
    _open_db(paths)
    if not no_ai:
        try:
            require_subscription_auth()
        except AIError as e:
            typer.echo(f"{e}\nRunning without AI.")
            no_ai = True
    with session_factory(paths.db_file)() as s:
        r = run(s, paths, settings, load_cards(), load_taxonomy(), use_ai=not no_ai)
    lk = r.link
    typer.echo(f"Statements: {lk.linked} lines linked to CSV rows, {lk.created} filled from "
               f"statements, {lk.replaced} replaced by CSV rows, {lk.attributed} attributed.")
    typer.echo(f"Rules: {r.rule_matched} transactions. AI merchants: {r.ai_merchants.cached} "
               f"cached, {r.ai_merchants.classified} new ({r.ai_merchants.calls} calls). "
               f"AI items: {r.ai_items.cached} cached, {r.ai_items.classified} new "
               f"({r.ai_items.calls} calls).")
    if r.ai_paused:
        typer.echo("AI paused: subscription limit reached. Rerun `fin process` later.")
    mm = r.match
    typer.echo(f"Amazon: {mm.by_method} matched, {mm.unmatched} unmatched, {mm.split} split "
               f"across items. Amazon purchases on other cards: {r.amazon_elsewhere}.")
    co = r.costco
    typer.echo(f"Costco: {co.matched} receipts matched, {co.unmatched_charges} warehouse charges "
               f"without a receipt, {co.unmatched_receipts} receipts without a charge.")
    typer.echo(f"Ledger: {r.ledger}. Unexpected merchants on dedicated cards: {r.unexpected}.")
    typer.echo(f"Open review items: {r.reviews_open or 'none'} "
               f"(auto-resolved this run: {r.reviews_closed}).")


@app.command()
def review() -> None:
    """Terminal summary of open review items."""
    from sqlalchemy import select

    from fin.db import models as m
    from fin.db import session_factory

    paths = data_paths(load_settings())
    _open_db(paths)
    with session_factory(paths.db_file)() as s:
        items = s.scalars(select(m.ReviewItem).where(m.ReviewItem.status == "open")
                          .order_by(m.ReviewItem.kind, m.ReviewItem.id)).all()
        if not items:
            typer.echo("No open review items.")
            return
        kind = None
        for it in items:
            if it.kind != kind:
                kind = it.kind
                typer.echo(f"\n{kind} ({sum(1 for x in items if x.kind == kind)})")
            typer.echo(f"  #{it.id} {it.ref_table}:{it.ref_id} {_review_summary(s, it)}")


def _review_summary(s, item) -> str:
    from fin.db import models as m

    d = json.loads(item.details_json or "{}")
    if item.ref_table == "transactions":
        t = s.get(m.Transaction, int(item.ref_id))
        if t:
            head = f"{t.txn_date or t.post_date} ${t.amount_cents / 100:,.2f} {t.description_clean}"
            return f"{head} | {d.get('reason') or d.get('category_id') or ''}"
    if item.ref_table == "orders":
        o = s.get(m.Order, int(item.ref_id))
        if o:
            return f"order {o.external_id} {o.order_date} ${o.total / 100:,.2f} | {d.get('reason')}"
    return json.dumps(d)[:160]


@app.command()
def serve(open_browser: Annotated[bool, typer.Option("--open/--no-open")] = False) -> None:
    """API and dashboard on http://127.0.0.1:8765."""
    import webbrowser

    import uvicorn

    from fin.api.app import WEB_DIST, create_app

    settings = load_settings()
    paths = data_paths(settings)
    _open_db(paths)
    url = f"http://{settings.server.host}:{settings.server.api_port}"
    if not (WEB_DIST / "index.html").exists():
        typer.echo("Dashboard not built yet: run `npm --prefix web install && npm --prefix web "
                   "run build`. Serving the API only.")
    typer.echo(f"Dashboard: {url}")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(create_app(paths.db_file), host=settings.server.host,
                port=settings.server.api_port, log_level="warning")


@app.command()
def rebuild() -> None:
    """Rebuild derived tables from raw/, keeping user overrides and rules."""
    from fin.db import session_factory
    from fin.rebuild import rebuild as do_rebuild

    paths = data_paths(load_settings())
    _open_db(paths)
    with session_factory(paths.db_file)() as s:
        res = do_rebuild(s)
    typer.echo(f"Re-parsed {res.reparsed} files from raw/.")
    for f in res.failed:
        typer.echo(f"  FAILED {f}")
    if res.missing:
        typer.echo(f"  Missing from raw/: {res.missing}")
    _print_import_problems(session_factory(paths.db_file))


@app.command()
def backup() -> None:
    """SQLite online backup to backups/."""
    from fin.backup import backup as do_backup

    paths = data_paths(load_settings())
    _open_db(paths)
    typer.echo(f"Backup: {do_backup(paths.db_file, paths.backups)}")


if __name__ == "__main__":
    app()
