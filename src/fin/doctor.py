# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`fin doctor` checks (SPEC §7)."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
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
    "browser-profiles", "/private/",
]
HOOKS = ("pre-commit", "commit-msg", "pre-push")


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


def check_cards(paths: DataPaths) -> list[Result]:
    """Card optimizer settings and storage (docs/SPEC-cards.md §15)."""
    from fin.config import BARE_DOMAIN, load_reward_categories

    try:
        cs = load_settings().cards
    except Exception as e:  # pydantic error
        return [Result("FAIL", "Cards settings", str(e).splitlines()[0])]
    domains = [d for ds in cs.doc_domains.values() for d in ds]
    bad = [d for d in domains if not BARE_DOMAIN.fullmatch(d)]
    out = [Result("FAIL" if bad else "PASS", "Cards settings",
                  f"not bare domains: {bad}" if bad else
                  f"[cards] valid; {len(domains)} extra doc domains, research={cs.research_model}, "
                  f"extract={cs.extract_model}")]
    try:
        out.append(Result("PASS", "reward_categories.json",
                          f"{len(load_reward_categories().ids)} reward categories"))
    except Exception as e:
        out.append(Result("FAIL", "reward_categories.json", str(e).splitlines()[0]))
    try:
        card_ids = [c.id for c in load_cards().cards]
    except Exception:
        card_ids = []  # reported by check_config
    missing = [c for c in card_ids if not cs.product_aliases.get(c)]
    out.append(Result("WARN" if missing else "PASS", "Card product aliases",
                      f"none for {missing}; add them under [cards.product_aliases]" if missing
                      else f"all {len(card_ids)} cards"))
    docs = paths.card_docs
    if docs.exists():
        mode = stat.S_IMODE(docs.stat().st_mode)
        out.append(Result("PASS" if mode == 0o700 else "FAIL", "card-docs mode",
                          "700" if mode == 0o700 else f"{mode:o}; run `chmod 700 {docs}`"))
    else:
        out.append(Result("FAIL", "card-docs mode", f"{docs} missing; run `fin init`"))
    return out


def check_bank(cards_path: Path | None = None, settings=None) -> list[Result]:
    """The checking account (docs/SPEC-checking.md §7.4, §12): bank_rules.yaml, and every
    bank site in approve mode in its own Chrome profile."""
    from fin.normalize.bank import load_bank_rules

    cards_path = cards_path or CONFIG_DIR / "cards.json"
    try:
        cards = load_cards(cards_path)
    except Exception:
        return []  # reported by check_config
    out = []
    try:
        n = len(load_bank_rules(cards))
        out.append(Result("PASS", "bank_rules.yaml", f"{n} rules; patterns compile, issuers known"))
    except Exception as e:  # ValueError from the checks, or a YAML error
        out.append(Result("FAIL", "bank_rules.yaml", str(e).splitlines()[0]))
    if not cards.bank_accounts:
        out.append(Result("INFO", "Bank accounts", "none in cards.json"))
        return out
    sync = (settings or load_settings()).sync
    for site in cards.bank_sites:
        n = len(cards.accounts_for_site(site))
        if sync.browser_for(site) == "extension":
            out.append(Result("FAIL", f"Bank site {site}", "site_browser is extension: it would "
                              "act in your everyday Chrome with all your logins; remove it"))
        elif site in sync.trusted_sites:
            out.append(Result("WARN", f"Bank site {site}", "in trusted_sites; bank sites stay in "
                              "approve mode, so `fin sync` refuses until you remove it"))
        else:
            out.append(Result("PASS", f"Bank site {site}",
                              f"{n} account(s); approve mode, own Chrome profile"))
    return out


def check_git_hygiene() -> list[Result]:
    gi = REPO_ROOT / ".gitignore"
    text = gi.read_text() if gi.exists() else ""
    lines = {ln.strip() for ln in text.splitlines()}
    missing = [p for p in REQUIRED_GITIGNORE if p not in lines and f"**/{p}/" not in lines]
    out = [Result("FAIL" if missing else "PASS", "Gitignore rules",
                  f"missing: {missing}" if missing else "all §7 patterns present")]
    out.append(check_git_hooks())
    return out


