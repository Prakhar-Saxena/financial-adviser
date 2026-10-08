import { useQuery } from "@tanstack/react-query";
import { AreaChart as AreaIcon, BarChart3, ClipboardCheck, Gift, LineChart as LineIcon, ShoppingCart, Wallet } from "lucide-react";
import { useState } from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { useNavigate } from "react-router-dom";
import { api, type Meta } from "@/api";
import { ChartCard } from "@/components/ChartCard";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { categoryColors, listColors, parentOf } from "@/lib/palette";
import { cn, money, monthLabel } from "@/lib/utils";

function Stat({ label, value, sub, icon: Icon, tint }: {
  label: string; value: string; sub?: string; icon: typeof Wallet; tint: string;
}) {
  return (
    <Card>
      <CardContent className="flex items-start gap-3 pt-4">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl"
              style={{ background: `color-mix(in srgb, ${tint} 16%, transparent)`, color: tint }}>
          <Icon className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <div className="text-xs text-muted">{label}</div>
          <div className="mt-0.5 text-2xl font-semibold text-ink">{value}</div>
          {sub && <div className="mt-0.5 text-xs text-ink-2">{sub}</div>}
        </div>
      </CardContent>
    </Card>
  );
}

export function Overview({ month, meta }: { month: string; meta: Meta }) {
  const nav = useNavigate();
  const { data, isFetching } = useQuery({
    queryKey: ["summary", month], queryFn: () => api.summary(month), placeholderData: (p) => p,
  });
  const { data: review } = useQuery({ queryKey: ["review"], queryFn: api.review });
  if (!data) return <p className="text-sm text-muted">Loading…</p>;
  const name = (list: { id: string; name: string }[], id: string) => list.find((x) => x.id === id)?.name ?? id;
  const cur = data.trend.find((t) => t.month === data.month);
  const share = (c?: number) => (cur && cur.cents ? Math.round(((c ?? 0) / cur.cents) * 100) : 0);
  const goCategory = (key: string) => nav(`/transactions?category=${key}`);
  const cat = categoryColors(meta, data.by_category);
  const catName = (id: string) => { const p = parentOf(id); return meta.categories.find((c) => c.id === p)?.name ?? p; };
  const cards = listColors(meta.cards.map((c) => c.id), data.by_card);
  const people = listColors(meta.people.map((p) => p.id), data.by_person);

  return (
    <div className={cn("transition-opacity", isFetching && "opacity-60")}>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label={`Spending, ${monthLabel(data.month)}`} value={money(data.total_cents, { whole: true })}
              icon={Wallet} tint="var(--cat-1)" />
        <Stat label="Card credits & rewards" value={money(-data.credits_rewards_cents, { whole: true })}
              sub="Not counted as spending" icon={Gift} tint="var(--cat-3)" />
        <Stat label="Amazon & Whole Foods share" value={`${share(cur?.amazon_cents)}%`}
              sub={share(cur?.costco_cents) ? `Costco ${share(cur?.costco_cents)}%` : undefined}
              icon={ShoppingCart} tint="var(--cat-2)" />
        <Stat label="Open review items" value={String(review?.length ?? "—")} sub="Things that need a look"
              icon={ClipboardCheck} tint="var(--cat-7)" />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <ChartCard id="overview-category" title="Spending by category" onSelect={goCategory}
                   views={["donut", "bar", "treemap", "table"]}
                   items={data.by_category.map((r) => ({
                     key: r.key, label: r.name ?? r.key, cents: r.cents,
                     color: cat.color(r.key), slot: cat.index(r.key),
                   }))} />
        <TrendCard trend={data.trend} />
        <ChartCard id="overview-person" title="By person" views={["donut", "bar", "table"]}
                   items={data.by_person.map((r) => ({
                     key: r.key, label: name(meta.people, r.key), cents: r.cents,
                     color: people.color(r.key), slot: people.index(r.key),
                   }))} />
        <ChartCard id="overview-card" title="By card" views={["bar", "donut", "table"]}
                   items={data.by_card.map((r) => ({
                     key: r.key, label: name(meta.cards, r.key), cents: r.cents,
                     color: cards.color(r.key), slot: cards.index(r.key),
                   }))} />
        <ChartCard id="overview-merchants" title="Top merchants" views={["bar", "treemap", "table"]}
                   items={data.top_merchants.map((r) => ({ key: r.key, label: r.key, cents: r.cents }))} />
        <ChartCard id="overview-subcategory" title="Subcategories" subtitle="Coloured by parent category"
                   views={["bar", "treemap", "table"]}
                   items={data.by_subcategory.slice(0, 14).map((r) => ({
                     key: r.key, label: r.name ?? r.key, cents: r.cents,
                     color: cat.color(r.key), slot: cat.index(r.key), group: catName(r.key),
                   }))} />
      </div>
    </div>
  );
}

