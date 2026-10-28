# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`claude -p` calls on the Claude subscription only (SPEC §14, SPEC-cards §11).

Roles: the classifier (haiku, no tools), card research (WebSearch + WebFetch limited to
allowlisted domains), card terms extraction (no tools) and merchant profiles (haiku, no tools;
WebSearch only on request). All share the clean environment, the subscription check, the audit
trail and a cross-process lock, so only one call runs at a time (SPEC §14.4).

Never uses --bare and never passes ANTHROPIC_API_KEY; the key is only checked for and
stripped from the child environment.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from fin.config import BARE_DOMAIN, REPO_ROOT, data_paths, load_reward_categories, load_taxonomy

PROMPTS_DIR = REPO_ROOT / "prompts"
SCHEMAS_DIR = REPO_ROOT / "schemas"
CATEGORY_PLACEHOLDER = "__CATEGORY_IDS__"
REWARD_CATEGORY_PLACEHOLDER = "__REWARD_CATEGORY_IDS__"
BENEFIT_KINDS_PLACEHOLDER = "__BENEFIT_KINDS__"
SUBSCRIPTION_AUTH = {"claude.ai", "oauth_token"}
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
LIMIT_PATTERN = re.compile(
    r"usage limit|rate limit|rate_limit|limit reached|limit will reset|out of extra usage",
    re.IGNORECASE,
)
STDIN_PROMPT = "Process the records on stdin exactly as the system prompt describes."
DISCOVER_PROMPT = "Find the documents for the card on stdin, as the system prompt describes."
# Tasks bound to the haiku model (SPEC §14.1, SPEC-cards §3).
HAIKU_ONLY = ("merchant_categorize", "item_categorize", "merchant_profile", "merchant_profile_web",
              "offer_extract")


class AIError(RuntimeError):
    pass


class AuthError(AIError):
    pass


class UsageLimitError(AIError):
    """The subscription limit was hit. Stop all AI work for this run; never retry."""

    MESSAGE = "AI paused: subscription limit reached. Rerun `fin process` later."


class AIBusyError(AIError):
    """Another `claude -p` call holds the lock and didn't finish within the wait."""


# ------------------------------------------------------- Claude Code leftovers

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def project_slug(path: Path) -> str:
    """Claude Code's folder name under ~/.claude/projects/ for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path.resolve()))


def remove_run_leftovers(run_dir: Path, projects: Path | None = None) -> bool:
    """Delete Claude Code's own folder for this run's cwd, if it made one.

    `--no-session-persistence` writes no transcript, but WebFetch still saves large results
    (fetched PDFs) under ~/.claude/projects/<slug>/<session>/tool-results/ (C0 spike). They're
    throwaway copies: the knowledge base keeps its own copies in card-docs/. The folder is unique
    to this run's folder, so nothing else is touched.
    """
    projects = projects or CLAUDE_PROJECTS
    target = projects / project_slug(run_dir)
    if target.parent != projects or not target.is_dir() or target.is_symlink():
        return False
    shutil.rmtree(target)
    return True


# ---------------------------------------------------------------------- lock


def lock_path() -> Path:
    return data_paths().locks / "claude.lock"


@contextmanager
def claude_lock(path: Path | None = None, wait: float | None = None) -> Iterator[None]:
    """One `claude -p` call at a time across processes (fcntl.flock on locks/claude.lock).

    wait=None blocks until the lock is free; otherwise raise AIBusyError after `wait` seconds.
    """
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if wait is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AIBusyError("another Claude call is running; try again") from None
                    time.sleep(0.2)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ----------------------------------------------------------------- environment


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Child environment for every `claude` subprocess (§14.2, also used by §9.1)."""
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if name in STRIPPED_ENV or name.startswith("CLAUDE_CODE_USE_"):
            del env[name]
    env["DISABLE_ERROR_REPORTING"] = "1"
    env["CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"] = "1"
    return env


