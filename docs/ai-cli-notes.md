# Claude CLI notes (SPEC §14.3 spike)

Run 2026-10-06 on macOS 26 (Darwin 25.2), Claude Code **2.1.285**, Pro subscription,
`authMethod: claude.ai`. Fake records only (`scripts/ai_spike.py`).

## 1. Flags (`claude --help` + parse probes)

Hidden flags don't appear in `--help`. Each was probed: an invalid value or missing file
gives a specific error, while an unknown flag gives `error: unknown option`.

| Flag | In `--help` | Accepted | Notes |
|---|---|---|---|
| `-p`, `--model`, `--tools`, `--strict-mcp-config`, `--mcp-config`, `--settings`, `--setting-sources`, `--allowedTools`, `--disallowedTools`, `--permission-mode`, `--output-format`, `--json-schema`, `--no-session-persistence`, `--safe-mode`, `--append-system-prompt` | yes | yes | |
| `--system-prompt-file` | **no** | yes | "System prompt file not found" for a bad path |
| `--append-system-prompt-file` | **no** | yes | "Append system prompt file not found" |
| `--max-turns` | **no** | yes | "must be a number" for a bad value |
| `--permission-mode default` | not in choices | yes | choices list `manual`, which looks like the new name; `default` still works |
| `--setting-sources ""` | yes | yes | empty list = load no user/project/local settings; `--settings` still applies |
| `--bare` | yes | never used | help confirms it needs `ANTHROPIC_API_KEY` or apiKeyHelper |

Also noticed: `--restricted` (drops code-running tools, ignores user/project/local settings,
confines file tools to working dirs). Not used yet; `--tools Write` + `--setting-sources ""`
already give the same effect for our sessions. Candidate for later hardening.

## 2. `--safe-mode` with `-p` on the subscription
Works. `ANTHROPIC_API_KEY` unset in parent and child; `claude auth status` → `claude.ai`.
No fallback needed.

## 3. `structured_output` with `--tools ""`
Works. `--output-format json --json-schema '<schema>'` returns `structured_output` that
validates against the schema. It takes **2 turns** (structured output uses an internal
tool call), so `--max-turns 3` leaves one turn of slack. No fallback needed.

## 4. Transcripts
- `--no-session-persistence`: no new `.jsonl` under `~/.claude/projects/` for the run dir,
  and no new transcript anywhere under that folder during the spike.
- Extra check for §9.1: `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1` **without**
  `--no-session-persistence` also wrote no transcript in `-p` mode. The control run without
  the variable wrote one (deleted afterwards). Interactive mode is checked by `fin sync selftest`.

## 5. Latency and usage (model `haiku`, task `merchant_categorize`)

| Batch | Wall time | Input tokens | Output tokens (of which thinking) | `total_cost_usd` estimate |
|---|---|---|---|---|
| 3 records | 7.1 s | 2,130 | 554 (352) | $0.0049 |
| 50 records | 31.2 s | 3,839 | 4,029 (2,023) | $0.0240 |

All keys came back exactly once at both sizes. Sample: STARBUCKS → `dining.coffee` 0.99,
SHELL OIL → `transportation.fuel` 0.99, TRADER JOE'S → `groceries` 0.98.
The cost figure is a client-side estimate; the subscription isn't billed per call.
About half the output is thinking. `--effort low` might trim it; untested and not in the spec.

## Hook payload format (recorded, for the guard)
PreToolUse/PostToolUse stdin JSON has `session_id, transcript_path, cwd, prompt_id,
permission_mode, hook_event_name, tool_name, tool_input, tool_use_id`, plus
`tool_response, duration_ms` on PostToolUse. `cwd` is the resolved real path
(`/private/tmp/...`, not `/tmp/...`). Verified end to end: a PreToolUse
`permissionDecision: "deny"` from `sync/guard.py` blocks a Write even when an
`Edit(...)` allow rule covers the path.
