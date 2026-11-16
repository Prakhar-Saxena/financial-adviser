# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Golden table for the utilities engine (docs/SPEC-utilities.md §8, §14): series and insight
texts to the cent, worked out by hand. Fictional accounts and amounts only."""

from datetime import date

import pytest

from fin.config import UtilitiesSettings
from fin.utilities.engine import Account, Payment, add_months, compute, spread

S = UtilitiesSettings()
ELECTRIC = Account("electric", "electric", "Example Power")
GAS = Account("gas", "gas", "Example Gas")
WATER = Account("water", "water", "Example Water", bill_every_months=3)
INTERNET = Account("internet", "internet", "Example Fiber")
PHONE = Account("phone", "phone", "Example Wireless")
PHONE_OLD = Account("phone_old", "phone", "Example Mobile", active_to=date(2025, 9, 30))
PREPAID = Account("phone_old", "phone", "Example Mobile", paid_in_advance=True)


def d(s: str) -> date:
    return date.fromisoformat(s)


def monthly(acct: str, first: str, amounts: list[int], day: int = 10) -> list[tuple]:
    return [(acct, f"{add_months(first, i)}-{day:02d}", c) for i, c in enumerate(amounts)]


def build(rows: list[tuple]) -> list[Payment]:
    """rows: (account, date, cents[, covers[, source]]); ids in row order from 1."""
    out = []
    for i, (acct, day, cents, *rest) in enumerate(rows, 1):
        covers = rest[0] if rest else 1
        source = rest[1] if len(rest) > 1 else "checking"
        out.append(Payment(i, acct, d(day), cents, covers, source))
    return out


GOLDEN = [
    # 1. Spreading with largest remainder: the extra cent goes to the earliest month.
    {"name": "quarterly_spread", "accounts": [WATER], "today": "2026-03-20",
     "payments": [("water", "2026-03-15", 10000, 3)],
     "series": {"water": {"2026-01": 3334, "2026-02": 3333, "2026-03": 3333}}, "texts": []},
    # 2. A prepaid plan spreads forward.
    {"name": "prepaid_forward", "accounts": [PREPAID], "today": "2026-02-20",
     "payments": [("phone_old", "2026-02-10", 4500, 3)],
     "series": {"phone_old": {"2026-02": 1500, "2026-03": 1500, "2026-04": 1500}}, "texts": []},
    # 3. A refund nets in its own month.
    {"name": "refund_nets", "accounts": [ELECTRIC], "today": "2026-02-15",
     "payments": [("electric", "2026-01-10", 8000), ("electric", "2026-01-20", -1234),
                  ("electric", "2026-02-10", 8100)],
     "series": {"electric": {"2026-01": 6766, "2026-02": 8100}}, "texts": []},
    # 4. Drift back: March has two payments, February none; the earlier one moves back.
    {"name": "drift_back", "accounts": [ELECTRIC], "today": "2026-05-05",
     "payments": [("electric", "2026-01-28", 7000), ("electric", "2026-03-01", 7100),
                  ("electric", "2026-03-30", 7200), ("electric", "2026-04-29", 7300)],
     "series": {"electric": {"2026-01": 7000, "2026-02": 7100, "2026-03": 7200,
                             "2026-04": 7300}},
     "counted": {2: "2026-02", 3: "2026-03"}, "texts": []},
    # 5. Drift forward: January has two, February none, March one; the later one moves on.
    {"name": "drift_forward", "accounts": [ELECTRIC], "today": "2026-03-10",
     "payments": [("electric", "2026-01-02", 6000), ("electric", "2026-01-31", 6100),
                  ("electric", "2026-03-03", 6200)],
     "series": {"electric": {"2026-01": 6000, "2026-02": 6100, "2026-03": 6200}},
     "counted": {2: "2026-02"}, "texts": []},
    # 6. No neighbor inside the account's history: two payments stay in their month.
    {"name": "drift_edges", "accounts": [ELECTRIC], "today": "2026-02-05",
     "payments": [("electric", "2026-01-03", 6000), ("electric", "2026-01-30", 6100)],
     "series": {"electric": {"2026-01": 12100}}, "texts": []},
    # 7. A catch-up payment covering two months on a monthly account: spread, never moved.
    {"name": "covers_two", "accounts": [INTERNET], "today": "2026-03-10",
     "payments": [("internet", "2026-01-05", 5000), ("internet", "2026-03-05", 10000, 2)],
     "series": {"internet": {"2026-01": 5000, "2026-02": 5000, "2026-03": 5000}},
     "texts": []},
    # 8. Year over year, up 38%; the payment is also 1.4x the same month a year earlier.
    {"name": "yoy_up", "accounts": [GAS], "today": "2026-01-20",
     "payments": monthly("gas", "2025-01", [10000] + [5000] * 11 + [13800]),
     "series": {"gas": {"2025-01": 10000, "2025-06": 5000, "2026-01": 13800}},
     "texts": ["Gas payment 1.4× your usual for a month.",
               "Gas cost 38% more this January than last ($138.00 against $100.00)."]},
    # 9. Year over year, down 30%: not unusual (only high payments are).
    {"name": "yoy_down", "accounts": [GAS], "today": "2026-01-20",
     "payments": monthly("gas", "2025-01", [10000] + [5000] * 11 + [7000]),
     "texts": ["Gas cost 30% less this January than last ($70.00 against $100.00)."]},
    # 10. Year over year under the 10% threshold: nothing.
    {"name": "yoy_small", "accounts": [GAS], "today": "2026-01-20",
     "payments": monthly("gas", "2025-01", [10000] + [5000] * 11 + [10500]), "texts": []},
    # 11. Without a year of history: no year-over-year, no seasonal baseline.
    {"name": "yoy_no_history", "accounts": [GAS], "today": "2026-01-20",
     "payments": monthly("gas", "2025-02", [5000] * 11 + [13800]), "texts": []},
    # 12. A flat bill changes; a one-time spike before it is ignored as the old level.
    {"name": "flat_change_spike", "accounts": [INTERNET], "today": "2026-04-20",
     "payments": monthly("internet", "2025-07", [5000] * 6 + [9000, 5000, 6000, 6000], 5),
     "texts": ["Internet went from $50.00 to $60.00 a month with the March payment.",
               "Internet payment 1.8× your usual for a month."]},
    # 13. A single spike, then back to normal: no change, only the unusual payment.
    {"name": "flat_spike_only", "accounts": [INTERNET], "today": "2026-04-10",
     "payments": monthly("internet", "2025-10", [5000] * 4 + [9000, 5000, 5000], 5),
     "texts": ["Internet payment 1.8× your usual for a month."]},
    # 14. Two changes: the latest one is reported, from the level just before it.
    {"name": "flat_two_changes", "accounts": [PHONE], "today": "2026-06-10",
     "payments": monthly("phone", "2026-01", [5000, 5000, 6000, 6000, 7000, 7000], 5),
     "texts": ["Phone went from $60.00 to $70.00 a month with the May payment."]},
    # 15. An unusual quarterly water payment, compared with the same quarter a year earlier;
    #     year over year and seasonality from the spread months.
    {"name": "water_quarter", "accounts": [WATER], "today": "2026-03-20",
     "payments": [("water", f"{m}-15", c, 3) for m, c in (
         ("2025-03", 9000), ("2025-06", 9000), ("2025-09", 9000), ("2025-12", 9000),
         ("2026-03", 18900))],
     "series": {"water": {"2025-01": 3000, "2025-12": 3000, "2026-01": 6300,
                          "2026-03": 6300}},
     "texts": ["Water payment 2.1× your usual for a quarter.",
               "Water cost 110% more this March than last ($63.00 against $30.00).",
               "November to March are 54% of a year's water cost."]},
    # 16. Missing: a cadence plus the grace days have passed.
    {"name": "missing", "accounts": [GAS], "today": "2026-05-25",
     "payments": monthly("gas", "2026-01", [5000, 5200, 4800, 5100], 8),
     "texts": ["No gas payment since Apr 8; one usually comes every 31 days."]},
    # 17. Not missing past active_to.
    {"name": "not_missing_after_end",
     "accounts": [Account("gas", "gas", "Example Gas", active_to=date(2026, 4, 30))],
     "today": "2026-05-25", "payments": monthly("gas", "2026-01", [5000, 5200, 4800, 5100], 8),
     "texts": []},
    # 18. A possible double payment.
    {"name": "double", "accounts": [ELECTRIC], "today": "2026-01-10",
     "payments": [("electric", "2025-12-03", 6900), ("electric", "2026-01-03", 7000),
                  ("electric", "2026-01-05", 7000)],
     "texts": ["Paid the electric bill $70.00 twice, on Jan 3 and Jan 5."]},
    # 19. A provider switch; the old account ended, so it isn't missing.
    {"name": "switch", "accounts": [PHONE_OLD, PHONE], "today": "2025-12-10",
     "payments": monthly("phone_old", "2025-06", [3000] * 4, 5)
     + [("phone", "2025-10-01", 4500), ("phone", "2025-11-01", 4500),
        ("phone", "2025-12-01", 4500)],
     "texts": ["Phone: new provider since October; $30.00 a month before, $45.00 since."]},
    # 20. A first payment, long after the paying account's history starts.
    {"name": "first_payment", "accounts": [INTERNET], "today": "2025-06-10",
     "history": {"checking": "2025-01-01"},
     "payments": [("internet", "2025-06-03", 6500)],
     "texts": ["First payment: it may include setup charges or a partial month."]},
    # 21. The ledger simply starts there: not a first payment.
    {"name": "ledger_start", "accounts": [INTERNET], "today": "2025-06-10",
     "history": {"checking": "2025-06-01"},
     "payments": [("internet", "2025-06-03", 6500)], "texts": []},
    # 22. A first payment with setup charges stays out of the baselines.
    {"name": "first_out_of_baseline", "accounts": [INTERNET], "today": "2025-09-10",
     "history": {"checking": "2025-01-01"},
     "payments": monthly("internet", "2025-06", [9900, 5000, 5000, 5000], 3), "texts": []},
    # 23. A budget plan: the same gas payment 5 months running.
    {"name": "budget", "accounts": [GAS], "today": "2026-01-15",
     "payments": monthly("gas", "2025-08", [4000] + [6000] * 5),
     "texts": ["The same gas payment 5 months running: a budget plan evens out the seasons, "
               "so these payments don't show your use."]},
    # 24. Seasonality: May to September carry the year's electricity.
    {"name": "seasonality", "accounts": [ELECTRIC], "today": "2026-01-05",
     "payments": monthly("electric", "2025-01", [5000] * 4 + [10000] + [20000] * 4
                         + [5000] * 3),
     "texts": ["May to September are 72% of a year's electric cost."]},
]


@pytest.mark.parametrize("case", GOLDEN, ids=[c["name"] for c in GOLDEN])
def test_golden(case):
    history = {k: d(v) for k, v in case.get("history", {}).items()}
    res = compute(case["accounts"], build(case["payments"]), S, d(case["today"]),
                  history_start=history)
    assert [i.text for i in res.insights] == case["texts"]
    for acct, expected in case.get("series", {}).items():
        series = res.accounts[acct].series
        assert {m: series[m] for m in expected} == expected
        assert sum(series.values()) == sum(p.amount_cents for p in build(case["payments"])
                                           if p.account_id == acct)
    for txn, month in case.get("counted", {}).items():
        placed = next(pl for v in res.accounts.values() for pl in v.placed
                      if pl.payment.txn_id == txn)
        assert placed.counted_in == month


def test_enough_golden_scenarios():
    assert len(GOLDEN) >= 20


@pytest.mark.parametrize(("amount", "n", "parts"), [
    (10000, 3, [3334, 3333, 3333]), (10001, 3, [3334, 3334, 3333]), (9, 12, [1] * 9 + [0] * 3),
    (-1000, 3, [-334, -333, -333]), (500, 1, [500]),
])
def test_spread_adds_up(amount, n, parts):
    assert spread(amount, n) == parts and sum(parts) == amount


def test_totals_and_cadence():
    rows = (monthly("phone_old", "2025-06", [3000] * 4, 5)
            + [("phone", "2025-10-01", 4500), ("electric", "2025-10-07", 8000)])
    res = compute([PHONE_OLD, PHONE, ELECTRIC], build(rows), S, d("2025-10-20"))
    assert res.by_service["phone"]["2025-09"] == 3000
    assert res.by_service["phone"]["2025-10"] == 4500
    assert res.total["2025-10"] == 12500
    old = res.accounts["phone_old"]
    assert old.cadence_days == 31 and not old.missing  # ended in September
    new = res.accounts["phone"]
    assert new.cadence_days == 30 and new.next_expected == d("2025-10-31")
    assert res.months[0] == "2023-11" and res.months[-1] == "2025-10"  # 24 months


def test_window_and_ranking():
    """Insights outside the window drop out; ties rank warning before notice."""
    rows = [("electric", "2024-01-03", 7000), ("electric", "2024-01-05", 7000)]
    res = compute([ELECTRIC], build(rows), S, d("2026-06-01"), months=12)
    assert [i.kind for i in res.insights] == ["missing"]
    res = compute([ELECTRIC], build(rows), S, d("2024-01-10"))
    assert [i.kind for i in res.insights] == ["double_payment"]
    assert res.insights[0].links == [1, 2] and res.insights[0].severity == "warning"
