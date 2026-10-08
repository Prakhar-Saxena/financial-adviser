import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Meta, type ReviewItem } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { money } from "@/lib/utils";
import { TxnDetailPanel } from "@/pages/Transactions";

const KIND_TITLES: Record<string, string> = {
  uncategorized: "Uncategorized",
  low_confidence: "Low-confidence guesses",
  unknown_person: "Unknown cardholder",
  unmatched_charge: "Charges without an order",
  unmatched_order_charge: "Orders without a card charge",
  unexpected_merchant: "Unexpected merchant on a dedicated card",
  statement_mismatch: "Statement doesn't match the ledger",
  parse_failure: "Files that need a look",
  sync_gap: "Sync gaps",
};

export function Review({ meta }: { meta: Meta }) {
  const qc = useQueryClient();
  const { data = [] } = useQuery({ queryKey: ["review"], queryFn: api.review });
  const resolve = useMutation({
    mutationFn: ({ id, status }: { id: number; status: "resolved" | "ignored" }) => api.resolve(id, status),
    onSuccess: () => qc.invalidateQueries(),
  });
  const resolveMany = useMutation({
    mutationFn: ({ ids, status }: { ids: number[]; status: "resolved" | "ignored" }) => api.resolveMany(ids, status),
    onSuccess: () => qc.invalidateQueries(),
  });
  const groups = data.reduce<Record<string, ReviewItem[]>>((acc, r) => {
    (acc[r.kind] ??= []).push(r);
    return acc;
  }, {});
  if (!data.length) return <p className="text-sm text-ink-2">Nothing to review.</p>;
  return (
    <div className="space-y-4">
      {Object.entries(groups).map(([kind, items]) => (
        <Card key={kind}>
          <CardHeader>
            <CardTitle>{KIND_TITLES[kind] ?? kind} <Badge className="ml-1.5">{items.length}</Badge></CardTitle>
            <div className="flex gap-2">
              {kind === "low_confidence" && (
                <Button size="sm" variant="outline"
                        onClick={() => resolveMany.mutate({ ids: items.map((i) => i.id), status: "resolved" })}>
                  All look right
                </Button>
              )}
              <Button size="sm" variant="ghost"
                      onClick={() => resolveMany.mutate({ ids: items.map((i) => i.id), status: "ignored" })}>
                Ignore all
              </Button>
            </div>
          </CardHeader>
          <CardContent className="divide-y divide-line">
            {items.map((r) => (
              <ReviewRow key={r.id} item={r} meta={meta}
                         onResolve={(status) => resolve.mutate({ id: r.id, status })} />
            ))}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function ReviewRow({ item, meta, onResolve }: {
  item: ReviewItem; meta: Meta; onResolve: (s: "resolved" | "ignored") => void;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const t = item.transaction;
  const catName = Object.fromEntries(meta.categories.map((c) => [c.id, c.name]));
  const setCat = useMutation({
    mutationFn: (category_id: string) => api.patchTransaction(t!.id, { category_id }),
    onSuccess: () => qc.invalidateQueries(),
  });
  const it = item.item;
  const setItemCat = useMutation({
    mutationFn: (category_id: string) => api.patchItem(it!.id, category_id),
    onSuccess: () => qc.invalidateQueries(),
  });
  const reason = (item.details.reason as string) ?? (item.details.description as string) ?? "";
  return (
    <div className="py-2.5">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        {t ? (
          <>
            <span className="tabular text-ink-2">{t.txn_date ?? t.post_date}</span>
            <button className="text-left text-ink underline-offset-2 hover:underline" onClick={() => setOpen(!open)}>
              {t.merchant ?? t.description}
            </button>
            <span className="tabular">{money(t.amount_cents)}</span>
            {item.kind === "low_confidence" && item.details.category_id ? (
              <span className="text-xs text-muted">guess: {catName[item.details.category_id as string]}</span>
            ) : null}
          </>
        ) : it ? (
          <>
            <span className="tabular text-ink-2">{it.order_date}</span>
            <span className="text-ink" title={it.title_raw}>{it.title}</span>
            <span className="text-xs capitalize text-muted">{it.merchant} item</span>
            <span className="tabular">{money(it.cents)}</span>
            {it.category_id && <span className="text-xs text-muted">guess: {catName[it.category_id]}</span>}
          </>
        ) : item.order ? (
          <span className="text-ink">Order {item.order.external_id} · {item.order.order_date} · {money(item.order.total_cents)}</span>
        ) : (
          <span className="text-ink">{item.ref_table} {item.ref_id}</span>
        )}
        {reason && <span className="text-xs text-muted">{reason}</span>}
        <div className="ml-auto flex items-center gap-2">
          {t && ["uncategorized", "low_confidence"].includes(item.kind) && (
            <Select defaultValue="" aria-label="Set category" onChange={(e) => e.target.value && setCat.mutate(e.target.value)}>
              <option value="" disabled>Set category…</option>
              {meta.categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          )}
          {it && (
            <Select defaultValue="" aria-label="Set item category" onChange={(e) => e.target.value && setItemCat.mutate(e.target.value)}>
              <option value="" disabled>Set category…</option>
              {meta.categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          )}
          {item.kind === "low_confidence" && <Button size="sm" variant="outline" onClick={() => onResolve("resolved")}>Looks right</Button>}
          <Button size="sm" variant="ghost" onClick={() => onResolve("ignored")}>Ignore</Button>
        </div>
      </div>
      {open && t && <div className="mt-3 rounded-md bg-surface-2 p-3"><TxnDetailPanel id={t.id} meta={meta} catName={catName} /></div>}
    </div>
  );
}
