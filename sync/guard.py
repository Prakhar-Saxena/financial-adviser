#!/usr/bin/env python3
"""Guard hook for browsing sessions (SPEC §9.1). Standard library only.

Registered as a PreToolUse and PostToolUse command hook for every tool:
    python3 sync/guard.py /ABS/control/guard_config.json

Reads the hook payload from stdin and prints a hook decision as JSON on stdout.
It's defense in depth, not a hard wall: the model writes the element descriptions
this hook sees. Approve mode and the user watching the browser are the real protections.

guard.log records only time, event, tool, decision and a short label.
Never page text, URLs' paths or typed values.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

PW = "mcp__playwright__"

# Tools the session may use at all. Anything else is denied (and logged).
BROWSER_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_find",
    "browser_wait_for", "browser_tabs", "browser_take_screenshot", "browser_pdf_save",
    "browser_console_messages", "browser_close",
    # action tools: prompt in approve mode, allowed in trusted mode
    "browser_click", "browser_type", "browser_press_key", "browser_select_option",
    "browser_fill_form", "browser_hover",
}
WRITE_TOOLS = {"Write"}

# Files the agent may write with the Write tool, inside the run folder.
WRITE_SUFFIXES = {".json", ".md"}
# Files browser tools may save via their `filename` argument, inside the run folder.
SAVE_SUFFIXES = {".pdf", ".png", ".jpeg", ".jpg", ".md", ".yml", ".yaml", ".txt", ".json"}
RESERVED_NAMES = {"guard.log", "guard_config.json", "mcp.json", "settings.json", "prompt.md"}

# Clicks / selections / typing targets that could move money or change the account.
DENY_ACTIONS = re.compile(
    r"""
    \b(make|schedule|submit|review)\b[\w\s]{0,12}\bpayments?\b
    | \bpay\s+(now|bill|balance|card|it|(your|my|the|this)\s+(bill|balance|card))\b
    | \bpay\b\s*$
    | \btransfers?\b | \bauto-?pay\b | \bsend\s+money\b | \bzelle\b
    | \bbuy\s+(it\s+)?(now|again)\b | \bplace\s+(your\s+)?order\b | \bcheck\s?out\b
    | \badd\s+to\s+(cart|basket|list)\b | \bsubscribe\b
    | \benroll(ment)?\b | \bactivate\b | \bredeem\b | \bapply\s+(now|for)\b
    | \badd\s+(this\s+)?offers?\b | \badd\s+to\s+card\b
    | \b(un)?lock\s+(your\s+|my\s+|this\s+)?card\b | \bclose\s+(your\s+|my\s+)?account\b
    | \bcancel\b | \bdelete\b | \bremove\b | \bsave\s+changes\b | \bsettings\b
    | \b(un)?archive\b | \bhide\s+(this\s+)?order\b | \bstart\s+a\s+return\b
    | \breturn\s+(or\s+replace\s+)?items?\b | \breplace\s+items?\b
    | \bwrite\s+a\s+(product\s+)?review\b | \bleave\s+(seller\s+)?feedback\b
    | \bpaperless\b | \bdispute\b | \breport\s+(a\s+)?(lost|stolen|card|fraud)\b
    | \brequest\s+(a\s+)?(credit|limit|replacement|new\s+card)\b
    | \brenew(al)?\b | \bauto-?renew\b | \bupgrade\b | \breorder\b | \border\s+again\b
    | \bpay\s+over\s+time\b | \bplan\s+it\b | \bsend\s*&?\s*split\b | \badd\s+(a\s+)?card\b
    | \btransfer\s+points\b | \buse\s+points\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Fields the agent must never type into. The user does all logins.
