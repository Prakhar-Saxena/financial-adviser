# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Monthly series, baselines and insights for utility accounts (docs/SPEC-utilities.md §8).

Pure Python, no I/O, so golden tests can pin it. Cents everywhere. Months are "YYYY-MM".

- A payment counts in its payment month, spread evenly over `covers_months` months with
  largest-remainder rounding (the extra cents go to the earliest months): backward from the
  payment month, or forward for a prepaid plan. A refund nets in its own month.
- Drift: on a monthly account, when a month gets two payments and a neighbor inside the
  account's history gets none, the earlier one moves back or the later one forward.
- A "first payment" is one that starts an account: it comes at least 45 days after the paying
  card's or account's history starts in the ledger, or within 60 days of `active_from`. The
  first payment of a ledger that simply starts there is an ordinary payment.
- Insights are computed, then worded by templates, then ranked by dollar impact, then severity.
"""

from __future__ import annotations

import calendar
import itertools
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

SEASONAL = ("electric", "gas", "water")
LABELS = {"electric": "Electric", "gas": "Gas", "water": "Water", "trash": "Trash",
          "internet": "Internet", "phone": "Phone", "other": "Other"}
SEVERITY_RANK = {"warning": 2, "notice": 1, "info": 0}
SWITCH_DAYS = 45       # a new account's first payment this close to another's last: a switch
NEW_ACCOUNT_DAYS = 45  # a first payment this long after its source's history starts is new
ACTIVE_FROM_DAYS = 60
BASELINE_PAYMENTS = 6
FIRST_PAYMENT_SHOWN_WHILE = 3  # the "first payment" insight shows until this many payments
SEASON_MONTHS = 5
SEASON_MIN_SHARE = 0.5
COVER_WORDS = {1: "a month", 2: "two months", 3: "a quarter", 12: "a year"}


@dataclass(frozen=True)
class Account:
    id: str
    service: str
    provider: str
    bill_every_months: int = 1
    paid_in_advance: bool = False
    active_from: date | None = None
    active_to: date | None = None
    in_config: bool = True

    @property
    def seasonal(self) -> bool:
        return self.service in SEASONAL


@dataclass(frozen=True)
class Payment:
    txn_id: int
    account_id: str
    day: date
    amount_cents: int
    covers_months: int = 1
    source: str = ""  # the card or bank account it was paid from

    @property
    def per_month(self) -> int:
        return round(self.amount_cents / self.covers_months)


@dataclass
class Placed:
    """Where a payment counts: its months and the cents in each, adding up to the payment."""

    payment: Payment
    months: list[str]
    parts: list[int]

    @property
    def counted_in(self) -> str:
        """Shown next to the payment: "2026-09", or "2026-07 to 2026-09" when spread."""
        if len(self.months) == 1:
            return self.months[0]
        return f"{self.months[0]} to {self.months[-1]}"


@dataclass
class Insight:
    kind: str
    severity: str  # info | notice | warning
    text: str
    service: str
    period: str  # YYYY-MM
    impact_cents: int = 0
    account_id: str | None = None  # None: about the whole service
    numbers: dict[str, int | float] = field(default_factory=dict)
    links: list[int] = field(default_factory=list)  # transaction ids


@dataclass
class AccountView:
    account: Account
    placed: list[Placed]
    series: dict[str, int]
    payments: list[Payment]  # positive ones, by date
    cadence_days: int
    first_is_new: bool
    last: Payment | None
    next_expected: date | None
    missing: bool


@dataclass
class Result:
    accounts: dict[str, AccountView]
    by_service: dict[str, dict[str, int]]
    total: dict[str, int]
    months: list[str]  # the window, oldest first
    insights: list[Insight]

    def for_account(self, account_id: str) -> list[Insight]:
        service = self.accounts[account_id].account.service
        return [i for i in self.insights if i.account_id == account_id
                or (i.account_id is None and i.service == service)]


# ------------------------------------------------------------------- months


def month_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def add_months(month: str, k: int) -> str:
    y, mo = int(month[:4]), int(month[5:7]) - 1 + k
    return f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"


def month_range(first: str, last: str) -> list[str]:
    out, m = [], first
    while m <= last:
        out.append(m)
        m = add_months(m, 1)
    return out


def month_name(month: str) -> str:
    return calendar.month_name[int(month[5:7])]


def short_day(d: date) -> str:
    return f"{calendar.month_abbr[d.month]} {d.day}"


def usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def median_cents(values: list[int]) -> int:
    return round(statistics.median(values))


# ------------------------------------------------------------------ series


def spread(amount: int, n: int) -> list[int]:
    """Split evenly into n parts that add up exactly; the extra cents go to the first parts."""
    sign = -1 if amount < 0 else 1
    base, extra = divmod(abs(amount), n)
    return [sign * (base + (1 if i < extra else 0)) for i in range(n)]


def place(account: Account, payments: list[Payment]) -> list[Placed]:
    out = []
    for p in sorted(payments, key=lambda p: (p.day, p.txn_id)):
        m = month_of(p.day)
        if p.amount_cents < 0:  # a refund nets in its own month
            out.append(Placed(p, [m], [p.amount_cents]))
            continue
        n = p.covers_months
        offsets = range(n) if account.paid_in_advance else range(1 - n, 1)
        out.append(Placed(p, [add_months(m, k) for k in offsets], spread(p.amount_cents, n)))
    if account.bill_every_months == 1:
        _fix_drift(out)
    return out


def _fix_drift(placed: list[Placed]) -> None:
    """Two payments in one month and none in a neighbor: the nearer one moves there."""
    movable = [pl for pl in placed if pl.payment.amount_cents > 0 and len(pl.months) == 1]
    if not movable:
        return
    by_month: dict[str, list[Placed]] = defaultdict(list)
    for pl in movable:
        by_month[pl.months[0]].append(pl)
    first, last = min(by_month), max(by_month)
    for m in month_range(first, last):
        here = by_month[m]
        if len(here) < 2:
            continue
        prev, nxt = add_months(m, -1), add_months(m, 1)
        if prev >= first and not by_month[prev]:
            moved = min(here, key=lambda pl: (pl.payment.day, pl.payment.txn_id))
            target = prev
        elif nxt <= last and not by_month[nxt]:
            moved = max(here, key=lambda pl: (pl.payment.day, pl.payment.txn_id))
            target = nxt
        else:
            continue
        here.remove(moved)
        moved.months = [target]
        by_month[target].append(moved)


def series_of(placed: list[Placed]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for pl in placed:
        for m, c in zip(pl.months, pl.parts, strict=True):
            out[m] += c
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- per account


def view(account: Account, payments: list[Payment], history_start: dict[str, date],
         today: date, grace_days: int) -> AccountView:
    placed = place(account, payments)
    pos = sorted((p for p in payments if p.amount_cents > 0), key=lambda p: (p.day, p.txn_id))
    recent = pos[-BASELINE_PAYMENTS:]
    gaps = [(b.day - a.day).days for a, b in itertools.pairwise(recent)]
    cadence = round(statistics.median(gaps)) if gaps else 30 * account.bill_every_months
    first_is_new = False
    if pos:
        p0 = pos[0]
        start = history_start.get(p0.source)
        first_is_new = bool(
            (start is not None and (p0.day - start).days >= NEW_ACCOUNT_DAYS)
            or (account.active_from is not None
                and 0 <= (p0.day - account.active_from).days <= ACTIVE_FROM_DAYS))
    last = pos[-1] if pos else None
    nxt = last.day + timedelta(days=cadence) if last else None
    active = account.in_config and (account.active_to is None or today <= account.active_to)
    missing = bool(active and nxt and today > nxt + timedelta(days=grace_days)
                   and (account.active_to is None or nxt <= account.active_to))
    return AccountView(account, placed, series_of(placed), pos, cadence, first_is_new, last,
                       nxt, missing)


def baseline_payments(v: AccountView, before: Payment) -> list[Payment]:
    """Payments before `before`, without a first payment, the last 6."""
    pos = v.payments[1:] if v.first_is_new else v.payments
    return [p for p in pos if (p.day, p.txn_id) < (before.day, before.txn_id)][-BASELINE_PAYMENTS:]


# ---------------------------------------------------------------- insights


def flat_step(v: AccountView, settings, window: set[str]) -> Insight | None:
    """The latest change of a flat bill that holds for `step_min_payments` payments."""
    pays = v.payments[1:] if v.first_is_new else v.payments
    vals = [p.per_month for p in pays]
    k, tol = settings.step_min_payments, settings.step_min_cents
    for i in range(len(vals) - 1, 0, -1):
        run = _run(vals[i:], vals[i], tol)
        if len(run) < k or abs(vals[i - 1] - vals[i]) < tol:
            continue
        back = _run(vals[max(0, i - BASELINE_PAYMENTS):i][::-1], vals[i - 1], tol)
        before = back if len(back) >= k else vals[max(0, i - BASELINE_PAYMENTS):i]
        old, new = median_cents(before), median_cents(run)
        if abs(new - old) < tol or month_of(pays[i].day) not in window:
            continue
        svc = v.account.service
        return Insight(
            "flat_change", "notice" if new > old else "info",
            f"{LABELS[svc]} went from {usd(old)} to {usd(new)} a month with the "
            f"{month_name(month_of(pays[i].day))} payment.",
            svc, month_of(pays[i].day), abs(new - old) * 12, v.account.id,
            {"before_cents": old, "after_cents": new}, [pays[i].txn_id])
    return None


def _run(vals: list[int], anchor: int, tol: int) -> list[int]:
    """The leading values within `tol` of `anchor`."""
    out = []
    for x in vals:
        if abs(x - anchor) >= tol:
            break
        out.append(x)
    return out


def year_over_year(service: str, series: dict[str, int], ref: str, settings) -> Insight | None:
    first = min(series)
    prev = add_months(ref, -12)
    cur, old = series.get(ref, 0), series.get(prev, 0)
    if prev < first or old <= 0:
        return None
    change = (cur - old) / old
    if abs(change) < settings.yoy_min_change or abs(cur - old) < settings.yoy_min_cents:
        return None
    up = cur > old
    return Insight(
        "year_over_year", "notice" if up else "info",
        f"{LABELS[service]} cost {round(abs(change) * 100)}% {'more' if up else 'less'} this "
        f"{month_name(ref)} than last ({usd(cur)} against {usd(old)}).",
        service, ref, abs(cur - old), None,
        {"this_cents": cur, "last_cents": old, "change": round(change, 4)})


def unusual(v: AccountView, settings, window: set[str]) -> list[Insight]:
    out = []
    first = v.payments[0] if v.first_is_new and v.payments else None
    by_txn = {pl.payment.txn_id: pl for pl in v.placed}
    for p in v.payments:
        if p is first or month_of(p.day) not in window:
            continue
        if v.account.seasonal:
            earlier = [add_months(m, -12) for m in by_txn[p.txn_id].months]
            if not v.series or min(earlier) < min(v.series):
                continue
            usual = sum(v.series.get(m, 0) for m in earlier)
        else:
            prior = baseline_payments(v, p)
            if len(prior) < 2:
                continue
            usual = median_cents([q.per_month for q in prior]) * p.covers_months
        if usual <= 0 or p.amount_cents < settings.outlier_ratio * usual:
            continue
        ratio = p.amount_cents / usual
        svc = v.account.service
        words = COVER_WORDS.get(p.covers_months, f"{p.covers_months} months")
        out.append(Insight(
            "unusual_payment", "warning" if ratio >= 2 else "notice",
            f"{LABELS[svc]} payment {ratio:.1f}× your usual for {words}.",
            svc, month_of(p.day), p.amount_cents - usual, v.account.id,
            {"amount_cents": p.amount_cents, "usual_cents": usual, "ratio": round(ratio, 2)},
            [p.txn_id]))
    return out


def missing(v: AccountView) -> Insight | None:
    if not v.missing or v.last is None:
        return None
    svc = v.account.service
    return Insight(
        "missing", "warning",
        f"No {svc} payment since {short_day(v.last.day)}; one usually comes every "
        f"{v.cadence_days} days.",
        svc, month_of(v.next_expected), v.last.amount_cents, v.account.id,
        {"cadence_days": v.cadence_days}, [v.last.txn_id])


def doubles(v: AccountView, settings, window: set[str]) -> list[Insight]:
    out = []
    pays = v.payments
    for i, a in enumerate(pays):
        for b in pays[i + 1:]:
            if (b.day - a.day).days > settings.duplicate_days:
                break
            if b.amount_cents == a.amount_cents and month_of(b.day) in window:
                out.append(Insight(
                    "double_payment", "warning",
                    f"Paid the {v.account.service} bill {usd(a.amount_cents)} twice, on "
                    f"{short_day(a.day)} and {short_day(b.day)}.",
                    v.account.service, month_of(b.day), a.amount_cents, v.account.id,
                    {"amount_cents": a.amount_cents}, [a.txn_id, b.txn_id]))
    return out


def switches(views: list[AccountView], window: set[str]) -> list[Insight]:
    out = []
    for new in views:
        if not new.payments:
            continue
        f = new.payments[0]
        for old in views:
            if old is new or old.account.service != new.account.service or not old.payments:
                continue
            if not (old.payments[0].day < f.day and abs((f.day - old.last.day).days)
                    <= SWITCH_DAYS) or month_of(f.day) not in window:
                continue
            before = median_cents([p.per_month for p in old.payments[-BASELINE_PAYMENTS:]])
            since_pays = new.payments[1:] or new.payments
            since = median_cents([p.per_month for p in since_pays])
            svc = new.account.service
            out.append(Insight(
                "provider_switch", "info",
                f"{LABELS[svc]}: new provider since {month_name(month_of(f.day))}; "
                f"{usd(before)} a month before, {usd(since)} since.",
                svc, month_of(f.day), abs(since - before) * 12, new.account.id,
                {"before_cents": before, "since_cents": since, "previous": old.account.id},
                [f.txn_id]))
    return out


def first_payment(v: AccountView, window: set[str]) -> Insight | None:
    if not (v.first_is_new and v.payments and len(v.payments) < FIRST_PAYMENT_SHOWN_WHILE):
        return None
    p = v.payments[0]
    if month_of(p.day) not in window:
        return None
    return Insight("first_payment", "info",
                   "First payment: it may include setup charges or a partial month.",
                   v.account.service, month_of(p.day), 0, v.account.id, {}, [p.txn_id])


def budget_plan(v: AccountView, settings, window: set[str]) -> Insight | None:
    if not v.account.seasonal:
        return None
    monthly = [pl for pl in v.placed if pl.payment.amount_cents > 0 and len(pl.months) == 1]
    if not monthly:
        return None
    run = [monthly[-1]]
    for pl in reversed(monthly[:-1]):
        nxt = run[-1]
        if (pl.payment.amount_cents == nxt.payment.amount_cents
                and add_months(pl.months[0], 1) == nxt.months[0]):
            run.append(pl)
        else:
            break
    if len(run) < settings.budget_min_months or run[0].months[0] not in window:
        return None
    svc = v.account.service
    return Insight(
        "budget_plan", "info",
        f"The same {svc} payment {len(run)} months running: a budget plan evens out the "
        "seasons, so these payments don't show your use.",
        svc, run[0].months[0], 0, v.account.id,
        {"months": len(run), "amount_cents": run[0].payment.amount_cents},
        [pl.payment.txn_id for pl in reversed(run)])


def seasonality(service: str, series: dict[str, int], ref: str) -> Insight | None:
    start = add_months(ref, -11)
    if min(series) > start:
        return None
    year = [series.get(m, 0) for m in month_range(start, ref)]
    total = sum(year)
    if total <= 0:
        return None
    months = month_range(start, ref)
    best_share, best_i = -1.0, 0
    for i in range(12):
        share = sum(year[(i + k) % 12] for k in range(SEASON_MONTHS)) / total
        if share > best_share + 1e-12:
            best_share, best_i = share, i
    if best_share < SEASON_MIN_SHARE:
        return None
    a, b = months[best_i], months[(best_i + SEASON_MONTHS - 1) % 12]
    return Insight(
        "seasonality", "info",
        f"{month_name(a)} to {month_name(b)} are {round(best_share * 100)}% of a year's "
        f"{service} cost.",
        service, ref, 0, None, {"share": round(best_share, 4)})


# ------------------------------------------------------------------ compute


def compute(accounts: list[Account], payments: list[Payment], settings, today: date,
            until: str | None = None, months: int | None = None,
            history_start: dict[str, date] | None = None) -> Result:
    """Everything for the tab and the CLI. `until` is the last month shown (default: today's),
    `months` the window's length (default: settings.default_months)."""
    until = until or month_of(today)
    months = months or settings.default_months
    window_list = month_range(add_months(until, 1 - months), until)
    window = set(window_list)
    by_acct: dict[str, list[Payment]] = defaultdict(list)
    for p in payments:
        if month_of(p.day) <= until:
            by_acct[p.account_id].append(p)
    views = {a.id: view(a, by_acct[a.id], history_start or {}, today,
                        settings.stale_grace_days) for a in accounts}

    by_service: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total: dict[str, int] = defaultdict(int)
    for v in views.values():
        for m, c in v.series.items():
            by_service[v.account.service][m] += c
            total[m] += c

    insights: list[Insight] = []
    for v in views.values():
        insights += [i for i in (missing(v), first_payment(v, window),
                                 budget_plan(v, settings, window)) if i]
        if not v.account.seasonal:
            step = flat_step(v, settings, window)
            insights += [step] if step else []
        insights += unusual(v, settings, window) + doubles(v, settings, window)
    insights += switches(list(views.values()), window)
    for svc, series in by_service.items():
        if svc not in SEASONAL or not series:
            continue
        ref = max(m for m in series if m <= until)
        if ref not in window:
            continue
        insights += [i for i in (year_over_year(svc, series, ref, settings),
                                 seasonality(svc, series, ref)) if i]
    insights.sort(key=lambda i: (-abs(i.impact_cents), -SEVERITY_RANK[i.severity], i.kind,
                                 i.account_id or "", i.period))
    return Result(views, {s: dict(sorted(v.items())) for s, v in by_service.items()},
                  dict(sorted(total.items())), window_list, insights)
