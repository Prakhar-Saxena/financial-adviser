# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Card optimizer Phase C1: fetch, verify, extract, compose, refresh, CLI and API.

Fictional cards only ("Example Bank" on example.com). HTTP goes through httpx.MockTransport,
Claude through the fake CLI (tests/fake_bin/claude).
"""

import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from fin.cards import compose
from fin.cards.discover import normalize_url
from fin.cards.docs import html_text
from fin.cards.extract import Source, budget
from fin.cards.fetch import Fetcher
from fin.cards.refresh import CardRefresher, refresh
from fin.cards.verify import DocText, normalize, numbers_in
from fin.config import CONFIG_DIR, CardsConfig, data_paths, load_settings, load_taxonomy
from fin.db import models as m
from fin.db import session_factory, upgrade
from fin.db.seed import sync_reference

FIXTURES = Path(__file__).parents[1] / "fixtures" / "cards"
DOCS = FIXTURES / "docs"
OUTPUTS = FIXTURES / "fake_outputs"

PRODUCT = "https://www.example.com/cards/everyday-visa"
TERMS = "https://www.example.com/cards/everyday-visa/rewards-terms"
GUIDE = "https://docs.example.com/benefits/everyday-visa-guide.pdf"


def example_cards() -> CardsConfig:
    d = json.loads((CONFIG_DIR / "cards.example.json").read_text())
    holder = [{"person": "self", "card_ending": "", "role": "primary"}]
    d["issuers"] = [{"id": "example", "name": "Example Bank", "start_url": "https://www.example.com/",
                     "allowed_domains": ["example.com"]}]
    d["cards"] = [
        {"id": "example_everyday", "issuer": "example", "product": "Example Bank Everyday Visa",
         "network": "visa", "card_type": "credit", "cardholders": holder},
        {"id": "example_travel", "issuer": "example", "product": "Example Bank Travel Card",
         "network": "visa", "card_type": "credit", "cardholders": holder},
    ]
    return CardsConfig.model_validate(d)


def example_settings(**cards_kw):
    s = load_settings()
    cs = s.cards.model_copy(update={
        "product_aliases": {"example_everyday": ["Everyday Visa"],
                            "example_travel": ["Travel Card"]},
        "doc_domains": {"common": []}, **cards_kw})
    return s.model_copy(update={"cards": cs})


class Web:
    """A fake web for httpx.MockTransport: url -> (status, headers, body)."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, dict, bytes]] = {
            "https://www.example.com/robots.txt": (200, {}, b"User-agent: *\nDisallow: /private/"),
            PRODUCT: (200, {"content-type": "text/html", "etag": '"p1"'},
                      (DOCS / "everyday_product.html").read_bytes()),
            TERMS: (200, {"content-type": "text/html"},
                    (DOCS / "everyday_rewards_terms.html").read_bytes()),
            GUIDE: (200, {"content-type": "application/pdf"},
                    (DOCS / "everyday_benefits_guide.pdf").read_bytes()),
        }
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        status, headers, body = self.routes.get(url, (404, {}, b"not found"))
        etag = headers.get("etag")
        if status == 200 and etag and request.headers.get("if-none-match") == etag:
            return httpx.Response(304)
        return httpx.Response(status, headers=headers, content=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def env(data_dir, fake_claude, tmp_path, monkeypatch):
    settings, cards = example_settings(), example_cards()
    paths = data_paths(settings)
    for d in paths.all_dirs:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    upgrade(paths.db_file)
    S = session_factory(paths.db_file)
    with S() as s:
        sync_reference(s, cards, load_taxonomy())
        s.commit()
    outputs = tmp_path / "outputs"
    shutil.copytree(OUTPUTS, outputs)
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUTS", str(outputs))
    web = Web()

    class Env:
        pass

    e = Env()
    e.settings, e.cards, e.paths, e.S, e.web, e.log, e.outputs = (
        settings, cards, paths, S, web, fake_claude, outputs)

    def run(card="example_everyday", **kw):
        with S() as s:
            return refresh(s, paths, kw.pop("settings", settings), cards,
                           card_ids=[card] if card else None, transport=web.transport,
                           sleep=lambda _: None, **kw)

    e.run = run
    e.calls = lambda: [json.loads(x) for x in fake_claude.read_text().splitlines()
                       if '"auth", "status"' not in x] if fake_claude.exists() else []
    return e


def tasks_called(e) -> list[str]:
    out = []
    for c in e.calls():
        argv = c["argv"]
        if "--system-prompt-file" in argv:
            out.append(Path(argv[argv.index("--system-prompt-file") + 1]).stem)
    return out


def next_day(e) -> None:
    """Pretend a day passed, so documents may be fetched again."""
    with e.S() as s:
        for d in s.scalars(select(m.CardDocument)):
            if d.last_checked_at:
                d.last_checked_at = "2026-01-01T00:00:00Z"
        s.commit()


def edit_output(e, task: str, fn) -> None:
    p = e.outputs / f"{task}.json"
    out = json.loads(p.read_text())
    fn(out)
    p.write_text(json.dumps(out))


def open_items(e, kind: str) -> list[dict]:
    with e.S() as s:
        return [json.loads(i.details_json) for i in s.scalars(select(m.ReviewItem).where(
            m.ReviewItem.kind == kind, m.ReviewItem.status == "open"))]


# ------------------------------------------------------------------ pure functions


def test_normalize_url():
    assert normalize_url("http://WWW.Example.com/a?utm_source=x&id=3&gclid=9#frag") == \
        "https://www.example.com/a?id=3"
    assert normalize_url("https://www.example.com") == "https://www.example.com/"
    assert normalize_url("ftp://example.com/x") is None
    assert normalize_url("not a url") is None


def test_quote_normalization():
    text = "[[page 1]]\nEarn 4X points — it’s the ﬁrst card with no fees. " \
           "Purchases at super-\nmarkets count. Visa® co-\nbrand.\n[[page 2]]\nOther text."
    doc = DocText.parse(text)
    assert doc.contains("Earn 4X points - it's the first card with no fees.", 1)
    assert doc.contains("purchases at supermarkets count", 1)
    assert doc.contains("Visa co-brand", 1)  # a real hyphen at a line end survives too
    assert doc.contains("other text", 1)  # the next page counts
    assert not doc.contains("earn 5x points", 1)
    assert normalize("A–B  \n c") == "a-b c"


def test_quote_page_window():
    doc = DocText.parse("[[page 1]]\nalpha\n[[page 2]]\nbeta\n[[page 3]]\ngamma\n[[page 4]]\ndelta")
    assert doc.contains("alpha", 2) and doc.contains("gamma", 2)
    assert not doc.contains("delta", 2)
    assert doc.contains("delta", None)  # no page: the whole document


def test_numbers_in_quotes():
    assert {4.0, 25000.0, 1.0} <= numbers_in("Earn 4X points on up to $25,000, then 1x.")
    assert 1.5 in numbers_in("1.5% cash back")
    assert 4.0 in numbers_in("four points per dollar")
    assert 0.0 in numbers_in("No foreign transaction fees")
    assert 10.0 in numbers_in("$10 monthly")


def test_html_text_drops_scripts_and_banners():
    text, sections = html_text((DOCS / "everyday_product.html").read_bytes())
    assert "tracking code" not in text and "cookies" not in text and "Sign in" not in text
    assert "[[section 2]]\nExample Bank Everyday Visa" in text
    assert "Annual fee: $95 | Charged on your first statement" in text
    assert sections == 4


def test_budget_sends_whole_pages_with_keywords():
    pages = [f"[[page {i}]]\n" + ("filler text " * 40) for i in range(1, 6)]
    pages[3] = "[[page 4]]\nEarn 4X points at restaurants. " + "annual fee " * 30
    text = "\n".join(pages)
    src = Source("D1", None, None, text)
    texts, sent = budget([src], "card_terms", max_chars=len(pages[0]) + len(pages[3]) + 50)
    assert sent == {"D1": [1, 4]}
    assert texts["D1"].count("[[page") == 2 and "Earn 4X" in texts["D1"]
    texts, sent = budget([src], "card_terms", max_chars=10**6)
    assert sent == {} and texts["D1"] == text


# ------------------------------------------------------------------ fetch outcomes


def fetch_one(e, url: str, doc_type="product_page"):
    with e.S() as s:
        d = m.CardDocument(card_id="example_everyday", url=url, doc_type=doc_type)
        s.add(d)
        s.flush()
        w = CardRefresher(s, e.paths, e.settings, e.cards,
                          Fetcher(e.settings.cards, e.web.transport, sleep=lambda _: None))
        outcome = w.fetch_one(e.cards.card("example_everyday"), d, ["example.com"])
        s.commit()
        n_ok = len(s.scalars(select(m.CardDocVersion).where(
            m.CardDocVersion.status == "ok")).all())
        return outcome, d.status, d.note, n_ok


@pytest.mark.parametrize("route,expected_status", [
    ((301, {"location": "https://evil.example.org/x"}, b""), "needs_domain"),
    ((200, {"content-type": "application/pdf", "content-length": str(30 << 20)}, b"%PDF-"),
     "broken"),
    ((403, {}, b"Forbidden"), "blocked"),
    ((503, {}, b"<html>Just a moment... cf-chl</html>"), "blocked"),
    ((404, {}, b""), "broken"),
    ((200, {"content-type": "image/png"}, b"\x89PNG"), "broken"),
    ((200, {"content-type": "text/html"}, (DOCS / "thin_js_page.html").read_bytes()),
     "needs_browser"),
])
def test_fetch_failures_store_nothing(env, route, expected_status):
    url = "https://www.example.com/cards/x"
    env.web.routes[url] = route
    outcome, status, note, n_ok = fetch_one(env, url)
    assert status == expected_status, (outcome, note)
    assert n_ok == 0
    assert not any((env.paths.card_docs / "files").rglob("*.*"))


def test_fetch_robots_disallow(env):
    url = "https://www.example.com/private/terms"
    env.web.routes[url] = (200, {"content-type": "text/html"},
                           (DOCS / "everyday_product.html").read_bytes())
    _, status, note, n_ok = fetch_one(env, url)
    assert status == "blocked" and "robots" in note and n_ok == 0
    assert not any(r.url.path == "/private/terms" for r in env.web.requests)


def test_fetch_retries_then_broken(env):
    url = "https://www.example.com/cards/flaky"
    env.web.routes[url] = (500, {}, b"")
    for expected in ("active", "active", "broken"):
        with env.S() as s:
            d = s.scalars(select(m.CardDocument).where(m.CardDocument.url == url)).first()
            if d is None:
                d = m.CardDocument(card_id="example_everyday", url=url, doc_type="product_page")
                s.add(d)
                s.flush()
            w = CardRefresher(s, env.paths, env.settings, env.cards,
                              Fetcher(env.settings.cards, env.web.transport, sleep=lambda _: None))
            w.fetch_one(env.cards.card("example_everyday"), d, ["example.com"])
            s.commit()
            assert d.status == expected


def test_fetch_honest_agent_no_cookies_and_pacing(env):
    env.web.routes[PRODUCT] = (200, {"content-type": "text/html", "set-cookie": "s=1"},
                               (DOCS / "everyday_product.html").read_bytes())
    slept = []
    f = Fetcher(env.settings.cards, env.web.transport, sleep=slept.append)
    assert f.fetch(PRODUCT, ["example.com"]).outcome == "ok"
    assert f.fetch(TERMS, ["example.com"]).outcome == "ok"
    assert all(r.headers["user-agent"] == env.settings.cards.user_agent for r in env.web.requests)
    assert all("cookie" not in r.headers for r in env.web.requests)
    assert slept and all(0 < x <= env.settings.cards.min_request_interval_s for x in slept)


def test_fetch_conditional_304(env):
    f = Fetcher(env.settings.cards, env.web.transport, sleep=lambda _: None)
    assert f.fetch(PRODUCT, ["example.com"], etag='"p1"').outcome == "not_modified"


# ------------------------------------------------------------------ full refresh


def test_first_refresh_builds_a_pending_profile(env):
    res = env.run()
    assert res.status == "ok", res.reports[0].error
    rep = res.reports[0]
    assert rep.outcome == "pending" and rep.version == 1
    assert tasks_called(env) == ["card_discover", "card_terms", "card_benefits"]
    with env.S() as s:
        docs = {d.url: d for d in s.scalars(select(m.CardDocument))}
        assert docs[PRODUCT].status == docs[TERMS].status == docs[GUIDE].status == "active"
        assert docs["https://reviews.example.org/everyday-visa"].status == "needs_domain"
        assert len(docs) == 4  # the utm duplicate was merged
        p = compose.load(compose.current(s, "example_everyday", "pending"))
        assert p.annual_fee_cents.value == 9500 and p.foreign_txn_fee_pct.value == 0
        assert len(p.earn_rules) == 3 and p.earn_rules[0].is_base
        supermarkets = next(r for r in p.earn_rules if "supermarkets" in r.categories)
        assert supermarkets.cap.amount_cents == 2_500_000 and supermarkets.geography == "us"
        assert all(ev.doc_version_id for r in p.earn_rules for ev in r.evidence)
        assert p.credits[0].merchants == ["example_eats"]
        assert {b.kind for b in p.benefits} == {"extended_warranty", "rental_car_cdw"}
        assert s.get(m.MerchantProfileRow, "example_eats").source == "kb"
    # Discovery's off-list URL waits for me, naming the domain.
    problems = open_items(env, "card_source_problem")
    assert [x["domain"] for x in problems] == ["reviews.example.org"]
    assert open_items(env, "card_terms_pending")[0]["first"] is True
    # Every stored file is private.
    for f in env.paths.card_docs.rglob("*"):
        assert (f.stat().st_mode & 0o777) in (0o600, 0o700)


def test_second_refresh_costs_nothing(env):
    env.run()
    n = len(env.calls())
    res = env.run()  # straight after: no fetch (once a day), no AI
    assert len(env.calls()) == n and res.reports[0].outcome == "unchanged"
    next_day(env)
    requests = len(env.web.requests)
    res = env.run()  # a day later: fetched again, text unchanged, still no AI
    assert len(env.web.requests) > requests
    assert len(env.calls()) == n
    assert res.reports[0].docs_checked == 3 and res.reports[0].docs_changed == 0
    with env.S() as s:
        assert [v.version for v in s.scalars(select(m.CardProfileVersion))] == [1]


def test_changed_document_reruns_one_task_and_shows_the_diff(env):
    env.run()
    with env.S() as s:
        compose.accept(s, "example_everyday")
        s.commit()
    body = (DOCS / "everyday_rewards_terms.html").read_text().replace(
        "up to $10 in statement credits", "up to $15 in statement credits")
    env.web.routes[TERMS] = (200, {"content-type": "text/html"}, body.encode())

    def fifteen(out):
        out["credits"][0]["amount_usd"] = 15
        q = out["credits"][0]["evidence"][0]
        q["quote"] = q["quote"].replace("$10", "$15")

    edit_output(env, "card_terms", fifteen)
    before = tasks_called(env)
    next_day(env)
    res = env.run()
    assert tasks_called(env)[len(before):] == ["card_terms"]
    assert res.reports[0].outcome == "pending" and res.reports[0].docs_changed == 1
    with env.S() as s:
        pending = compose.current(s, "example_everyday", "pending")
        d = json.loads(pending.diff_json)
        assert d["material"] and d["changed"][0]["fields"]["amount_cents"] == [1000, 1500]
        assert compose.current(s, "example_everyday", "active").version == 1


def test_removed_rule_pending_then_reject_proposes_nothing(env):
    env.run()
    with env.S() as s:
        compose.accept(s, "example_everyday")
        s.commit()
    edit_output(env, "card_terms", lambda out: out["earn_rules"].pop(1))  # drop 3X dining
    res = env.run(force=True)
    assert res.reports[0].outcome == "pending"
    with env.S() as s:
        d = json.loads(compose.current(s, "example_everyday", "pending").diff_json)
        assert [x["item"] for x in d["removed"]] == ["3x dining"]
        compose.reject(s, "example_everyday")
        s.commit()
    res = env.run(force=True)
    assert res.reports[0].outcome == "rejected_before"
    with env.S() as s:
        assert compose.current(s, "example_everyday", "pending") is None
        assert compose.current(s, "example_everyday", "active").version == 1
    assert not open_items(env, "card_terms_pending")


def test_wording_change_publishes_without_approval(env):
    env.run()
    with env.S() as s:
        compose.accept(s, "example_everyday")
        s.commit()
    edit_output(env, "card_terms", lambda out: out["notes"].append("Points don't expire."))
    res = env.run(force=True)
    assert res.reports[0].outcome == "published" and res.reports[0].version == 2


@pytest.mark.parametrize("mode", ["bad_quote", "bad_number"])
def test_unverified_items_are_dropped_and_reported(env, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    res = env.run()
    assert res.reports[0].outcome == "pending"
    with env.S() as s:
        p = compose.load(compose.current(s, "example_everyday", "pending"))
        assert not any("supermarkets" in r.categories for r in p.earn_rules)
        assert p.benefits[0].kind == "rental_car_cdw"  # the first benefit was dropped too
        ex = s.scalars(select(m.CardExtraction).where(
            m.CardExtraction.task == "card_terms")).one()
        assert ex.status == "partial" and ex.n_rejected == 1
    items = open_items(env, "card_terms_unverified")
    assert {i["task"] for i in items} == {"card_terms", "card_benefits"}
    reason = items[0]["problems"][0]["reasons"][0]
    assert ("quote not found" in reason) if mode == "bad_quote" else ("in none of" in reason)


def test_usage_limit_stops_and_next_run_resumes(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    res = env.run(card=None)
    assert res.status == "limited" and "subscription limit" in res.message
    assert [r.outcome for r in res.reports] == ["limited"]  # stopped at the first card
    with env.S() as s:
        assert s.get(m.CardRefreshRun, res.run_id).status == "limited"
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    res = env.run(if_due=True)
    assert res.reports[0].outcome == "pending"


def test_missing_benefits_guide_is_a_source_problem(env, monkeypatch):
    travel = "https://www.example.com/cards/travel"
    env.web.routes[travel] = (200, {"content-type": "text/html"},
                              (DOCS / "travel_product.html").read_bytes())
    edit_output(env, "card_discover", lambda out: out.update(documents=[
        {"url": travel, "doc_type": "product_page", "title": "Travel Card", "why": "",
         "confidence": 0.9}], not_found=["benefits_guide"]))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "bad_quote")  # the Everyday output won't verify
    env.run(card="example_travel")
    keys = {x["key"] for x in open_items(env, "card_source_problem")}
    assert "missing:benefits_guide" in keys
    # I save the guide by hand: the next run picks it up from the drop zone.
    manual = env.paths.card_docs / "manual" / "example_travel"
    manual.mkdir(parents=True)
    (manual / "travel_benefits_guide.html").write_bytes(
        (DOCS / "travel_product.html").read_bytes())
    env.run(card="example_travel")
    keys = {x["key"] for x in open_items(env, "card_source_problem")}
    assert "missing:benefits_guide" not in keys
    with env.S() as s:
        d = s.scalars(select(m.CardDocument).where(m.CardDocument.origin == "manual_file")).one()
        assert d.doc_type == "benefits_guide" and d.status == "active"


def test_needs_domain_document_activates_when_allowlisted(env):
    env.run()
    review = "https://reviews.example.org/everyday-visa"
    env.web.routes["https://reviews.example.org/robots.txt"] = (404, {}, b"")
    env.web.routes[review] = (200, {"content-type": "text/html"},
                              (DOCS / "everyday_product.html").read_bytes())
    s2 = example_settings(doc_domains={"common": [], "example_everyday": ["example.org"]})
    env.run(settings=s2)
    with env.S() as s:
        assert s.scalars(select(m.CardDocument).where(
            m.CardDocument.url == review)).one().status == "active"
    assert not open_items(env, "card_source_problem")


def test_one_refresh_at_a_time(env):
    from fin.cards.refresh import RefreshBusyError, refresh_lock

    with refresh_lock(env.paths), pytest.raises(RefreshBusyError):
        env.run()


def test_stale_running_rows_are_aborted(env):
    from fin.cards.refresh import abort_stale_runs

    with env.S() as s:
        s.add(m.CardRefreshRun(trigger="cli", started_at="2026-01-01T00:00:00Z"))
        s.commit()
        assert abort_stale_runs(s) == 1


# ------------------------------------------------------------------ CLI


def test_cli_refresh_accept_sources_add_source(env, monkeypatch):
    from fin.cards import cli as cards_cli
    from fin.cli import app

    monkeypatch.setattr(cards_cli, "load_cards", lambda: env.cards)
    monkeypatch.setattr(cards_cli, "load_settings", lambda: env.settings)
    env.run()
    r = CliRunner()
    out = r.invoke(app, ["cards", "status"]).output
    assert "example_everyday" in out and "v1" in out and "Pending" in out
    out = r.invoke(app, ["cards", "accept", "example_everyday"], input="y\n").output
    assert "earn 4x: supermarkets, up to $25,000/calendar_year then 1, us" in out
    assert "v1 is now active" in out
    assert "nothing pending" in r.invoke(app, ["cards", "reject", "example_everyday"]).output
    out = r.invoke(app, ["cards", "sources", "--card", "example_everyday"]).output
    assert "needs_domain" in out and "not on this card's allowlist" in out
    bad = r.invoke(app, ["cards", "add-source", "example_everyday",
                         "https://other.example.net/x", "--type", "product_page"])
    assert bad.exit_code == 2 and "isn't on this card's allowlist" in bad.output
    ok = r.invoke(app, ["cards", "add-source", "example_everyday",
                        "https://www.example.com/cards/everyday-visa/pricing?utm_medium=x",
                        "--type", "pricing_terms"])
    assert ok.exit_code == 0
    with env.S() as s:
        assert s.scalars(select(m.CardDocument).where(
            m.CardDocument.origin == "user_url")).one().url.endswith("/pricing")


def test_cli_add_doc(env, monkeypatch):
    from fin.cards import cli as cards_cli
    from fin.cli import app

    monkeypatch.setattr(cards_cli, "load_cards", lambda: env.cards)
    monkeypatch.setattr(cards_cli, "load_settings", lambda: env.settings)
    out = CliRunner().invoke(app, ["cards", "add-doc", "example_everyday",
                                   str(DOCS / "everyday_benefits_guide.pdf"),
                                   "--type", "benefits_guide"]).output
    assert "changed" in out
    with env.S() as s:
        v = s.scalars(select(m.CardDocVersion)).one()
        assert v.status == "ok" and v.n_pages == 4 and Path(v.file_path).exists()


# ------------------------------------------------------------------ API


@pytest.fixture
def client(env, monkeypatch):
    from fin.api import app as api_app

    monkeypatch.setattr(api_app, "load_cards", lambda: env.cards)
    monkeypatch.setattr(api_app, "load_settings", lambda: env.settings)
    return TestClient(api_app.create_app(env.paths.db_file, web_dist=None),
                      base_url="http://127.0.0.1")


def test_api_profiles_documents_and_decisions(env, client):
    env.run()
    n = len(env.calls())
    rows = client.get("/api/cards/profiles").json()
    assert rows[0]["card_id"] == "example_everyday" and rows[0]["pending_version"] == 1
    detail = client.get("/api/cards/profiles/example_everyday").json()
    assert detail["pending"]["first"] and detail["active"] is None
    assert {d["status"] for d in detail["documents"]} == {"active", "needs_domain"}
    assert {i["kind"] for i in detail["review"]} == {"card_terms_pending", "card_source_problem"}
    assert client.get("/api/cards/profiles/nope").status_code == 404

    by_type = {d["doc_type"]: d for d in detail["documents"] if d["version_id"]}
    pdf = client.get(f"/api/cards/documents/{by_type['benefits_guide']['version_id']}")
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["x-content-type-options"] == "nosniff"
    html = client.get(f"/api/cards/documents/{by_type['product_page']['version_id']}")
    assert html.headers["content-type"].startswith("text/plain")
    assert "[[section 2]]" in html.text and "<script" not in html.text
    assert len(env.calls()) == n  # no GET calls Claude

    url = "/api/cards/profiles/example_everyday/accept"
    assert client.post(url, content='{"version": 1}').status_code == 415
    assert client.post(url, json={"version": 9}).status_code == 409
    assert client.post(url, json={"version": 1}).json()["status"] == "active"
    assert client.post("/api/cards/refresh", data={"card_id": "x"}).status_code == 415


def test_api_document_refuses_paths_outside_card_docs(env, client, tmp_path):
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF-1.4 secret")
    with env.S() as s:
        d = m.CardDocument(card_id="example_everyday", url="https://www.example.com/x",
                           doc_type="other")
        s.add(d)
        s.flush()
        v = m.CardDocVersion(document_id=d.id, status="ok", content_type="application/pdf",
                             file_path=str(env.paths.card_docs / "files" / ".." / ".." /
                                           ".." / "secret.pdf"))
        s.add(v)
        s.commit()
        vid = v.id
    assert client.get(f"/api/cards/documents/{vid}").status_code == 404
    assert client.get("/api/cards/documents/999").status_code == 404


def test_api_refresh_runs_in_background(env, client, monkeypatch):
    import fin.cards.refresh as refresh_mod

    seen = {}

    def fake_refresh(s, paths, settings, cards, **kw):
        seen.update(kw)
        kw["run"].status = "ok"
        s.commit()

    monkeypatch.setattr(refresh_mod, "refresh", fake_refresh)
    r = client.post("/api/cards/refresh", json={"card_id": "example_everyday"})
    run_id = r.json()["run_id"]
    import time

    for _ in range(50):
        if client.get(f"/api/cards/refresh/{run_id}").json()["status"] != "running":
            break
        time.sleep(0.05)
    assert client.get(f"/api/cards/refresh/{run_id}").json()["status"] == "ok"
    assert seen["card_ids"] == ["example_everyday"] and seen["trigger"] == "dashboard"
    assert client.post("/api/cards/refresh", json={"card_id": "nope"}).status_code == 404


# ------------------------------------------------------------------ real-run fixes (C1 review)


def _words(rows):
    """Fake pdfplumber words: (x0, x1) per word."""
    return [{"x0": a, "x1": b} for a, b in rows]


def test_column_gutter():
    from fin.cards.docs import column_gutter

    two_cols = _words([(40, 90), (100, 280)] * 40 + [(330, 380), (390, 570)] * 40)
    x = column_gutter(two_cols, 612)
    assert x is not None and 280 <= x <= 330
    one_col = _words([(40 + (i % 7) * 10, 300 + (i % 5) * 20) for i in range(80)]
                     + [(320, 560)] * 40)
    assert column_gutter(one_col, 612) is None
    narrow = _words([(40, 150)] * 100 + [(400, 420)] * 5)  # a label column: not balanced
    assert column_gutter(narrow, 612) is None
    assert column_gutter(_words([(40, 90), (330, 380)] * 10), 612) is None  # too few words


def test_item_survives_one_bad_quote(env):
    def extra_quote(out):
        out["earn_rules"][1]["evidence"].append(
            {"doc": "D1", "page": 1, "quote": "This sentence is in no document at all."})

    edit_output(env, "card_terms", extra_quote)
    env.run()
    with env.S() as s:
        p = compose.load(compose.current(s, "example_everyday", "pending"))
        dining = next(r for r in p.earn_rules if "dining" in r.categories)
        assert [e.quote for e in dining.evidence] == ["Earn 3X points at restaurants worldwide."]
        ex = s.scalars(select(m.CardExtraction).where(
            m.CardExtraction.task == "card_terms")).one()
        assert ex.status == "ok"
        rule = json.loads(ex.output_json)["earn_rules"][1]
        assert rule["_verify"]["warnings"][0].startswith("quote not found")


def test_retext_rereads_stored_files_without_downloading(env, monkeypatch):
    import fin.cards.docs as docs_mod

    env.run()
    requests = len(env.web.requests)
    real = docs_mod.html_text
    monkeypatch.setattr(docs_mod, "html_text",
                        lambda data: (real(data)[0] + "\nA new line from better parsing.", 4))
    res = env.run(retext=True)
    assert len(env.web.requests) == requests  # nothing downloaded
    assert res.reports[0].docs_changed == 2  # the two HTML documents got new text
    assert tasks_called(env)[-2:] == ["card_terms", "card_benefits"]


def test_quote_on_another_section_is_kept_with_the_right_section():
    from fin.cards.verify import check_evidence

    doc = DocText.parse("[[section 1]]\nintro\n[[section 2]]\nmore\n[[section 3]]\nx\n"
                        "[[section 9]]\nUp to $10 in statement credits each month.")
    ev = [{"doc": "D1", "page": 2, "quote": "up to $10 in statement credits each month"}]
    c = check_evidence(ev, {"D1": doc})
    assert c.ok and ev[0]["page"] == 9 and "corrected to 9" in c.warnings[0]
    ev = [{"doc": "D2", "page": 1, "quote": "up to $10 in statement credits each month"}]
    assert not check_evidence(ev, {"D1": doc, "D2": DocText.parse("other text")}).ok


def test_rate_after_cap_may_be_the_verified_base_rate(env):
    def unquoted_rate_after(out):
        rule = out["earn_rules"][0]
        rule["cap"]["rate_after"] = 1  # the base rate, though this quote says "then 1X" anyway
        rule["evidence"][0]["quote"] = ("Earn 4X points at U.S. supermarkets on up to $25,000 in "
                                        "purchases per calendar year")

    edit_output(env, "card_terms", unquoted_rate_after)
    env.run()
    with env.S() as s:
        p = compose.load(compose.current(s, "example_everyday", "pending"))
        assert any("supermarkets" in r.categories for r in p.earn_rules)


def test_cached_extraction_is_checked_again_without_ai(env):
    env.run()
    with env.S() as s:
        ex = s.scalars(select(m.CardExtraction).where(m.CardExtraction.task == "card_terms")).one()
        out = json.loads(ex.output_json)
        out["earn_rules"][1]["_verify"] = {"ok": False, "reasons": ["old verifier"],
                                           "warnings": []}
        ex.output_json, ex.status = json.dumps(out), "partial"
        ex_id = ex.id
        s.commit()
    n = len(env.calls())
    env.run()
    assert len(env.calls()) == n
    with env.S() as s:
        ex = s.get(m.CardExtraction, ex_id)
        assert ex.status == "ok" and json.loads(ex.output_json)["earn_rules"][1]["_verify"]["ok"]


def test_renamed_credit_is_not_material(env):
    env.run()
    with env.S() as s:
        compose.accept(s, "example_everyday")
        s.commit()
    edit_output(env, "card_terms", lambda out: out["credits"][0].update(name="Example Eats credit"))
    res = env.run(force=True)
    assert res.reports[0].outcome == "published"
    with env.S() as s:
        d = json.loads(compose.current(s, "example_everyday", "active").diff_json)
        assert not d["added"] and not d["removed"]
        assert d["reworded"][0]["item"] == (
            "Dining credit ($10/month) -> Example Eats credit ($10/month)")


def test_agreement_must_name_the_card(env):
    generic = (DOCS / "everyday_rewards_terms.html").read_bytes().replace(
        b"including the Everyday Visa", b"including all our cards")
    for path in ("agreement", "rules"):
        env.web.routes[f"https://www.example.com/{path}"] = (
            200, {"content-type": "text/html"}, generic)
    _, status, _, n_ok = fetch_one(env, "https://www.example.com/agreement",
                                   doc_type="cardmember_agreement")
    assert status == "needs_browser" and n_ok == 0
    _, status, _, n_ok = fetch_one(env, "https://www.example.com/rules", doc_type="program_terms")
    assert status == "active" and n_ok == 1  # program rules may name only the issuer


def test_incomplete_candidate_withdraws_the_pending_version(env):
    env.run()
    edit_output(env, "card_terms", lambda out: out["annual_fee"]["evidence"][0].update(
        quote="Annual fee: $9,999"))
    res = env.run(force=True)
    assert res.reports[0].outcome.startswith("incomplete")
    with env.S() as s:
        assert compose.current(s, "example_everyday", "pending") is None
    assert not open_items(env, "card_terms_pending")


def test_manual_file_need_not_name_the_card(env, tmp_path):
    from fin.cards.refresh import add_doc

    page = tmp_path / "pricing.html"
    page.write_bytes((DOCS / "everyday_rewards_terms.html").read_bytes()
                     .replace(b"Everyday Visa", b"card").replace(b"Example Bank", b"the bank"))
    with env.S() as s:
        d, outcome = add_doc(s, env.paths, env.settings, env.cards,
                             env.cards.card("example_everyday"), page, "pricing_terms")
        assert outcome == "changed" and d.status == "active"
    thin = tmp_path / "thin.html"
    thin.write_bytes((DOCS / "thin_js_page.html").read_bytes())
    with env.S() as s:
        d, outcome = add_doc(s, env.paths, env.settings, env.cards,
                             env.cards.card("example_everyday"), thin, "pricing_terms")
        assert outcome == "rejected" and d.status == "broken"  # still has to have real text


def test_plain_points_map_to_the_issuers_program():
    from fin.cards.compose import currency_id

    s = example_settings()
    chase = example_cards().cards[0].model_copy(update={"issuer": "chase"})
    assert currency_id(chase, {"currency_name": "points", "currency_kind": "points"}, s) == \
        "chase_ur"
    other = example_cards().cards[0]
    assert currency_id(other, {"currency_name": "points", "currency_kind": "points"}, s) == \
        "example_points"
