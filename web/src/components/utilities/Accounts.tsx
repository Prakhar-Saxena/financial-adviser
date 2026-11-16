// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

import { Line, LineChart, ResponsiveContainer } from "recharts";
import type { UtilityAccount, UtilityInsight } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { SINGLE } from "@/lib/palette";
import { money } from "@/lib/utils";
import { Chip, SERVICE_LABELS, Severity, shortDate } from "./shared";

/** The top insights, biggest dollar impact first (SPEC-utilities §10.4). */
export function Insights({ items, accounts, onAccount, limit = 5, bare = false }: {
  items: UtilityInsight[]; accounts: UtilityAccount[]; onAccount?: (id: string) => void; limit?: number;
  bare?: boolean; // a plain list, inside another card
}) {
  const target = (i: UtilityInsight) =>
    i.account_id ?? accounts.find((a) => a.service === i.service && !a.ended)?.id;
  const list = items.length === 0 ? (
    <p className="text-sm text-muted">Nothing stands out in this range.</p>
  ) : (
    <ul className="divide-y divide-line">
      {items.slice(0, limit).map((i, n) => {
        const id = target(i);
        return (
          <li key={n} className="flex flex-wrap items-center gap-3 py-2 text-sm">
            <Severity level={i.severity} />
            <span className="min-w-0 flex-1 text-ink">{i.text}</span>
            {onAccount && id && (
              <Button size="sm" variant="ghost" onClick={() => onAccount(id)}>See account</Button>
            )}
          </li>
        );
      })}
    </ul>
  );
  if (bare) return list;
  return (
    <Card>
      <CardHeader><CardTitle>Insights</CardTitle></CardHeader>
      <CardContent>{list}</CardContent>
    </Card>
  );
}

function chips(a: UtilityAccount) {
  return (
    <span className="flex flex-wrap gap-1">
      {a.missing && <Chip tone="warning">Missing</Chip>}
      {a.first_payment && <Chip>First payment</Chip>}
      {a.budget_plan && <Chip>Budget plan</Chip>}
      {a.payments === 0 && <Chip>No payments yet</Chip>}
    </span>
  );
}

function versus(a: UtilityAccount) {
  if (a.prev_12_cents == null || !a.prev_12_cents) return <span className="text-muted">—</span>;
  const pct = Math.round(((a.last_12_cents - a.prev_12_cents) / a.prev_12_cents) * 100);
  return <span>{pct > 0 ? "+" : ""}{pct}%</span>;
}

function Spark({ points }: { points: { month: string; cents: number }[] }) {
  // De-emphasis line, the latest month as the accent dot.
  const last = points.length - 1;
  return (
    <div className="h-7 w-24" aria-hidden>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 4, right: 4, bottom: 4, left: 4 }}>
          <Line dataKey="cents" stroke="var(--cat-other)" strokeWidth={1.5} isAnimationActive={false}
                dot={(p: { index?: number; cx?: number; cy?: number }) => p.index === last
                  ? <circle key="last" cx={p.cx} cy={p.cy} r={3} fill={SINGLE} stroke="var(--surface)" strokeWidth={1.5} />
                  : <g key={p.index} />} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Accounts (SPEC-utilities §10.5). Ended ones fold under "Earlier accounts". */
export function Accounts({ accounts, onAccount }: {
  accounts: UtilityAccount[]; onAccount: (id: string) => void;
}) {
  const current = accounts.filter((a) => !a.ended);
  const earlier = accounts.filter((a) => a.ended);
  return (
    <Card>
      <CardHeader><CardTitle>Accounts</CardTitle></CardHeader>
      <CardContent className="overflow-x-auto">
        <AccountTable rows={current} onAccount={onAccount} />
        {earlier.length > 0 && (
          <details className="mt-3">
            <summary className="cursor-pointer text-sm text-ink-2">Earlier accounts ({earlier.length})</summary>
            <AccountTable rows={earlier} onAccount={onAccount} />
          </details>
        )}
      </CardContent>
    </Card>
  );
}

function AccountTable({ rows, onAccount }: { rows: UtilityAccount[]; onAccount: (id: string) => void }) {
  if (!rows.length) return <p className="text-sm text-muted">No accounts.</p>;
  return (
    <table className="w-full min-w-[760px] text-sm">
      <thead className="text-xs text-muted">
        <tr>
          <th className="py-1.5 text-left font-normal">Service</th>
          <th className="py-1.5 text-left font-normal">Provider</th>
          <th className="py-1.5 text-left font-normal">Paid from</th>
          <th className="py-1.5 text-left font-normal">Last payment</th>
          <th className="py-1.5 text-right font-normal">Last 12 months</th>
          <th className="py-1.5 text-right font-normal">Average month</th>
          <th className="py-1.5 text-right font-normal">Against a year ago</th>
          <th className="py-1.5 font-normal" />
          <th className="py-1.5 font-normal" />
        </tr>
      </thead>
      <tbody className="tabular">
        {rows.map((a) => (
          <tr key={a.id} className="border-t border-line">
            <td className="py-2">{SERVICE_LABELS[a.service] ?? a.service}</td>
            <td className="py-2">
              <button className="text-left text-ink underline-offset-2 hover:underline" onClick={() => onAccount(a.id)}>
                {a.provider}
              </button>
            </td>
            <td className="py-2 text-ink-2">{a.paid_from.join(", ") || "—"}</td>
            <td className="py-2 text-ink-2">
              {a.last_payment ? `${shortDate(a.last_payment.date)} · ${money(a.last_payment.amount_cents)}` : "—"}
            </td>
            <td className="py-2 text-right">{money(a.last_12_cents)}</td>
            <td className="py-2 text-right">{a.average_month_cents == null ? "—" : money(a.average_month_cents)}</td>
            <td className="py-2 text-right">{versus(a)}</td>
            <td className="py-2 pl-3"><Spark points={a.sparkline} /></td>
            <td className="py-2">{chips(a)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
