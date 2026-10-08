import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createColumnHelper, flexRender, getCoreRowModel, getSortedRowModel, useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Meta, type Txn } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { cn, money } from "@/lib/utils";

const col = createColumnHelper<Txn>();
const TYPES = ["purchase", "refund", "payment", "credit", "reward", "fee", "interest", "adjustment"];

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
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v); else next.delete(k);
    setParams(next);
  };
  const { data = [], isFetching } = useQuery({
    queryKey: ["transactions", filters],
    queryFn: () => api.transactions(filters),
    placeholderData: (prev) => prev,
  });
  const [sorting, setSorting] = useState<SortingState>([]);
  const [open, setOpen] = useState<number | null>(null);
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
    col.accessor("card_id", { header: "Card", cell: (c) => <span className="text-ink-2">{cardName[c.getValue()] ?? c.getValue()}</span> }),
    col.accessor("person_id", { header: "Person", cell: (c) => <span className="text-ink-2">{personName[c.getValue() ?? ""] ?? "Unknown"}</span> }),
    col.accessor("category_id", { header: "Category", cell: (c) => {
      const t = c.row.original;
      if (["payment", "credit", "reward", "adjustment"].includes(t.type)) return <Badge>{t.type}</Badge>;
      return (
        <span className="text-ink-2">
          {catName[c.getValue() ?? "uncategorized"] ?? c.getValue()}
          {t.category_source === "split" && <Badge className="ml-1.5">split</Badge>}
        </span>);
    } }),
    col.accessor("amount_cents", { header: () => <div className="text-right">Amount</div>,
      cell: (c) => <div className={cn("text-right tabular", c.getValue() < 0 && "text-good")}>{money(c.getValue())}</div> }),
  ], [open, catName, cardName, personName]);

  const table = useReactTable({
    data, columns, state: { sorting }, onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(),
  });
  const total = data.filter((t) => ["purchase", "refund", "fee", "interest"].includes(t.type))
    .reduce((s, t) => s + t.amount_cents, 0);

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Select value={filters.card ?? ""} onChange={(e) => set("card", e.target.value)} aria-label="Card">
          <option value="">All cards</option>
          {meta.cards.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </Select>
        <Select value={filters.person ?? ""} onChange={(e) => set("person", e.target.value)} aria-label="Person">
          <option value="">Everyone</option>
          {meta.people.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Select>
        <Select value={filters.category ?? ""} onChange={(e) => set("category", e.target.value)} aria-label="Category">
          <option value="">All categories</option>
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
                    onClick={() => setOpen(open === row.original.id ? null : row.original.id)}>
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
  const patch = useMutation({ mutationFn: (b: { category_id?: string; person_id?: string }) => api.patchTransaction(id, b), onSuccess: refresh });
  const mkRule = useMutation({
    mutationFn: () => api.createRule({ pattern: rule ?? "", category_id: ruleCat }),
    onSuccess: (r) => { setNote(`Rule saved; ${r.note}.`); setRule(null); },
    onError: (e) => setNote(String(e)),
  });
  if (!d) return <p className="text-sm text-muted">Loading…</p>;
  const spending = !["payment", "credit", "reward", "adjustment"].includes(d.type);
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
        ) : (
          <p className="text-xs text-muted">No order or receipt matched{d.merchant_group === "amazon" ? " yet" : ""}.</p>
        )}
      </div>
    </div>
  );
}
