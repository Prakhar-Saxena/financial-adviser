"""After a browsing session: checks, sync_runs rows, transcript detection (SPEC §9.1)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fin.ingest.manifest import ManifestCheck, is_session_log, validate_run
from fin.sync.launcher import SessionPlan


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def read_guard_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def guard_blocks(entries: list[dict[str, Any]]) -> int:
    return sum(e.get("decision") in ("deny", "block") for e in entries)


def transcript_dir(cwd: Path, projects: Path | None = None) -> Path:
    projects = projects or Path.home() / ".claude" / "projects"
    return projects / re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))


def new_transcripts(cwd: Path, since: float, projects: Path | None = None) -> list[Path]:
    d = transcript_dir(cwd, projects)
    if not d.exists():
        return []
    return [p for p in d.rglob("*.jsonl") if p.stat().st_mtime >= since]


def selftest_checks(
    plan: SessionPlan, started: float, projects: Path | None = None
) -> tuple[list[Check], ManifestCheck, list[str]]:
    log = read_guard_log(plan.guard_log)

    def has(tool: str, decision: str, label_part: str, event: str = "PreToolUse") -> bool:
        return any(
            e.get("event") == event and e.get("tool") == f"mcp__playwright__{tool}"
            and e.get("decision") == decision and label_part in (e.get("label") or "")
            for e in log
        )

    checks = []
    opened = has("browser_navigate", "allow", "example.com") and has(
        "browser_navigate", "allow", "example.com", event="PostToolUse"
    )
    checks.append(Check(
        "Chrome opened example.com", opened,
        "guard.log shows the navigation and the page URL" if opened
        else "no allowed navigation to example.com with a confirmed page URL in guard.log",
    ))

    pdf = plan.run_dir / "selftest_example.pdf"
    pdf_ok = pdf.is_file() and pdf.read_bytes()[:5] == b"%PDF-"
    checks.append(Check(
        "Agent saved a PDF into the run folder", pdf_ok,
        f"{pdf.name}, {pdf.stat().st_size} bytes" if pdf_ok else f"{pdf} missing or not a PDF",
    ))

    blocked = has("browser_navigate", "deny", "iana.org")
    checks.append(Check(
        "Guard blocked navigation to another domain", blocked,
        "denied browser_navigate to www.iana.org" if blocked
        else "no denied navigation to iana.org in guard.log",
    ))

    mcheck = validate_run(plan.run_dir, "selftest", set(), plan.run_date)
    checks.append(Check(
        "Manifest validates", mcheck.ok,
        "manifest.json and run_report.json are valid" if mcheck.ok else "; ".join(mcheck.errors),
    ))

    # Read the raw report: it may fail schema validation yet still list the tools.
    try:
        raw_report = json.loads((plan.run_dir / "run_report.json").read_text())
    except (OSError, json.JSONDecodeError):
        raw_report = {}
    tools = raw_report.get("tools_available") if isinstance(raw_report, dict) else None
    tools = tools if isinstance(tools, list) else []
    shell_listed = [t for t in tools if re.search(r"bash|shell|powershell|repl", t, re.I)]
    shell_called = [
        e["tool"] for e in log
        if re.search(r"bash|shell|powershell|repl", e.get("tool", ""), re.I)
    ]
    no_bash = bool(tools) and not shell_listed and not shell_called
    checks.append(Check(
        "Session has no Bash tool", no_bash,
        f"agent reported {len(tools)} tools, none a shell; guard saw no shell calls" if no_bash
        else f"tools_available={tools or 'not reported'}, shell calls={shell_called}",
    ))

    foreign = [t for t in tools if t.startswith("mcp__") and not t.startswith("mcp__playwright__")]
    only_pw = bool(tools) and not foreign
    checks.append(Check(
        "No browser tools besides the Playwright server", only_pw,
        "no other MCP or Claude-in-Chrome tools reported" if only_pw
        else f"other tools: {foreign[:5] or 'tools not reported'}",
    ))

    transcripts = new_transcripts(plan.session_cwd, started, projects)
    checks.append(Check(
        "No transcript saved", not transcripts,
        "nothing new under ~/.claude/projects for this session" if not transcripts
        else f"found {[str(p) for p in transcripts]}",
    ))

    session_files = sorted(
        str(p.relative_to(plan.run_dir)) for p in plan.run_dir.rglob("*")
        if p.is_file() and (is_session_log(p.name) or p.parent != plan.run_dir)
    )
    return checks, mcheck, session_files


def wait_for_quiet_transcripts(seconds: float = 1.0) -> None:
    """Give the CLI a moment to flush any transcript before checking for it."""
    time.sleep(seconds)