def check_git_hooks(repo: Path = REPO_ROOT) -> Result:
    """The privacy hooks run on every commit and push (SPEC §7)."""
    proc = _run(["git", "-C", str(repo), "config", "core.hooksPath"])
    if (proc.stdout.strip() if proc else "") != "scripts/hooks":
        return Result("FAIL", "Git hooks", "not installed; run `fin init`")
    d = repo / "scripts" / "hooks"
    off = [h for h in HOOKS if not os.access(d / h, os.X_OK)]
    if off or not (d / "privacy_scan.py").is_file():
        bad = off or ["privacy_scan.py"]
        return Result("FAIL", "Git hooks", f"missing or not executable: {bad}; run "
                      "`git checkout -- scripts/hooks`")
    return Result("PASS", "Git hooks", f"{', '.join(HOOKS)} (core.hooksPath=scripts/hooks)")


def _privacy_scan():
    """scripts/hooks/privacy_scan.py, the same code the git hooks run (stdlib only)."""
    name = "fin_privacy_scan"
    if name not in sys.modules:
        path = REPO_ROOT / "scripts" / "hooks" / "privacy_scan.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod  # its dataclasses look themselves up here
        spec.loader.exec_module(mod)
    return sys.modules[name]


def check_privacy(paths: DataPaths) -> list[Result]:
    """No data of mine in tracked files or commit messages: the repo is public (SPEC §7)."""
    try:
        scan = _privacy_scan()
        terms = scan.load_terms(REPO_ROOT, paths.root)
        if terms.problem:
            return [Result("FAIL", "Privacy scan", terms.problem)]
        files, messages = scan.scan_tree(REPO_ROOT, terms)
    except Exception as e:  # git missing, unreadable file: report, don't crash doctor
        return [Result("FAIL", "Privacy scan", f"couldn't run: {e}")]
    against = terms.summary() + ("" if terms.real else "; config/cards.json has no real values")
    out = [Result("FAIL", "Privacy scan", f"{len(files)} finding(s) in tracked files, first "
                  f"{files[0].where}: {files[0].kind}. Run `make privacy-check`")
               if files else Result("PASS", "Privacy scan", f"tracked files clean ({against})")]
    if messages:
        out.append(Result("WARN", "Commit messages", f"{len(messages)} finding(s), first "
                          f"{messages[0].where}: {messages[0].kind}. Only a history rewrite "
                          "removes them"))
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


def check_claude_leftovers(paths: DataPaths, projects: Path | None = None) -> Result:
    """Claude Code folders left under ~/.claude/projects/ by `claude -p` runs in ai-runs/."""
    from fin.ai.claude_cli import CLAUDE_PROJECTS, project_slug

    projects = projects or CLAUDE_PROJECTS
    prefix = project_slug(paths.ai_runs) + "-"
    left = [d.name for d in projects.glob(prefix + "*")] if projects.is_dir() else []
    if left:
        return Result("WARN", "Claude run leftovers",
                      f"{len(left)} folder(s) {projects}/{prefix}*; safe to delete")
    return Result("PASS", "Claude run leftovers", "none under ~/.claude/projects")


def run_all(paths: DataPaths) -> list[Result]:
    results = [check_filevault()]
    results += check_data_dir(paths)
    results += [check_claude_auth(), check_api_key_env(), check_chrome()]
    results += check_node()
    results += [check_playwright_pin()]
    results += check_config()
    results += check_cards(paths)
    results += check_bank()
    results.append(check_claude_leftovers(paths))
    results += check_git_hygiene()
    results += check_privacy(paths)
    results += check_extension_token(paths)
    results.append(Result(
        "INFO", "Usage credits",
        "Keep usage credits / extra usage OFF in your Claude account, so limits pause work "
        "instead of billing (§14.4).",
    ))
    return results
