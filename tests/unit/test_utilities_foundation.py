# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Utilities foundation (docs/SPEC-utilities.md Phase U0): config, settings, tables. Fictional
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


# ------------------------------------------------------------------ database


@pytest.fixture
def db(tmp_path):
    from fin.config import load_taxonomy
    from fin.db import session_factory, upgrade
    from fin.db.seed import sync_reference

    path = tmp_path / "t.sqlite"
    upgrade(path)
    with session_factory(path)() as s:
        sync_reference(s, CARDS, load_taxonomy())
    return path


def add_txn(s, i, card="checking", amount=8_123, desc="EXAMPLE POWER AUTOPAY"):
    from fin.db import models as m

    if s.get(m.Import, 1) is None:
        s.add(m.Import(id=1, source="bank", kind="transactions", file_path="/nowhere/x.csv",
                       sha256="h", status="ok"))
    s.add(m.Transaction(id=i, card_id=card, post_date="2026-09-01", amount_cents=amount,
                        description_raw=desc, description_clean=desc, type="purchase",
                        dedupe_key=f"k{i}", import_id=1))
    s.flush()


def test_review_kinds_and_check(db):
    from sqlalchemy.exc import IntegrityError

    from fin.db import models as m
    from fin.db import session_factory
    from fin.review import PROCESS_KINDS

    for kind in ("utility_unassigned", "utility_missing"):
        assert kind in m.REVIEW_KINDS and kind in PROCESS_KINDS
    with session_factory(db)() as s:
        s.add(m.ReviewItem(kind="utility_missing", ref_table="utility_accounts", ref_id="gas"))
        s.commit()
        s.add(m.ReviewItem(kind="utility_overdue", ref_table="utility_accounts", ref_id="gas"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_migration_downgrades_and_upgrades(tmp_path):
    from alembic import command
    from sqlalchemy import inspect

    from fin.db import alembic_config, make_engine, upgrade

    path = tmp_path / "d.sqlite"
    upgrade(path)
    command.downgrade(alembic_config(path), "0010")
    assert not {"utility_accounts", "utility_payments"} & set(
        inspect(make_engine(path)).get_table_names())
    upgrade(path)
    assert {"utility_accounts", "utility_payments"} <= set(
        inspect(make_engine(path)).get_table_names())


def test_accounts_mirror_from_config(db):
    from fin.db import models as m
    from fin.db import session_factory
    from fin.db.seed import sync_utility_accounts

    with session_factory(db)() as s:
        rows = {r.id: r for r in s.query(m.UtilityAccount)}
        assert set(rows) == {"electric", "gas", "water", "internet", "phone"}
        assert rows["water"].bill_every_months == 3 and rows["gas"].provider == "Example Gas"
        assert all(r.in_config for r in rows.values())

        # Gas leaves config and the internet account gains dates: the gas row stays, marked.
        doc = mutate(lambda d: d.update(utility_accounts=[
            u if u["id"] != "internet" else {**u, "active_from": "2025-03-01",
                                             "active_to": "2026-06-30"}
            for u in d["utility_accounts"] if u["id"] != "gas"]))
        sync_utility_accounts(s, CardsConfig.model_validate(doc))
        s.commit()
        rows = {r.id: r for r in s.query(m.UtilityAccount)}
        assert not rows["gas"].in_config and rows["electric"].in_config
        assert (rows["internet"].active_from, rows["internet"].active_to) == (
            "2025-03-01", "2026-06-30")

        sync_utility_accounts(s, CARDS)  # back in config
        s.commit()
        assert s.get(m.UtilityAccount, "gas").in_config


def test_process_mirrors_accounts(tmp_path):
    """`fin process` mirrors them too, so a config edit counts without `fin init`."""
    from datetime import date

    from fin.config import data_paths, load_taxonomy
    from fin.db import models as m
    from fin.db import session_factory, upgrade
    from fin.db.seed import sync_reference
    from fin.pipeline import process

    paths = data_paths()
    paths.db_file.parent.mkdir(parents=True)
    upgrade(paths.db_file)
    no_utils = CardsConfig.model_validate(mutate(lambda d: d.pop("utility_accounts")))
    with session_factory(paths.db_file)() as s:
        sync_reference(s, no_utils, load_taxonomy())
        assert s.query(m.UtilityAccount).count() == 0
        process(s, paths, Settings(), CARDS, load_taxonomy(), use_ai=False,
                today=date(2026, 11, 15))
        assert s.query(m.UtilityAccount).count() == 5


def test_payment_link_cascades_and_rebuild_keeps_accounts(db):
    from fin.db import models as m
    from fin.db import session_factory
    from fin.rebuild import DERIVED, rebuild

    assert DERIVED.index(m.UtilityPayment) < DERIVED.index(m.Transaction)
    assert m.UtilityAccount not in DERIVED
    with session_factory(db)() as s:
        add_txn(s, 1)
        add_txn(s, 2, amount=6_000, desc="EXAMPLE GAS")
        s.add(m.UtilityPayment(transaction_id=1, account_id="electric", method="pattern"))
        s.add(m.UtilityPayment(transaction_id=2, account_id="gas", method="override",
                               covers_months=2))
        s.add(m.UserOverride(target_type="transaction", stable_key="k2",
                             field="utility_account", value="gas"))
        s.commit()
        s.delete(s.get(m.Transaction, 1))
        s.commit()
        assert [p.transaction_id for p in s.query(m.UtilityPayment)] == [2]

        rebuild(s)
        assert s.query(m.UtilityPayment).count() == 0
        assert s.query(m.UtilityAccount).count() == 5
        assert s.query(m.UserOverride).filter_by(field="utility_account").count() == 1


def test_payment_checks(db):
    from sqlalchemy.exc import IntegrityError

    from fin.db import models as m
    from fin.db import session_factory

    for bad in ({"method": "guess"}, {"covers_months": 0}, {"account_id": "nowhere"}):
        with session_factory(db)() as s:
            add_txn(s, 1)
            s.add(m.UtilityPayment(**{"transaction_id": 1, "account_id": "electric",
                                      "method": "pattern", **bad}))
            with pytest.raises(IntegrityError):
                s.commit()
