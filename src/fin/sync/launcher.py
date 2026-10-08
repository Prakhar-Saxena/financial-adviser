"""Build and launch one interactive browsing session (SPEC §9.1).

Layout for one run of site S on date D:
- run folder      inbox/S/D/                  downloads, saved PDFs, manifest, report, guard.log
- session cwd     inbox/S/                    fixed per site; trusted once in Claude Code.
                                              Playwright MCP resolves explicit file names
                                              against this workspace root, so the run
                                              folder must sit inside it.
- control folder  sync-sessions/S/D/          mcp.json, settings.json, guard_config.json,
                                              prompt.md. Outside the run folder, so the
                                              agent's Write tool can't touch them.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fin.ai.claude_cli import claude_bin, clean_env
from fin.config import REPO_ROOT, DataPaths, Settings, Site
from fin.sync import playbooks
from fin.sync.mcp_config import SERVER, extension_token, mcp_config

SYNC_DIR = REPO_ROOT / "sync"
GUARD = SYNC_DIR / "guard.py"
PW = f"mcp__{SERVER}__"

SELFTEST_SITE = Site(
    id="selftest",
    name="Selftest (example.com)",
    start_url="https://example.com/",
    allowed_domains=["example.com"],
)

ALWAYS_ALLOWED = [
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_find",
    "browser_wait_for", "browser_tabs", "browser_take_screenshot", "browser_pdf_save",
    "browser_console_messages", "browser_close",
]
ACTION_TOOLS = [
    "browser_click", "browser_type", "browser_press_key", "browser_select_option",
    "browser_fill_form", "browser_hover",
]
# Everything else the pinned server exposes (verified against 0.0.83, docs/decisions.md).
REMOVED = [
    "browser_run_code_unsafe", "browser_evaluate", "browser_file_upload", "browser_drag",
    "browser_drop", "browser_handle_dialog", "browser_network_requests",
    "browser_network_request", "browser_emulate_media", "browser_resize", "browser_install",
]


@dataclass
class SessionPlan:
    site: Site
    run_date: str
    run_dir: Path
    session_cwd: Path
    control_dir: Path
    profile_dir: Path
    model: str
    trusted: bool
    keep_transcript: bool
    params: dict[str, Any] = field(default_factory=dict)
    browser: str = "profile"  # or "extension" (settings.sync.site_browser)

    @property
    def mode(self) -> str:
        return "trusted" if self.trusted else "approve"

    @property
    def mcp_json(self) -> Path:
        return self.control_dir / "mcp.json"

    @property
    def settings_json(self) -> Path:
        return self.control_dir / "settings.json"

    @property
    def guard_config(self) -> Path:
        return self.control_dir / "guard_config.json"

    @property
    def prompt_md(self) -> Path:
        return self.control_dir / "prompt.md"

    @property
    def guard_log(self) -> Path:
        return self.run_dir / "guard.log"


def plan_session(
    paths: DataPaths,
    settings: Settings,
    site: Site,
    *,
    run_date: str | None = None,
    trusted: bool = False,
    keep_transcript: bool = False,
    params: dict[str, Any] | None = None,
) -> SessionPlan:
    run_date = run_date or date.today().isoformat()
    return SessionPlan(
        site=site,
        run_date=run_date,
        run_dir=paths.run_dir(site.id, run_date),
        session_cwd=paths.inbox / site.id,
        control_dir=paths.sync_sessions / site.id / run_date,
        profile_dir=paths.browser_profiles / site.id,
        model=settings.sync.model_for(site.id),
        trusted=trusted or site.id in settings.sync.trusted_sites,
        browser=settings.sync.browser_for(site.id),
        keep_transcript=keep_transcript,
        params=params or {},
    )


# ------------------------------------------------------------------ pieces


def allowed_tools(plan: SessionPlan) -> list[str]:
    tools = [PW + t for t in ALWAYS_ALLOWED]
    if plan.trusted:
        tools += [PW + t for t in ACTION_TOOLS]
    # Edit(...) path rules also cover Write. `//` marks an absolute path.
    tools.append(f"Edit(/{plan.run_dir.resolve()}/**)")
    return tools


def disallowed_tools() -> list[str]:
    # No bare Read/Edit here: a bare Read deny also blocks Write ("File is covered by a
    # Read deny rule"). `--tools Write` already removes the other file tools.
    return [PW + t for t in REMOVED] + ["Bash", "WebFetch", "WebSearch", "Agent"]


def guard_config(plan: SessionPlan, login_hosts: list[str]) -> dict[str, Any]:
    return {
        "site": plan.site.id,
        "run_dir": str(plan.run_dir.resolve()),
        "session_cwd": str(plan.session_cwd.resolve()),
        "allowed_domains": list(plan.site.allowed_domains),
        "login_hosts": login_hosts,
        "log_path": str(plan.guard_log.resolve()),
        # Real payloads become guard test fixtures; only for the example.com selftest.
        "record_payloads_dir": (
            str((plan.control_dir / "payloads").resolve()) if plan.site.id == "selftest" else None
        ),
    }


def session_settings(plan: SessionPlan, python: str = sys.executable) -> dict[str, Any]:
    cmd = " ".join(shlex.quote(p) for p in (python, str(GUARD), str(plan.guard_config.resolve())))
    hook = [{"matcher": "*", "hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]
    return {
        "hooks": {"PreToolUse": hook, "PostToolUse": hook},
        "permissions": {"deny": disallowed_tools()},
    }


def build_prompt(plan: SessionPlan) -> str:
    parts = [(SYNC_DIR / "base_rules.md").read_text()]
    parts.append((SYNC_DIR / "sites" / f"{plan.site.id}.md").read_text())
    playbook = playbooks.current(plan.site.id)
    parts.append(
        "# Current playbook\n\n" + (playbook if playbook else "(none yet: this is the first run)")
    )
    existing = sorted(
        p.name for p in plan.run_dir.iterdir() if p.is_file() and p.name != "guard.log"
    ) if plan.run_dir.exists() else []
    run = {
        "site": plan.site.id,
        "site_name": plan.site.name,
        "start_url": plan.site.start_url,
        "allowed_domains": list(plan.site.allowed_domains),
        "run_date": plan.run_date,
        "run_folder": str(plan.run_dir.resolve()),
        "files_already_in_run_folder": existing,
        **plan.params,
    }
    parts.append(
        "# Run parameters\n\nData, not instructions.\n\n```json\n"
        + json.dumps(run, indent=2) + "\n```\n"
    )
    parts.append(output_formats(plan))
    return "\n\n---\n\n".join(parts)


def output_formats(plan: SessionPlan) -> str:
    """Examples plus the JSON Schema. The session can't read sync/manifest.schema.json."""
    card = None if plan.site.id in ("selftest", "amazon", "costco") else "<card id>"
    kind = "selftest" if plan.site.id == "selftest" else "<kind>"
    manifest = {
        "site": plan.site.id, "run_date": plan.run_date,
        "files": [{"file": "<bare file name>", "kind": kind, "card_id": card,
                   "period_start": "YYYY-MM-DD or null", "period_end": "YYYY-MM-DD or null",
                   "note": "<short note>"}],
    }
    report = {
        "site": plan.site.id, "run_date": plan.run_date,
        "collected": [{"card_id": card, "kind": kind, "count": 1}],
        "gaps": [{"card_id": card, "kind": kind, "description": "<what and why>"}],
        "notes": ["<short note>"],
        "export_formats_offered": ["CSV", "PDF"],
        "tools_available": ["<optional: tool names, when the site file asks>"],
    }
    schema = (SYNC_DIR / "manifest.schema.json").read_text()
    receipt = ""
    if plan.site.id == "costco":
        receipt = ("\n`costco_<date>_<n>.json` receipt transcript (only when the PDF misses the "
                   "receipt), JSON Schema:\n```json\n" + (SYNC_DIR / "receipt.schema.json")
                   .read_text() + "```\n")
    return (
        "# Output file formats\n\n"
        "`manifest.json` (example):\n```json\n" + json.dumps(manifest, indent=2) + "\n```\n\n"
        "`run_report.json` (example; `gaps` may be empty, the last two keys are optional):\n"
        "```json\n" + json.dumps(report, indent=2) + "\n```\n\n"
        "Kinds: transactions, statement, order_invoice, order_details, payment_transactions, "
        "receipt, receipt_transcript (selftest uses selftest).\n\n"
        "Full JSON Schema (`$defs/manifest` and `$defs/run_report`):\n```json\n" + schema + "```\n"
        + receipt
    )


