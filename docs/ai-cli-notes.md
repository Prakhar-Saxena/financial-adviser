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

# Card optimizer spike (SPEC-cards Phase C0)

Run 2026-10-26, Claude Code **2.1.308**, Pro subscription, `authMethod: claude.ai`,
`ANTHROPIC_API_KEY` unset. Probes used example.com/iana.org; the real runs used one card
(Chase Freedom Unlimited) and public documents only. Script: `scripts/cards_spike.py`.

## 1. Tools with `--setting-sources ""`, `--safe-mode`, `-p`
Works on the subscription. `--tools "WebSearch,WebFetch"`, `--allowedTools`, `--disallowedTools "mcp__*"`,
`--permission-mode dontAsk` (listed in help), `--no-chrome`, `--effort` all accepted.

## 2. WebFetch domain rules under `dontAsk`
| Allow rules | example.com | www.example.com | www.iana.org |
|---|---|---|---|
| `WebFetch(domain:example.com)` | fetched | **denied** | denied |
| + `WebFetch(domain:*.example.com)` | fetched | fetched | denied |

A bare rule doesn't cover subdomains; the `*.` form does. Denials come back at once as a tool
error ("Permission to use WebFetch has been denied because Claude Code is running in don't ask
mode"); no prompt, no hang. They're listed in the result JSON's `permission_denials`
(tool name + input), which `run_task` records in meta.json (tool and URL only).

## 3. Structured output with tools, limits, schema keywords
- `structured_output` comes back after a multi-turn tool workflow (card_discover: 5 turns).
- `--max-turns` reached: exit 1, `subtype: error_max_turns`, `is_error: true`,
  `errors: ["Reached maximum number of turns (2)"]`, **no** `structured_output`. → `AIError`, not a limit.
- `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION=1`: the first search runs; later ones return a tool
  error ("this turn's web search budget is used up (limit: 1 WebSearch call per turn …)") and the
  model still finishes with `structured_output`. In `-p` the whole run is one turn, so it is a
  per-session cap.
- `usage.server_tool_use.web_search_requests` stayed 0 even when searches ran, so the JSON doesn't
  count searches. Only `num_turns` and tokens are available.
- Schema keywords used by the card schemas and accepted (outputs validated with jsonschema):
  `$defs` + `$ref`, `anyOf` with `{"type": "null"}`, type arrays (`["string","null"]`), `enum`
  (including `null`), `pattern`, `minLength`/`maxLength`, `minItems`/`maxItems`, `minimum`/
  `maximum`/`exclusiveMinimum`, `additionalProperties: false`. The model respects `maxLength`
  (it truncated a string and said so).

## 4. Transcripts and memory
- No `.jsonl` transcript and no `memory/` folder for any run (`--no-session-persistence`,
  `--safe-mode`).
- **Gap:** the card_discover run (WebFetch on a PDF) created
  `~/.claude/projects/<run-dir-slug>/<session-id>/tool-results/webfetch-<id>.pdf` (the fetched
  public PDF, mode 644). `--no-session-persistence` doesn't cover large tool results. The no-tools
  runs (card_terms, merchant_profile) left nothing. Contents here are public issuer documents,
  never personal data, but it is a file outside the data dir. Proposed fix in decisions.md.

## 5. Real runs on one card (Chase Freedom Unlimited)
`total_cost_usd` is the client's estimate; the subscription isn't billed per call.

| Task | Model | Wall | Turns | Input (cache write) | Output | Estimate |
|---|---|---|---|---|---|---|
| card_discover | sonnet | 18.0 s | 5 | 6,742 (+9,616 cache read) | 1,171 | $0.072 |
| card_terms (4 docs, 134,642 chars) | sonnet | 11.9 s | 2 | 51,153 | 1,718 | $0.222 |
| card_terms, same input | haiku | 59.8 s | 2 | 51,150 | 14,843 | $0.018 |

card_discover returned 4 documents, all on allowlisted domains: product page, rewards program
agreement PDF, a chase.com cardmember agreement PDF and the 2018 CFPB agreement. It found no
benefits guide or pricing page (`not_found`), and no permission denials happened.

card_terms side by side (every quote from both models was found verbatim in the documents):

| | sonnet | haiku |
|---|---|---|
| currency, annual fee, FTF | cash back, $0, 3% | same (more quotes) |
| base rule | 1.5, `categories: ["other"]`, exclusions (cash advances, fees, …) | 1.5, no categories, no exclusions |
| 3% dining + food delivery, 3% drugstores | yes | yes |
| 5% travel via Chase Travel | issuer_portal, merchant "Chase Travel" | issuer_portal; also the FROSCH exclusion |
| 2% Lyft (in app) | yes; `valid_to` not set | yes; `valid_to` not set, end date 09/30/2027 in notes |
| DoorDash $10/quarter | credit | credit |
| notes | 3: welcome offer, DashPass, old CFPB doc | 7: also redemption value 0.8¢ vs 1¢, Lyft end date, referral |

Both are usable. Sonnet is 5× faster with 1/9 the output (it thinks less); haiku is more thorough
in notes but slower and missed the base-rule exclusions. Neither filled `valid_to` from a date stated
only in prose, so the prompt needs a line on that (C1).

## 6. `--effort low` for merchant_profile (haiku, 10 generic queries)
| Effort | Wall | Output tokens | Estimate | Categories |
|---|---|---|---|---|
| default | 11.4 s | 2,394 | $0.0019 | — |
| low | 8.5 s | 2,127 | $0.0012 | identical; MCC guesses differ slightly (DoorDash 5812 vs 5814, Target 5310 vs 5311) |

`--effort low` works with `-p` and saves about 11% of output tokens (the second run also got a cache
read). Small but free, so `merchant_profile` uses it from Phase C2 if you agree.