type TrendView = "columns" | "line" | "area";
const TREND_ICONS = { columns: BarChart3, line: LineIcon, area: AreaIcon };

function TrendCard({ trend }: { trend: { month: string; cents: number }[] }) {
  const [view, setView] = useState<TrendView>(() => {
    try { return (localStorage.getItem("chart:overview-trend") as TrendView) || "columns"; } catch { return "columns"; }
  });
  const choose = (v: TrendView) => {
    setView(v);
    try { localStorage.setItem("chart:overview-trend", v); } catch { /* private mode */ }
  };
  const data = trend.map((t) => ({ ...t, label: monthLabel(t.month) }));
  const axes = (
    <>
      <CartesianGrid vertical={false} stroke="var(--grid)" />
      <XAxis dataKey="label" tickLine={false} axisLine={{ stroke: "var(--axis)" }} tick={{ fill: "var(--ink-2)", fontSize: 12 }} />
      <YAxis tickLine={false} axisLine={false} width={56} tick={{ fill: "var(--muted)", fontSize: 11 }}
             tickFormatter={(v: number) => money(v, { whole: true })} />
      <Tooltip cursor={{ fill: "var(--surface-2)", stroke: "var(--axis)" }}
               content={({ active, payload }) => active && payload?.length ? (
                 <div className="rounded-md border border-line bg-surface px-2.5 py-1.5 text-xs shadow">
                   <div className="text-ink-2">{payload[0].payload.label}</div>
                   <div className="tabular font-medium">{money(payload[0].payload.cents)}</div>
                 </div>) : null} />
    </>
  );
  const margin = { top: 16, right: 12, bottom: 0, left: 8 };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Last 3 months</CardTitle>
        <div className="flex rounded-lg border border-line bg-surface-2 p-0.5" role="group" aria-label="Chart type">
          {(Object.keys(TREND_ICONS) as TrendView[]).map((v) => {
            const Icon = TREND_ICONS[v];
            return (
              <button key={v} onClick={() => choose(v)} aria-pressed={view === v} title={v}
                      className={cn("rounded-md p-1.5 text-muted hover:text-ink", view === v && "bg-surface text-ink shadow-sm")}>
                <Icon className="h-3.5 w-3.5" />
              </button>
            );
          })}
        </div>
      </CardHeader>
      <CardContent>
        <div style={{ height: 220 }}>
          <ResponsiveContainer width="100%" height="100%">
            {view === "line" ? (
              <LineChart data={data} margin={margin}>
                {axes}
                <Line dataKey="cents" stroke="var(--series-1)" strokeWidth={2} isAnimationActive={false}
                      dot={{ r: 4, fill: "var(--series-1)", stroke: "var(--surface)", strokeWidth: 2 }} />
              </LineChart>
            ) : view === "area" ? (
              <AreaChart data={data} margin={margin}>
                {axes}
                <Area dataKey="cents" stroke="var(--series-1)" strokeWidth={2} fill="var(--series-1)"
                      fillOpacity={0.1} isAnimationActive={false}
                      dot={{ r: 4, fill: "var(--series-1)", stroke: "var(--surface)", strokeWidth: 2 }} />
              </AreaChart>
            ) : (
              <BarChart data={data} margin={margin} barCategoryGap="35%">
                {axes}
                <Bar dataKey="cents" fill="var(--series-1)" radius={[4, 4, 0, 0]} maxBarSize={56} isAnimationActive={false} />
              </BarChart>
            )}
          </ResponsiveContainer>
        </div>
        <table className="mt-2 w-full text-xs text-ink-2">
          <tbody>
            <tr>{data.map((d) => <td key={d.month} className="text-center tabular">{money(d.cents, { whole: true })}</td>)}</tr>
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
