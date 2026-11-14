# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The privacy hooks (SPEC §7): privacy_scan.py, the git hooks around it, the Claude guard."""

import importlib.util
import itertools
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from fin.db import models as m
from fin.db import upgrade

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "scripts" / "hooks"


def load(name):
    spec = importlib.util.spec_from_file_location(f"t_{name}", HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = load("privacy_scan")
guard = load("claude_bash_guard")

# A made-up "real" setup: the scanner must treat all of this as the user's own data.
CARDS = {
    "schema_version": 1,
    "people": [
        {"id": "self", "display_name": "Me", "full_name": "Jordan Realname",
         "relationship": "self", "name_aliases": []},
        {"id": "casey", "display_name": "Casey", "full_name": "Casey Hope Realname",
         "relationship": "spouse", "name_aliases": ["CASEY H MAIDENNAME"]},
        {"id": "dad", "display_name": "Dad", "full_name": "Morgan Realname",
         "relationship": "father", "name_aliases": []},
    ],
    "issuers": [{"id": "bank", "name": "Harborview Bank",
                 "start_url": "https://online.harborviewbank.example/",
                 "allowed_domains": ["harborviewbank.example"]}],
    "item_sources": [],
    "cards": [{"id": "visa", "cardholders": [{"person": "self", "card_ending": "4321"},
                                             {"person": "casey", "card_ending": "98765"}]}],
    "bank_accounts": [{"id": "checking", "site": "bank", "product": "Harborview Rewards Checking",
                       "account_ending": "7788"}],
    "private_words": ["Maplewood Lane"],
}
CLUSTER = [1137, 2291, 3418, 4723, 5862, 6149, 7375, 8406, 9134, 10257]
TITLE = "Stoneware Pour Over Coffee Dripper Ceramic Cone with Stand, Matte Black"
DEFAULTS = {
    "transactions": {"card_id": "visa", "post_date": "2026-09-01", "description_clean": "x",
                     "type": "purchase", "source": "csv", "import_id": 1,
                     "review_status": "ok"},
    "statements": {"card_id": "visa", "period_start": "2026-08-22", "period_end": "2026-09-21",
                   "pdf_path": "x", "parse_method": "regex"},
    "statement_lines": {"statement_id": 1, "line_no": 1, "section": "PURCHASE",
                        "txn_date": "2026-09-01", "description": "x", "amount_cents": 0},
    "orders": {"merchant": "amazon", "channel": "online", "raw_path": "x"},
    "order_items": {"order_id": 1, "line_no": 1},
    "order_charges": {"merchant": "amazon", "kind": "charge"},
}
_n = itertools.count()


def insert(con, table, **values):
    """A row with whatever NOT NULL columns the real schema needs, filled with filler."""
    row = {**DEFAULTS.get(table, {}), **values}
    for _, name, typ, notnull, default, pk in con.execute(f'PRAGMA table_info("{table}")'):
        if notnull and default is None and not pk and name not in row:
            row[name] = 0 if typ.upper() in ("INTEGER", "BOOLEAN") else f"filler-{next(_n)}"
    con.execute(f'INSERT INTO "{table}" ({", ".join(row)}) VALUES ({", ".join("?" * len(row))})',
                list(row.values()))


@pytest.fixture
def ledger(data_dir):
    """The real schema (fin's migrations), holding a few rows of made-up 'real' data."""
    db = data_dir / "db" / "finance.sqlite"
    db.parent.mkdir(parents=True)
    upgrade(db)
    con = sqlite3.connect(db)
    insert(con, "transactions", amount_cents=5871, txn_date="2026-09-14", post_date="2026-09-15",
           description_raw="AMAZON MKTP US*7RT2K9QX1 Amzn.com/bill WA",
           order_ref="112-5550001-7654321", card_ending="4321")
    insert(con, "transactions", amount_cents=2817, txn_date="2026-09-03",
           post_date="2026-09-03", description_raw="SHELL OIL 57444012 OGDENVILLE IL")
    insert(con, "transactions", amount_cents=-123456, txn_date="2026-09-20",
           post_date="2026-09-21", description_raw="BEST BUY #1123 OGDENVILLE IL")
    for c in CLUSTER:
        insert(con, "transactions", amount_cents=c, description_raw="MISC")
    insert(con, "statements", previous_balance=48733, payments=-48733, purchases=51290,
           new_balance=51290, rewards_json=json.dumps({"kind": "points", "earned": 6875,
                                                       "lines": []}))
    insert(con, "statement_lines", amount_cents=5871, txn_date="2026-09-14",
           description="AMAZON MKTP US*7RT2K9QX1", cardholder_name="MORGAN T REALNAME")
    insert(con, "orders", external_id="113-7777777-1234567", order_date="2026-09-14",
           total=5871, subtotal=5571, tax=300, location="OGDENVILLE #375")
    insert(con, "order_items", title_raw=TITLE, line_total_cents=5571)
    insert(con, "order_charges", amount_cents=5871, charge_date="2026-09-14",
           order_ref="113-7777777-1234567", raw_text="Visa ****4321 -$58.71")
    con.commit()
    con.close()
    return db


@pytest.fixture
def setup_repo(tmp_path):
    """A repo dir whose config/cards.json is 'real'."""
    r = tmp_path / "repo"
    (r / "config").mkdir(parents=True)
    (r / "config" / "cards.json").write_text(json.dumps(CARDS))
    return r


@pytest.fixture
def terms(setup_repo, ledger, data_dir):
    t = scan.load_terms(setup_repo, data_dir)
    assert t.problem is None and t.real
    return t


def kinds(text, t):
    return {f.kind.split(" (")[0] for f in scan.scan_unit(scan.numbered(text), t, str)}


# --------------------------------------------------------------------- terms


def test_terms_from_cards_and_ledger(terms):
    names = {n.lower() for n in terms.names}
    assert {"casey hope realname", "casey h maidenname", "casey", "morgan realname",
            "morgan", "morgan t realname"} <= names
    # You (the copyright line names you), a middle name alone, and generic labels are not.
    assert not names & {"jordan", "realname", "jordan realname", "hope", "dad", "self", "me"}
    assert {"Maplewood Lane", "Harborview Bank", "harborviewbank.example",
            "online.harborviewbank.example", "Harborview Rewards Checking"} <= set(terms.words)
    assert terms.endings == {"4321", "98765", "7788"}
    assert scan.KINDS["order"] in {scan.KINDS[k] for k in terms.ids.values()}


def test_ledger_columns_exist_in_the_models():
    tables = m.Base.metadata.tables
    for table, cols in scan.LEDGER.items():
        assert table in tables, table
        assert set(cols) <= set(tables[table].columns.keys()), table


def test_ledger_opened_read_only(setup_repo, ledger, data_dir):
    before = (ledger.read_bytes(), sorted(p.name for p in ledger.parent.iterdir()))
    scan.load_terms(setup_repo, data_dir)
    assert (ledger.read_bytes(), sorted(p.name for p in ledger.parent.iterdir())) == before


def test_real_cards_without_ledger_fails_closed(setup_repo, data_dir):
    t = scan.load_terms(setup_repo, data_dir)
    assert t.real and "no ledger" in t.problem


def test_example_cards_hold_nothing_of_yours(setup_repo, data_dir):
    (setup_repo / "config" / "cards.example.json").write_text(json.dumps(CARDS))
    t = scan.load_terms(setup_repo, data_dir)
    assert not t.real and t.problem is None and not t.names and not t.words


# ------------------------------------------------------------------ matching


@pytest.mark.parametrize(("line", "kind"), [
    ("Paid by Casey Hope  Realname", "name"),
    ("CASEY H MAIDENNAME", "name"),
    ("person_id = 'casey'", "name"),
    ("We bank at harborview bank", "word"),
    ("see https://online.harborviewbank.example/login", "word"),
    ("1 Maplewood-Lane", "word"),
    ("Prime Visa ****4321", "ending"),
    ("checking account ending in 7788", "ending"),
    ("Order #112-5550001-7654321", "order"),
    ("order 11377777771234567", "order"),
    ("AMAZON MKTP US*7RT2K9QX1", "code"),
    ("ref 57444012", "digits"),
    ("BEST BUY #1123 OGDENVILLE IL", "descriptor"),
    ("warehouse: Ogdenville #375", "location"),
    ("total $1,234.56", "large"),
    ('"amount_cents": -123456,', "large"),
    ("Previous balance $487.33", "statement"),
    ("09/03 SHELL OIL 28.17", "dated"),
    ("Sep 3, 2026 Shell 28.17", "dated"),
    ("Points earned this period 6,875", "points"),
    ("4532 0151 1283 " "0366", "card"),  # split: no real-looking number in this file
    ("routing number 0210" "00021", "routing"),
])
def test_flags(terms, line, kind):
    found = scan.scan_unit([(7, line)], terms, lambda ln: f"f.txt:{ln}")
    assert kind in {f.key for f in found}, found
    assert all(f.where == "f.txt:7" for f in found)


@pytest.mark.parametrize("line", [
    "Copyright (C) 2026 Jordan Realname",   # you
    "I hope this works; grace period",      # a middle name on its own
    "card ending 0000 and ****0000",        # placeholders
    "costs $28.17",                         # a lone small amount, no date or merchant
    "09/03 KROGER 28.17",                   # its date, but another merchant
    "$12.00 and $9.17",                     # whole dollars and amounts under $10
    "4111 1111 1111 1111",                  # a well-known test card number
    "routing 123456789",                    # fails the checksum
    "version 1.20.0 and 3.12.15",           # not money
    "Ceramic Cone with Stand",              # part of a title, too short
])
def test_passes(terms, line):
    assert scan.scan_unit([(1, line)], terms, str) == []


def test_audit_reports_lone_amounts(terms):
    assert scan.scan_unit([(1, "costs $28.17")], terms, str) == []
    terms.audit = True
    assert [f.key for f in scan.scan_unit([(1, "costs $28.17")], terms, str)] == ["alone"]
    # Whole numbers count too in the audit, money word or not; years don't.
    assert [f.key for f in scan.scan_unit([(1, "add(s, 2817, 'k')")], terms, str)] == ["alone"]
    assert scan.scan_unit([(1, "add(s, 2026, 'k')")], terms, str) == []


def test_audit_skips_common_prices(terms):
    # 40 of the ledger's amounts end in .99 between $12 and $51: $24.99 matches by chance.
    terms.amounts.update({d * 100 + 99: scan.Amount() for d in range(12, 52)})
    terms.prepare()
    terms.audit = True
    assert scan.scan_unit([(1, "costs $24.99")], terms, str) == []
    assert [f.key for f in scan.scan_unit([(1, "costs $28.17")], terms, str)] == ["alone"]


def test_codes_next_to_a_money_word_are_not_cents(terms):
    line = "`taxes_government` (9311), `charity` (8398), `dining` (5812, 5813)"
    assert scan.money_values(line) == set()
    assert scan.money_values('"amount_cents": 2817,') == {2817}


def test_title_across_lines(terms):
    text = "Delivered\nStoneware Pour Over\nCoffee Dripper Ceramic Cone with Stand, Matte Black\n"
    assert kinds(text, terms) == {"an item title from your orders"}


def test_many_amounts_of_yours_in_one_place(terms):
    mine = "\n".join(f"line {c // 100}.{c % 100:02d}" for c in CLUSTER)
    assert kinds(mine, terms) == {"an amount from your ledger"}
    # Two of yours among thirty made-up amounts is chance.
    made_up = "\n".join(f"x {d}.{(d * 7) % 100 or 1:02d}" for d in range(11, 41))
    mixed = made_up + "\nline 11.37\nline 22.91\n"
    assert kinds(mixed, terms) == set()


def test_output_never_holds_the_value(terms):
    lines = ["Casey Hope Realname", "Order #112-5550001-7654321", "Previous balance $487.33",
             "harborview bank", "AMAZON MKTP US*7RT2K9QX1", "Prime Visa ****4321"]
    out = " ".join(str(f) for f in scan.scan_unit(scan.numbered("\n".join(lines)), terms, str))
    for secret in ("Casey", "Realname", "5550001", "487.33", "harborview", "7RT2K9QX1", "4321"):
        assert secret.lower() not in out.lower()


def test_parse_diff_counts_lines():
    diff = (b"diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -1 +1,3 @@\n-old\n"
            b"+new one\n++++ looks like a header\n+third\n@@ -10 +11,0 @@\n-gone\n"
            b"diff --git a/y z.txt b/y z.txt\n--- /dev/null\n+++ b/y z.txt\n@@ -0,0 +5 @@\n+y\n")
    assert scan.parse_diff(diff) == {
        "x.txt": [(1, "new one"), (2, "+++ looks like a header"), (3, "third")],
        "y z.txt": [(5, "y")]}


def test_strip_message():
    scissors = "# ------------------------ >8 ------------------------"
    msg = f"Subject\n# a comment\nbody\n{scissors}\n+diff"
    assert scan.strip_message(msg) == "Subject\n\nbody"


# --------------------------------------------------------------------- hooks


def git(r, *args, env, check=True):
    return subprocess.run(["git", *args], cwd=r, capture_output=True, text=True, env=env,
                          check=check)


@pytest.fixture
def hook_repo(tmp_path, ledger):
    r = tmp_path / "hooked"
    r.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    git(r, "init", "-q", "-b", "main", env=env)
    shutil.copytree(HOOKS, r / "scripts" / "hooks",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for k, v in (("core.hooksPath", "scripts/hooks"), ("user.email", "t@example.com"),
                 ("user.name", "T")):
        git(r, "config", k, v, env=env)
    (r / "config").mkdir()
    (r / "config" / "cards.json").write_text(json.dumps(CARDS))
    (r / ".gitignore").write_text("config/cards.json\n/private/\n")
    git(r, "add", ".gitignore", "scripts", env=env)
    proc = git(r, "commit", "-q", "-m", "Hooks", env=env, check=False)
    assert proc.returncode == 0, proc.stderr
    return r, env


def commit(repo, files, message="Add files", *extra):
    r, env = repo
    for name, data in files.items():
        p = r / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        git(r, "add", "-f", name, env=env)
    return git(r, "commit", "-q", "-m", message, *extra, env=env, check=False)


def test_precommit_refuses_your_data(hook_repo):
    proc = commit(hook_repo, {"docs/notes.md": "x\nWe bank at Harborview Bank.\n",
                              "tests/fixtures/x/stmt.txt": "09/14 AMAZON MKTP US*7RT2K9QX1 58.71"})
    assert proc.returncode != 0
    assert "docs/notes.md:2: a private word" in proc.stderr
    assert "tests/fixtures/x/stmt.txt:1:" in proc.stderr and "commit refused" in proc.stderr
    assert "Harborview" not in proc.stderr and "58.71" not in proc.stderr


@pytest.mark.parametrize("name", ["private/bank-notes.md", "config/cards.json.bak"])
def test_precommit_blocks_private_paths(hook_repo, name):
    proc = commit(hook_repo, {name: "nothing in here"})
    assert proc.returncode != 0 and "blocked" in proc.stderr


@pytest.mark.parametrize("line", [
    "Checking lives at Harborview Bank.", "start at online.harborviewbank.example",
    "see harborviewbank.example/statements", "the Harborview Rewards Checking account",
])
def test_precommit_refuses_a_bank_site_without_private_words(hook_repo, line):
    """The bank's words come from bank_accounts and its issuer entry, not only private_words."""
    r, _ = hook_repo
    (r / "config" / "cards.json").write_text(json.dumps({**CARDS, "private_words": []}))
    proc = commit(hook_repo, {"docs/notes.md": f"x\n{line}\n"}, "Add notes")
    assert proc.returncode != 0 and "docs/notes.md:2: a private word" in proc.stderr
    assert "harborview" not in proc.stderr.lower()
    proc = commit(hook_repo, {"docs/notes.md": "x\n"}, f"Notes: {line}")
    assert proc.returncode != 0 and "harborview" not in proc.stderr.lower()


def test_make_cards_example_writes_none_of_the_bank():
    make = load_script("make_cards_example")
    real = {**CARDS, "_notes": ["Real config for Jordan at Harborview Bank"],
            "issuers": CARDS["issuers"] + [{"id": "chase", "name": "Chase",
                                             "start_url": "https://www.chase.com/",
                                             "allowed_domains": ["chase.com"]}],
            "cards": [{"id": "visa", "issuer": "chase", "network": "visa", "cardholders": [
                {"person": "self", "card_ending": "4321"},
                {"person": "casey", "card_ending": "98765"}]}],
            "bank_accounts": [{**CARDS["bank_accounts"][0], "account_type": "checking",
                               "statement_markers": ["HARBORVIEW REWARDS CHECKING SUMMARY"],
                               "money_url_words": ["harborpay"]}]}
    text = json.dumps(make.make_example(real)).lower()
    words, endings = scan.site_terms(real)
    names, _ = scan.people_terms(real)
    for w in [*words, *endings, *names, "harborview", "harborpay", "maplewood", "realname"]:
        assert w.lower() not in text, w
    example = json.loads(text)
    assert example["bank_accounts"][0]["product"] == "example bank checking"
    assert {"bank.example"} == {d for s in example["issuers"] for d in s["allowed_domains"]
                                if s["id"] == "bank"}
    # A config with no bank still gets the fictional one, so the example shows the shape.
    assert make.make_example({**real, "bank_accounts": []})["bank_accounts"][0]["id"] == "checking"


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"s_{name}", REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_binary_files_need_a_text_source(hook_repo):
    png = b"\x89PNG\r\n\x1a\n\0\0\0"
    proc = commit(hook_repo, {"tests/fixtures/d/guide.pdf": b"%PDF\0\x01"})
    assert proc.returncode != 0 and "binary file" in proc.stderr
    proc = commit(hook_repo, {"tests/fixtures/d/guide.pdf": b"%PDF\0\x01",
                              "tests/fixtures/d/guide.src.html": "<p>made up</p>",
                              "docs/screenshots/overview.png": png})
    assert proc.returncode == 0, proc.stderr


def test_commit_msg_refuses_your_data(hook_repo):
    proc = commit(hook_repo, {"a.txt": "fine"}, "Split the Best Buy charge for Casey")
    assert proc.returncode != 0 and "commit message, line 1: a name" in proc.stderr
    proc = commit(hook_repo, {"a.txt": "fine"}, "Split charges by person")
    assert proc.returncode == 0, proc.stderr


def test_prepush_checks_every_commit(hook_repo, tmp_path):
    r, env = hook_repo
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote), env=env)
    git(r, "remote", "add", "origin", str(remote), env=env)
    assert git(r, "push", "-q", "origin", "main", env=env, check=False).returncode == 0
    assert commit(hook_repo, {"ok.txt": "fine"}).returncode == 0
    # A commit made without the hooks still can't leave: pre-push checks it.
    assert commit(hook_repo, {"t.py": "assert total == 123456  # cents"}, "Test",
                  "--no-verify").returncode == 0
    proc = git(r, "push", "origin", "main", env=env, check=False)
    assert proc.returncode != 0 and "t.py:1: an amount over $1,000" in proc.stderr
    assert "push refused" in proc.stderr
    pushed = git(remote, "rev-list", "--count", "main", env=env).stdout.strip()
    assert pushed == "1"  # only the first push got through


