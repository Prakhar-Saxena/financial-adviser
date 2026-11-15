// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createColumnHelper, flexRender, getCoreRowModel, getSortedRowModel, useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Meta, type Txn, type TxnDetail, type TxnPatch } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { excludedLabel, FLOW_CHOICES, isAccount } from "@/lib/flows";
import { categoryColors } from "@/lib/palette";
import { cn, money } from "@/lib/utils";

const col = createColumnHelper<Txn>();
const TYPES = ["purchase", "refund", "payment", "credit", "reward", "fee", "interest", "adjustment",
               "transfer", "deposit"];

export function Transactions({ month, meta }: { month: string; meta: Meta }) {
  const [params, setParams] = useSearchParams();
  const filters = {
    month, // the header's month picker is the single month control
    card: params.get("card") ?? undefined,
    person: params.get("person") ?? undefined,
    category: params.get("category") ?? undefined,
    group: params.get("group") ?? undefined,
    type: params.get("type") ?? undefined,
    review: params.get("review") ?? undefined,
  };
  // ?txn=ID shows that one row, whatever its month: where a transfer's "Paid ..." link lands.
  const one = params.get("txn");
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v); else next.delete(k);
    setParams(next);
  };
  const { data = [], isFetching } = useQuery({
    queryKey: ["transactions", filters, one],
    queryFn: () => api.transactions(one ? { id: one } : filters),
    placeholderData: (prev) => prev,
  });
  const [sorting, setSorting] = useState<SortingState>([]);
  // The row I opened; a linked row starts open, and following another link resets it.
  const [opened, setOpen] = useState<{ from: string | null; id: number | null } | null>(null);
  const open = opened && opened.from === one ? opened.id : one ? Number(one) : null;
  const { data: summary } = useQuery({ queryKey: ["summary", month], queryFn: () => api.summary(month) });
  const cat = useMemo(() => categoryColors(meta, summary?.by_category ?? []), [meta, summary]);
  // Several categories at once: the Overview donut's "Other" links here with all of its members.
  const several = filters.category?.includes(",") ? filters.category.split(",") : null;
  const catName = useMemo(() => Object.fromEntries(meta.categories.map((c) => [c.id, c.name])), [meta]);
  const cardName = useMemo(() => Object.fromEntries(meta.cards.map((c) => [c.id, c.name])), [meta]);
  const personName = useMemo(() => Object.fromEntries(meta.people.map((p) => [p.id, p.name])), [meta]);

  const columns = useMemo(() => [
    col.display({
      id: "expand",
      cell: ({ row }) => row.original.id === open
        ? <ChevronDown className="h-4 w-4 text-muted" /> : <ChevronRight className="h-4 w-4 text-muted" />,
    }),
    col.accessor((t) => t.txn_date ?? t.post_date, { id: "date", header: "Date",
      cell: (c) => <span className="tabular text-ink-2">{c.getValue()}</span> }),
    col.accessor("description", { header: "Description", cell: (c) => (
      <div>
        <div className="text-ink">{c.row.original.merchant ?? c.getValue()}</div>
        {c.row.original.merchant && <div className="text-xs text-muted">{c.getValue()}</div>}
      </div>) }),
    col.accessor("card_id", { header: "Card or account", cell: (c) => <span className="text-ink-2">{cardName[c.getValue()] ?? c.getValue()}</span> }),
    col.accessor("person_id", { header: "Person", cell: (c) => <span className="text-ink-2">{personName[c.getValue() ?? ""] ?? "Unknown"}</span> }),
    col.accessor("category_id", { header: "Category", cell: (c) => {
      const t = c.row.original;
      if (!t.counts_as_spending) return <Badge title="Not counted as spending">{excludedLabel(t)}</Badge>;
      return (
        <span className="inline-flex items-center gap-1.5 text-ink-2">
          <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: cat.color(c.getValue()) }} />
          {catName[c.getValue() ?? "uncategorized"] ?? c.getValue()}
          {t.category_source === "split" && <Badge className="ml-1.5">split</Badge>}
        </span>);
    } }),
    col.accessor("amount_cents", { header: () => <div className="text-right">Amount</div>,
      cell: (c) => <div className={cn("text-right tabular", c.getValue() < 0 && "text-good")}>{money(c.getValue())}</div> }),
  ], [open, catName, cardName, personName, cat]);

  const table = useReactTable({
    data, columns, state: { sorting }, onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(),
  });
  const total = data.filter((t) => t.counts_as_spending)
    .reduce((s, t) => s + t.amount_cents, 0);

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {one && (
          <span className="flex items-center gap-2 rounded-md border border-line bg-surface-2 px-2 py-1 text-sm text-ink-2">
            One transaction
            <Button size="sm" variant="ghost" onClick={() => set("txn", "")}>Show all</Button>
          </span>
        )}
        <Select value={filters.card ?? ""} onChange={(e) => set("card", e.target.value)} aria-label="Card or account">
          <option value="">{meta.cards.some((c) => c.kind === "account") ? "All cards and accounts" : "All cards"}</option>
          {(["card", "account"] as const).map((kind) => {
            const list = meta.cards.filter((c) => c.kind === kind);
            return list.length ? (
              <optgroup key={kind} label={kind === "card" ? "Cards" : "Accounts"}>
                {list.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </optgroup>
            ) : null;
          })}
        </Select>
        <Select value={filters.person ?? ""} onChange={(e) => set("person", e.target.value)} aria-label="Person">
          <option value="">Everyone</option>
          {meta.people.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Select>
        <Select value={filters.category ?? ""} onChange={(e) => set("category", e.target.value)} aria-label="Category"
                title={several ? several.map((c) => catName[c] ?? c).join(", ") : undefined}>
          <option value="">All categories</option>
          {several && <option value={filters.category}>{several.length} categories (Other)</option>}
          {meta.categories.filter((c) => !c.parent_id).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </Select>
        <Select value={filters.group ?? ""} onChange={(e) => set("group", e.target.value)} aria-label="Merchant group">
          <option value="">All merchants</option>
          <option value="amazon">Amazon</option>
          <option value="whole_foods">Whole Foods</option>
          <option value="costco_warehouse">Costco warehouse</option>
        </Select>
        <Select value={filters.type ?? ""} onChange={(e) => set("type", e.target.value)} aria-label="Type">
          <option value="">All types</option>
          {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </Select>
        <label className="flex items-center gap-1.5 text-sm text-ink-2">
          <input type="checkbox" checked={!!filters.review} onChange={(e) => set("review", e.target.checked ? "1" : "")} />
          Needs review
        </label>
        <span className="ml-auto text-sm text-ink-2">
          {data.length} transactions · spending <span className="tabular text-ink">{money(total)}</span>
        </span>
      </div>
      <Card className={cn("overflow-x-auto", isFetching && "opacity-70")}>
        <table className="w-full text-sm">
          <thead>
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} className="border-b border-line text-left text-xs text-muted">
                {hg.headers.map((h) => (
                  <th key={h.id} className="px-3 py-2 font-medium" onClick={h.column.getToggleSortingHandler()}>
                    {flexRender(h.column.columnDef.header, h.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <Fragment key={row.id}>
                <tr className="cursor-pointer border-b border-line hover:bg-surface-2"
                    onClick={() => setOpen({ from: one, id: open === row.original.id ? null : row.original.id })}>
                  {row.getVisibleCells().map((cell) => (
                    <td key={cell.id} className="px-3 py-2 align-top">
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
                {open === row.original.id && (
                  <tr className="border-b border-line bg-surface-2">
                    <td colSpan={columns.length} className="px-6 py-4">
                      <TxnDetailPanel id={row.original.id} meta={meta} catName={catName} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {data.length === 0 && (
              <tr><td colSpan={columns.length} className="px-3 py-6 text-center text-muted">No transactions match.</td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

export function TxnDetailPanel({ id, meta, catName }: { id: number; meta: Meta; catName: Record<string, string> }) {
  const qc = useQueryClient();
  const { data: d } = useQuery({ queryKey: ["txn", id], queryFn: () => api.transaction(id) });
  const [rule, setRule] = useState<string | null>(null);
  const [ruleCat, setRuleCat] = useState("");
  const [note, setNote] = useState("");
  const refresh = () => qc.invalidateQueries();
  const patch = useMutation({ mutationFn: (b: TxnPatch) => api.patchTransaction(id, b), onSuccess: refresh });
  const mkRule = useMutation({
    mutationFn: () => api.createRule({ pattern: rule ?? "", category_id: ruleCat }),
    onSuccess: (r) => { setNote(`Rule saved; ${r.note}.`); setRule(null); },
    onError: (e) => setNote(String(e)),
  });
  if (!d) return <p className="text-sm text-muted">Loading…</p>;
  const spending = d.counts_as_spending;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="space-y-3 text-sm">
        <div className="text-xs text-muted">Bank description</div>
        <div className="font-mono text-xs text-ink-2">{d.description_raw}</div>
        <div className="text-xs text-muted">
          From {d.source === "statement" ? "the statement" : "the activity export"} · posted {d.post_date}
          {d.order_ref && <> · order {d.order_ref}</>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {spending && (
            <Select value={d.category_id ?? "uncategorized"} aria-label="Category"
                    onChange={(e) => patch.mutate({ category_id: e.target.value })}>
              {meta.categories.map((c) => (
                <option key={c.id} value={c.id}>{c.parent_id ? `  ${catName[c.parent_id]} › ${c.name}` : c.name}</option>
              ))}
            </Select>
          )}
          <Select value={d.person_id ?? ""} aria-label="Person" onChange={(e) => patch.mutate({ person_id: e.target.value })}>
            <option value="" disabled>Unknown person</option>
            {meta.people.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
          {spending && rule === null && (
            <Button variant="outline" size="sm" onClick={() => { setRule(`${d.description.split(" ")[0]}\\b`); setRuleCat(d.category_id ?? ""); }}>
              Create rule…
            </Button>
          )}
        </div>
        {rule !== null && (
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-line bg-surface p-2">
            <span className="text-xs text-muted">Descriptions starting with</span>
            <input className="h-8 rounded border border-line bg-surface px-2 font-mono text-xs" value={rule} onChange={(e) => setRule(e.target.value)} aria-label="Pattern" />
            <Select value={ruleCat} onChange={(e) => setRuleCat(e.target.value)} aria-label="Rule category">
              <option value="" disabled>Category…</option>
              {meta.categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
            <Button size="sm" disabled={!ruleCat || !rule} onClick={() => mkRule.mutate()}>Apply to all future matches</Button>
            <Button size="sm" variant="ghost" onClick={() => setRule(null)}>Cancel</Button>
          </div>
        )}
        {note && <p className="text-xs text-ink-2">{note}</p>}
        {(isAccount(meta, d.card_id) || d.transfer.length > 0) && (
          <WhyPanel d={d} meta={meta} checking={isAccount(meta, d.card_id)}
                    onPatch={(b) => patch.mutate(b)} busy={patch.isPending} />
        )}
        {d.reviews.length > 0 && (
          <div className="rounded-md bg-warning-bg p-2 text-xs text-ink">
            {d.reviews.map((r) => <div key={r.id}>Needs review: {r.kind.replace(/_/g, " ")}{r.details.reason ? ` — ${r.details.reason}` : ""}</div>)}
          </div>
        )}
      </div>
      <div className="text-sm">
        {d.match ? (
          <>
            <div className="mb-2 text-xs text-muted">
              {d.match.order.merchant === "amazon" ? "Amazon" : d.match.order.merchant} order {d.match.order.external_id}
              {" "}· {d.match.order.order_date} · {money(d.match.order.total_cents)} · matched {d.match.method}
            </div>
            <table className="w-full">
              <tbody>
                {d.match.items.map((i, n) => (
                  <tr key={n} className="border-b border-line last:border-0">
                    <td className="py-1 pr-2 text-ink" title={i.title_raw}>{i.quantity > 1 ? `${i.quantity} × ` : ""}{i.title}</td>
                    <td className="py-1 pr-2 text-xs text-muted">{catName[i.category_id ?? ""] ?? "—"}</td>
                    <td className="py-1 text-right tabular">{money(i.line_total_cents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {d.allocations.length > 0 && (
              <>
                <div className="mb-1 mt-3 text-xs text-muted">This charge, split by item category (tax and shipping spread proportionally)</div>
                {d.allocations.map((a) => (
                  <div key={a.category_id} className="flex justify-between text-ink-2">
                    <span>{catName[a.category_id] ?? a.category_id}</span><span className="tabular">{money(a.amount_cents)}</span>
                  </div>
                ))}
              </>
            )}
          </>
        ) : spending ? (
          <p className="text-xs text-muted">No order or receipt matched{d.merchant_group === "amazon" ? " yet" : ""}.</p>
        ) : null}
      </div>
    </div>
  );
}

function shortDate(iso: string) {
  return new Date(`${iso}T12:00:00`).toLocaleString("en-US", { month: "short", day: "numeric" });
}

/** Why a checking row is or isn't spending, the other side of a transfer, and my say on it. */
function WhyPanel({ d, meta, checking, onPatch, busy }: {
  d: TxnDetail; meta: Meta; checking: boolean; onPatch: (b: TxnPatch) => void; busy: boolean;
}) {
  const [all, setAll] = useState(false);
  const moneyIn = d.amount_cents < 0;
  const paidFrom = !checking; // a card payment: the checking row that paid it
  const linkTo = (x: TxnDetail["transfer"][number]) => (
    <Link key={x.txn_id} to={`/transactions?txn=${x.txn_id}`} className="text-ink underline underline-offset-2">
      {paidFrom ? `Paid from ${x.name}` : `${moneyIn ? "Came back from" : "Paid"} ${x.name}`}, {shortDate(x.date)}
    </Link>
  );
  const why: Record<string, string> = {
    card_payment: d.transfer.length
      ? "Not spending: those purchases already count on the card."
      : "A card payment with no matching payment on a card yet. It isn't counted while it waits.",
    card_payment_other: "A payment to a card you don't track. Those purchases aren't in the ledger, so this may be real spending.",
    own_transfer: "Money moving between your own accounts: not spending.",
    investment: "Money to or from an investment account: not spending.",
  };
  const flows = meta.bank_type_flows[d.type] ?? [];
  return (
    <div className="space-y-2 rounded-md border border-line bg-surface p-2.5 text-xs">
      {d.transfer.length > 0 && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
          {d.transfer.map(linkTo)}
          {d.transfer[0].method === "amount_only" && (
            <span className="text-muted">matched on the amount only; the description doesn't say</span>
          )}
        </div>
      )}
      {checking && (
        <>
          <div className="text-ink-2">
            {d.counts_as_spending ? "Counts as spending." : why[d.flow_kind ?? ""] ?? "Money in: not spending."}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {d.counts_as_spending ? (
              <Button size="sm" variant="outline" disabled={busy}
                      onClick={() => onPatch({ type: moneyIn ? "deposit" : "transfer", apply_to_all: all })}>
                This isn't spending
              </Button>
            ) : (
              <Button size="sm" variant="outline" disabled={busy}
                      onClick={() => onPatch({ type: moneyIn ? "refund" : "purchase", apply_to_all: all })}>
                This is spending
              </Button>
            )}
            {flows.length > 1 && (
              <Select value={d.flow_kind ?? ""} aria-label="What this is" disabled={busy}
                      onChange={(e) => onPatch({ type: d.type, flow_kind: e.target.value, apply_to_all: all })}>
                {flows.map((f) => <option key={f ?? ""} value={f ?? ""}>{FLOW_CHOICES[f ?? ""] ?? f}</option>)}
              </Select>
            )}
            <label className="flex items-center gap-1.5 text-ink-2">
              <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
              Every row like it
            </label>
          </div>
        </>
      )}
    </div>
  );
}
