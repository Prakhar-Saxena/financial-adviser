# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Utilities foundation (docs/SPEC-utilities.md Phase U0): config, settings. Fictional
providers only ("Example Power", "Example Gas", ...)."""

import copy
import json
import tomllib
from datetime import date

import pytest
from pydantic import ValidationError

from fin.config import (
    CONFIG_DIR,
    SEASONAL_SERVICES,
    UTILITY_SERVICES,
    CardsConfig,
    Settings,
    UtilityAccount,
    load_cards,
    load_settings,
)

EXAMPLE = json.loads((CONFIG_DIR / "cards.example.json").read_text())
CARDS = load_cards(CONFIG_DIR / "cards.example.json")


def mutate(fn):
    doc = copy.deepcopy(EXAMPLE)
    fn(doc)
    return doc


# -------------------------------------------------------------------- config


def test_example_has_fictional_utilities():
    accts = CARDS.utility_accounts
    assert [u.id for u in accts] == ["electric", "gas", "water", "internet", "phone"]
    assert all(u.provider.startswith("Example ") and u.private for u in accts)
    assert CARDS.utility_account("water").bill_every_months == 3
    assert [u.id for u in accts if u.seasonal] == ["electric", "gas", "water"]
    assert CARDS.utility_account("electric").patterns[0].search("example power autopay")
    with pytest.raises(KeyError):
        CARDS.utility_account("trash")


def test_service_lists_agree_with_the_model():
    field = UtilityAccount.model_fields["service"].annotation
    assert set(field.__args__) == set(UTILITY_SERVICES)
    assert set(SEASONAL_SERVICES) < set(UTILITY_SERVICES)


def test_missing_utility_accounts_still_loads():
    cards = CardsConfig.model_validate(mutate(lambda d: d.pop("utility_accounts")))
    assert cards.utility_accounts == []


def test_dates_and_defaults():
    u = UtilityAccount.model_validate({"id": "phone_old", "service": "phone",
                                       "provider": "Example Mobile",
                                       "active_from": "2025-01-01", "active_to": "2026-03-31"})
    assert (u.active_from, u.active_to) == (date(2025, 1, 1), date(2026, 3, 31))
    assert u.private and u.bill_every_months == 1 and not u.paid_in_advance
    assert u.payment_patterns == [] and not u.seasonal


@pytest.mark.parametrize("fn", [
    lambda d: d["utility_accounts"][0].update(id="amex_gold"),       # id shared with a card
    lambda d: d["utility_accounts"][0].update(id="checking"),        # ...with a bank account
    lambda d: d["utility_accounts"].append(dict(d["utility_accounts"][0])),
    lambda d: d["utility_accounts"][0].update(id="Electric"),
    lambda d: d["utility_accounts"][0].update(service="cable"),
    lambda d: d["utility_accounts"][0].update(provider=""),
    lambda d: d["utility_accounts"][0].update(payment_patterns=["(unclosed"]),
    lambda d: d["utility_accounts"][0].update(payment_patterns=[" "]),  # would match every row
    lambda d: d["utility_accounts"][0].update(bill_every_months=4),
    lambda d: d["utility_accounts"][0].update(active_from="2026-05-01", active_to="2026-04-30"),
    lambda d: d["utility_accounts"][0].update(active_to="soon"),
    lambda d: d["utility_accounts"][0].update(account_number="1"),  # no such field
])
def test_invalid_utility_accounts(fn):
    with pytest.raises(ValidationError):
        CardsConfig.model_validate(mutate(fn))


# ------------------------------------------------------------------ settings


def test_utilities_settings_match_the_spec_defaults():
    toml = tomllib.loads((CONFIG_DIR / "settings.toml").read_text())["utilities"]
    assert Settings().utilities.model_dump() == toml == load_settings().utilities.model_dump()
    assert toml == {"stale_grace_days": 10, "yoy_min_change": 0.10, "yoy_min_cents": 500,
                    "step_min_payments": 2, "step_min_cents": 100, "outlier_ratio": 1.3,
                    "duplicate_days": 7, "budget_min_months": 4, "default_months": 24}


@pytest.mark.parametrize("bad", [
    {"outlier_ratio": 1.0}, {"step_min_payments": 0}, {"stale_grace_days": -1},
    {"budget_min_months": 1}, {"unknown": 1},
])
def test_bad_utilities_settings(bad):
    with pytest.raises(ValidationError):
        Settings.model_validate({"utilities": bad})