def test_prepush_checks_a_rewritten_history(hook_repo, tmp_path):
    """After a rewrite nothing is shared with the remote, so every commit is checked."""
    r, env = hook_repo
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote), env=env)
    git(r, "remote", "add", "origin", str(remote), env=env)
    git(r, "push", "-q", "origin", "main", env=env)
    git(r, "checkout", "-q", "--orphan", "new", env=env)
    assert commit(hook_repo, {"d.md": "Previous balance $487.33"}, "Root",
                  "--no-verify").returncode == 0
    assert commit(hook_repo, {"e.md": "fine"}, "Later").returncode == 0
    proc = git(r, "push", "--force", "origin", "new:main", env=env, check=False)
    assert proc.returncode != 0 and "d.md:1: a statement total" in proc.stderr


def test_hooks_fail_closed_without_the_ledger(hook_repo, ledger):
    ledger.unlink()
    proc = commit(hook_repo, {"a.txt": "fine"})
    assert proc.returncode != 0 and "no ledger" in proc.stderr


def test_doctor_git_hooks(hook_repo):
    from fin.doctor import check_git_hooks

    r, _ = hook_repo
    assert check_git_hooks(r).status == "PASS"
    (r / "scripts" / "hooks" / "pre-push").chmod(0o644)
    assert check_git_hooks(r).status == "FAIL"