def claude_bin() -> str:
    path = shutil.which("claude")
    if not path:
        raise AIError("`claude` CLI not found on PATH")
    return path


def auth_status(env: dict[str, str] | None = None) -> dict[str, Any]:
    proc = subprocess.run(
        [claude_bin(), "auth", "status"],
        capture_output=True, text=True, env=env or clean_env(), timeout=30,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise AuthError(f"`claude auth status` did not return JSON (exit {proc.returncode})") from e


def require_subscription_auth(env: dict[str, str] | None = None) -> str:
    """Abort unless Claude Code is logged in with the subscription. Returns authMethod."""
    status = auth_status(env)
    method = status.get("authMethod")
    if not status.get("loggedIn") or method not in SUBSCRIPTION_AUTH:
        raise AuthError(
            f"Claude auth method is {method!r}; need one of {sorted(SUBSCRIPTION_AUTH)}. "
            "Run `claude auth login` with your Claude subscription."
        )
    return method


# ---------------------------------------------------------------------- tasks


@dataclass(frozen=True)
class Task:
    name: str
    prompt_path: Path
    prompt_version: str
    schema: dict[str, Any]

    @property
    def schema_json(self) -> str:
        return json.dumps(self.schema, separators=(",", ":"))


def _fill_enums(node: Any, enums: dict[str, list[str]]) -> Any:
    """Replace ["__PLACEHOLDER__"] lists with their ids, at call time."""
    if isinstance(node, dict):
        return {k: _fill_enums(v, enums) for k, v in node.items()}
    if isinstance(node, list):
        if len(node) == 1 and isinstance(node[0], str) and node[0] in enums:
            return enums[node[0]]
        return [_fill_enums(v, enums) for v in node]
    return node


def _placeholder_values(schema_text: str) -> dict[str, list[str]]:
    from fin.cards.models import BENEFIT_KINDS

    enums: dict[str, list[str]] = {}
    if CATEGORY_PLACEHOLDER in schema_text:
        enums[CATEGORY_PLACEHOLDER] = load_taxonomy().ids
    if REWARD_CATEGORY_PLACEHOLDER in schema_text:
        enums[REWARD_CATEGORY_PLACEHOLDER] = load_reward_categories().ids
    if BENEFIT_KINDS_PLACEHOLDER in schema_text:
        enums[BENEFIT_KINDS_PLACEHOLDER] = list(BENEFIT_KINDS)
    return enums


def load_task(
    name: str,
    prompts_dir: Path = PROMPTS_DIR,
    schemas_dir: Path = SCHEMAS_DIR,
    schema_name: str | None = None,
) -> Task:
    """Load prompts/<name>.md and schemas/<schema_name or name>.json, enums filled in."""
    prompt_path = prompts_dir / f"{name}.md"
    header = prompt_path.read_text().splitlines()[0]
    m = re.fullmatch(r"<!--\s*prompt_version:\s*(\S+)\s*-->", header.strip())
    if not m:
        raise AIError(f"{prompt_path} must start with <!-- prompt_version: X -->")
    schema_text = (schemas_dir / f"{schema_name or name}.json").read_text()
    schema = _fill_enums(json.loads(schema_text), _placeholder_values(schema_text))
    jsonschema.Draft202012Validator.check_schema(schema)
    return Task(name, prompt_path, m.group(1), schema)


# ----------------------------------------------------------------------- calls


@dataclass
class CallResult:
    output: dict[str, Any]
    raw: dict[str, Any]
    duration_ms: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Invocation:
    """A complete `claude -p` command: argv plus extra environment for the child."""

    argv: list[str]
    model: str
    role: str  # classify | research | extract | merchant | merchant_web
    env_extra: dict[str, str] = field(default_factory=dict)
    allowed_domains: tuple[str, ...] = ()


def build_argv(task: Task, model: str = "haiku", max_turns: int = 3) -> list[str]:
    """The classifier's argv (SPEC §14.1). Unchanged for the SPEC §14 tasks."""
    if model != "haiku":
        raise AIError("classification always uses the haiku model (SPEC §14.1)")
    return _no_tools_argv(task, model, max_turns)


def _no_tools_argv(task: Task, model: str, max_turns: int, effort: str | None = None) -> list[str]:
    argv = [
        claude_bin(), "-p", STDIN_PROMPT,
        "--model", model,
        "--safe-mode",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--system-prompt-file", str(task.prompt_path.resolve()),
        "--output-format", "json",
        "--json-schema", task.schema_json,
        "--max-turns", str(max_turns),
    ]
    if effort:
        argv += ["--effort", effort]
    return argv


def _with_tools_argv(
    task: Task, model: str, max_turns: int, prompt: str, tools: list[str], allowed: list[str],
) -> list[str]:
    """argv for a role with tools. Only the rules given here apply: --setting-sources "" keeps
    user settings from adding allow rules, and dontAsk denies everything else without a prompt."""
    return [
        claude_bin(), "-p", prompt,
        "--model", model,
        "--safe-mode",
        "--setting-sources", "",
        "--tools", ",".join(tools),
        "--allowedTools", *allowed,
        "--disallowedTools", "mcp__*",
        "--permission-mode", "dontAsk",
        "--strict-mcp-config",
        "--no-chrome",
        "--no-session-persistence",
        "--system-prompt-file", str(task.prompt_path.resolve()),
        "--output-format", "json",
        "--json-schema", task.schema_json,
        "--max-turns", str(max_turns),
    ]


def webfetch_rules(domains: list[str] | tuple[str, ...]) -> list[str]:
    """One `domain:x` and one `domain:*.x` rule per domain: a bare rule doesn't cover
    subdomains (verified in the C0 spike, docs/ai-cli-notes.md)."""
    rules = []
    for d in domains:
        if not BARE_DOMAIN.fullmatch(d):
            raise AIError(f"not a bare domain: {d!r}")
        rules += [f"WebFetch(domain:{d})", f"WebFetch(domain:*.{d})"]
    return rules


def research_invocation(
    task: Task, model: str, domains: list[str], *, max_turns: int = 30, max_searches: int = 20,
) -> Invocation:
    """card_discover: WebSearch, and WebFetch on the allowlisted domains only (SPEC-cards §7.1)."""
    if not domains:
        raise AIError("research needs at least one allowlisted domain")
    argv = _with_tools_argv(task, model, max_turns, DISCOVER_PROMPT, ["WebSearch", "WebFetch"],
                            ["WebSearch", *webfetch_rules(domains)])
    return Invocation(argv, model, "research",
                      {"CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": str(max_searches)},
                      tuple(domains))


def extract_invocation(task: Task, model: str, *, max_turns: int = 3) -> Invocation:
    """card_terms / card_benefits / card_question: no tools, the classifier's flags."""
    if task.name in HAIKU_ONLY:
        raise AIError(f"{task.name} is not an extraction task")
    return Invocation(_no_tools_argv(task, model, max_turns), model, "extract")


def merchant_invocation(
    task: Task, *, web: bool = False, max_turns: int = 3, effort: str | None = None,
    max_searches: int = 3,
) -> Invocation:
    """merchant_profile (haiku, no tools) or, only when asked, merchant_profile_web
    (haiku, WebSearch only)."""
    if not web:
        return Invocation(_no_tools_argv(task, "haiku", max_turns, effort), "haiku", "merchant")
    argv = _with_tools_argv(task, "haiku", max_turns, STDIN_PROMPT, ["WebSearch"], ["WebSearch"])
    if effort:
        argv += ["--effort", effort]
    return Invocation(argv, "haiku", "merchant_web",
                      {"CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": str(max_searches)})


def run_task(
    task: Task,
    payload: dict[str, Any],
    run_dir: Path,
    *,
    model: str = "haiku",
    max_turns: int = 3,
    timeout: int = 180,
    invocation: Invocation | None = None,
    lock_wait: float | None = None,
) -> CallResult:
    """One `claude -p` call. Writes input.json, output.json and meta.json into run_dir.

    Without `invocation` it's a classifier call (SPEC §14.1). The call holds the cross-process
    lock from the auth check to the end.
    """
    inv = invocation or Invocation(build_argv(task, model, max_turns), model, "classify")
    if "--bare" in inv.argv:
        raise AssertionError("--bare requires an API key")
    env = clean_env()
    for name in inv.env_extra:
        if name.startswith("ANTHROPIC_") or name.startswith("CLAUDE_CODE_USE_"):
            raise AssertionError(f"{name} must never be passed to claude")
    env.update(inv.env_extra)

    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    input_text = json.dumps(payload, ensure_ascii=False)
    _write_private(run_dir / "input.json", input_text)

    with claude_lock(wait=lock_wait):
        require_subscription_auth(env)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                inv.argv, input=input_text, capture_output=True, text=True,
                cwd=run_dir, env=env, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise AIError(f"claude -p timed out after {timeout}s") from e
        finally:
            remove_run_leftovers(run_dir)
    duration_ms = int((time.monotonic() - started) * 1000)

    meta: dict[str, Any] = {
        "task": task.name, "prompt_version": task.prompt_version, "model": inv.model,
        "role": inv.role, "allowed_domains": list(inv.allowed_domains),
        "exit_code": proc.returncode, "duration_ms": duration_ms,
    }
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError:
        meta["error"] = "non-JSON output"
        _write_private(run_dir / "meta.json", json.dumps(meta, indent=2))
        if LIMIT_PATTERN.search(proc.stdout + proc.stderr):
            raise UsageLimitError(UsageLimitError.MESSAGE) from None
        raise AIError(f"claude -p returned non-JSON output (exit {proc.returncode})") from None

    meta.update(
        {
            "session_id": raw.get("session_id"),
            "num_turns": raw.get("num_turns"),
            "usage": raw.get("usage"),
            # Client-side estimate only; the subscription isn't billed per call (§14.2).
            "total_cost_usd_estimate": raw.get("total_cost_usd"),
            "is_error": raw.get("is_error"),
            "subtype": raw.get("subtype"),
            # Tool calls the permission rules refused (research roles): tool and URL only.
            "permission_denials": [
                {"tool": d.get("tool_name"), "url": (d.get("tool_input") or {}).get("url")}
                for d in raw.get("permission_denials") or []
            ],
        }
    )
    _write_private(run_dir / "output.json", json.dumps(raw, indent=2, ensure_ascii=False))
    _write_private(run_dir / "meta.json", json.dumps(meta, indent=2))

    if raw.get("is_error") or proc.returncode != 0:
        text = f"{raw.get('result', '')} {raw.get('subtype', '')} {proc.stderr}"
        if LIMIT_PATTERN.search(text):
            raise UsageLimitError(UsageLimitError.MESSAGE)
        raise AIError(f"claude -p failed: {raw.get('subtype')}: {str(raw.get('result'))[:200]}")

    output = raw.get("structured_output")
    if output is None:
        raise AIError("claude -p returned no structured_output")
    try:
        jsonschema.validate(output, task.schema)
    except jsonschema.ValidationError as e:
        raise AIError(f"structured_output failed schema validation: {e.message}") from e
    return CallResult(output=output, raw=raw, duration_ms=duration_ms, meta=meta)


def check_keys(sent: list[str], results: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Return (missing, unexpected_or_duplicate) keys. Every key must come back exactly once."""
    seen: list[str] = [r["key"] for r in results]
    missing = [k for k in sent if k not in seen]
    extra = [k for k in seen if k not in sent or seen.count(k) > 1]
    return missing, sorted(set(extra))


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