LOGIN_FIELDS = re.compile(
    r"""
    \buser\s*-?\s*(id|name)\b | \busername\b | \be-?mail\b | \bpass(word|code|phrase)\b
    | \bpin\b | \bone[\s-]?time\b | \botp\b | \b(verification|security|access)\s+code\b
    | \b2fa\b | \bssn\b | \bsocial\s+security\b | \blog\s?in\b | \bsign\s?in\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
PAGE_URL = re.compile(r"Page URL:\s*(\S+)")


# ----------------------------------------------------------------- helpers


def host_allowed(host: str, cfg: dict) -> bool:
    host = host.lower().rstrip(".")
    for d in cfg["allowed_domains"]:
        if host == d or host.endswith("." + d):
            return True
    return host in {h.lower() for h in cfg.get("login_hosts", [])}


def url_ok(url: str, cfg: dict) -> tuple[bool, str]:
    if url in ("about:blank",):
        return True, "about:blank"
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        return False, f"scheme {parts.scheme or '?'}"
    host = parts.hostname or ""
    return host_allowed(host, cfg), host


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def label(text: str | None) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:40]


def collect_text(node) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return "\n".join(collect_text(v) for v in node.values())
    if isinstance(node, list):
        return "\n".join(collect_text(v) for v in node)
    return ""


# ---------------------------------------------------------------- decisions


def pre_tool(payload: dict, cfg: dict) -> tuple[str, str, str]:
    """Return (decision 'allow'|'deny', reason, log label)."""
    tool = payload.get("tool_name", "")
    args = payload.get("tool_input") or {}
    run_dir = Path(cfg["run_dir"])
    cwd = Path(payload.get("cwd") or cfg.get("session_cwd") or run_dir)

    if tool in WRITE_TOOLS:
        target = Path(args.get("file_path", ""))
        if not target.is_absolute():
            target = cwd / target
        name = target.name
        if not inside(target, run_dir):
            return "deny", "Writes are allowed only inside this run's folder.", name
        if target.suffix.lower() not in WRITE_SUFFIXES:
            return "deny", "Only .json and .md files may be written.", name
        if name in RESERVED_NAMES:
            return "deny", f"{name} is reserved.", name
        return "allow", "", name

    if not tool.startswith(PW) or tool[len(PW):] not in BROWSER_TOOLS:
        return "deny", f"Tool {tool} is not available in browsing sessions.", ""

    short = tool[len(PW):]

    # Any URL argument must stay on the site.
    url = args.get("url")
    if isinstance(url, str) and url:
        ok, host = url_ok(url, cfg)
        if not ok:
            return "deny", (
                f"Navigation to {host} is outside this site's allowed domains. "
                "Stay on the site; if the task needs another site, stop and tell the user."
            ), host
        lbl = host
    else:
        lbl = ""

    # Files saved by browser tools must land in the run folder.
    fname = args.get("filename")
    if isinstance(fname, str) and fname:
        target = Path(fname)
        if not target.is_absolute():
            target = cwd / target
        if not inside(target, run_dir):
            return "deny", f"Save files inside the run folder: {run_dir}", target.name
        if target.suffix.lower() not in SAVE_SUFFIXES:
            return "deny", f"File type {target.suffix or '(none)'} is not allowed.", target.name
        if target.name in RESERVED_NAMES:
            return "deny", f"{target.name} is reserved.", target.name
        lbl = lbl or target.name

    if short in ("browser_click", "browser_select_option", "browser_hover", "browser_type"):
        element = args.get("element")
        if not isinstance(element, str) or not element.strip():
            return "deny", "Describe the element in `element` so the action can be checked.", ""
        lbl = label(element)
        if short != "browser_hover" and DENY_ACTIONS.search(element):
            return "deny", (
                f"'{lbl}' looks like an action that pays, buys, enrolls or changes the "
                "account. This session is read-only. If the task needs it, stop and tell the user."
            ), lbl
        if short == "browser_type" and LOGIN_FIELDS.search(element):
            return "deny", "Never type into login or verification fields. Ask the user.", lbl

    if short == "browser_fill_form":
        for field in args.get("fields") or []:
            desc = f"{field.get('element') or ''} {field.get('name') or ''}".strip()
            if not desc:
                return "deny", "Describe each form field so it can be checked.", ""
            if LOGIN_FIELDS.search(desc):
                reason = "Never fill login or verification fields. Ask the user."
                return "deny", reason, label(desc)
            if DENY_ACTIONS.search(desc):
                reason = "That form field looks like a payment or account change."
                return "deny", reason, label(desc)
        lbl = f"{len(args.get('fields') or [])} fields"

    return "allow", "", lbl


# Sites that give every export the same name (Amex: activity.csv). The guard renames a fresh
# download so the next card's export can't overwrite it, and tells the agent the new name.
GENERIC_DOWNLOADS = {"activity.csv"}


def rename_generic_downloads(cfg: dict) -> list[str]:
    run_dir = Path(cfg["run_dir"])
    notes = []
    for name in GENERIC_DOWNLOADS:
        p = run_dir / name
        if p.is_file():
            n = 1
            while (run_dir / f"{p.stem}-{n}{p.suffix}").exists():
                n += 1
            target = run_dir / f"{p.stem}-{n}{p.suffix}"
            p.rename(target)
            notes.append(f"The download {name} was renamed to {target.name}; use that name in "
                         "the manifest.")
    return notes


def post_tool(payload: dict, cfg: dict) -> tuple[str, str, str]:
    """After a browser action, make sure the page is still on the site."""
    tool = payload.get("tool_name", "")
    if not tool.startswith(PW):
        return "allow", "", ""
    text = collect_text(payload.get("tool_response"))
    urls = PAGE_URL.findall(text)
    if not urls:
        return "allow", "", ""
    ok, host = url_ok(urls[-1], cfg)
    if ok:
        return "allow", "", host
    return "block", (
        f"The page is now on {host}, outside this site's allowed domains. "
        "Go back with browser_navigate_back, do nothing on that page, and stop to tell the user."
    ), host


# --------------------------------------------------------------------- main


def log(cfg: dict, event: str, tool: str, decision: str, lbl: str) -> None:
    line = json.dumps(
        {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": event, "tool": tool, "decision": decision, "label": lbl,
        }
    )
    path = Path(cfg["log_path"])
    try:
        with path.open("a") as f:
            f.write(line + "\n")
        path.chmod(0o600)
    except OSError:
        pass


def record(cfg: dict, payload: dict) -> None:
    """Selftest only: keep full payloads as test fixtures (example.com, no personal data)."""
    rec = cfg.get("record_payloads_dir")
    if not rec:
        return
    try:
        d = Path(rec)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        n = len(list(d.glob("*.json")))
        name = f"{n:03d}_{payload.get('hook_event_name', 'x')}_{payload.get('tool_name', 'x')}"
        (d / f"{re.sub(r'[^A-Za-z0-9_]', '_', name)}.json").write_text(json.dumps(payload))
    except OSError:
        pass


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: guard.py /ABS/guard_config.json", file=sys.stderr)
        return 2
    try:
        cfg = json.loads(Path(argv[1]).read_text())
        payload = json.loads(sys.stdin.read() or "{}")
    except (OSError, json.JSONDecodeError) as e:
        # Fail closed: exit code 2 blocks the tool call.
        print(f"guard: cannot read config or payload: {e}", file=sys.stderr)
        return 2

    event = payload.get("hook_event_name", "PreToolUse")
    tool = payload.get("tool_name", "")
    record(cfg, payload)
    if event == "PostToolUse":
        decision, reason, lbl = post_tool(payload, cfg)
        renamed = rename_generic_downloads(cfg) if tool.startswith(PW) else []
        log(cfg, event, tool, decision, lbl)
        if decision == "block":
            print(json.dumps({"decision": "block", "reason": reason}))
        elif renamed:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                     "additionalContext": " ".join(renamed)}}))
        return 0

    decision, reason, lbl = pre_tool(payload, cfg)
    log(cfg, event, tool, decision, lbl)
    if decision == "deny":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"Blocked by guard: {reason}",
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