def test_doctor_privacy(hook_repo, data_dir, monkeypatch):
    from fin import doctor
    from fin.config import DataPaths

    r, env = hook_repo
    monkeypatch.setattr(doctor, "REPO_ROOT", r)
    assert [x.status for x in doctor.check_privacy(DataPaths(data_dir))] == ["PASS"]
    assert commit(hook_repo, {"b.md": "Order 113-7777777-1234567"}, "For Casey",
                  "--no-verify").returncode == 0
    res = {x.name: x for x in doctor.check_privacy(DataPaths(data_dir))}
    assert res["Privacy scan"].status == "FAIL" and "b.md:1" in res["Privacy scan"].detail
    assert res["Commit messages"].status == "WARN"


# --------------------------------------------------------------- Claude guard


@pytest.mark.parametrize(("command", "denied"), [
    ("git commit --no-verify -m x", True),
    ("git commit -n -m x", True),
    ("cd repo && git commit -anm wip", True),
    ("git commit --no-veri -m x", True),
    ("git push --no-verify origin main", True),
    ("git -c core.hooksPath=/dev/null commit -m x", True),
    ("git config --unset core.hookspath", True),
    ("rm scripts/hooks/pre-push", True),
    ("chmod -x scripts/hooks/pre-commit", True),
    ("git send-pack origin", True),
    ('git commit -m "Explain the -n flag"', False),
    ("git commit -am wip", False),
    ("git push -n origin main", False),
    ("git log -n 3", False),
    ("uv run pytest", False),
])
def test_claude_bash_guard(command, denied):
    assert (guard.reason(command) is not None) is denied
    proc = subprocess.run([sys.executable, str(HOOKS / "claude_bash_guard.py")], text=True,
                          capture_output=True, input=json.dumps(
                              {"tool_name": "Bash", "tool_input": {"command": command}}))
    assert proc.returncode == 0
    out = json.loads(proc.stdout) if proc.stdout else {}
    assert (out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny") is denied


def test_claude_settings_run_the_guard():
    hooks = json.loads((REPO / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    assert any(h["matcher"] == "Bash" and "claude_bash_guard.py" in x["command"]
               for h in hooks for x in h["hooks"])
