import json
import stat

import pytest

from fin.config import DataPaths, Settings, load_cards
from fin.sync import launcher
from fin.sync.mcp_config import mcp_config, preset_chrome_pdf_download


@pytest.fixture
def plan(tmp_path, fake_claude):
    paths = DataPaths(tmp_path / "data")
    return launcher.plan_session(paths, Settings(), launcher.SELFTEST_SITE, run_date="2026-10-06")


def arg(argv, flag):
    return argv[argv.index(flag) + 1]


def test_paths(plan, tmp_path):
    root = tmp_path / "data"
    assert plan.run_dir == root / "inbox" / "selftest" / "2026-10-06"
    assert plan.session_cwd == root / "inbox" / "selftest"
    assert plan.control_dir == root / "sync-sessions" / "selftest" / "2026-10-06"
    assert plan.profile_dir == root / "browser-profiles" / "selftest"
    assert plan.run_dir.parent == plan.session_cwd  # explicit PDF names resolve in the root
    assert plan.control_dir not in plan.run_dir.parents


def test_argv(plan):
    argv = launcher.build_argv(plan)
    assert "--bare" not in argv and "-p" not in argv
    assert arg(argv, "--model") == "sonnet"
    assert arg(argv, "--tools") == "Write"
    assert "--strict-mcp-config" in argv
    assert "--no-chrome" in argv and "--chrome" not in argv
    assert arg(argv, "--mcp-config") == str(plan.mcp_json.resolve())
    assert arg(argv, "--settings") == str(plan.settings_json.resolve())
    assert arg(argv, "--setting-sources") == ""
    assert arg(argv, "--permission-mode") == "default"
    assert arg(argv, "--append-system-prompt-file") == str(plan.prompt_md.resolve())
    assert argv[-1] == str(plan.prompt_md.resolve())  # no opening prompt

    allowed = argv[argv.index("--allowedTools") + 1: argv.index("--disallowedTools")]
    assert "mcp__playwright__browser_pdf_save" in allowed
    assert "mcp__playwright__browser_click" not in allowed  # approve mode
    assert f"Edit(/{plan.run_dir.resolve()}/**)" in allowed
    assert allowed[-1].startswith("Edit(//")
    end = argv.index("--append-system-prompt-file")
    disallowed = argv[argv.index("--disallowedTools") + 1: end]
    for t in ("browser_run_code_unsafe", "browser_evaluate", "browser_file_upload",
              "browser_drag", "browser_drop", "browser_handle_dialog"):
        assert f"mcp__playwright__{t}" in disallowed
    assert "Bash" in disallowed and "WebFetch" in disallowed
    # A bare Read/Edit deny would also block Write into the run folder.
    assert not {"Read", "Edit", "NotebookEdit"} & set(disallowed)


def test_trusted_mode(tmp_path, fake_claude):
    paths = DataPaths(tmp_path)
    s = Settings.model_validate({"sync": {"trusted_sites": ["selftest"]}})
    plan = launcher.plan_session(paths, s, launcher.SELFTEST_SITE)
    argv = launcher.build_argv(plan)
    assert arg(argv, "--permission-mode") == "dontAsk"
    assert "mcp__playwright__browser_click" in argv


def test_site_model_override(tmp_path):
    s = Settings.model_validate({"sync": {"site_models": {"citi": "haiku"}}})
    site = load_cards().site("citi")
    assert launcher.plan_session(DataPaths(tmp_path), s, site).model == "haiku"


def test_env(plan, monkeypatch):
    base = {"ANTHROPIC_API_KEY": "sk", "CLAUDE_CODE_USE_BEDROCK": "1", "HOME": "/h"}
    env = launcher.session_env(plan, base)
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_USE_BEDROCK" not in env
    assert env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    plan.keep_transcript = True
    assert "CLAUDE_CODE_SKIP_PROMPT_HISTORY" not in launcher.session_env(plan, base)


def test_mcp_config(tmp_path):
    cfg = mcp_config("0.0.83", tmp_path / "prof", tmp_path / "out")
    server = cfg["mcpServers"]["playwright"]
    assert server["command"] == "npx"
    a = server["args"]
    assert "@playwright/mcp@0.0.83" in a
    assert a[a.index("--browser") + 1] == "chrome"
    assert a[a.index("--user-data-dir") + 1] == str((tmp_path / "prof").resolve())
    assert a[a.index("--output-dir") + 1] == str((tmp_path / "out").resolve())
    assert a[a.index("--caps") + 1] == "pdf"
    assert a[a.index("--codegen") + 1] == "python"
    assert "--save-session" in a and "--no-webmcp" in a
    for bad in ("--extension", "--isolated", "--allow-unrestricted-file-access",
                "--allowed-origins"):
        assert bad not in a
    with pytest.raises(ValueError):
        mcp_config("latest", tmp_path, tmp_path)


