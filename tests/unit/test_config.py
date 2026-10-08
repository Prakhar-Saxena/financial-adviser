import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fin.config import (
    CONFIG_DIR,
    CardsConfig,
    Settings,
    data_dir,
    icloud_risk,
    load_cards,
    load_settings,
    load_taxonomy,
)

EXAMPLE = json.loads((CONFIG_DIR / "cards.example.json").read_text())


def test_example_and_settings_load():
    cards = load_cards(CONFIG_DIR / "cards.example.json")
    assert len(cards.cards) == 6 and len(cards.people) == 4
    assert {i.id for i in cards.issuers} == {"amex", "chase", "citi"}
    s = load_settings()
    assert s.general.history_months == 3 and s.sync.sync_overlap_days == 14
    assert s.match.amazon_window_days == (-1, 3)
    assert "uncategorized" in load_taxonomy().ids


def mutate(fn):
    doc = copy.deepcopy(EXAMPLE)
    fn(doc)
    return doc


@pytest.mark.parametrize("fn", [
    lambda d: d["cards"][0]["cardholders"].append(dict(d["cards"][0]["cardholders"][0])),
    lambda d: d["cards"][0]["cardholders"][1].update(role="primary"),
    lambda d: d["cards"][0]["cardholders"][0].update(person="nobody"),
    lambda d: d["cards"][0].update(issuer="discover"),
    lambda d: d["cards"][0]["cardholders"][0].update(card_ending="12a4"),
    lambda d: d["cards"][0]["cardholders"][0].update(card_ending="123"),
    lambda d: d["cards"][0].update(reconcile_with=["walmart"]),
    lambda d: d["cards"].append(dict(d["cards"][0])),
    lambda d: d["issuers"][0].update(allowed_domains=["https://chase.com"]),
    lambda d: d["issuers"][0].update(start_url="http://chase.com"),
    lambda d: d.update(unexpected=1),
])
def test_invalid_cards(fn):
    with pytest.raises(ValidationError):
        CardsConfig.model_validate(mutate(fn))


def test_blank_endings_allowed():
    doc = mutate(lambda d: [h.update(card_ending="") for c in d["cards"] for h in c["cardholders"]])
    CardsConfig.model_validate(doc)


def test_expected_groups_family():
    cards = load_cards(CONFIG_DIR / "cards.example.json")
    costco, prime, csr = (cards.card(i) for i in
                          ("citi_costco", "chase_prime_visa", "chase_sapphire_reserve"))
    assert costco.expects_group("costco_gas") and costco.expects_group("costco_warehouse")
    assert not costco.expects_group("amazon") and not costco.expects_group(None)
    assert prime.expects_group("whole_foods") and not prime.expects_group("costco_online")
    assert csr.expects_group("anything") and csr.expects_group(None)


@pytest.mark.parametrize("v", ["latest", "^0.0.83", "0.0", "next"])
def test_playwright_version_must_be_exact(v):
    with pytest.raises(ValidationError):
        Settings.model_validate({"sync": {"playwright_mcp_version": v}})


def test_classifier_model_locked():
    with pytest.raises(ValidationError):
        Settings.model_validate({"ai": {"classify_model": "sonnet"}})


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("FIN_DATA_DIR", str(tmp_path / "elsewhere"))
    assert data_dir(Settings()) == (tmp_path / "elsewhere").resolve()


def test_default_data_dir(monkeypatch):
    monkeypatch.delenv("FIN_DATA_DIR", raising=False)
    assert data_dir(Settings()) == (Path.home() / "FinancialAdviserData").resolve()


def test_icloud_risk():
    home = Path.home()
    assert icloud_risk(home / "Documents" / "fin")
    assert icloud_risk(home / "Desktop")
    assert icloud_risk(home / "Library" / "Mobile Documents" / "x")
    assert icloud_risk(home / "FinancialAdviserData") is None
