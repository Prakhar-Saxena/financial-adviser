"""`fin doctor` checks (SPEC §7)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fin.ai.claude_cli import STRIPPED_ENV, SUBSCRIPTION_AUTH, AIError, auth_status
from fin.config import (
    CONFIG_DIR,
    EXACT_VERSION,
    REPO_ROOT,
    DataPaths,
    icloud_risk,
    load_cards,
    load_settings,
    load_taxonomy,
)

CHROME_PATHS = (
    Path("/Applications/Google Chrome.app"),
    Path.home() / "Applications" / "Google Chrome.app",
)
REQUIRED_GITIGNORE = [
    "config/cards.json", ".env", "*.sqlite", "*.pdf", "*.csv", "*.qfx", "*.html",
    "browser-profiles",
]


@dataclass
class Result:
    status: str  # PASS / FAIL / WARN / INFO
    name: str
    detail: str


def _run(argv: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_filevault() -> Result:
    proc = _run(["fdesetup", "status"])
    out = (proc.stdout if proc else "").strip()
    if "FileVault is On" in out:
        return Result("PASS", "FileVault", out)
    return Result("FAIL", "FileVault", out or "could not run fdesetup; turn FileVault on (§19.1)")


def check_data_dir(paths: DataPaths) -> list[Result]:
    root = paths.root
    out = []
    risk = icloud_risk(root)
    if risk:
        out.append(Result("FAIL", "Data dir location", f"{root} is under {risk} (iCloud risk)"))
    else:
        out.append(Result("PASS", "Data dir location", f"{root}"))
    if not root.exists():
        out.append(Result("FAIL", "Data dir mode", f"{root} does not exist; run `fin init`"))
        return out
    mode = stat.S_IMODE(root.stat().st_mode)
    if mode == 0o700:
        out.append(Result("PASS", "Data dir mode", "700"))
    else:
        out.append(Result("FAIL", "Data dir mode", f"{mode:o}; run `chmod 700 {root}`"))
    loose = [
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.is_file() and stat.S_IMODE(p.stat().st_mode) & 0o077
        and "browser-profiles" not in p.parts
    ]
    if loose:
        out.append(Result("WARN", "Data file modes", f"{len(loose)} files readable by others, "
                          f"e.g. {loose[0]}; run `fin init` to fix"))
    db = paths.db_file
    if not db.exists():
        out.append(Result("FAIL", "Database", "missing; run `fin init`"))
    else:
        from fin.db import current_revision, head_revision

        cur, head = current_revision(db), head_revision()
        if cur == head:
            out.append(Result("PASS", "Database", f"{db} (schema {cur})"))
        else:
            out.append(Result("WARN", "Database", f"schema {cur}, latest {head}; the next fin "
                              "command upgrades it (with a backup)"))
    return out


def check_claude_auth() -> Result:
    if not shutil.which("claude"):
        return Result("FAIL", "Claude auth", "`claude` CLI not found on PATH")
    try:
        status = auth_status()
    except (AIError, OSError, subprocess.TimeoutExpired) as e:
        return Result("FAIL", "Claude auth", str(e))
    method = status.get("authMethod")
    if status.get("loggedIn") and method in SUBSCRIPTION_AUTH:
        sub = status.get("subscriptionType", "?")
        return Result("PASS", "Claude auth", f"authMethod={method}, subscription={sub}")
    return Result("FAIL", "Claude auth", f"authMethod={method!r}; need claude.ai or oauth_token")


def check_api_key_env() -> Result:
    present = [n for n in STRIPPED_ENV if n in os.environ]
    present += [n for n in os.environ if n.startswith("CLAUDE_CODE_USE_")]
    if "ANTHROPIC_API_KEY" in present:
        return Result("FAIL", "ANTHROPIC_API_KEY", "is set; unset it (subscription only, §2)")
    if present:
        return Result("WARN", "Provider env vars", f"set: {present}; fin strips them for claude")
    return Result("PASS", "ANTHROPIC_API_KEY", "not set")


def check_chrome() -> Result:
    for p in CHROME_PATHS:
        if p.exists():
            return Result("PASS", "Google Chrome", str(p))
    return Result("FAIL", "Google Chrome", "not found in /Applications or ~/Applications")


def check_node() -> list[Result]:
    node, npx = shutil.which("node"), shutil.which("npx")
    if not node or not npx:
        return [Result("FAIL", "Node.js", "node/npx not on PATH; install Node.js LTS")]
    proc = _run([node, "--version"])
    ver = proc.stdout.strip() if proc else "?"
    m = re.match(r"v(\d+)", ver)
    if m and int(m.group(1)) % 2 == 1:
        return [Result("WARN", "Node.js", f"{ver} is an odd (non-LTS) release")]
    return [Result("PASS", "Node.js", f"{ver} ({node})")]


def check_playwright_pin() -> Result:
    v = load_settings().sync.playwright_mcp_version
    if EXACT_VERSION.fullmatch(v):
        return Result("PASS", "Playwright MCP pin", f"@playwright/mcp@{v}")
    return Result("FAIL", "Playwright MCP pin", f"{v!r} is not an exact version")


def check_config() -> list[Result]:
    out = []
    cards_path = CONFIG_DIR / "cards.json"
    if not cards_path.exists():
        out.append(Result("FAIL", "cards.json", "missing; `fin init` copies the example"))
    else:
        try:
            cards = load_cards()
            out.append(Result("PASS", "cards.json", f"{len(cards.cards)} cards, "
                              f"{len(cards.people)} people"))
        except Exception as e:  # pydantic or JSON error
            out.append(Result("FAIL", "cards.json", str(e).splitlines()[0]))
        mode = stat.S_IMODE(cards_path.stat().st_mode)
        if mode & 0o077:
            out.append(Result("WARN", "cards.json mode", f"{mode:o}; run `chmod 600`"))
    try:
        out.append(Result("PASS", "categories.json", f"{len(load_taxonomy().ids)} categories"))
    except Exception as e:
        out.append(Result("FAIL", "categories.json", str(e).splitlines()[0]))
    return out


def check_git_hygiene() -> list[Result]:
    gi = REPO_ROOT / ".gitignore"
    text = gi.read_text() if gi.exists() else ""
    lines = {ln.strip() for ln in text.splitlines()}
    missing = [p for p in REQUIRED_GITIGNORE if p not in lines and f"**/{p}/" not in lines]
    out = [Result("FAIL" if missing else "PASS", "Gitignore rules",
                  f"missing: {missing}" if missing else "all §7 patterns present")]
    proc = _run(["git", "-C", str(REPO_ROOT), "config", "core.hooksPath"])
    hooks = proc.stdout.strip() if proc else ""
    if hooks == "scripts/hooks":
        out.append(Result("PASS", "Pre-commit guard", "core.hooksPath=scripts/hooks"))
    else:
        out.append(Result("FAIL", "Pre-commit guard", "not installed; run `fin init`"))
    return out


def check_extension_token(paths: DataPaths) -> list[Result]:
    from fin.sync.mcp_config import TOKEN_FILE

    if "extension" not in load_settings().sync.site_browser.values():
        return []
    p = paths.secrets / TOKEN_FILE
    if not p.exists():
        return [Result("WARN", "Playwright extension token",
                       f"no {p.name} in secrets/; extension-mode syncs will wait for you to "
                       "pick a tab")]
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        return [Result("FAIL", "Playwright extension token", f"mode {mode:o}; run chmod 600 {p}")]
    return [Result("PASS", "Playwright extension token", "present, private")]


def run_all(paths: DataPaths) -> list[Result]:
    results = [check_filevault()]
    results += check_data_dir(paths)
    results += [check_claude_auth(), check_api_key_env(), check_chrome()]
    results += check_node()
    results += [check_playwright_pin()]
    results += check_config()
    results += check_git_hygiene()
    results += check_extension_token(paths)
    results.append(Result(
        "INFO", "Usage credits",
        "Keep usage credits / extra usage OFF in your Claude account, so limits pause work "
        "instead of billing (§14.4).",
    ))
    return results