def test_prepare_writes_private_control_files(plan):
    launcher.prepare(plan, "0.0.83")
    for f in (plan.mcp_json, plan.settings_json, plan.guard_config, plan.prompt_md):
        assert f.exists() and stat.S_IMODE(f.stat().st_mode) == 0o600
    for d in (plan.run_dir, plan.control_dir, plan.profile_dir):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    settings = json.loads(plan.settings_json.read_text())
    for event in ("PreToolUse", "PostToolUse"):
        (entry,) = settings["hooks"][event]
        assert entry["matcher"] == "*"
        cmd = entry["hooks"][0]["command"]
        assert "sync/guard.py" in cmd and str(plan.guard_config.resolve()) in cmd

    gc = json.loads(plan.guard_config.read_text())
    assert gc["allowed_domains"] == ["example.com"]
    assert gc["run_dir"] == str(plan.run_dir.resolve())
    assert gc["record_payloads_dir"]  # selftest only

    prompt = plan.prompt_md.read_text()
    assert "Rules for every browsing session" in prompt
    assert "Site: selftest" in prompt
    assert str(plan.run_dir.resolve()) in prompt


def test_payload_recording_only_for_selftest(tmp_path):
    plan = launcher.plan_session(DataPaths(tmp_path), Settings(), load_cards().site("chase"))
    assert launcher.guard_config(plan, [])["record_payloads_dir"] is None


def test_rerun_sets_aside_previous_outputs(plan):
    launcher.prepare(plan, "0.0.83")
    (plan.run_dir / "manifest.json").write_text("{}")
    (plan.run_dir / "statement.pdf").write_bytes(b"%PDF-")
    moved = launcher.prepare(plan, "0.0.83")
    assert moved == ["manifest.json"]
    assert not (plan.run_dir / "manifest.json").exists()
    assert (plan.run_dir / "statement.pdf").exists()
    assert "statement.pdf" in plan.prompt_md.read_text()
    assert list((plan.control_dir / "previous").rglob("manifest.json"))


def test_chrome_pdf_preference(tmp_path):
    prof = tmp_path / "prof"
    assert preset_chrome_pdf_download(prof) == "set"
    prefs = json.loads((prof / "Default" / "Preferences").read_text())
    assert prefs["plugins"]["always_open_pdf_externally"] is True
    assert preset_chrome_pdf_download(prof) == "already set"
    (prof / "SingletonLock").write_text("")
    assert preset_chrome_pdf_download(prof) == "skipped: profile in use"


def test_prompt_carries_output_formats_that_validate(plan):
    """The session can't Read the schema, so the prompt's examples must be valid as written."""
    import re

    from fin.ingest.manifest import validate_run

    launcher.prepare(plan, "0.0.83")
    prompt = plan.prompt_md.read_text()
    assert "# Output file formats" in prompt and '"$defs"' in prompt
    blocks = re.findall(r"```json\n(.*?)\n```", prompt.split("# Output file formats")[1], re.S)
    manifest, report = json.loads(blocks[0]), json.loads(blocks[1])
    manifest["files"][0].update(file="selftest_example.pdf", period_start=None, period_end=None)
    report["tools_available"] = ["mcp__playwright__browser_navigate", "Write"]
    (plan.run_dir / "selftest_example.pdf").write_bytes(b"%PDF-")
    (plan.run_dir / "manifest.json").write_text(json.dumps(manifest))
    (plan.run_dir / "run_report.json").write_text(json.dumps(report))
    check = validate_run(plan.run_dir, "selftest", set(), plan.run_date)
    assert check.ok, check.errors


def test_selftest_no_bash_check_reads_invalid_report(plan, tmp_path):
    from fin.sync.runs import selftest_checks

    launcher.prepare(plan, "0.0.83")
    (plan.run_dir / "run_report.json").write_text(json.dumps(
        {"collected": [{"file": "x"}], "tools_available": ["mcp__playwright__browser_snapshot"]}))
    checks, mcheck, _ = selftest_checks(plan, 0, projects=tmp_path / "projects")
    by_name = {c.name: c for c in checks}
    assert not mcheck.ok
    assert by_name["Session has no Bash tool"].ok
    assert by_name["No transcript saved"].ok
    assert by_name["No browser tools besides the Playwright server"].ok
    tools = ["mcp__playwright__browser_snapshot", "mcp__claude-in-chrome__computer"]
    (plan.run_dir / "run_report.json").write_text(json.dumps({"tools_available": tools}))
    checks, _, _ = selftest_checks(plan, 0, projects=tmp_path / "projects")
    assert not {c.name: c for c in checks}["No browser tools besides the Playwright server"].ok


