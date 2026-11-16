// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { UtilitiesSummary } from "@/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { SINGLE } from "@/lib/palette";
import { money, monthLabel } from "@/lib/utils";
import { Legend } from "./MonthlyCost";
import { SERVICE_LABELS, serviceColors } from "./shared";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const EARLIER = "var(--cat-other)";

/** One line per year on a January–December axis: this year in colour, earlier years muted
 * (the emphasis form). Appears once a year of payments exists (SPEC-utilities §10.3). */
export function YearOverYear({ data }: { data: UtilitiesSummary }) {
  const [service, setService] = useState("all");
  const thisYear = data.until.slice(0, 4);
  const series: Record<string, number> = {};
  for (const [svc, months] of Object.entries(data.history)) {
    if (service !== "all" && svc !== service) continue;
    for (const [m, c] of Object.entries(months)) series[m] = (series[m] ?? 0) + c;
  }
  const years = [...new Set(Object.keys(series).map((m) => m.slice(0, 4)))]
    .filter((y) => y <= thisYear).sort();
  const rows = MONTHS.map((label, i) => {
    const row: Record<string, string | number | null> = { label };
    for (const y of years) {
      const m = `${y}-${String(i + 1).padStart(2, "0")}`;
      row[y] = m <= data.until && m in series ? series[m] : null;
    }
    return row;
  });
  const accent = service === "all" ? SINGLE : serviceColors(data.services.map((s) => ({ key: s, cents: 1 }))).color(service);
  const legend = years.map((y) => ({ key: y, label: y === thisYear ? `${y} (this year)` : y,
                                     color: y === thisYear ? accent : EARLIER }));
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Year over year</CardTitle>
          <div className="mt-0.5 text-xs text-muted">The same month in each year, by payment month.</div>
        </div>
        <Select value={service} onChange={(e) => setService(e.target.value)} aria-label="Service">
          <option value="all">All utilities</option>
          {data.services.map((s) => <option key={s} value={s}>{SERVICE_LABELS[s] ?? s}</option>)}
        </Select>
      </CardHeader>
      <CardContent>
        {data.yoy_from ? (
          <p className="text-sm text-ink-2">
            Appears from {monthLabel(data.yoy_from)}, once a year of payments is in the ledger.
          </p>
        ) : (
          <>
            <Legend series={legend} />
            <div style={{ height: 240 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: 8 }}>
                  <CartesianGrid vertical={false} stroke="var(--grid)" />
                  <XAxis dataKey="label" tickLine={false} axisLine={{ stroke: "var(--axis)" }}
                         tick={{ fill: "var(--ink-2)", fontSize: 11 }} />
                  <YAxis tickLine={false} axisLine={false} width={56} tick={{ fill: "var(--muted)", fontSize: 11 }}
                         tickFormatter={(v: number) => money(v, { whole: true })} />
                  <Tooltip cursor={{ stroke: "var(--axis)" }}
                           content={({ active, payload, label }) => active && payload?.length ? (
                             <div className="rounded-md border border-line bg-surface px-2.5 py-1.5 text-xs shadow">
                               <div className="mb-1 text-ink-2">{label}</div>
                               {[...payload].reverse().filter((p) => p.value != null).map((p) => (
                                 <div key={String(p.dataKey)} className="flex justify-between gap-4">
                                   <span className="text-ink-2">{String(p.dataKey)}</span>
                                   <span className="tabular text-ink">{money(Number(p.value))}</span>
                                 </div>
                               ))}
                             </div>) : null} />
                  {years.map((y) => (
                    <Line key={y} dataKey={y} stroke={y === thisYear ? accent : EARLIER}
                          strokeWidth={2} connectNulls={false} isAnimationActive={false}
                          dot={y === thisYear ? { r: 4, fill: accent, stroke: "var(--surface)", strokeWidth: 2 } : false} />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
