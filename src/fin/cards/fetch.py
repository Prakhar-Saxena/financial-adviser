# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Download a card's public documents (docs/SPEC-cards.md §7.2).

HTTPS only, every redirect hop on the card's allowlist, robots.txt respected for our honest user
agent, no cookies (a new client per document), conditional requests, a size cap and at most one
request every `min_request_interval_s` seconds per host. A refusal (401, 403, a challenge page)
is never retried with another identity: the document becomes `blocked`, and the manual path
(§7.6) takes over.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from fin.config import CardsSettings

MAX_HOPS = 5
TIMEOUT = 30
CHALLENGE_MARKERS = (b"cf-chl", b"captcha", b"just a moment", b"access denied",
                     b"are you a robot", b"/_incapsula_resource", b"px-captcha")


REDIRECTS = (301, 302, 303, 307, 308)


def _redirect(r: httpx.Response) -> bool:
    return r.status_code in REDIRECTS and bool(r.headers.get("location"))


def host_allowed(host: str, domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


@dataclass
class Fetched:
    """`outcome`: ok | not_modified | off_allowlist | robots | blocked | gone | too_large |
    failed. `data` is set only for ok."""

    outcome: str
    url: str
    status: int | None = None
    data: bytes = b""
    content_type: str = ""
    etag: str | None = None
    last_modified: str | None = None
    detail: str = ""


class Fetcher:
    def __init__(self, settings: CardsSettings, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ua = settings.user_agent
        self.max_bytes = settings.max_doc_mb << 20
        self.interval = settings.min_request_interval_s
        self.transport = transport
        self.sleep, self.clock = sleep, clock
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | str] = {}
        self.requests = 0

    def _client(self) -> httpx.Client:
        # A fresh client per document: no cookie jar survives between documents.
        return httpx.Client(transport=self.transport, timeout=TIMEOUT, follow_redirects=False,
                            headers={"User-Agent": self.ua})

    def _pace(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.interval - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[host] = self.clock()
        self.requests += 1

    def _robots_for(self, client: httpx.Client, url: str, domains: list[str]
                    ) -> RobotFileParser | str:
        """The parsed robots.txt for this URL's host, or a failure outcome string."""
        parts = urlsplit(url)
        key = parts.netloc.lower()
        if key in self._robots:
            return self._robots[key]
        rp = RobotFileParser()
        target = f"https://{parts.netloc}/robots.txt"
        result: RobotFileParser | str = rp
        try:
            for _ in range(MAX_HOPS + 1):
                self._pace(urlsplit(target).hostname or "")
                r = client.get(target)
                if _redirect(r):
                    target = urljoin(target, r.headers.get("location", ""))
                    nxt = urlsplit(target)
                    if nxt.scheme != "https" or not host_allowed(nxt.hostname or "", domains):
                        rp.parse([])  # robots.txt off the allowlist: treat as no rules
                        break
                    continue
                if r.status_code in (401, 403):
                    rp.parse(["User-agent: *", "Disallow: /"])
                elif r.status_code >= 500 or r.status_code == 429:
                    result = "failed"
                elif r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp.parse([])  # 404 and the like: no rules
                break
            else:
                result = "failed"  # too many redirects
        except httpx.HTTPError:
            result = "failed"
        if result == "failed":
            return result  # not cached: retried on the next document from this host
        self._robots[key] = rp
        return rp

    def fetch(self, url: str, domains: list[str], *, etag: str | None = None,
              last_modified: str | None = None) -> Fetched:
        with self._client() as client:
            for _ in range(MAX_HOPS + 1):
                parts = urlsplit(url)
                host = (parts.hostname or "").lower()
                if parts.scheme != "https" or not host_allowed(host, domains):
                    return Fetched("off_allowlist", url, detail=host or parts.scheme)
                rp = self._robots_for(client, url, domains)
                if isinstance(rp, str):
                    return Fetched(rp, url, detail="robots.txt unavailable")
                if not rp.can_fetch(self.ua, url):
                    return Fetched("robots", url, detail="robots.txt disallows")
                headers = {}
                if etag:
                    headers["If-None-Match"] = etag
                if last_modified:
                    headers["If-Modified-Since"] = last_modified
                self._pace(host)
                try:
                    got = self._get(client, url, headers)
                except httpx.HTTPError as e:
                    return Fetched("failed", url, detail=type(e).__name__)
                if isinstance(got, str):
                    return Fetched(got, url, detail=f"over {self.max_bytes >> 20} MB")
                r, data = got
                if _redirect(r):
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                return self._outcome(url, r, data)
        return Fetched("failed", url, detail="too many redirects")

    def _get(self, client: httpx.Client, url: str, headers: dict[str, str]
             ) -> tuple[httpx.Response, bytes] | str:
        with client.stream("GET", url, headers=headers) as r:
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                return "too_large"
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > self.max_bytes:
                    return "too_large"
            return r, bytes(buf)

    def _outcome(self, url: str, r: httpx.Response, data: bytes) -> Fetched:
        code = r.status_code
        if code == 304:
            return Fetched("not_modified", url, code)
        if code in (404, 410):
            return Fetched("gone", url, code)
        if code in (401, 403) or (code in (200, 503) and _challenge(data)):
            return Fetched("blocked", url, code, detail="refused or challenge page")
        if code != 200:
            return Fetched("failed", url, code, detail=f"HTTP {code}")
        return Fetched("ok", url, code, data, r.headers.get("content-type", ""),
                       r.headers.get("etag"), r.headers.get("last-modified"))


def _challenge(data: bytes) -> bool:
    """A bot-check page instead of the document. Only short pages count, so a real terms page
    that mentions "access denied" somewhere isn't mistaken for one."""
    head = data[:20000].lower()
    return len(data) < 20000 and any(mk in head for mk in CHALLENGE_MARKERS)
