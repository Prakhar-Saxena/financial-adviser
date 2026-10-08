"""Guard hook decisions from hook payloads (SPEC §9.1, §18).

write_pre/write_post.json were recorded from a real Claude Code session. Browser payloads
use the same envelope; `fin sync selftest` records real ones under sync-sessions/.../payloads.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import FIXTURES

REPO = Path(__file__).resolve().parents[2]
GUARD_PATH = REPO / "sync" / "guard.py"
spec = importlib.util.spec_from_file_location("guard", GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

PW = "mcp__playwright__"


@pytest.fixture
def env(tmp_path):
    cwd = tmp_path / "inbox" / "chase"
    run = cwd / "2026-10-06"
    run.mkdir(parents=True)
    cfg = {
        "site": "chase", "run_dir": str(run), "session_cwd": str(cwd),
        "allowed_domains": ["chase.com"], "login_hosts": ["secure.chaseauth.example"],
        "log_path": str(run / "guard.log"),
    }
    cfg_path = tmp_path / "guard_config.json"
    cfg_path.write_text(json.dumps(cfg))
    return cfg, cfg_path, cwd, run


def recorded(name, cwd, run):
    text = (FIXTURES / "hook_payloads" / name).read_text()
    text = text.replace("{RUN_DIR}", str(run)).replace("{SESSION_CWD}", str(cwd))
    return json.loads(text)


def pw(tool, cwd, event="PreToolUse", **tool_input):
    return {"hook_event_name": event, "tool_name": PW + tool, "tool_input": tool_input,
            "cwd": str(cwd), "session_id": "s", "permission_mode": "default"}


def decide(payload, cfg):
    fn = guard.post_tool if payload["hook_event_name"] == "PostToolUse" else guard.pre_tool
    return fn(payload, cfg)[0]


# ---------------------------------------------------------------- writes


def test_recorded_write_inside_run_folder_allowed(env):
    cfg, _, cwd, run = env
    assert decide(recorded("write_pre.json", cwd, run), cfg) == "allow"
    assert decide(recorded("write_post.json", cwd, run), cfg) == "allow"


@pytest.mark.parametrize("path", [
    "{cwd}/manifest.json",              # session cwd, not run folder
    "{run}/../2026-10-01/manifest.json",
    "/etc/passwd.json",
    "{run}/notes.txt",                  # wrong type
    "{run}/script.py",
    "{run}/guard.log",
    "{run}/guard_config.json",          # reserved
    "manifest.json",                    # relative: resolves against cwd
])
def test_writes_outside_run_folder_or_wrong_type_denied(env, path):
    cfg, _, cwd, run = env
    p = recorded("write_pre.json", cwd, run)
    p["tool_input"]["file_path"] = path.format(cwd=cwd, run=run)
    assert decide(p, cfg) == "deny"


def test_relative_write_into_run_folder_allowed(env):
    cfg, _, cwd, run = env
    p = recorded("write_pre.json", cwd, run)
    p["tool_input"]["file_path"] = f"{run.name}/run_report.json"
    assert decide(p, cfg) == "allow"


# ------------------------------------------------------------ navigation


@pytest.mark.parametrize("url", [
    "https://www.chase.com/", "https://secure.chase.com/web/auth/dashboard",
    "https://chase.com/x", "https://secure.chaseauth.example/login", "about:blank",
])
def test_navigation_allowed(env, url):
    cfg, _, cwd, _ = env
    assert decide(pw("browser_navigate", cwd, url=url), cfg) == "allow"


@pytest.mark.parametrize("url", [
    "https://www.iana.org/help/example-domains", "https://chase.com.evil.example/",
    "https://evilchase.com/", "file:///etc/passwd", "javascript:alert(1)",
    "data:text/html,hi", "https://amazon.com/",
])
def test_off_site_navigation_denied(env, url):
    cfg, _, cwd, _ = env
    assert decide(pw("browser_navigate", cwd, url=url), cfg) == "deny"


def test_new_tab_url_checked(env):
    cfg, _, cwd, _ = env
    assert decide(pw("browser_tabs", cwd, action="new", url="https://evil.example/"), cfg) == "deny"
    assert decide(pw("browser_tabs", cwd, action="list"), cfg) == "allow"


def test_post_tool_off_site_page_blocked(env):
    cfg, _, cwd, _ = env
    p = pw("browser_click", cwd, event="PostToolUse", element="Statements link", target="e1")
    p["tool_response"] = [{"type": "text", "text": "### Page\n- Page URL: https://phish.example/x"}]
    decision, reason, label = guard.post_tool(p, cfg)
    assert decision == "block" and "navigate_back" in reason and label == "phish.example"
    p["tool_response"] = [{"type": "text", "text": "- Page URL: https://secure.chase.com/a\n"}]
    assert decide(p, cfg) == "allow"


# ------------------------------------------------------------ clicks


@pytest.mark.parametrize("element", [
    "Make a payment button", "Pay now", "Pay bill", "Pay your balance", "Schedule payment",
    "Transfer money", "Set up autopay", "Send money with Zelle", "Buy now", "Place your order",
    "Proceed to checkout", "Add to cart", "Subscribe & Save", "Enroll in paperless",
    "Activate card", "Redeem points", "Apply now", "Add offer", "Add to card",
    "Lock card", "Close account", "Cancel order", "Delete", "Remove item", "Save changes",
    "Account settings", "Pay",
    # Amazon / Chase additions (Phase 1)
    "Buy it again", "Archive order", "Hide order", "Return or replace items", "Start a return",
    "Write a product review", "Leave seller feedback", "Pay card", "Go paperless",
    "Dispute a transaction", "Report lost or stolen card", "Request a credit limit increase",
    # Costco / Citi (Phase 2)
    "Renew membership", "Upgrade to Executive", "Reorder", "Order Again", "Turn on auto-renew",
    # Amex (Phase 3)
    "Set up Pay Over Time", "Plan It", "Send & Split", "Add a card", "Transfer Points",
    "Use Points for Charges",
])
def test_deny_list_clicks(env, element):
    cfg, _, cwd, _ = env
    assert decide(pw("browser_click", cwd, element=element, target="e5"), cfg) == "deny"


@pytest.mark.parametrize("element", [
    "Payment activity tab", "Apply", "Apply filter button", "Download account activity",
    "Statements & documents", "Order details link", "Invoice", "Next page",
    "Date range dropdown", "Paid with Visa ending 0000",
    "View invoice", "Your Orders", "past 3 months filter", "Return to order list",
    "Statements & documents link", "Download account activity icon", "Archived statements tab",
    "Statements & Activity", "Account switcher Gold Card", "Card Member column",
])
def test_allowed_clicks(env, element):
    cfg, _, cwd, _ = env
    assert decide(pw("browser_click", cwd, element=element, target="e5"), cfg) == "allow"


def test_click_without_description_denied(env):
    cfg, _, cwd, _ = env
    assert decide(pw("browser_click", cwd, target="e5"), cfg) == "deny"


def test_hover_not_deny_listed(env):
    cfg, _, cwd, _ = env
    assert decide(pw("browser_hover", cwd, element="Pay now", target="e2"), cfg) == "allow"


# ------------------------------------------------------------ typing


@pytest.mark.parametrize("element", [
    "Username field", "User ID", "Password", "Passcode", "PIN", "One-time code",
    "Verification code", "Email address", "SSN last 4", "Sign in", "Security code",
])
def test_login_typing_denied(env, element):
    cfg, _, cwd, _ = env
    assert decide(pw("browser_type", cwd, element=element, target="e3", text="x"), cfg) == "deny"


def test_search_typing_allowed(env):
    cfg, _, cwd, _ = env
    p = pw("browser_type", cwd, element="Search all orders", target="e3", text="2026")
    assert decide(p, cfg) == "allow"


def test_payment_amount_typing_denied(env):
    cfg, _, cwd, _ = env
    p = pw("browser_type", cwd, element="Payment amount for Pay now", target="e3", text="10")
    assert decide(p, cfg) == "deny"


def test_fill_form_login_denied_date_allowed(env):
    cfg, _, cwd, _ = env
    login = pw("browser_fill_form", cwd, fields=[
        {"name": "Password", "type": "textbox", "target": "e1", "value": "x"}])
    assert decide(login, cfg) == "deny"
    dates = pw("browser_fill_form", cwd, fields=[
        {"name": "From date", "type": "textbox", "target": "e1", "value": "07/01/2026"},
        {"name": "To date", "type": "textbox", "target": "e2", "value": "10/01/2026"}])
    assert decide(dates, cfg) == "allow"


# ------------------------------------------------------- tools and files


@pytest.mark.parametrize("tool", [
    "Bash", "Read", "Edit", "WebFetch", "WebSearch", "Agent",
    PW + "browser_evaluate", PW + "browser_run_code_unsafe", PW + "browser_file_upload",
    PW + "browser_handle_dialog", PW + "browser_network_requests", PW + "browser_drag",
    "mcp__other__tool", "mcp__claude-in-chrome__navigate", "mcp__claude-in-chrome__computer",
    "EndConversation",
])
def test_other_tools_denied(env, tool):
    cfg, _, cwd, _ = env
    p = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}, "cwd": str(cwd)}
    assert decide(p, cfg) == "deny"


def test_pdf_save_filename_must_be_in_run_folder(env):
    cfg, _, cwd, run = env
    assert decide(pw("browser_pdf_save", cwd, filename=str(run / "a.pdf")), cfg) == "allow"
    assert decide(pw("browser_pdf_save", cwd, filename=f"{run.name}/b.pdf"), cfg) == "allow"
    assert decide(pw("browser_pdf_save", cwd), cfg) == "allow"  # auto-named into output dir
    assert decide(pw("browser_pdf_save", cwd, filename="a.pdf"), cfg) == "deny"  # cwd root
    assert decide(pw("browser_pdf_save", cwd, filename="/tmp/a.pdf"), cfg) == "deny"
    assert decide(pw("browser_pdf_save", cwd, filename=str(run / "a.sh")), cfg) == "deny"
    assert decide(pw("browser_snapshot", cwd, filename="/tmp/snap.md"), cfg) == "deny"


# ------------------------------------------------------------- end to end


def run_guard(cfg_path, payload):
    return subprocess.run(
        [sys.executable, str(GUARD_PATH), str(cfg_path)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=10,
    )


def test_subprocess_deny_output_and_log(env):
    cfg, cfg_path, cwd, run = env
    proc = run_guard(cfg_path, pw("browser_navigate", cwd, url="https://www.iana.org/x?acct=123"))
    assert proc.returncode == 0
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["hookEventName"] == "PreToolUse"
    log = [json.loads(line) for line in (run / "guard.log").read_text().splitlines()]
    assert log[-1] == {**log[-1], "decision": "deny", "label": "www.iana.org"}
    assert "acct" not in (run / "guard.log").read_text()  # no URL paths or queries logged


def test_subprocess_allow_prints_nothing(env):
    """Allow must not print a decision, so approve-mode prompts still happen."""
    cfg, cfg_path, cwd, _ = env
    proc = run_guard(cfg_path, pw("browser_click", cwd, element="Statements", target="e1"))
    assert proc.returncode == 0 and proc.stdout == ""


def test_subprocess_post_block(env):
    cfg, cfg_path, cwd, _ = env
    p = pw("browser_navigate_back", cwd, event="PostToolUse")
    p["tool_response"] = {"content": [{"type": "text", "text": "Page URL: https://x.example/"}]}
    out = json.loads(run_guard(cfg_path, p).stdout)
    assert out["decision"] == "block"


def test_fails_closed_on_bad_config(tmp_path):
    bad = tmp_path / "missing.json"
    proc = run_guard(bad, {"tool_name": "Write"})
    assert proc.returncode == 2


def test_no_typed_values_in_log(env):
    cfg, cfg_path, cwd, run = env
    run_guard(cfg_path, pw("browser_type", cwd, element="Search", target="e1", text="SECRET123"))
    assert "SECRET123" not in (run / "guard.log").read_text()


def test_records_payloads_only_when_configured(env, tmp_path):
    cfg, cfg_path, cwd, run = env
    run_guard(cfg_path, pw("browser_snapshot", cwd))
    assert not (tmp_path / "payloads").exists()
    cfg["record_payloads_dir"] = str(tmp_path / "payloads")
    cfg_path.write_text(json.dumps(cfg))
    run_guard(cfg_path, pw("browser_snapshot", cwd))
    assert len(list((tmp_path / "payloads").glob("*.json"))) == 1


def test_generic_download_is_renamed_and_agent_told(env):
    cfg, cfg_path, cwd, run = env
    (run / "activity.csv").write_text("first")
    p = pw("browser_click", cwd, event="PostToolUse", element="Download", target="e1")
    out = json.loads(run_guard(cfg_path, p).stdout)
    assert "activity-1.csv" in out["hookSpecificOutput"]["additionalContext"]
    (run / "activity.csv").write_text("second")
    run_guard(cfg_path, p)
    assert sorted(x.name for x in run.glob("activity*")) == ["activity-1.csv", "activity-2.csv"]
    assert (run / "activity-1.csv").read_text() == "first"
