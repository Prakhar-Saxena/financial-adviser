# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Phase C0 spike (docs/SPEC-cards.md §16): one real card_discover and card_terms run on ONE card.

    uv run python scripts/cards_spike.py discover chase_freedom_unlimited
    uv run python scripts/cards_spike.py terms chase_freedom_unlimited RUN_DIR --model haiku
    uv run python scripts/cards_spike.py merchant --effort low

`discover` writes its run folder under ai-runs/. `terms` downloads the allowlisted URLs that
discover returned (a minimal stand-in for the Phase C1 fetcher: HTTPS, allowlist on every
redirect hop, robots.txt, the honest user agent, no cookies, size limit), keeps the files in its
own run folder in the data dir, and runs card_terms on their text. Nothing personal is sent.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from fin.ai import claude_cli as cc
from fin.ai.runs import new_run_dir
from fin.config import data_paths, load_cards, load_reward_categories, load_settings

WANTED = ["product_page", "rewards_terms", "program_terms", "pricing_terms",
          "cardmember_agreement", "benefits_guide", "benefit_terms"]
TERMS_TYPES = {"product_page", "rewards_terms", "program_terms", "pricing_terms",
               "cardmember_agreement", "benefit_terms"}


def allowed_host(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def usage_line(res: cc.CallResult) -> str:
    u = res.raw.get("usage") or {}
    return (f"{res.duration_ms / 1000:.1f}s, turns={res.raw.get('num_turns')}, "
            f"in={u.get('input_tokens')} cache_read={u.get('cache_read_input_tokens')} "
            f"cache_write={u.get('cache_creation_input_tokens')} out={u.get('output_tokens')}, "
            f"est=${res.raw.get('total_cost_usd')}")


# ------------------------------------------------------------------- discover


def discover(card_id: str) -> None:
    settings, cards = load_settings(), load_cards()
    card = cards.card(card_id)
    domains = settings.cards.domains_for(card, cards.site(card.issuer))
    task = cc.load_task("card_discover")
    inv = cc.research_invocation(task, settings.cards.research_model, domains,
                                 max_turns=settings.cards.discover_max_turns,
                                 max_searches=settings.cards.discover_max_searches)
    payload = {"card": {"id": card.id, "product": card.product, "issuer": card.issuer,
                        "network": card.network},
               "allowed_domains": domains, "wanted": WANTED, "known_documents": [],
               "today": time.strftime("%Y-%m-%d")}
    run = new_run_dir(data_paths(settings).ai_runs, "card_discover")
    res = cc.run_task(task, payload, run, invocation=inv,
                      timeout=settings.cards.timeout_seconds)
    print(f"run: {run}\n{usage_line(res)}")
    print("permission denials:", json.loads((run / "meta.json").read_text())["permission_denials"])
    for d in res.output["documents"]:
        ok = allowed_host(urlsplit(d["url"]).hostname or "", domains)
        print(f"  {'ok ' if ok else 'OFF'} {d['doc_type']:<21} {d['confidence']:.2f} {d['url']}")
    print("not_found:", res.output["not_found"], "\nnotes:", res.output["notes"])


# ---------------------------------------------------------------------- fetch


def fetch(url: str, domains: list[str], ua: str, max_bytes: int) -> tuple[bytes, str] | str:
    robots: dict[str, RobotFileParser] = {}
    for _ in range(6):
        parts = urlsplit(url)
        if parts.scheme != "https" or not allowed_host(parts.hostname or "", domains):
            return f"off-allowlist or non-https hop: {parts.hostname}"
        base = f"https://{parts.netloc}"
        if base not in robots:
            rp = RobotFileParser()
            with httpx.Client(headers={"User-Agent": ua}, timeout=30) as c:
                r = c.get(base + "/robots.txt")
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            robots[base] = rp
        if not robots[base].can_fetch(ua, url):
            return "robots.txt disallows"
        with httpx.Client(headers={"User-Agent": ua}, timeout=30, follow_redirects=False) as c:
            r = c.get(url)
        if r.is_redirect:
            url = urljoin(url, r.headers["location"])
            time.sleep(1)
            continue
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        if len(r.content) > max_bytes:
            return "too large"
        return r.content, r.headers.get("content-type", "")
    return "too many redirects"


def pdf_text(data: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(f"[[page {i}]]\n{p.extract_text() or ''}"
                         for i, p in enumerate(pdf.pages, 1))


def html_text(data: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "lxml")
    for t in soup(["script", "style", "nav", "header", "footer", "form", "noscript", "svg"]):
        t.decompose()
    out, n = [], 0
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th"]):
        txt = " ".join(el.get_text(" ", strip=True).split())
        if not txt:
            continue
        if el.name in ("h1", "h2", "h3") or n == 0:
            n += 1
            out.append(f"[[section {n}]]")
        out.append(txt)
    return "\n".join(out)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                                   "®": None, "™": None, " ": " "}))
    s = re.sub(r"-\s*\n\s*", "", s)
    return " ".join(s.split())


# ---------------------------------------------------------------------- terms


