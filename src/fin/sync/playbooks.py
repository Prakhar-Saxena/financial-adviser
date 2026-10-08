"""Per-site playbooks (SPEC §9.1). The agent proposes; the user accepts or rejects."""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from fin.config import REPO_ROOT

PLAYBOOKS_DIR = REPO_ROOT / "sync" / "playbooks"
HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def path_for(site: str, root: Path = PLAYBOOKS_DIR) -> Path:
    return root / f"{site}.md"


def current(site: str, root: Path = PLAYBOOKS_DIR) -> str | None:
    p = path_for(site, root)
    return p.read_text() if p.exists() else None


def login_hosts(site: str, root: Path = PLAYBOOKS_DIR) -> list[str]:
    """Hosts listed under a `## Login hosts` heading, one per bullet line."""
    text = current(site, root) or ""
    hosts: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("#"):
            in_section = line.strip().lower().lstrip("#").strip() == "login hosts"
            continue
        if in_section:
            item = line.strip().lstrip("-*").strip().strip("`").lower()
            if HOST.fullmatch(item):
                hosts.append(item)
    return hosts


def diff(site: str, proposed: str, root: Path = PLAYBOOKS_DIR) -> str:
    old = current(site, root) or ""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), proposed.splitlines(keepends=True),
            fromfile=f"playbooks/{site}.md", tofile="playbook_proposed.md",
        )
    )


def accept(site: str, proposed: str, root: Path = PLAYBOOKS_DIR) -> Path:
    p = path_for(site, root)
    p.write_text(proposed if proposed.endswith("\n") else proposed + "\n")
    return p
