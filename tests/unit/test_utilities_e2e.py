# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Utility payments through `fin import`, `fin process`, the CLI and `fin rebuild`
(docs/SPEC-utilities.md §14 "End to end"). Fictional config and files only."""

import json
from datetime import date

from sqlalchemy import select
from typer.testing import CliRunner

from fin.cli import app
from fin.config import data_paths, load_cards, load_settings, load_taxonomy
from fin.db import models as m
from fin.db import session_factory
from fin.pipeline import process
from fin.rebuild import rebuild
from fin.utilities.check import spending_snapshot
from fin.utilities.series import utilities
from tests.conftest import FIXTURES
from tests.unit.test_bank_pipeline import drop_run, example_config, import_inbox  # noqa: F401

TODAY = date(2026, 11, 2)
FILES = FIXTURES / "utilities"
BANK = [("ExportedTransactions.csv", (FILES / "ExportedTransactions.csv").read_bytes(),
         "transactions", "checking", "2025-10-01", "2026-10-31")]
CARD = [("activity.csv", (FILES / "activity_freedom.csv").read_bytes(), "transactions",
         "chase_freedom_unlimited", "2026-04-01", "2026-11-02")]
INSIGHTS = [
    "Internet went from $55.00 to $65.00 a month with the August payment.",
    "Electric cost 25% more this October than last ($100.00 against $80.00).",
    "First payment: it may include setup charges or a partial month.",
]


def run():
    settings = load_settings()
    paths = data_paths(settings)
    with session_factory(paths.db_file)() as s:
        return process(s, paths, settings, load_cards(), load_taxonomy(), use_ai=False,
                       today=TODAY)


def links(db):
    with session_factory(db)() as s:
        return sorted(s.execute(select(m.Transaction.dedupe_key, m.UtilityPayment.account_id,
                                       m.UtilityPayment.method, m.UtilityPayment.covers_months)
                                .join(m.Transaction,
                                      m.Transaction.id == m.UtilityPayment.transaction_id)).all())


def snapshot(db):
    with session_factory(db)() as s:
        return spending_snapshot(s)


def test_utilities_end_to_end(data_dir, example_config):  # noqa: F811
    cards_json = example_config / "accounts.json"
    with_utilities = cards_json.read_text()
    doc = json.loads(with_utilities)
    doc.pop("utility_accounts")
    cards_json.write_text(json.dumps(doc))

    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    drop_run(data_dir, "bank", BANK)
    drop_run(data_dir, "chase", CARD)
    import_inbox()
    db = data_paths(load_settings()).db_file
    with session_factory(db)() as s:  # the trash bill: a bills row no account matches
        trash = s.scalar(select(m.Transaction).where(
            m.Transaction.description_raw.like("TOWN TRASH%")))
        s.add(m.UserOverride(target_type="transaction", stable_key=trash.dedupe_key,
                             field="category_id", value="bills.utilities"))
        trash_id = trash.id
        s.commit()
    r = run()
    assert r.utilities.total == 0 and r.utilities.unassigned == 1
    without = snapshot(db)

    # The same ledger with the fictional utility accounts: spending doesn't move a cent.
    cards_json.write_text(with_utilities)
    r = run()
    assert snapshot(db) == without
    assert r.utilities.linked == {"pattern": 25} and r.utilities.unassigned == 1
    counts: dict[str, int] = {}
    for _, acct, _method, covers in links(db):
        counts[acct] = counts.get(acct, 0) + 1
        assert covers == (3 if acct == "water" else 1)
    assert counts == {"electric": 13, "water": 4, "internet": 7, "phone": 1}

    with session_factory(db)() as s:
        res = utilities(s, load_settings(), TODAY)
        electric = res.accounts["electric"].series
        assert (electric["2025-10"], electric["2026-01"], electric["2026-10"]) == (
            8000, 14000, 10000)
        water = res.accounts["water"].series
        assert water["2025-10"] == water["2025-12"] == 3000 and water["2026-09"] == 3300
        assert res.accounts["internet"].series["2026-08"] == 6500
        assert res.total["2026-10"] == 10000 + 6500 + 4000  # water's Sep payment: Jul-Sep
        assert [i.text for i in res.insights] == INSIGHTS
        assert not any(v.missing for v in res.accounts.values())

    out = CliRunner().invoke(app, ["utilities", "trends"])
    assert out.exit_code == 0 and INSIGHTS[0] in out.output
    out = CliRunner().invoke(app, ["utilities", "unassigned"])
    assert out.exit_code == 0 and "TOWN TRASH SERVICE" in out.output

    # Corrections: the trash bill isn't a utility; the phone payment was prepaid for 3 months.
    with session_factory(db)() as s:
        phone = s.scalar(select(m.UtilityPayment).where(m.UtilityPayment.account_id == "phone"))
        phone_id = phone.transaction_id
    out = CliRunner().invoke(app, ["utilities", "assign", str(trash_id), "none"])
    assert out.exit_code == 0 and "0 unassigned" in out.output, out.output
    out = CliRunner().invoke(app, ["utilities", "assign", str(phone_id), "phone",
                                   "--covers", "3"])
    assert out.exit_code == 0 and "covering 3 month(s)" in out.output, out.output
    with session_factory(db)() as s:
        series = utilities(s, load_settings(), TODAY).accounts["phone"].series
        assert series == {"2026-08": 1334, "2026-09": 1333, "2026-10": 1333}  # backward

    out = CliRunner().invoke(app, ["utilities", "check", "--snapshot"])
    assert out.exit_code == 0 and "FAIL" not in out.output, out.output

    # `fin rebuild` then `fin process`: the same links, and the corrections survive.
    kept = links(db)
    with session_factory(db)() as s:
        rebuild(s)
    r = run()
    assert links(db) == kept and r.utilities.unassigned == 0 and r.utilities.not_utility == 1
    assert snapshot(db) == without
    out = CliRunner().invoke(app, ["utilities", "check"])
    assert out.exit_code == 0 and "PASS  Spending equals the snapshot" in out.output
