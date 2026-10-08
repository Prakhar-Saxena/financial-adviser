"""Playwright MCP server config for one browsing run (SPEC §9.1)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fin.config import EXACT_VERSION

SERVER = "playwright"


TOKEN_FILE = "playwright_extension_token"  # noqa: S105 (a file name in secrets/, not a secret)


def extension_token(secrets_dir: Path) -> str | None:
    """The Playwright extension's connection token, if the user saved one (mode 600)."""
    p = secrets_dir / TOKEN_FILE
    if not p.exists():
        return None
    token = p.read_text().strip()
    # The extension's status page shows "PLAYWRIGHT_MCP_EXTENSION_TOKEN=<token>"; accept both.
    if token.startswith("PLAYWRIGHT_MCP_EXTENSION_TOKEN="):
        token = token.split("=", 1)[1]
    return token.strip().strip("'\"") or None


def mcp_config(version: str, profile_dir: Path, output_dir: Path,
               browser: str = "profile", token: str | None = None) -> dict[str, Any]:
    if not EXACT_VERSION.fullmatch(version):
        raise ValueError(f"@playwright/mcp version must be exact, got {version!r}")
    if browser == "extension":
        # The user's own Chrome via the Playwright extension: the user approves the
        # connection, and the session only reaches tabs in its own tab group.
        where = ["--extension"]
    else:
        where = ["--browser", "chrome", "--user-data-dir", str(profile_dir.resolve())]
    return {
        "mcpServers": {
            SERVER: {
                "command": "npx",
                "args": [
                    "-y",
                    f"@playwright/mcp@{version}",
                    *where,
                    "--output-dir", str(output_dir.resolve()),
                    "--caps", "pdf",
                    "--codegen", "python",
                    "--save-session",
                    # Don't expose tools that web pages register via WebMCP.
                    "--no-webmcp",
                    "--file-paths", "absolute",
                ],
                # The token makes the extension connect without the tab picker, and makes the
                # server fail after 30 s instead of waiting forever if it can't connect.
                **({"env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": token}}
                   if browser == "extension" and token else {}),
            }
        }
    }


def preset_chrome_pdf_download(profile_dir: Path) -> str:
    """Make PDFs download instead of opening in Chrome's viewer (SPEC §9.1).

    Edits <profile>/Default/Preferences before Chrome starts. Skips if the profile
    is in use. Returns what happened, for the run log.
    """
    if (profile_dir / "SingletonLock").exists() or (profile_dir / "SingletonLock").is_symlink():
        return "skipped: profile in use"
    prefs_path = profile_dir / "Default" / "Preferences"
    prefs: dict[str, Any] = {}
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text())
        except json.JSONDecodeError:
            return "skipped: Preferences unreadable"
    plugins = prefs.setdefault("plugins", {})
    if plugins.get("always_open_pdf_externally") is True:
        return "already set"
    plugins["always_open_pdf_externally"] = True
    prefs_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = prefs_path.with_suffix(".fin-tmp")
    tmp.write_text(json.dumps(prefs))
    os.replace(tmp, prefs_path)
    return "set"
