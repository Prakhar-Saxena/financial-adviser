// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Link } from "react-router-dom";
import { api, type Meta, type UnassignedUtility, type UtilityAccount } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { money, monthLabel } from "@/lib/utils";
import { Insights } from "./Accounts";
import { SERVICE_LABELS, serviceColors, shortDate } from "./shared";

const COVERS = [1, 2, 3, 4, 6, 12];

/** One account: its chart, insights and payments (SPEC-utilities §10.6). */
export function AccountPanel({ id, until, accounts, onClose }: {
  id: string; until: string; accounts: UtilityAccount[]; onClose: () => void;
}) {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["utility-account", id, until], queryFn: () => api.utilityAccount(id, until) });
  const covers = useMutation({
    mutationFn: ({ txn, n }: { txn: number; n: number }) => api.setUtilityCovers(txn, n),
    onSuccess: () => qc.invalidateQueries(),
  });
  if (!data) return <Card><CardContent className="pt-4 text-sm text-muted">Loading…</CardContent></Card>;
  const a = data.account;
  const color = serviceColors([{ key: a.service, cents: 1 }]).color(a.service);
  const rows = Object.entries(data.series).map(([m, c]) => ({ month: m, label: monthLabel(m), cents: c }));
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{a.provider}</CardTitle>
          <div className="mt-0.5 text-xs text-muted">
            {SERVICE_LABELS[a.service] ?? a.service}
            {a.bill_every_months > 1 && ` · billed every ${a.bill_every_months} months`}
            {a.paid_in_advance && " · paid in advance"}
            {a.ended && " · ended"}
          </div>
        </div>
        <Button size="icon" variant="ghost" onClick={onClose} aria-label="Close account"><X className="h-4 w-4" /></Button>
      </CardHeader>
      <CardContent className="space-y-4">
        {rows.length > 0 && (
          <div style={{ height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 8 }} barCategoryGap="25%">
                <CartesianGrid vertical={false} stroke="var(--grid)" />
                <XAxis dataKey="label" tickLine={false} axisLine={{ stroke: "var(--axis)" }}
                       tick={{ fill: "var(--ink-2)", fontSize: 11 }} minTickGap={12} />
                <YAxis tickLine={false} axisLine={false} width={56} tick={{ fill: "var(--muted)", fontSize: 11 }}
                       tickFormatter={(v: number) => money(v, { whole: true })} />
                <Tooltip cursor={{ fill: "var(--surface-2)" }}
                         content={({ active, payload }) => active && payload?.length ? (
                           <div className="rounded-md border border-line bg-surface px-2.5 py-1.5 text-xs shadow">
                             <div className="text-ink-2">{payload[0].payload.label}</div>
                             <div className="tabular font-medium">{money(payload[0].payload.cents)}</div>
                           </div>) : null} />
                <Bar dataKey="cents" fill={color} radius={[4, 4, 0, 0]} maxBarSize={36} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
        {data.insights.length > 0 && <Insights items={data.insights} accounts={accounts} limit={20} bare />}
        {a.best_card_query && (
          <p className="text-sm">
            <Link className="text-ink underline underline-offset-2" to={`/best-card?q=${encodeURIComponent(a.best_card_query)}`}>
              Best card for this bill
            </Link>
            <span className="ml-2 text-xs text-muted">Some providers charge a fee to pay by card.</span>
          </p>
        )}
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead className="text-xs text-muted">
              <tr>
                <th className="py-1.5 text-left font-normal">Date</th>
                <th className="py-1.5 text-right font-normal">Amount</th>
                <th className="py-1.5 pl-4 text-left font-normal">Paid from</th>
                <th className="py-1.5 text-left font-normal">Counted in</th>
                <th className="py-1.5 text-left font-normal">Covers</th>
                <th className="py-1.5 font-normal" />
              </tr>
            </thead>
            <tbody className="tabular">
              {data.payments.map((p) => (
                <tr key={p.transaction_id} className="border-t border-line">
                  <td className="py-1.5">{shortDate(p.date)} {p.date.slice(0, 4)}</td>
                  <td className="py-1.5 text-right">{money(p.amount_cents)}</td>
                  <td className="py-1.5 pl-4 text-ink-2">{p.paid_from}</td>
                  <td className="py-1.5 text-ink-2">
                    {p.months.length === 1 ? monthLabel(p.months[0])
                      : `${monthLabel(p.months[0])} – ${monthLabel(p.months[p.months.length - 1])}`}
                    {p.moved && <span className="ml-1 text-xs text-muted">(moved: two payments fell in one month)</span>}
                  </td>
                  <td className="py-1.5">
                    {p.amount_cents > 0 ? (
                      <Select value={p.covers_months} aria-label="Months this payment covers" className="h-8"
                              onChange={(e) => covers.mutate({ txn: p.transaction_id, n: Number(e.target.value) })}>
                        {[...new Set([...COVERS, p.covers_months])].sort((x, y) => x - y).map((n) =>
                          <option key={n} value={n}>{n} month{n > 1 ? "s" : ""}</option>)}
                      </Select>
                    ) : <span className="text-xs text-muted">refund</span>}
                  </td>
                  <td className="py-1.5 text-right">
                    <Link className="text-xs text-ink-2 underline-offset-2 hover:underline" to={`/transactions?txn=${p.transaction_id}`}>
                      Transaction
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

/** Payments in a bills category that match no account (SPEC-utilities §10.7). */
export function Unassigned({ meta }: { meta: Meta }) {
  const { data = [] } = useQuery({ queryKey: ["utility-unassigned"], queryFn: api.utilityUnassigned });
  if (!data.length) return null;
  return (
    <Card id="unassigned">
      <CardHeader>
        <div>
          <CardTitle>Payments to sort</CardTitle>
          <div className="mt-0.5 text-xs text-muted">Bill payments that match no utility account. Your choice survives a rebuild.</div>
        </div>
      </CardHeader>
      <CardContent className="divide-y divide-line">
        {data.map((r) => <UnassignedRow key={r.transaction_id} row={r} meta={meta} />)}
      </CardContent>
    </Card>
  );
}

function UnassignedRow({ row, meta }: { row: UnassignedUtility; meta: Meta }) {
  const qc = useQueryClient();
  const [account, setAccount] = useState("");
  const [all, setAll] = useState(false);
  const save = useMutation({
    mutationFn: (account_id: string) =>
      api.assignUtility({ transaction_id: row.transaction_id, account_id, all_from_merchant: all }),
    onSuccess: () => qc.invalidateQueries(),
  });
  return (
    <div className="flex flex-wrap items-center gap-3 py-2.5 text-sm">
      <span className="tabular text-ink-2">{row.date}</span>
      <Link className="text-ink underline-offset-2 hover:underline" to={`/transactions?txn=${row.transaction_id}`}>
        {row.merchant}
      </Link>
      <span className="tabular">{money(row.amount_cents)}</span>
      <span className="text-xs text-muted">{row.paid_from}{row.reason ? ` · ${row.reason}` : ""}</span>
      <div className="ml-auto flex flex-wrap items-center gap-2">
        <Select value={account} onChange={(e) => setAccount(e.target.value)} aria-label="Utility account">
          <option value="" disabled>Account…</option>
          {meta.utility_accounts.map((u) => (
            <option key={u.id} value={u.id}>{u.provider} ({SERVICE_LABELS[u.service] ?? u.service})</option>
          ))}
        </Select>
        <label className="flex items-center gap-1.5 text-xs text-ink-2">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
          Every payment from this merchant
        </label>
        <Button size="sm" variant="outline" disabled={!account || save.isPending} onClick={() => save.mutate(account)}>
          Assign
        </Button>
        <Button size="sm" variant="ghost" disabled={save.isPending} onClick={() => save.mutate("none")}>
          Not a utility
        </Button>
      </div>
    </div>
  );
}
