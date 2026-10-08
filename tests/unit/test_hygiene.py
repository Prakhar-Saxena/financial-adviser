"""Pre-commit guard, gitignore, redaction and playbooks."""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from fin.doctor import check_git_hygiene
from fin.sync import playbooks

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "scripts" / "hooks" / "pre-commit"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "-q"], cwd=r, check=True, env=env)
    (r / "scripts" / "hooks").mkdir(parents=True)
    shutil.copy(HOOK, r / "scripts" / "hooks" / "pre-commit")
    subprocess.run(["git", "config", "core.hooksPath", "scripts/hooks"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=r, check=True)
    return r, env


def commit(repo, files):
    r, env = repo
    for name, data in files.items():
        p = r / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        subprocess.run(["git", "add", "-f", name], cwd=r, check=True, env=env)
    return subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=r, capture_output=True,
                          text=True, env=env)


@pytest.mark.parametrize("name", [
    "config/cards.json", ".env", "db/finance.sqlite", "statement.pdf", "data/activity.CSV",
    "export.qfx", "page.html", "x/browser-profiles/chase/Cookies", "orders.zip",
])
def test_precommit_blocks(repo, name):
    proc = commit(repo, {"ok.py": b"x", name: b"secret"})
    assert proc.returncode != 0 and "blocked" in proc.stderr


def test_precommit_blocks_large_file(repo):
    proc = commit(repo, {"big.json": b"0" * (1024 * 1024 + 1)})
    assert proc.returncode != 0 and "larger than 1 MB" in proc.stderr


def test_precommit_allows_code_and_fixtures(repo):
    proc = commit(repo, {"src/a.py": b"x", "tests/fixtures/chase/activity.csv": b"a,b",
                         "config/cards.example.json": b"{}", "web/index.html": b"<html>"})
    assert proc.returncode == 0, proc.stderr


def test_repo_gitignore_and_check():
    results = {r.name: r for r in check_git_hygiene()}
    assert results["Gitignore rules"].status == "PASS"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "config/cards.json"], cwd=REPO
    ).returncode == 0
    assert ignored
    not_ignored = subprocess.run(
        ["git", "check-ignore", "-q", "tests/fixtures/x/a.csv"], cwd=REPO
    ).returncode != 0
    assert not_ignored


# ------------------------------------------------------------------ redact


CARDS = {
    "people": [
        {"relationship": "self", "full_name": "Jordan Realname", "name_aliases": []},
        {"relationship": "spouse", "full_name": "Casey M Realname",
         "name_aliases": ["CASEY REALNAME", "CASEY M MAIDEN"]},
    ],
    "cards": [{"cardholders": [{"card_ending": "4321"}, {"card_ending": "98765"}]}],
}


def test_redact_text():
    redact = load_script("redact")
    r = redact.Redactor(CARDS, b"salt")
    src = (
        "JORDAN REALNAME\n123 Main Street\nApt 4\nSpringfield, IL 62701\n"
        "Card ending in 4321 and 98765. Account 1234567890123. alex@example.org\n"
        "Order 111-2223334-5556667 and again 111-2223334-5556667\n"
        "STARBUCKS 800-555-1212 SEATTLE WA\n"
        "TRANSACTIONS FOR CASEY M  REALNAME and Casey Realname and CASEY M MAIDEN\n"
        "Jordan alone\n"
    )
    out = r.text(src)
    assert "REALNAME" not in out.upper() and "MAIDEN" not in out and "Jordan" not in out
    assert "ALEX EXAMPLE" in out  # self, upper case kept
    assert "SAM Q EXAMPLE and Sam Q Example and SAM Q EXAMPLE" in out  # spouse, all aliases
    assert "Fake alone" in out
    assert "Main Street" not in out and "Springfield" not in out and "[ADDRESS]" in out
    assert "4321" not in out and "98765" not in out
    assert "1234567890123" not in out and "alex@example.org" not in out
    assert "STARBUCKS" in out and "SEATTLE WA" in out
    ids = [w for w in out.split() if w.startswith("111-")]
    assert len(ids) == 2 and ids[0] == ids[1] and ids[0] != "111-2223334-5556667"


def test_redact_digits_depend_on_salt():
    redact = load_script("redact")
    a = redact.Redactor({}, b"one").text("ref 12345678")
    b = redact.Redactor({}, b"two").text("ref 12345678")
    assert a != b and len(a) == len(b)


def test_redact_html():
    redact = load_script("redact")
    r = redact.Redactor(CARDS, b"s")
    html = ('<html><script>var u="Jordan Realname"</script><body>'
            '<a href="/acct/12345678">Casey Realname</a><p>Visa ****4321</p></body></html>')
    out = redact.redact_html(r, html)
    assert "<script" not in out and "Realname" not in out and "4321" not in out
    assert "12345678" not in out


# ---------------------------------------------------------------- playbooks


def test_playbook_login_hosts_and_diff(tmp_path):
    (tmp_path / "chase.md").write_text(
        "# Chase\n\nStatements are under Documents.\n\n## Login hosts\n"
        "- secure.chase.com\n- `login.chaseauth.example`\n- not a host\n\n## Avoid\n- x.example\n"
    )
    assert playbooks.login_hosts("chase", tmp_path) == [
        "secure.chase.com", "login.chaseauth.example"]
    d = playbooks.diff("chase", "# Chase\n\nNew text.\n", tmp_path)
    assert "-Statements are under Documents." in d and "+New text." in d
    playbooks.accept("amazon", "# Amazon", tmp_path)
    assert (tmp_path / "amazon.md").read_text() == "# Amazon\n"


def test_redact_endings_option_spares_money():
    redact = load_script("redact")
    r = redact.Redactor({}, b"s", {"4321"})
    out = r.text("Chase4321-Activity XXXX XXXX XXXX 4321 card 4321 paid $4321.00 and 4321.50",
                 addresses=False)
    assert out.count("4321") == 2 and "$4321.00" in out and "4321.50" in out
    assert "Chase0000" in out or "Chase4321" not in out


def test_redact_city_zip_mid_line():
    redact = load_script("redact")
    out = redact.Redactor({}, b"s").text("SPRINGFIELD, IL 62701-1234 Total before tax: $19.99")
    assert "SPRINGFIELD" not in out and "62701" not in out and "Total before tax: $19.99" in out


def test_redact_city_zip_without_state():
    redact = load_script("redact")
    out = redact.Redactor({}, b"s").text("Springfield 62701 All Search Amazon EN\nReturns 46")
    assert "Springfield" not in out and "62701" not in out and "Returns 46" in out


def test_redact_ending_with_colon():
    redact = load_script("redact")
    out = redact.Redactor({}, b"s").text("Account number ending in: 5678", addresses=False)
    assert "5678" not in out and out.endswith("0000")
