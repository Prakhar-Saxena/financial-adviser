#!/usr/bin/env python3
# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Claude Code PreToolUse hook for Bash (`.claude/settings.json`): no skipping the git hooks.

The privacy checks in scripts/hooks/ only work if they run (SPEC §7). This denies Bash
commands that would skip them: `--no-verify`, `git commit -n`, changing `core.hooksPath`,
`git send-pack`, and removing or disabling the hook files. It is a seatbelt for Claude
sessions in this repo, not the gate: pre-push still checks every commit before it leaves.
Anything it can't parse is allowed. Stdlib only.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

# `git commit` options that take the next word as their value.
COMMIT_ARGS = {"-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c",
               "--reedit-message", "-t", "--template", "--author", "--date", "--fixup",
               "--squash", "--trailer", "--cleanup", "--pathspec-from-file"}
# Short options that take the rest of the word as their value (`-mfix`).
SHORT_WITH_VALUE = set("mFCctS")
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n"}
GIT_GLOBAL_ARGS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
HOOK_FILES = re.compile(
    r"\b(rm|mv|chmod|unlink|truncate|ln|cp)\b[^;&|\n]*(scripts/hooks|\.git/hooks)"
    r"|>\s*\S*(scripts/hooks|\.git/hooks)/")


def words(command: str) -> list[str]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def git_commands(tokens: list[str]) -> list[list[str]]:
    """Each `git ...` invocation as [subcommand, args...]."""
    out, i = [], 0
    while i < len(tokens):
        if tokens[i] == "git" or tokens[i].endswith("/git"):
            j = i + 1
            while j < len(tokens) and tokens[j].startswith("-"):
                j += 2 if tokens[j] in GIT_GLOBAL_ARGS else 1
            k = j
            while k < len(tokens) and tokens[k] not in SEPARATORS:
                k += 1
            if j < k:
                out.append(tokens[j:k])
            i = k
        else:
            i += 1
    return out


def commit_skips_hooks(args: list[str]) -> bool:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return False
        if a in COMMIT_ARGS:
            i += 2
            continue
        if a.startswith("--no-veri"):
            return True
        if a.startswith("-") and not a.startswith("--"):
            for ch in a[1:]:
                if ch == "n":
                    return True
                if ch in SHORT_WITH_VALUE:
                    break
        i += 1
    return False


def reason(command: str) -> str | None:
    low = command.lower()
    if "hookspath" in low:
        return "it changes git's core.hooksPath, which turns off the privacy checks"
    if "--no-verify" in low and "git" in low:
        return "--no-verify skips the privacy checks"
    if HOOK_FILES.search(command):
        return "it removes or changes the git hooks"
    try:
        tokens = words(command)
    except ValueError:
        return None  # unbalanced quotes: let it through, the git hooks still run
    for cmd in git_commands(tokens):
        sub, args = cmd[0], cmd[1:]
        if sub == "send-pack":
            return "git send-pack pushes without the pre-push check"
        if sub == "commit" and commit_skips_hooks(args):
            return "git commit -n skips the privacy checks"
        if any(a.startswith("--no-veri") for a in args):
            return "--no-verify skips the privacy checks"
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        command = payload.get("tool_input", {}).get("command", "")
    except (ValueError, AttributeError):
        return 0
    if payload.get("tool_name") != "Bash" or not isinstance(command, str):
        return 0
    why = reason(command)
    if why:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Blocked: {why}. The repo is public and these checks "
                                        "keep the user's data out of it (SPEC §7). Fix what "
                                        "the hook found instead; never bypass it.",
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
