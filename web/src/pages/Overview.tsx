import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useNavigate } from "react-router-dom";
import { api, type Meta } from "@/api";
import { BarList } from "@/components/BarList";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { money, monthLabel } from "@/lib/utils";

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="text-xs text-muted">{label}</div>
        <div className="mt-1 text-3xl font-semibold text-ink">{value}</div>
        {sub && <div className="mt-1 text-xs text-ink-2">{sub}</div>}
      </CardContent>
    </Card>
  );
}

export function Overview({ month, meta }: { month: string; meta: Meta }) {
  const nav = useNavigate();
  const { data, isFetching } = useQuery({
    queryKey: ["summary", month],
    queryFn: () => api.summary(month),
    placeholderData: (prev) => prev,
  });
  const { data: review } = useQuery({ queryKey: ["review"], queryFn: api.review });
  if (!data) return <p className="text-sm text-muted">Loading…</p>;
  const name = (list: { id: string; name: string }[], id: string) =>
    list.find((x) => x.id === id)?.name ?? id;
  const cur = data.trend.find((t) => t.month === data.month);
  const amazonShare = cur && cur.cents ? Math.round((cur.amazon_cents / cur.cents) * 100) : 0;
  const costcoShare = cur && cur.cents ? Math.round((cur.costco_cents / cur.cents) * 100) : 0;

  return (
    <div className={isFetching ? "opacity-60 transition-opacity" : "transition-opacity"}>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label={`Spending, ${monthLabel(data.month)}`} value={money(data.total_cents, { whole: true })} />
        <Stat label="Card credits & rewards" value={money(-data.credits_rewards_cents, { whole: true })}
              sub="Not counted as spending" />
        <Stat label="Amazon & Whole Foods share" value={`${amazonShare}%`}
              sub={costcoShare ? `Costco ${costcoShare}%` : undefined} />
        <Stat label="Open review items" value={String(review?.length ?? "—")}
              sub="Things that need a look" />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <BarList
          title="Spending by category"
          items={data.by_category.map((r) => ({ label: r.name ?? r.key, cents: r.cents }))}
          onSelect={(label) => {
            const row = data.by_category.find((r) => (r.name ?? r.key) === label);
            if (row) nav(`/transactions?category=${row.key}`);
          }}
        />
        <TrendCard trend={data.trend} />
        <BarList title="By person"
                 items={data.by_person.map((r) => ({ label: name(meta.people, r.key), cents: r.cents }))} />
        <BarList title="By card"
                 items={data.by_card.map((r) => ({ label: name(meta.cards, r.key), cents: r.cents }))} />
        <BarList title="Top merchants"
                 items={data.top_merchants.map((r) => ({ label: r.key, cents: r.cents }))} />
        <BarList title="Subcategories"
                 items={data.by_subcategory.slice(0, 12).map((r) => ({ label: r.name ?? r.key, cents: r.cents }))} />
      </div>
    </div>
  );
}

function TrendCard({ trend }: { trend: { month: string; cents: number }[] }) {
  const data = trend.map((t) => ({ ...t, label: monthLabel(t.month) }));
  return (
    <Card>
      <CardHeader><CardTitle>Last 3 months</CardTitle></CardHeader>
      <CardContent>
        <div style={{ height: 220 }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 20, right: 8, bottom: 0, left: 8 }} barCategoryGap="35%">
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis dataKey="label" tickLine={false} axisLine={{ stroke: "var(--axis)" }}
                     tick={{ fill: "var(--ink-2)", fontSize: 12 }} />
              <YAxis tickLine={false} axisLine={false} width={56}
                     tick={{ fill: "var(--muted)", fontSize: 11 }}
                     tickFormatter={(v: number) => money(v, { whole: true })} />
              <Tooltip
                cursor={{ fill: "var(--surface-2)" }}
                content={({ active, payload }) =>
                  active && payload?.length ? (
                    <div className="rounded-md border border-line bg-surface px-2.5 py-1.5 text-xs shadow">
                      <div className="text-ink-2">{payload[0].payload.label}</div>
                      <div className="tabular font-medium">{money(payload[0].payload.cents)}</div>
                    </div>
                  ) : null
                }
              />
              <Bar dataKey="cents" fill="var(--series-1)" radius={[4, 4, 0, 0]} maxBarSize={56} isAnimationActive={false} />
            </BarChart>
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
