// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { UtilitiesSummary } from "@/api";
import { ViewSwitch } from "@/components/ChartCard";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { listColors } from "@/lib/palette";
import { cn, money, monthLabel } from "@/lib/utils";
import { SERVICE_LABELS, serviceColors } from "./shared";

export const RANGES = [{ months: 12, label: "12 months" }, { months: 24, label: "24 months" },
                       { months: 36, label: "36 months" }, { months: 240, label: "All" }];

type Series = { key: string; label: string; color: string };

/** Monthly cost, stacked by service or by account (SPEC-utilities §10.2). */
export function MonthlyCost({ data, range, onRange, onAccount }: {
  data: UtilitiesSummary; range: number; onRange: (m: number) => void;
  onAccount: (id: string) => void;
}) {
  const [by, setBy] = useState<"service" | "account">("service");
  const [view, setView] = useState<"bar" | "table">("bar");
  const accountsOf = (svc: string) => data.accounts.filter((a) => a.service === svc);
  const series: Series[] = by === "service"
    ? (() => {
        const c = serviceColors(data.services.map((s) => ({
          key: s, cents: data.rows.reduce((t, r) => t + (r.by_service[s] ?? 0), 0) })));
        return data.services.map((s) => ({ key: s, label: SERVICE_LABELS[s] ?? s, color: c.color(s) }));
      })()
    : (() => {
        const ids = data.accounts.map((a) => a.id);
        const c = listColors(ids, ids.map((id) => ({
          key: id, cents: data.rows.reduce((t, r) => t + (r.by_account[id] ?? 0), 0) })));
        return data.accounts.map((a) => ({ key: a.id, label: a.provider, color: c.color(a.id) }));
      })();
  const rows = data.rows.map((r) => ({
    month: r.month, label: monthLabel(r.month), total: r.total,
    ...(by === "service" ? r.by_service : r.by_account),
  }));
  // A service segment opens its account; with several, the current one.
  const open = (key: string) => {
    if (by === "account") return onAccount(key);
    const list = accountsOf(key);
    const pick = list.find((a) => !a.ended) ?? list[0];
    if (pick) onAccount(pick.id);
  };
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Monthly cost</CardTitle>
          <div className="mt-0.5 text-xs text-muted">By payment month: a payment usually pays for the month before.</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-lg border border-line bg-surface-2 p-0.5" role="group" aria-label="Stack by">
            {(["service", "account"] as const).map((b) => (
              <button key={b} onClick={() => setBy(b)} aria-pressed={by === b}
                      className={cn("rounded-md px-2 py-1 text-xs text-ink-2 hover:text-ink",
                                    by === b && "bg-surface text-ink shadow-sm")}>
                By {b}
              </button>
            ))}
          </div>
          <Select value={range} onChange={(e) => onRange(Number(e.target.value))} aria-label="Range">
            {RANGES.map((r) => <option key={r.months} value={r.months}>{r.label}</option>)}
          </Select>
          <ViewSwitch views={["bar", "table"]} view={view} onChange={(v) => setView(v as "bar" | "table")} />
        </div>
      </CardHeader>
      <CardContent>
        <Legend series={series} />
        {view === "table" ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted">
                <tr><th className="py-1 text-left font-normal">Month</th>
                  {series.map((s) => <th key={s.key} className="py-1 text-right font-normal">{s.label}</th>)}
                  <th className="py-1 text-right font-normal">Total</th></tr>
              </thead>
              <tbody className="tabular">
                {[...rows].reverse().map((r) => (
                  <tr key={r.month} className="border-t border-line">
                    <td className="py-1">{r.label}</td>
                    {series.map((s) => <td key={s.key} className="py-1 text-right">
                      {money(Number((r as Record<string, unknown>)[s.key] ?? 0))}</td>)}
                    <td className="py-1 text-right font-medium">{money(r.total)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div style={{ height: 260 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 8 }} barCategoryGap="20%">
                <CartesianGrid vertical={false} stroke="var(--grid)" />
                <XAxis dataKey="label" tickLine={false} axisLine={{ stroke: "var(--axis)" }}
                       tick={{ fill: "var(--ink-2)", fontSize: 11 }} minTickGap={12} />
                <YAxis tickLine={false} axisLine={false} width={56} tick={{ fill: "var(--muted)", fontSize: 11 }}
                       tickFormatter={(v: number) => money(v, { whole: true })} />
                <Tooltip cursor={{ fill: "var(--surface-2)" }}
                         content={({ active, payload }) => active && payload?.length ? (
                           <StackTip label={payload[0].payload.label as string} total={payload[0].payload.total as number}
                                     items={series.map((s) => ({ ...s, cents: Number(payload[0].payload[s.key] ?? 0) }))} />
                         ) : null} />
                {series.map((s) => (
                  // The surface-coloured stroke is the 2px gap between stacked segments.
                  <Bar key={s.key} dataKey={s.key} stackId="cost" fill={s.color} stroke="var(--surface)"
                       strokeWidth={2} maxBarSize={44} isAnimationActive={false} cursor="pointer"
                       onClick={() => open(s.key)} name={s.label} />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function Legend({ series }: { series: { key: string; label: string; color: string; muted?: boolean }[] }) {
  if (series.length < 2) return null;
  return (
    <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
      {series.map((s) => (
        <span key={s.key} className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} />{s.label}
        </span>
      ))}
    </div>
  );
}

function StackTip({ label, total, items }: {
  label: string; total: number; items: (Series & { cents: number })[];
}) {
  return (
    <div className="rounded-md border border-line bg-surface px-2.5 py-1.5 text-xs shadow">
      <div className="mb-1 text-ink-2">{label}</div>
      {items.filter((i) => i.cents).map((i) => (
        <div key={i.key} className="flex items-center justify-between gap-4">
          <span className="flex items-center gap-1.5 text-ink-2">
            <span className="h-2 w-2 rounded-full" style={{ background: i.color }} />{i.label}
          </span>
          <span className="tabular text-ink">{money(i.cents)}</span>
        </div>
      ))}
      <div className="mt-1 flex justify-between gap-4 border-t border-line pt-1 font-medium">
        <span>Total</span><span className="tabular">{money(total)}</span>
      </div>
    </div>
  );
}