def test_driven_session_round_trip(tmp_path, fake_claude):
    import threading
    import time

    from fin.sync.driver import Driver, driven_argv, send

    paths = DataPaths(tmp_path / "data")
    plan = launcher.plan_session(paths, Settings(), launcher.SELFTEST_SITE, trusted=True)
    launcher.prepare(plan, "0.0.83")
    argv = driven_argv(plan)
    assert argv[1:4] == ["-p", "--input-format", "stream-json"]
    assert "--no-session-persistence" in argv and arg(argv, "--permission-mode") == "dontAsk"
    with pytest.raises(ValueError):
        driven_argv(launcher.plan_session(paths, Settings(), launcher.SELFTEST_SITE))

    d = Driver(plan)
    t = threading.Thread(target=lambda: setattr(d, "code", d.run()))
    t.start()

    def wait_for(text, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if d.log_path.exists() and text in d.log_path.read_text():
                return
            time.sleep(0.1)
        raise AssertionError(f"{text!r} not in log: {d.log_path.read_text()}")

    wait_for("AGENT: LOGIN_NEEDED")
    assert d.status_path.read_text().strip() == "waiting"
    send(plan.control_dir, "logged in")
    wait_for("AGENT: got: logged in")
    send(plan.control_dir, "/exit")
    t.join(timeout=10)
    log = d.log_path.read_text()
    assert d.code == 0 and "SESSION EXITED: 0" in log
    assert "TOOL: mcp__playwright__browser_navigate" in log
    assert d.status_path.read_text().strip() == "exited"


def test_live_session_finds_newest_running(tmp_path):
    from fin.sync.driver import live_session

    for day, status in (("2026-10-05", "exited"), ("2026-10-06", "waiting"), ("2026-10-07", "")):
        d = tmp_path / day / "driver"
        d.mkdir(parents=True)
        if status:
            (d / "status").write_text(status + "\n")
    assert live_session(tmp_path).name == "2026-10-06"
    (tmp_path / "2026-10-06" / "driver" / "status").write_text("exited\n")
    assert live_session(tmp_path) is None
    assert live_session(tmp_path / "missing") is None


def test_extension_mode_for_costco(tmp_path):
    cfg = mcp_config("0.0.83", tmp_path / "prof", tmp_path / "out", browser="extension")
    a = cfg["mcpServers"]["playwright"]["args"]
    assert "--extension" in a and "--user-data-dir" not in a and "--browser" not in a
    for kept in ("--no-webmcp", "--save-session", "--output-dir"):
        assert kept in a
    s = Settings.model_validate({"sync": {"site_browser": {"costco": "extension"}}})
    plan = launcher.plan_session(DataPaths(tmp_path), s, load_cards().site("costco"))
    assert plan.browser == "extension"
    assert launcher.plan_session(DataPaths(tmp_path), s, load_cards().site("chase")).browser == (
        "profile")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.model_validate({"sync": {"site_browser": {"costco": "remote"}}})


def test_extension_token_only_in_extension_mode(tmp_path):
    from fin.sync.mcp_config import extension_token

    secrets = tmp_path / "data" / "secrets"
    secrets.mkdir(parents=True)
    assert extension_token(secrets) is None
    token_file = secrets / "playwright_extension_token"
    token_file.write_text('PLAYWRIGHT_MCP_EXTENSION_TOKEN="tok-123"\n')
    assert extension_token(secrets) == "tok-123"
    (secrets / "playwright_extension_token").write_text("tok-123\n")
    assert extension_token(secrets) == "tok-123"
    cfg = mcp_config("0.0.83", tmp_path, tmp_path, browser="extension", token="tok-123")
    assert cfg["mcpServers"]["playwright"]["env"] == {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok-123"}
    assert "env" not in mcp_config("0.0.83", tmp_path, tmp_path, token="tok-123")[
        "mcpServers"]["playwright"]
    s = Settings.model_validate({"sync": {"site_browser": {"costco": "extension"}}})
    paths = DataPaths(tmp_path / "data")
    plan = launcher.plan_session(paths, s, load_cards().site("costco"))
    launcher.prepare(plan, "0.0.83")
    assert "tok-123" in plan.mcp_json.read_text()
    assert stat.S_IMODE(plan.mcp_json.stat().st_mode) == 0o600
    chase = launcher.plan_session(paths, s, load_cards().site("chase"))
    launcher.prepare(chase, "0.0.83")
    assert "tok-123" not in chase.mcp_json.read_text()
