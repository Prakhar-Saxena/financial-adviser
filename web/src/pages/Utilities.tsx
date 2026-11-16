// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useQuery } from "@tanstack/react-query";
import { CalendarDays, Gauge, Receipt, TrendingUp } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Meta, type UtilitiesSummary } from "@/api";
import { Accounts, Insights } from "@/components/utilities/Accounts";
import { AccountPanel, Unassigned } from "@/components/utilities/AccountPanel";
import { MonthlyCost } from "@/components/utilities/MonthlyCost";
import { shortDate } from "@/components/utilities/shared";
import { YearOverYear } from "@/components/utilities/YearOverYear";
import { Card, CardContent } from "@/components/ui/card";
import { cn, money, monthLabel } from "@/lib/utils";
import { Stat } from "@/pages/Overview";

const pct = (a: number, b: number) => `${a >= b ? "+" : ""}${Math.round(((a - b) / b) * 100)}%`;

/** Utilities: what each service costs per month, from the payments in the ledger
 * (docs/SPEC-utilities.md §10). The header's month picker sets the last month shown. */
export function Utilities({ month, meta }: { month: string; meta: Meta }) {
  const [params, setParams] = useSearchParams();
  const [range, setRange] = useState(24);
  const { data, isFetching } = useQuery({
    queryKey: ["utilities", month, range], queryFn: () => api.utilities(month, range),
    placeholderData: (p) => p,
  });
  const account = params.get("account");
  const openAccount = (id: string | null) => {
    setParams(id ? { account: id } : {});
    if (id) window.scrollTo({ top: 0, behavior: "smooth" });
  };
  if (!data) return <p className="text-sm text-muted">Loading…</p>;
  if (!data.accounts.length) {
    return (
      <p className="text-sm text-ink-2">
        No utility accounts yet. Add them to <code>config/accounts.json</code> under <code>utility_accounts</code>{" "}
        (docs/SPEC-utilities.md §5), then run <code>fin init</code> and <code>fin process</code>.
      </p>
    );
  }
  return (
    <div className={cn("space-y-4 transition-opacity", isFetching && "opacity-60")}>
      <Tiles data={data} />
      {account && (
        <AccountPanel id={account} until={data.until} accounts={data.accounts} onClose={() => openAccount(null)} />
      )}
      <Unassigned meta={meta} />
      <div className="grid gap-4 lg:grid-cols-2">
        <MonthlyCost data={data} range={range} onRange={setRange} onAccount={openAccount} />
        <YearOverYear data={data} />
      </div>
      <Insights items={data.insights} accounts={data.accounts} onAccount={openAccount} />
      <Accounts accounts={data.accounts} onAccount={openAccount} />
      <Footer data={data} />
    </div>
  );
}

function Tiles({ data }: { data: UtilitiesSummary }) {
  const s = data.stats;
  const since = data.data_from && data.data_from > data.months[0] ? data.data_from : null;
  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      <Stat label="Utilities, last 12 months" value={money(s.last_12_cents, { whole: true })} icon={Receipt}
            tint="var(--cat-1)"
            sub={s.prev_12_cents ? `${pct(s.last_12_cents, s.prev_12_cents)} against the 12 before`
              : since ? `Payments since ${monthLabel(since)}` : undefined} />
      <Stat label={`Utilities, ${monthLabel(s.month)}`} value={money(s.month_cents, { whole: true })}
            icon={CalendarDays} tint="var(--cat-2)"
            sub={s.month_year_ago_cents ? `${pct(s.month_cents, s.month_year_ago_cents)} against a year ago`
              : s.average_month_cents ? `${pct(s.month_cents, s.average_month_cents)} against the average month`
              : undefined} />
      <Stat label="Average month" icon={Gauge} tint="var(--cat-3)"
            value={s.average_month_cents == null ? "—" : money(s.average_month_cents, { whole: true })}
            sub="Each current account, over the months it covers" />
      <Card>
        <CardContent className="flex items-start gap-3 pt-4">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl"
                style={{ background: "color-mix(in srgb, var(--cat-7) 16%, transparent)", color: "var(--cat-7)" }}>
            <TrendingUp className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <div className="text-xs text-muted">Biggest change</div>
            <div className="mt-0.5 text-sm font-medium text-ink">{s.biggest_change ?? "Nothing stands out yet"}</div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function Footer({ data }: { data: UtilitiesSummary }) {
  return (
    <div className="space-y-2 text-xs text-ink-2">
      <ul className="space-y-0.5">
        {data.accounts.filter((a) => !a.ended).map((a) => (
          <li key={a.id}>
            <span className="text-ink">{a.provider}</span>:{" "}
            {a.last_payment ? `Last payment ${shortDate(a.last_payment.date)}` : "No payments yet"}
            {a.next_expected && ` · next expected around ${shortDate(a.next_expected)}`}
            {a.missing && " · late"}
          </li>
        ))}
      </ul>
      <p className="text-muted">
        These are payments from the cards and accounts this app tracks, counted in the month they left. A utility
        paid from anywhere else doesn't show here, and payments can't tell using more apart from paying more per unit.
      </p>
    </div>
  );
}
