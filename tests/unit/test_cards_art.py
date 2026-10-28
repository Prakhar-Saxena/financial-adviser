# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: F811  (pytest fixtures imported from test_cards_refresh)

"""Official card pictures (docs/SPEC-cards.md §7.8): chosen from the card's stored pages,
downloaded through the document fetcher, served by the local API. Fictional cards only."""

from typer.testing import CliRunner

from fin.cards.art import (
    card_shaped,
    fetch_art,
    image_kind,
    image_size,
    stored,
)
from fin.cards.fetch import Fetcher
from fin.cli import app
from tests.unit.test_cards_refresh import DOCS, PRODUCT, client, env  # noqa: F401  (fixtures)

BASE = "https://www.example.com/img/"


def png(w: int, h: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00" + b"\x00" * 64)


def jpeg(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (b"\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big")
           + b"\x03" + b"\x00" * 9)
    return b"\xff\xd8" + app0 + sof + b"\x00" * 32


PICTURES = """
<img src="/img/everyday-visa-lifestyle.jpg" alt="Everyday Visa">
<img src="/img/everyday-visa-card.png" alt="Example Bank Everyday Visa card">
<img src="/img/everyday-visa-business.png" alt="Example Bank Everyday Visa Business card">
<img src="/img/travel-card.png" alt="Example Bank Travel Card">
<img src="/img/everyday-visa.svg" alt="Everyday Visa">
<img src="https://cdn.example.org/everyday-visa-card.png" alt="Everyday Visa">
<img src="data:image/png;base64,AAAA" alt="Everyday Visa">
"""


def with_pictures(e, lifestyle_first: bool = False) -> None:
    page = (DOCS / "everyday_product.html").read_text().replace("</body>", PICTURES + "</body>")
    if lifestyle_first:  # the photo names the card better than the card does
        page = page.replace('alt="Example Bank Everyday Visa card"', 'alt="Card"')
    e.web.routes[PRODUCT] = (200, {"content-type": "text/html"}, page.encode())
    e.web.routes[BASE + "everyday-visa-card.png"] = (200, {"content-type": "image/png"},
                                                    png(400, 252))
    e.web.routes[BASE + "everyday-visa-business.png"] = (200, {"content-type": "image/png"},
                                                        png(400, 252))
    e.web.routes[BASE + "everyday-visa-lifestyle.jpg"] = (200, {"content-type": "image/jpeg"},
                                                         jpeg(300, 400))
    e.run()


def art(e, **kw):
    fetcher = Fetcher(e.settings.cards, e.web.transport, sleep=lambda _: None)
    with e.S() as s:
        r = fetch_art(s, e.paths, kw.pop("settings", e.settings), e.cards,
                      e.cards.card("example_everyday"), fetcher, **kw)
    return r, fetcher.requests


def test_image_headers():
    assert image_kind(png(10, 5)) == "png" and image_size(png(400, 252), "png") == (400, 252)
    assert image_kind(jpeg(10, 5)) == "jpg" and image_size(jpeg(306, 192), "jpg") == (306, 192)
    vp8x = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (305).to_bytes(3, "little") \
        + (191).to_bytes(3, "little")
    assert image_kind(vp8x) == "webp" and image_size(vp8x, "webp") == (306, 192)
    assert image_kind(b"<svg onload=alert(1)>") is None
    assert card_shaped((400, 252)) and not card_shaped((300, 400)) and not card_shaped((60, 38))


def test_picks_the_card_downloads_once_and_serves_it(env, client):
    with_pictures(env)
    r, n = art(env)
    assert r.outcome == "ok" and r.url == BASE + "everyday-visa-card.png", r
    pic, info = stored(env.paths, "example_everyday")
    assert pic.read_bytes() == png(400, 252) and info["alt"] == "Example Bank Everyday Visa card"
    assert pic.stat().st_mode & 0o777 == 0o600
    assert not any("example.org" in str(q.url) for q in env.web.requests)

    r, n = art(env)
    assert r.outcome == "unchanged" and n == 0

    meta = {c["id"]: c for c in client.get("/api/meta").json()["cards"]}
    assert meta["example_everyday"]["art"] == info["sha256"][:12]
    assert meta["example_travel"]["art"] is None
    got = client.get("/api/cards/art/example_everyday")
    assert got.headers["content-type"] == "image/png" and got.content == png(400, 252)
    assert got.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/cards/art/example_travel").status_code == 404
    assert client.get("/api/cards/art/..%2Fdb").status_code == 404


def test_a_photo_that_is_not_card_shaped_is_skipped(env):
    with_pictures(env, lifestyle_first=True)
    r, _ = art(env)
    assert r.outcome == "ok" and r.url.endswith("everyday-visa-card.png"), r


def test_settings_override_and_its_checks(env):
    with_pictures(env)
    override = lambda url: env.settings.model_copy(update={"cards": env.settings.cards.model_copy(  # noqa: E731
        update={"art": {"example_everyday": url}})})
    env.web.routes[BASE + "other.png"] = (200, {"content-type": "image/png"}, png(316, 200))
    env.web.routes[BASE + "page.html"] = (200, {"content-type": "text/html"}, b"<html></html>")
    r, _ = art(env, settings=override(BASE + "other.png"))
    assert r.outcome == "ok" and stored(env.paths, "example_everyday")[1]["page"] == "settings"
    r, _ = art(env, settings=override(BASE + "page.html"), force=True)
    assert r.outcome == "not_an_image"
    r, n = art(env, settings=override("https://cdn.example.org/x.png"), force=True)
    assert r.outcome == "off_allowlist" and n == 0
    assert stored(env.paths, "example_everyday")[1]["url"] == BASE + "other.png"


def test_cli_lists_without_downloading(env, monkeypatch):
    with_pictures(env)
    monkeypatch.setattr("fin.cards.cli.load_cards", lambda: env.cards)
    monkeypatch.setattr("fin.cards.cli.load_settings", lambda: env.settings)
    before = len(env.web.requests)
    out = CliRunner().invoke(app, ["cards", "art", "--list", "--card", "example_everyday"])
    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()
    assert "everyday-visa-card.png" in lines[1] and "business" not in lines[1]
    assert not any(".svg" in x or "example.org" in x for x in lines)
    assert len(env.web.requests) == before
