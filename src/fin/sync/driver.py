"""Driven browsing session: the same session as `fin sync`, run from a script (decided 2026-10-06).

The user lets Claude Code run syncs, but the interactive `claude` UI needs a terminal. The
driver runs the identical session (same tools, guard, MCP config and prompt) in print mode with
streaming JSON input, so messages can be sent one at a time:

- trusted mode only: nobody can answer per-click prompts here (`--permission-mode dontAsk`
  denies anything not allowed). The guard hook still checks every action.
- the user still does every login. If the site isn't signed in, the agent replies
  LOGIN_NEEDED and the driver waits for a "logged in" message.
- control: message files dropped in `<control dir>/driver/inbox/` are sent in name order;
  "/exit" ends the session. Status is in `driver/status` (running | waiting | exited).
- `driver/driver.log` records only the agent's own text and tool names, never tool results
  (page content stays out of logs, §7).
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from fin.sync import launcher

DRIVEN_START = (
    "start\n\n"
    "This session is driven by a script; the user can't type here, but is watching the "
    "Chrome window and can log in there. After opening the start page, take one "
    "browser_snapshot of it to check whether you are already signed in. If you are, "
    "continue with the task right away. If you are not, or the site asks for a password or "
    "code, reply with exactly LOGIN_NEEDED and stop; you'll get 'logged in' once the user "
    "has signed in. At the very end, after writing the three files, reply with exactly "
    "SYNC_DONE."
)


DRIVEN_HOLD = (
    "start\n\n"
    "This session is driven by a script and is in HOLD mode: the user wants to look at the "
    "site themselves first. Open the start page and take one browser_snapshot to check "
    "whether you are signed in. Reply with exactly SIGNED_IN or LOGIN_NEEDED, then stop. Do "
    "not click, navigate or save anything else until you receive a message that says "
    "'continue'. The user may click around in the Chrome window meanwhile; that is expected."
)


def driven_argv(plan: launcher.SessionPlan) -> list[str]:
    if not plan.trusted:
        raise ValueError("a driven session must be trusted (no one can answer prompts)")
    argv = launcher.build_argv(plan)
    return [argv[0], "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--no-session-persistence", *argv[1:]]


class Driver:
    def __init__(self, plan: launcher.SessionPlan, start_message: str = DRIVEN_START):
        self.plan = plan
        self.start_message = start_message
        self.dir = plan.control_dir / "driver"
        self.inbox = self.dir / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        for old in self.inbox.glob("*"):
            old.unlink()
        self.log_path = self.dir / "driver.log"
        self.status_path = self.dir / "status"
        self.proc: subprocess.Popen | None = None

    def _log(self, line: str) -> None:
        with self.log_path.open("a") as f:
            f.write(time.strftime("%H:%M:%S ") + line.rstrip() + "\n")
        self.log_path.chmod(0o600)

    def _status(self, s: str) -> None:
        self.status_path.write_text(s + "\n")

    def _send(self, text: str) -> None:
        msg = {"type": "user", "message": {"role": "user",
                                           "content": [{"type": "text", "text": text}]}}
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        self._log(f"YOU: {text.splitlines()[0][:80]}")
        self._status("running")

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        self._log("AGENT: " + " ".join(block["text"].split())[:2000])
                    elif block.get("type") == "tool_use":
                        self._log(f"TOOL: {block.get('name')}")
            elif ev.get("type") == "result":
                self._log(f"TURN DONE: {ev.get('subtype')} "
                          f"({ev.get('num_turns')} turns, is_error={ev.get('is_error')})")
                self._status("waiting")

    def run(self) -> int:
        self.log_path.write_text("")
        self.proc = subprocess.Popen(
            driven_argv(self.plan), cwd=self.plan.session_cwd,
            env=launcher.session_env(self.plan), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        reader = threading.Thread(target=self._read, daemon=True)
        reader.start()
        self._send(self.start_message)
        while self.proc.poll() is None:
            for f in sorted(self.inbox.glob("*")):
                text = f.read_text().strip()
                f.unlink()
                if text == "/exit":
                    self._log("YOU: /exit")
                    assert self.proc.stdin
                    self.proc.stdin.close()
                    break
                if text:
                    self._send(text)
            time.sleep(1)
        reader.join(timeout=10)
        code = self.proc.wait()
        self._status("exited")
        self._log(f"SESSION EXITED: {code}")
        return code


def send(control_dir: Path, text: str) -> Path:
    inbox = control_dir / "driver" / "inbox"
    path = inbox / f"{time.time_ns()}.msg"
    path.write_text(text)
    return path


def live_session(site_sessions: Path) -> Path | None:
    """Newest control dir whose driver hasn't exited (sessions can run past midnight)."""
    if not site_sessions.exists():
        return None
    for control in sorted(site_sessions.iterdir(), reverse=True):
        status = control / "driver" / "status"
        if status.exists() and status.read_text().strip() != "exited":
            return control
    return None