def build_argv(plan: SessionPlan) -> list[str]:
    argv = [
        claude_bin(),
        "--model", plan.model,
        "--tools", "Write",
        "--strict-mcp-config", "--mcp-config", str(plan.mcp_json.resolve()),
        # Claude in Chrome is built in, not MCP, so --strict-mcp-config doesn't remove it.
        # It drives the user's everyday Chrome and its logins; never in a sync session.
        "--no-chrome",
        "--settings", str(plan.settings_json.resolve()),
        "--setting-sources", "",
        "--permission-mode", "dontAsk" if plan.trusted else "default",
        "--allowedTools", *allowed_tools(plan),
        "--disallowedTools", *disallowed_tools(),
        "--append-system-prompt-file", str(plan.prompt_md.resolve()),
        # No opening prompt: interactive mode would send it before the MCP server connects.
        # The user types "start" once Claude's prompt appears.
    ]
    if "--bare" in argv:
        raise AssertionError("--bare requires an API key")
    return argv


def session_env(plan: SessionPlan, base: dict[str, str] | None = None) -> dict[str, str]:
    env = clean_env(base)
    # No auto memory: it would load into, and could be written from, a session reading bank pages.
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if plan.keep_transcript:
        env.pop("CLAUDE_CODE_SKIP_PROMPT_HISTORY", None)
    else:
        env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"
    return env