def terms(card_id: str, discover_run: Path, model: str, reuse_docs: Path | None) -> None:
    settings, cards = load_settings(), load_cards()
    card = cards.card(card_id)
    domains = settings.cards.domains_for(card, cards.site(card.issuer))
    found = json.loads((discover_run / "output.json").read_text())["structured_output"]
    run = new_run_dir(data_paths(settings).ai_runs, f"card_terms_{model}")
    docs_dir = run / "docs"
    docs_dir.mkdir(mode=0o700)

    documents, seen = [], set()
    for d in found["documents"]:
        if d["doc_type"] not in TERMS_TYPES:
            continue
        url = d["url"].split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        key = hashlib.sha256(url.encode()).hexdigest()[:12]
        cached = reuse_docs / f"{key}.txt" if reuse_docs else None
        if cached and cached.exists():
            text = cached.read_text()
        else:
            got = fetch(url, domains, settings.cards.user_agent, settings.cards.max_doc_mb << 20)
            time.sleep(settings.cards.min_request_interval_s)
            if isinstance(got, str):
                print(f"  skip {url}: {got}")
                continue
            data, ctype = got
            text = pdf_text(data) if "pdf" in ctype or data[:4] == b"%PDF" else html_text(data)
        (docs_dir / f"{key}.txt").write_text(text)
        (docs_dir / f"{key}.txt").chmod(0o600)
        print(f"  D{len(documents) + 1} {d['doc_type']:<21} {len(text):>7} chars {url}")
        documents.append({"ref": f"D{len(documents) + 1}", "doc_type": d["doc_type"],
                          "title": d["title"], "url": url, "text": text})
    total = sum(len(d["text"]) for d in documents)
    if total > settings.cards.max_extract_chars:
        sys.exit(f"{total} chars is over max_extract_chars; the C1 page budget isn't built yet")

    rc = load_reward_categories()
    payload = {"card": {"product": card.product, "issuer": card.issuer, "network": card.network},
               "documents": documents,
               "reward_categories": [{"id": c.id, "description": c.description}
                                     for c in rc.categories],
               "today": time.strftime("%Y-%m-%d")}
    task = cc.load_task("card_terms")
    res = cc.run_task(task, payload, run / "call",
                      invocation=cc.extract_invocation(task, model),
                      timeout=settings.cards.timeout_seconds)
    print(f"run: {run}\n{model}: {usage_line(res)} ({total} chars of documents)")
    texts = {d["ref"]: norm(d["text"]) for d in documents}
    out = res.output

    def quotes_ok(evidence: list[dict]) -> str:
        return "".join("✓" if norm(e["quote"]) in texts.get(e["doc"], "") else "✗"
                       for e in evidence)

    print(f"currency: {out['currency_name']} ({out['currency_kind']})")
    print(f"annual fee: ${out['annual_fee']['usd']} {quotes_ok(out['annual_fee']['evidence'])}; "
          f"FTF: {out['foreign_transaction_fee']['percent']}% "
          f"{quotes_ok(out['foreign_transaction_fee']['evidence'])}")
    for r in out["earn_rules"]:
        cap = r["cap"]
        print(f"  {'BASE ' if r['is_base'] else ''}{r['rate']}{r['rate_unit'][0]} "
              f"cats={r['categories']} merchants={r['merchant_names']} ch={r['channel']}"
              f"{'/' + r['portal'] if r['portal'] else ''} geo={r['geography']}"
              f"{f' cap=${cap['amount_usd']}/{cap['period']}→{cap['rate_after']}' if cap else ''}"
              f" excl={r['excluded_categories'] + r['exclusions']} {quotes_ok(r['evidence'])}")
    for c in out["credits"]:
        print(f"  credit {c['name']}: ${c['amount_usd']}/{c['period']} {quotes_ok(c['evidence'])}")
    for n in out["notes"]:
        print(f"  note: {n}")


# ------------------------------------------------------------------- merchant


def merchant(effort: str | None) -> None:
    """Spike item 6: merchant_profile on generic public merchant names, with and without
    --effort."""
    rc = load_reward_categories()
    queries = ["whole foods", "Costco gas", "DoorDash", "Delta flight", "UBER EATS", "CVS",
               "Shell", "Target", "Netflix", "asdf qwerty"]
    payload = {"records": [{"key": f"q{i}", "query": q} for i, q in enumerate(queries)],
               "known": [{"key": "costco_warehouse", "name": "Costco warehouse",
                          "reward_category": "wholesale_clubs"}],
               "reward_categories": [{"id": c.id, "description": c.description}
                                     for c in rc.categories]}
    task = cc.load_task("merchant_profile")
    run = new_run_dir(data_paths().ai_runs, f"merchant_profile_{effort or 'default'}")
    res = cc.run_task(task, payload, run, invocation=cc.merchant_invocation(task, effort=effort))
    print(f"effort={effort or 'default'}: {usage_line(res)}")
    for r in res.output["results"]:
        new = r["new"] or {}
        print(f"  {queries[int(r['key'][1:])]:<14} merchant={r['is_merchant']} "
              f"match={r['match_key']} cat={new.get('reward_category')} "
              f"mcc={new.get('mcc_guess')} conf={r['confidence']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("card")
    t = sub.add_parser("terms")
    t.add_argument("card")
    t.add_argument("discover_run", type=Path)
    t.add_argument("--model", default="sonnet")
    t.add_argument("--reuse-docs", type=Path, help="docs/ folder of an earlier terms run")
    mp = sub.add_parser("merchant")
    mp.add_argument("--effort", choices=["low", "medium", "high"])
    a = ap.parse_args()
    if a.cmd == "discover":
        discover(a.card)
    elif a.cmd == "terms":
        terms(a.card, a.discover_run, a.model, a.reuse_docs)
    else:
        merchant(a.effort)


if __name__ == "__main__":
    main()
