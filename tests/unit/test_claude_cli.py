import json

import pytest

from fin.ai import claude_cli as cc


def records(n=3):
    return [{"key": f"k{i}", "description": "STARBUCKS", "amount": 4.5, "date": "2026-09-01"}
            for i in range(n)]


def test_clean_env_strips_keys_and_provider_switches():
    env = cc.clean_env({
        "ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "u",
        "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "PATH": "/bin",
    })
    assert env == {
        "PATH": "/bin", "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    }


def test_argv_flags(fake_claude):
    task = cc.load_task("merchant_categorize")
    argv = cc.build_argv(task)
    assert "--bare" not in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--tools") + 1] == ""
    for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence", "-p"):
        assert flag in argv
    assert "--mcp-config" not in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--max-turns") + 1] == "3"
    prompt = argv[argv.index("--system-prompt-file") + 1]
    assert prompt.startswith("/") and prompt.endswith("prompts/merchant_categorize.md")
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    enum = schema["properties"]["results"]["items"]["properties"]["category_id"]["enum"]
    assert "uncategorized" in enum and "dining.coffee" in enum
    assert cc.CATEGORY_PLACEHOLDER not in enum


def test_only_haiku(fake_claude):
    with pytest.raises(cc.AIError):
        cc.build_argv(cc.load_task("merchant_categorize"), model="sonnet")


def test_success_writes_audit_trail_and_no_api_key(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
    run = tmp_path / "run"
    res = cc.run_task(cc.load_task("merchant_categorize"), {"records": records()}, run)
    assert [r["key"] for r in res.output["results"]] == ["k0", "k1", "k2"]
    for name in ("input.json", "output.json", "meta.json"):
        assert (run / name).exists()
        assert (run / name).stat().st_mode & 0o077 == 0
    meta = json.loads((run / "meta.json").read_text())
    assert meta["total_cost_usd_estimate"] == 0.001 and meta["model"] == "haiku"
    calls = [json.loads(line) for line in fake_claude.read_text().splitlines()]
    assert calls, "fake claude was not called"
    for call in calls:
        assert "ANTHROPIC_API_KEY" not in call["env_keys"]
    assert calls[-1]["cwd"] == str(run.resolve())


@pytest.mark.parametrize("mode", ["limit", "limit_text"])
def test_usage_limit(fake_claude, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(cc.UsageLimitError, match="subscription limit reached"):
        cc.run_task(cc.load_task("merchant_categorize"), {"records": records()}, tmp_path / "r")


@pytest.mark.parametrize("mode", ["badjson", "schema_invalid", "error"])
def test_errors(fake_claude, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(cc.AIError) as e:
        cc.run_task(cc.load_task("merchant_categorize"), {"records": records()}, tmp_path / "r")
    assert not isinstance(e.value, cc.UsageLimitError)


@pytest.mark.parametrize("method", ["api_key", "api_key_helper", "third_party", "none"])
def test_refuses_non_subscription_auth(fake_claude, tmp_path, monkeypatch, method):
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", method)
    with pytest.raises(cc.AuthError):
        cc.run_task(cc.load_task("merchant_categorize"), {"records": records()}, tmp_path / "r")


@pytest.mark.parametrize("method", ["claude.ai", "oauth_token"])
def test_accepts_subscription_auth(fake_claude, monkeypatch, method):
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", method)
    assert cc.require_subscription_auth() == method


def test_missing_keys_detected(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "missing_key")
    recs = records()
    res = cc.run_task(cc.load_task("merchant_categorize"), {"records": recs}, tmp_path / "r")
    missing, extra = cc.check_keys([r["key"] for r in recs], res.output["results"])
    assert missing == ["k0"] and extra == []


def test_check_keys_duplicates_and_unknown():
    missing, extra = cc.check_keys(["a", "b"], [{"key": "a"}, {"key": "a"}, {"key": "z"}])
    assert missing == ["b"] and extra == ["a", "z"]
