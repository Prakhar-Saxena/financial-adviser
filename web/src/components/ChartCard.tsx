import { BarChart3, LayoutGrid, PieChart as PieIcon, Table2 } from "lucide-react";
import { useState } from "react";
import {
  Bar, BarChart, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, Treemap, XAxis, YAxis,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { MAX, OTHER, SINGLE } from "@/lib/palette";
import { cn, money } from "@/lib/utils";

export type Item = { key: string; label: string; cents: number; color?: string; slot?: number };
export type View = "bar" | "donut" | "treemap" | "table";

const ICONS: Record<View, typeof BarChart3> = {
  bar: BarChart3, donut: PieIcon, treemap: LayoutGrid, table: Table2,
};
const NAMES: Record<View, string> = { bar: "Bars", donut: "Donut", treemap: "Treemap", table: "Table" };

function useView(id: string, views: View[]): [View, (v: View) => void] {
  const [view, setView] = useState<View>(() => {
    try {
      const v = localStorage.getItem(`chart:${id}`) as View | null;
      return v && views.includes(v) ? v : views[0];
    } catch { return views[0]; }
  });
  return [view, (v) => {
    setView(v);
    try { localStorage.setItem(`chart:${id}`, v); } catch { /* private mode */ }
  }];
}

export function ViewSwitch({ views, view, onChange }: {
  views: View[]; view: View; onChange: (v: View) => void;
}) {
  return (
    <div className="flex rounded-lg border border-line bg-surface-2 p-0.5" role="group" aria-label="Chart type">
      {views.map((v) => {
        const Icon = ICONS[v];
        return (
          <button key={v} onClick={() => onChange(v)} aria-pressed={view === v} title={NAMES[v]}
                  className={cn("rounded-md p-1.5 text-muted transition-colors hover:text-ink",
                                view === v && "bg-surface text-ink shadow-sm")}>
            <Icon className="h-3.5 w-3.5" />
          </button>
        );
      })}
    </div>
  );
}

function Tip({ item, total }: { item: Item; total: number }) {
  return (
    <div className="rounded-md border border-line bg-surface px-2.5 py-1.5 text-xs shadow">
      <div className="flex items-center gap-1.5 text-ink-2">
        <span className="h-2 w-2 rounded-full" style={{ background: item.color ?? SINGLE }} />
        {item.label}
      </div>
      <div className="tabular font-medium text-ink">
        {money(item.cents)}{total ? ` · ${Math.round((item.cents / total) * 100)}%` : ""}
      </div>
    </div>
  );
}

export function ChartCard({ id, title, items, views = ["bar", "donut", "treemap", "table"], onSelect,
                            subtitle }: {
  id: string; title: string; items: Item[]; views?: View[]; onSelect?: (key: string) => void;
  subtitle?: string;
}) {
  const [view, setView] = useView(id, views);
  const data = items.filter((i) => i.cents > 0).sort((a, b) => b.cents - a.cents);
  const total = data.reduce((s, i) => s + i.cents, 0);
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{title}</CardTitle>
          {subtitle && <div className="mt-0.5 text-xs text-muted">{subtitle}</div>}
        </div>
        <ViewSwitch views={views} view={view} onChange={setView} />
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <p className="text-sm text-muted">Nothing in this period.</p>
        ) : view === "donut" ? (
          <Donut data={data} total={total} onSelect={onSelect} />
        ) : view === "treemap" ? (
          <Tree data={data} total={total} onSelect={onSelect} />
        ) : view === "table" ? (
          <TableView data={data} total={total} />
        ) : (
          <Bars data={data} total={total} onSelect={onSelect} />
        )}
      </CardContent>
    </Card>
  );
}

