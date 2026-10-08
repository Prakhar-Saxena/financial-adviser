import { useState } from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { money } from "@/lib/utils";

type Item = { label: string; cents: number };

/** One series, one hue (series slot 1): horizontal bars with hover tooltip and a table twin. */
export function BarList({ title, items, onSelect }: {
  title: string;
  items: Item[];
  onSelect?: (label: string) => void;
}) {
  const [table, setTable] = useState(false);
  const data = items.filter((i) => i.cents !== 0);
  const total = data.reduce((s, i) => s + i.cents, 0);
  const height = Math.max(120, data.length * 30 + 32);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <Button variant="ghost" size="sm" onClick={() => setTable(!table)} aria-pressed={table}>
          {table ? "Chart" : "Table"}
        </Button>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <p className="text-sm text-muted">No spending this month.</p>
        ) : table ? (
          <table className="w-full text-sm">
            <tbody>
              {data.map((d) => (
                <tr key={d.label} className="border-b border-line last:border-0">
                  <td className="py-1.5 text-ink-2">{d.label}</td>
                  <td className="py-1.5 text-right tabular">{money(d.cents)}</td>
                  <td className="w-16 py-1.5 text-right tabular text-muted">
                    {total ? `${Math.round((d.cents / total) * 100)}%` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div style={{ height }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} layout="vertical" margin={{ top: 0, right: 64, bottom: 0, left: 0 }}
                        barCategoryGap={6}>
                <XAxis type="number" hide domain={[0, "dataMax"]} />
                <YAxis type="category" dataKey="label" width={150} tickLine={false} axisLine={false}
                       tick={{ fill: "var(--ink-2)", fontSize: 12 }} />
                <Tooltip
                  cursor={{ fill: "var(--surface-2)" }}
                  content={({ active, payload }) =>
                    active && payload?.length ? (
                      <div className="rounded-md border border-line bg-surface px-2.5 py-1.5 text-xs shadow">
                        <div className="text-ink-2">{payload[0].payload.label}</div>
                        <div className="tabular font-medium text-ink">
                          {money(payload[0].payload.cents)}
                          {total ? ` · ${Math.round((payload[0].payload.cents / total) * 100)}%` : ""}
                        </div>
                      </div>
                    ) : null
                  }
                />
                <Bar dataKey="cents" fill="var(--series-1)" radius={[0, 4, 4, 0]} maxBarSize={18} isAnimationActive={false}
                     cursor={onSelect ? "pointer" : undefined}
                     onClick={(d) => onSelect?.((d as unknown as Item).label)}
                     label={{ position: "right", fill: "var(--ink-2)", fontSize: 12,
                              formatter: (v: unknown) => money(Number(v), { whole: true }) }} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
