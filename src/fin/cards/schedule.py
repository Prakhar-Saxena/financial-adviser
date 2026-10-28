# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`fin cards schedule` (docs/SPEC-cards.md Phase C5): a launchd job on the 1st of each month
that runs `fin cards refresh --if-due --trigger schedule`.

A user LaunchAgent, so it runs as me, with my keychain and my Claude login. launchd starts jobs
with a bare PATH, so the plist names the directories of `uv`, `claude` and `node`. The scheduled
run checks the subscription login first and records the result, so `fin cards schedule status`
can say whether `claude -p` started by launchd sees it. No API key is ever set.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fin.config import REPO_ROOT, DataPaths

LABEL = "local.financial-adviser.cards-refresh"
DAY, HOUR, MINUTE = 1, 9, 30
BASE_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path(paths: DataPaths) -> Path:
    return paths.logs / "cards-schedule.log"


def tool_dirs() -> list[str]:
    """Directories of the tools the job needs, found on the PATH I run this from."""
    dirs = []
    for tool in ("uv", "claude", "node"):
        found = shutil.which(tool)
        if found and str(Path(found).parent) not in dirs:
            dirs.append(str(Path(found).parent))
    return dirs


def plist(paths: DataPaths, uv: str, dirs: list[str], repo: Path = REPO_ROOT,
          home: Path | None = None) -> dict[str, Any]:
    log = str(log_path(paths))
    return {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "--project", str(repo), "fin", "cards", "refresh",
                             "--if-due", "--trigger", "schedule"],
        "WorkingDirectory": str(repo),
        "StartCalendarInterval": {"Day": DAY, "Hour": HOUR, "Minute": MINUTE},
        # Only PATH and HOME: never ANTHROPIC_API_KEY or any other credential (SPEC §2).
        "EnvironmentVariables": {"PATH": ":".join([*dirs, *BASE_PATH]),
                                 "HOME": str(home or Path.home())},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "RunAtLoad": False,
        "ProcessType": "Background",
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def install(paths: DataPaths) -> Path:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("`uv` isn't on PATH")
    if not shutil.which("claude"):
        raise RuntimeError("`claude` isn't on PATH")
    paths.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    with target.open("wb") as f:
        plistlib.dump(plist(paths, uv, tool_dirs()), f)
    target.chmod(0o600)
    r = _launchctl("bootstrap", _domain(), str(target))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
    return target


def uninstall() -> bool:
    target = plist_path()
    if loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    if target.exists():
        target.unlink()
        return True
    return False


def loaded() -> bool:
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def run_now() -> None:
    """Start the job now through launchd, the same way the 1st of the month does."""
    r = _launchctl("kickstart", f"{_domain()}/{LABEL}")
    if r.returncode != 0:
        raise RuntimeError(f"launchctl kickstart failed: {r.stderr.strip() or r.stdout.strip()}")


def log_tail(paths: DataPaths, n: int = 15) -> list[str]:
    p = log_path(paths)
    if not p.exists():
        return []
    return p.read_text(errors="replace").splitlines()[-n:]