function Bars({ data, total, onSelect }: { data: Item[]; total: number; onSelect?: (k: string) => void }) {
  const height = Math.max(120, data.length * 30 + 16);
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 0, right: 64, bottom: 0, left: 0 }} barCategoryGap={6}>
          <XAxis type="number" hide domain={[0, "dataMax"]} />
          <YAxis type="category" dataKey="label" width={170} tickLine={false} axisLine={false}
                 tick={<BarLabel />} interval={0} />
          <Tooltip cursor={{ fill: "var(--surface-2)" }}
                   content={({ active, payload }) => active && payload?.length
                     ? <Tip item={payload[0].payload as Item} total={total} /> : null} />
          <Bar dataKey="cents" radius={[0, 4, 4, 0]} maxBarSize={18} isAnimationActive={false}
               cursor={onSelect ? "pointer" : undefined}
               onClick={(d) => onSelect?.((d as unknown as Item).key)}
               label={{ position: "right", fill: "var(--ink-2)", fontSize: 12,
                        formatter: (v: unknown) => money(Number(v), { whole: true }) }}>
            {data.map((d) => <Cell key={d.key} fill={d.color ?? SINGLE} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// One line per bar: long names are cut with an ellipsis; the tooltip has the full name.
function BarLabel({ x = 0, y = 0, payload }: { x?: number; y?: number; payload?: { value: string } }) {
  const v = payload?.value ?? "";
  return (
    <text x={x - 6} y={y} dy={4} textAnchor="end" fontSize={12} fill="var(--ink-2)">
      <title>{v}</title>
      {v.length > 22 ? `${v.slice(0, 21)}…` : v}
    </text>
  );
}

function Donut({ data, total, onSelect }: { data: Item[]; total: number; onSelect?: (k: string) => void }) {
  // The coloured entities (at most MAX, slots 1..n) + "Other". Segments run in slot order, so
  // touching segments are validated neighbour pairs; the legend carries identity, not colour alone.
  const coloured = data.filter((d) => d.color && d.color !== OTHER).slice(0, MAX);
  const rest = data.filter((d) => !coloured.includes(d));
  const segs: Item[] = [...coloured].sort((a, b) => (a.slot ?? 99) - (b.slot ?? 99));
  if (rest.length) {
    segs.push({ key: "__other", label: rest.length === 1 ? rest[0].label : `Other (${rest.length})`,
                cents: rest.reduce((s, r) => s + r.cents, 0), color: OTHER });
  }
  return (
    <div className="flex flex-col items-center gap-4 sm:flex-row">
      <div className="relative h-52 w-52 shrink-0">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie data={segs} dataKey="cents" nameKey="label" innerRadius="62%" outerRadius="96%"
                 stroke="var(--surface)" strokeWidth={2} startAngle={90} endAngle={-270}
                 isAnimationActive={false} cursor={onSelect ? "pointer" : undefined}
                 onClick={(d) => { const k = (d as unknown as { key: string }).key; if (k !== "__other") onSelect?.(k); }}>
              {segs.map((s) => <Cell key={s.key} fill={s.color} />)}
            </Pie>
            <Tooltip content={({ active, payload }) => active && payload?.length
              ? <Tip item={payload[0].payload as Item} total={total} /> : null} />
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <div className="text-xs text-muted">Total</div>
          <div className="text-xl font-semibold text-ink">{money(total, { whole: true })}</div>
        </div>
      </div>
      <ul className="w-full space-y-1.5 text-sm">
        {segs.map((s) => (
          <li key={s.key} className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: s.color }} />
            <span className="truncate text-ink-2">{s.label}</span>
            <span className="ml-auto tabular text-ink">{money(s.cents, { whole: true })}</span>
            <span className="w-10 text-right tabular text-xs text-muted">
              {Math.round((s.cents / total) * 100)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Tree({ data, total, onSelect }: { data: Item[]; total: number; onSelect?: (k: string) => void }) {
  // Any two tiles can touch, and no set of more than three palette colours is distinguishable in
  // every pairing, so tiles share one hue: size carries the value, labels carry identity.
  const nodes = data.map((d) => ({ ...d, name: d.label, size: d.cents, color: SINGLE }));
  return (
    <div style={{ height: 260 }}>
      <ResponsiveContainer width="100%" height="100%">
        <Treemap data={nodes} dataKey="size" isAnimationActive={false} aspectRatio={4 / 3}
                 onClick={(n) => onSelect?.((n as unknown as Item).key)}
                 content={<TreeTile total={total} />}>
          <Tooltip content={({ active, payload }) => active && payload?.length
            ? <Tip item={payload[0].payload as Item} total={total} /> : null} />
        </Treemap>
      </ResponsiveContainer>
    </div>
  );
}

function TreeTile(props: { x?: number; y?: number; width?: number; height?: number; color?: string;
                           label?: string; cents?: number; total: number; depth?: number }) {
  const { x = 0, y = 0, width = 0, height = 0, color, label, cents = 0, depth } = props;
  if (depth === 0) return null;
  const fits = width > 84 && height > 40;
  const text = label ?? "";
  return (
    <g>
      <rect x={x + 1} y={y + 1} width={Math.max(0, width - 2)} height={Math.max(0, height - 2)} rx={6}
            fill={color ?? SINGLE} stroke="var(--surface)" strokeWidth={2} />
      {fits && (
        <g>
          {/* Label on a surface-coloured chip so it stays legible on every tile colour. */}
          <rect x={x + 6} y={y + 6} rx={4} width={Math.min(width - 12, 150)} height={32}
                fill="var(--surface)" fillOpacity={0.9} />
          <text x={x + 12} y={y + 19} fontSize={11} fill="var(--ink)">
            {text.length > 20 ? `${text.slice(0, 19)}…` : text}
          </text>
          <text x={x + 12} y={y + 32} fontSize={11} fill="var(--ink-2)">{money(cents, { whole: true })}</text>
        </g>
      )}
    </g>
  );
}

function TableView({ data, total }: { data: Item[]; total: number }) {
  return (
    <table className="w-full text-sm">
      <tbody>
        {data.map((d) => (
          <tr key={d.key} className="border-b border-line last:border-0">
            <td className="py-1.5">
              <span className="mr-2 inline-block h-2 w-2 rounded-full" style={{ background: d.color ?? SINGLE }} />
              <span className="text-ink-2">{d.label}</span>
            </td>
            <td className="py-1.5 text-right tabular">{money(d.cents)}</td>
            <td className="w-16 py-1.5 text-right tabular text-muted">{Math.round((d.cents / total) * 100)}%</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
