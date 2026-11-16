// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Meta, type ReviewItem, type TxnPatch } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { money } from "@/lib/utils";
import { problemText } from "@/pages/BestCard";
import { TxnDetailPanel } from "@/pages/Transactions";

// Both transfer kinds share one group (docs/SPEC-checking.md §13); so do the utility kinds
// (docs/SPEC-utilities.md §10), which are sorted on the Utilities tab.
const TRANSFER_KINDS = ["unmatched_transfer", "uncertain_transfer"];
const UTILITY_KINDS = ["utility_unassigned", "utility_missing"];
const groupOf = (kind: string) =>
  TRANSFER_KINDS.includes(kind) ? "card_payments" : UTILITY_KINDS.includes(kind) ? "utilities" : kind;

const KIND_TITLES: Record<string, string> = {
  card_payments: "Card payments to check",
  utilities: "Utilities",
  bank_balance_gap: "Checking: running balance breaks",
  uncategorized: "Uncategorized",
  low_confidence: "Low-confidence guesses",
  unknown_person: "Unknown cardholder",
  unmatched_charge: "Charges without an order",
  unmatched_order_charge: "Orders without a card charge",
  unexpected_merchant: "Unexpected merchant on a dedicated card",
  statement_mismatch: "Statement doesn't match the ledger",
  parse_failure: "Files that need a look",
  sync_gap: "Sync gaps",
  card_terms_pending: "Card terms: waiting for approval",
  card_terms_unverified: "Card terms: didn't check out",
  card_source_problem: "Card terms: document problems",
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
  const grouped = data.reduce<Record<string, ReviewItem[]>>((acc, r) => {
    (acc[groupOf(r.kind)] ??= []).push(r);
    return acc;
  }, {});
  // Card payments first: they decide what counts as spending at all.
  const groups = Object.fromEntries(Object.entries(grouped).sort(
    ([a], [b]) => Number(b === "card_payments") - Number(a === "card_payments")));
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
            {kind === "utilities" && (
              <p className="pb-2 text-xs text-muted">
                Sort these on the <Link className="underline" to="/utilities">Utilities</Link> tab: a payment that
                matches no account, or an account whose payment is late.
              </p>
            )}
            {kind === "card_payments" && (
              <p className="pb-2 text-xs text-muted">
                A choice here is kept through <code>fin rebuild</code>. Links to card payments refresh on the next <code>fin process</code>.
              </p>
            )}
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
  const [all, setAll] = useState(false);
  const setType = useMutation({
    mutationFn: (body: TxnPatch) => api.patchTransaction(t!.id, { ...body, apply_to_all: all }),
    onSuccess: () => qc.invalidateQueries(),
  });
  const transfer = t && TRANSFER_KINDS.includes(item.kind);
  const spending = t && t.amount_cents < 0 ? "refund" : "purchase";
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
        ) : item.kind === "utility_missing" ? (
          <>
            <Link to={`/utilities?account=${item.ref_id}`} className="text-ink underline-offset-2 hover:underline">
              {meta.utility_accounts.find((u) => u.id === item.ref_id)?.provider ?? item.ref_id}
            </Link>
            <span className="text-xs text-ink-2">
              No payment since {String(item.details.last_payment)}; expected around {String(item.details.expected)}
            </span>
          </>
        ) : item.kind.startsWith("card_") ? (
          <>
            <Link to="/best-card" className="text-ink underline-offset-2 hover:underline">{item.ref_id}</Link>
            <span className="text-xs text-ink-2">
              {item.kind === "card_terms_pending"
                ? `v${item.details.version} ${item.details.first ? "(first terms)" : `(${item.details.summary})`}: accept or reject in Best card → Card terms`
                : problemText(item.details)}
            </span>
          </>
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
          {transfer && (
            <>
              <label className="flex items-center gap-1.5 text-xs text-ink-2">
                <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
                Every row like it
              </label>
              {item.kind === "uncertain_transfer" ? (
                <>
                  <Button size="sm" variant="outline" disabled={setType.isPending}
                          onClick={() => setType.mutate({ type: "transfer", flow_kind: "card_payment" })}>
                    Yes, a card payment
                  </Button>
                  <Button size="sm" variant="outline" disabled={setType.isPending}
                          onClick={() => setType.mutate({ type: spending })}>
                    No, it's spending
                  </Button>
                </>
              ) : (
                <>
                  <Button size="sm" variant="outline" disabled={setType.isPending}
                          onClick={() => setType.mutate({ type: spending })}>
                    It's spending
                  </Button>
                  <Button size="sm" variant="outline" disabled={setType.isPending}
                          onClick={() => setType.mutate({ type: "transfer", flow_kind: t!.flow_kind ?? "own_transfer" })}>
                    Not spending
                  </Button>
                </>
              )}
            </>
          )}
          {item.kind === "utility_unassigned" && (
            <Link to="/utilities#unassigned" className="text-xs text-ink-2 underline underline-offset-2">Sort on Utilities</Link>
          )}
          {item.kind === "low_confidence" && <Button size="sm" variant="outline" onClick={() => onResolve("resolved")}>Looks right</Button>}
          <Button size="sm" variant="ghost" onClick={() => onResolve("ignored")}>Ignore</Button>
        </div>
      </div>
      {open && t && <div className="mt-3 rounded-md bg-surface-2 p-3"><TxnDetailPanel id={t.id} meta={meta} catName={catName} /></div>}
    </div>
  );
}
