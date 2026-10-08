"""Classifier calls through `claude -p`, on the Claude subscription only (SPEC §14).

Never uses --bare and never passes ANTHROPIC_API_KEY; the key is only checked for and
stripped from the child environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from fin.config import REPO_ROOT, load_taxonomy

PROMPTS_DIR = REPO_ROOT / "prompts"
SCHEMAS_DIR = REPO_ROOT / "schemas"
CATEGORY_PLACEHOLDER = "__CATEGORY_IDS__"
SUBSCRIPTION_AUTH = {"claude.ai", "oauth_token"}
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
LIMIT_PATTERN = re.compile(
    r"usage limit|rate limit|rate_limit|limit reached|limit will reset|out of extra usage",
    re.IGNORECASE,
)
STDIN_PROMPT = "Process the records on stdin exactly as the system prompt describes."


class AIError(RuntimeError):
    pass


class AuthError(AIError):
    pass


class UsageLimitError(AIError):
    """The subscription limit was hit. Stop all AI work for this run; never retry."""

    MESSAGE = "AI paused: subscription limit reached. Rerun `fin process` later."


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


def _fill_categories(node: Any, ids: list[str]) -> Any:
    if isinstance(node, dict):
        return {k: _fill_categories(v, ids) for k, v in node.items()}
    if isinstance(node, list):
        return ids if node == [CATEGORY_PLACEHOLDER] else [_fill_categories(v, ids) for v in node]
    return node


def load_task(name: str, prompts_dir: Path = PROMPTS_DIR, schemas_dir: Path = SCHEMAS_DIR) -> Task:
    prompt_path = prompts_dir / f"{name}.md"
    header = prompt_path.read_text().splitlines()[0]
    m = re.fullmatch(r"<!--\s*prompt_version:\s*(\S+)\s*-->", header.strip())
    if not m:
        raise AIError(f"{prompt_path} must start with <!-- prompt_version: X -->")
    schema = json.loads((schemas_dir / f"{name}.json").read_text())
    schema = _fill_categories(schema, load_taxonomy().ids)
    jsonschema.Draft202012Validator.check_schema(schema)
    return Task(name, prompt_path, m.group(1), schema)


# ----------------------------------------------------------------------- calls


@dataclass
class CallResult:
    output: dict[str, Any]
    raw: dict[str, Any]
    duration_ms: int
    meta: dict[str, Any] = field(default_factory=dict)


def build_argv(task: Task, model: str = "haiku", max_turns: int = 3) -> list[str]:
    if model != "haiku":
        raise AIError("classification always uses the haiku model (SPEC §14.1)")
    return [
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


def run_task(
    task: Task,
    payload: dict[str, Any],
    run_dir: Path,
    *,
    model: str = "haiku",
    max_turns: int = 3,
    timeout: int = 180,
) -> CallResult:
    """One `claude -p` call. Writes input.json, output.json and meta.json into run_dir."""
    argv = build_argv(task, model, max_turns)
    if "--bare" in argv:
        raise AssertionError("--bare requires an API key")
    env = clean_env()
    require_subscription_auth(env)

    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    input_text = json.dumps(payload, ensure_ascii=False)
    _write_private(run_dir / "input.json", input_text)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, input=input_text, capture_output=True, text=True,
            cwd=run_dir, env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise AIError(f"claude -p timed out after {timeout}s") from e
    duration_ms = int((time.monotonic() - started) * 1000)

    meta: dict[str, Any] = {
        "task": task.name, "prompt_version": task.prompt_version, "model": model,
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
