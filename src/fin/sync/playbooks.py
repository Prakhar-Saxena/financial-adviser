# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-site playbooks (SPEC §9.1). The agent proposes; the user accepts or rejects.

A bank site's playbook names the bank, so it lives in the gitignored `private/playbooks/`
(docs/SPEC-checking.md §1.1, §7.5); every other site's lives in `sync/playbooks/`.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from fin.config import CONFIG_DIR, REPO_ROOT, load_cards

PLAYBOOKS_DIR = REPO_ROOT / "sync" / "playbooks"
PRIVATE_PLAYBOOKS_DIR = REPO_ROOT / "private" / "playbooks"
HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def bank_sites(cards_path: Path | None = None) -> list[str]:
    """Bank sites from cards.json. No cards.json means no bank; a broken one raises, so a bank
    playbook can't fall through to the public folder."""
    path = cards_path or CONFIG_DIR / "cards.json"
    return load_cards(path).bank_sites if path.exists() else []


def root_for(site: str) -> Path:
    return PRIVATE_PLAYBOOKS_DIR if site in bank_sites() else PLAYBOOKS_DIR


def path_for(site: str, root: Path | None = None) -> Path:
    return (root or root_for(site)) / f"{site}.md"


def current(site: str, root: Path | None = None) -> str | None:
    p = path_for(site, root)
    return p.read_text() if p.exists() else None


def login_hosts(site: str, root: Path | None = None) -> list[str]:
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


def diff(site: str, proposed: str, root: Path | None = None) -> str:
    old = current(site, root) or ""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), proposed.splitlines(keepends=True),
            fromfile=f"playbooks/{site}.md", tofile="playbook_proposed.md",
        )
    )


def accept(site: str, proposed: str, root: Path | None = None) -> Path:
    p = path_for(site, root)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.write_text(proposed if proposed.endswith("\n") else proposed + "\n")
    return p
