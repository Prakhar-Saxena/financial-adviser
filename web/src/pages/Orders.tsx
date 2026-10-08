import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Meta } from "@/api";
import { ChartCard } from "@/components/ChartCard";
import { categoryColors } from "@/lib/palette";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn, money, monthLabel } from "@/lib/utils";

const MERCHANTS = [{ id: "amazon", name: "Amazon" }, { id: "costco", name: "Costco" }];

export function Orders({ month, meta }: { month: string; meta: Meta }) {
  const [params, setParams] = useSearchParams();
  const merchant = params.get("merchant") === "costco" ? "costco" : "amazon";
  const setMerchant = (id: string) => setParams(id === "amazon" ? {} : { merchant: id });
  const [allMonths, setAllMonths] = useState(false);
  const m = allMonths ? undefined : month;
  const { data: summary } = useQuery({ queryKey: ["ordersSummary", merchant, m],
                                       queryFn: () => api.ordersSummary(merchant, m) });
  const { data: orders = [] } = useQuery({ queryKey: ["orders", merchant, m],
                                           queryFn: () => api.orders(merchant, m) });
  // When the selected month is empty, look at all months to say where the data is.
  const { data: everything = [] } = useQuery({
    queryKey: ["orders", merchant, undefined],
    queryFn: () => api.orders(merchant),
    enabled: !allMonths && orders.length === 0,
  });
  const latest = everything.length ? everything[0].order_date.slice(0, 7) : null;
  const noun = merchant === "costco" ? "receipts" : "orders";
  const merchantName = MERCHANTS.find((x) => x.id === merchant)?.name ?? merchant;
  const cat = categoryColors(meta, summary?.by_category ?? []);
  const catName = Object.fromEntries(meta.categories.map((c) => [c.id, c.name]));
  const [open, setOpen] = useState<number | null>(null);
  const unmatched = orders.filter((o) => !o.matched).length;

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {MERCHANTS.map((x) => (
          <Button key={x.id} variant={merchant === x.id ? "default" : "outline"} size="sm"
                  onClick={() => setMerchant(x.id)}>{x.name}</Button>
        ))}
        <label className="ml-2 flex items-center gap-1.5 text-sm text-ink-2">
          <input type="checkbox" checked={allMonths} onChange={(e) => setAllMonths(e.target.checked)} />
          All months
        </label>
        {summary && (
          <span className="ml-auto text-sm text-ink-2">
            {summary.orders} {merchant === "costco" ? "receipts" : "orders"} · {money(summary.total_cents)}
            {unmatched > 0 && <> · <span className="text-ink">{unmatched} not matched to a card charge</span></>}
          </span>
        )}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <ChartCard id={`orders-${merchant}-category`} title="Item spending by category"
                   items={(summary?.by_category ?? []).map((r) => ({
                     key: r.key, label: r.name ?? r.key, cents: r.cents,
                     color: cat.color(r.key), slot: cat.index(r.key) }))} />
        <ChartCard id={`orders-${merchant}-items`} title="Top items" subtitle="Bars coloured by item category"
                   views={["bar", "treemap", "table"]}
                   items={(summary?.top_items ?? []).map((r, i) => ({
                     key: `${i}`, label: r.count > 1 ? `${r.title} ×${r.count}` : r.title, cents: r.cents,
                     color: cat.color(r.category_id) }))} />
      </div>
      <Card className="mt-4">
        <CardHeader><CardTitle>{merchant === "costco" ? "Receipts" : "Orders"}</CardTitle></CardHeader>
        <CardContent className="divide-y divide-line">
          {orders.length === 0 && (
            <div className="flex flex-wrap items-center gap-3 py-2 text-sm text-ink-2">
              {allMonths || !latest ? (
                <span>No {merchantName} {noun} imported yet.</span>
              ) : (
                <>
                  <span>
                    No {merchantName} {noun} in {monthLabel(month)}. The latest are from{" "}
                    {monthLabel(latest)} ({everything.length} in total).
                  </span>
                  <Button size="sm" variant="outline" onClick={() => setAllMonths(true)}>
                    Show all months
                  </Button>
                </>
              )}
            </div>
          )}
          {orders.map((o) => (
            <div key={o.id} className="py-2">
              <button className="flex w-full items-center gap-3 text-left text-sm"
                      onClick={() => setOpen(open === o.id ? null : o.id)}>
                {open === o.id ? <ChevronDown className="h-4 w-4 text-muted" /> : <ChevronRight className="h-4 w-4 text-muted" />}
                <span className="tabular text-ink-2">{o.order_date}</span>
                <span className="text-ink">{merchant === "costco" ? (o.location ?? "Costco") : o.external_id}</span>
                <span className="text-xs text-muted">{o.items.length} items</span>
                {!o.matched && <Badge className="bg-warning-bg text-ink">no card charge</Badge>}
                <span className="ml-auto tabular">{money(o.total_cents)}</span>
              </button>
              {open === o.id && (
                <table className="mt-2 w-full text-sm">
                  <tbody>
                    {o.items.map((i, n) => (
                      <tr key={n} className="border-t border-line">
                        <td className="py-1 pl-7 text-ink" title={i.title_raw}>
                          {i.quantity > 1 ? `${i.quantity} × ` : ""}{i.title}
                          {i.sku && <span className="ml-2 text-xs text-muted">#{i.sku}</span>}
                        </td>
                        <td className="py-1 text-xs text-muted">{catName[i.category_id ?? ""] ?? "—"}</td>
                        <td className={cn("py-1 text-right tabular", i.discount_cents && "text-ink")}>
                          {money(i.line_total_cents - i.discount_cents)}
                          {i.discount_cents > 0 && <span className="ml-1 text-xs text-good">(−{money(i.discount_cents)})</span>}
                        </td>
                      </tr>
                    ))}
                    <tr className="border-t border-line text-xs text-muted">
                      <td className="py-1 pl-7">Tax{o.discounts_cents ? " · savings included above" : ""}</td>
                      <td /><td className="py-1 text-right tabular">{money(o.tax_cents)}</td>
                    </tr>
                  </tbody>
                </table>
              )}
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
