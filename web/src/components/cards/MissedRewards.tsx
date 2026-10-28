// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

// "Missed rewards" (docs/SPEC-cards.md §13.2): where another card would have earned more,
// statement credits this period, and the cheat sheet. All computed locally; no AI.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type CreditUse } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { money } from "@/lib/utils";

export function MissedRewards() {
  return (
    <div className="space-y-4">
      <Tips />
      <Credits />
      <Cheatsheet />
    </div>
  );
}

function Tips() {
  const [months, setMonths] = useState(3);
  const { data } = useQuery({ queryKey: ["tips", months], queryFn: () => api.tips(months) });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Where another card would have earned more</CardTitle>
        <Select value={String(months)} onChange={(e) => setMonths(Number(e.target.value))} aria-label="Months">
          {[1, 3, 6, 12].map((m) => <option key={m} value={m}>Last {m} month{m > 1 ? "s" : ""}</option>)}
        </Select>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {!data ? <p className="text-muted">Loading…</p> : (
          <>
            <p className="text-xs text-muted">
              {data.since} to {data.until} · {data.estimate}
              {data.top.length > 0 && <> · about <span className="font-medium text-ink">{money(data.total_difference_cents)}</span> in all</>}
            </p>
            {data.top.length === 0 && <p className="text-ink-2">Nothing to improve: you used the best card everywhere.</p>}
            {data.top.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead className="text-left text-xs text-muted">
                    <tr><th className="py-1 pr-3 font-normal">Merchant</th><th className="pr-3 text-right font-normal">Purchases</th>
                        <th className="pr-3 text-right font-normal">Spend</th><th className="pr-3 font-normal">Card used</th>
                        <th className="pr-3 font-normal">Better card</th><th className="text-right font-normal">Difference</th></tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {data.top.map((r) => (
                      <tr key={r.key} className="align-top" title={r.text ?? ""}>
                        <td className="py-1.5 pr-3">{r.merchant} <span className="text-xs text-muted">{r.category.replace(/_/g, " ")}</span></td>
                        <td className="py-1.5 pr-3 text-right tabular">{r.purchases}</td>
                        <td className="py-1.5 pr-3 text-right tabular">{money(r.spend_cents)}</td>
                        <td className="py-1.5 pr-3 text-ink-2">{r.cards_used.map((c) => `${c.product}${r.cards_used.length > 1 ? ` (${c.purchases})` : ""}`).join(", ")}</td>
                        <td className="py-1.5 pr-3">{r.better_product}</td>
                        <td className="py-1.5 text-right tabular font-medium">{money(r.difference_cents)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {data.unsure.length > 0 && (
              <p className="text-xs text-ink-2">
                Not sure about these merchants' category, so they're left out: {data.unsure.map((r) => `${r.merchant} (${money(r.difference_cents)})`).join(", ")}.
                Fix one in Find a card → Change.
              </p>
            )}
            {data.unknown_purchases > 0 && (
              <p className="text-xs text-muted">{data.unknown_purchases} purchases at {data.unknown_descriptors} merchants aren't known yet; the next fin process looks them up.</p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function Credits() {
  const qc = useQueryClient();
  const { data = [] } = useQuery({ queryKey: ["credits"], queryFn: api.credits });
  const mark = useMutation({
    mutationFn: api.markCredit,
    onSuccess: (rows) => { qc.setQueryData(["credits"], rows); qc.invalidateQueries({ queryKey: ["best"] }); },
  });
  const enroll = useMutation({
    mutationFn: (c: CreditUse & { value: string | null }) => api.putCardSettings({ cards: [{ card_id: c.card_id,
      requirements: [{ ref: c.enroll_ref, kind: "enrollment", value: c.value }] }] }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["credits"] }),
  });
  const byCard = data.reduce<Record<string, CreditUse[]>>((acc, c) => { (acc[c.product] ??= []).push(c); return acc; }, {});
  if (!data.length) return null;
  return (
    <Card>
      <CardHeader><CardTitle>Statement credits this period</CardTitle></CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p className="text-xs text-muted">
          Matched to the credits on your statements. In-app credits (Uber Cash, DoorDash) don't post, so mark them by hand.
        </p>
        {Object.entries(byCard).map(([product, rows]) => (
          <section key={product} className="space-y-2">
            <h3 className="font-medium">{product}{rows[0].as_of && !rows[0].fresh && <span className="ml-2 text-xs font-normal text-critical">data as of {rows[0].as_of}</span>}</h3>
            {rows.map((c) => {
              const pct = c.known ? Math.min(100, (c.used_cents / c.amount_cents) * 100) : 0;
              return (
                <div key={c.identity} className="grid grid-cols-1 items-center gap-2 sm:grid-cols-[minmax(0,16rem)_1fr_auto]">
                  <div>{c.name} <span className="text-xs text-muted">{c.period_key}</span></div>
                  <div className="flex items-center gap-2">
                    <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-2" role="img"
                         aria-label={c.known ? `${money(c.used_cents)} of ${money(c.amount_cents)} used` : "use unknown"}>
                      <div className="h-full bg-series-1" style={{ width: `${pct}%` }} />
                    </div>
                    <span className="w-40 text-right text-xs tabular">
                      {c.known ? `${money(c.used_cents)} of ${money(c.amount_cents)}` : `? of ${money(c.amount_cents)}`}
                    </span>
                    <span className="w-20 text-xs text-muted">{c.days_left} days left</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {c.enrollment_required && (
                      <Select value={c.enrolled === null ? "" : c.enrolled ? "yes" : "no"} aria-label="Enrolled"
                              onChange={(e) => enroll.mutate({ ...c, value: e.target.value || null })}>
                        <option value="">enrolled?</option>
                        <option value="yes">enrolled</option>
                        <option value="no">not enrolled</option>
                      </Select>
                    )}
                    {c.how === "manual" ? (
                      <Button size="sm" variant="ghost" onClick={() => mark.mutate({ card_id: c.card_id, identity: c.identity, period_key: c.period_key, clear: true })}>Undo mark</Button>
                    ) : (
                      <>
                        <Button size="sm" variant="ghost" onClick={() => mark.mutate({ card_id: c.card_id, identity: c.identity, period_key: c.period_key, used: "full" })}>Used</Button>
                        <Button size="sm" variant="ghost" onClick={() => mark.mutate({ card_id: c.card_id, identity: c.identity, period_key: c.period_key, used: "none" })}>Unused</Button>
                      </>
                    )}
                    {c.how === "manual" && <Badge>marked by you</Badge>}
                  </div>
                </div>
              );
            })}
          </section>
        ))}
      </CardContent>
    </Card>
  );
}

function Cheatsheet() {
  const { data } = useQuery({ queryKey: ["cheatsheet"], queryFn: api.cheatsheet });
  if (!data) return null;
  const table = (title: string, rows: typeof data.categories) => (
    <section>
      <h3 className="mb-1 font-medium">{title}</h3>
      <table className="w-full text-sm">
        <tbody className="divide-y divide-line">
          {rows.map((r) => (
            <tr key={r.key}>
              <td className="py-1 pr-3">{r.label}</td>
              <td className="py-1 pr-3 font-medium">{r.product ?? "-"}</td>
              <td className="py-1 pr-3 text-right tabular">{r.cents_per_dollar.toFixed(1)}¢/$</td>
              <td className="py-1 text-xs text-ink-2">{r.rule}{r.runner_up ? ` · next: ${r.runner_up} ${r.runner_up_cents_per_dollar?.toFixed(1)}¢` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
  return (
    <Card>
      <CardHeader><CardTitle>Cheat sheet</CardTitle></CardHeader>
      <CardContent className="grid gap-6 lg:grid-cols-2">
        {table("By category", data.categories)}
        {table("Your merchants", data.merchants)}
      </CardContent>
    </Card>
  );
}