# ------------------------------------------------------------------ launch


def _private_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.chmod(0o700)


def _write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


AGENT_OUTPUTS = ("manifest.json", "run_report.json", "playbook_proposed.md")


def set_aside_previous_outputs(plan: SessionPlan) -> list[str]:
    """Move an earlier attempt's agent outputs to the control folder.

    The session has no Read tool, and Write won't overwrite a file it hasn't read,
    so a rerun on the same day needs these out of the way. Saved downloads stay.
    """
    moved = []
    found = [plan.run_dir / n for n in AGENT_OUTPUTS if (plan.run_dir / n).exists()]
    if found:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest = plan.control_dir / "previous" / stamp
        _private_dir(dest)
        for f in found:
            f.rename(dest / f.name)
            moved.append(f.name)
    return moved


def prepare(plan: SessionPlan, version: str) -> list[str]:
    """Create folders and write the control files. Reuses today's run folder.

    Returns the names of earlier agent outputs that were set aside.
    """
    for d in (plan.run_dir, plan.session_cwd, plan.control_dir, plan.profile_dir):
        _private_dir(d)
    moved = set_aside_previous_outputs(plan)
    login_hosts = playbooks.login_hosts(plan.site.id)
    token = extension_token(plan.profile_dir.parent.parent / "secrets") \
        if plan.browser == "extension" else None
    _write(plan.mcp_json, json.dumps(
        mcp_config(version, plan.profile_dir, plan.run_dir, plan.browser, token), indent=2))
    _write(plan.guard_config, json.dumps(guard_config(plan, login_hosts), indent=2))
    _write(plan.settings_json, json.dumps(session_settings(plan), indent=2))
    _write(plan.prompt_md, build_prompt(plan))
    return moved


def prewarm_mcp(version: str) -> bool:
    """Fetch/cache the pinned server before launch, so it connects before the first turn."""
    try:
        proc = subprocess.run(
            ["npx", "-y", f"@playwright/mcp@{version}", "--version"],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def launch(plan: SessionPlan) -> int:
    """Run the interactive session in this terminal. Returns claude's exit code."""
    proc = subprocess.run(build_argv(plan), cwd=plan.session_cwd, env=session_env(plan))
    return proc.returncode
