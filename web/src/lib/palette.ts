import type { Meta } from "@/api";

// Categorical colours: CSS variables, validated in light and dark (dataviz validate_palette.js).
// A chart colours its top MAX entities with the first MAX slots, ordered by a stable rank
// (all-time category spend, card order, person order), so slots are never skipped and every
// touching pair is a validated pair. Everything else is the neutral "Other".
export const MAX = 5;
export const OTHER = "var(--cat-other)";
export const SINGLE = "var(--series-1)";
// Slots that pass the all-pairs check (validate_palette.js --pairs all) in light and dark:
// the only colours a treemap, where any two tiles can touch, may use.
export const TREEMAP_SAFE = ["var(--cat-1)", "var(--cat-2)", "var(--cat-3)"];

export type Colors = { color: (key: string | null | undefined) => string; index: (key: string) => number };

export function colorMap(rows: { key: string; cents: number }[], rank: (key: string) => number,
                         max = MAX): Colors {
  const totals = new Map<string, number>();
  for (const r of rows) if (r.cents > 0) totals.set(r.key, (totals.get(r.key) ?? 0) + r.cents);
  const chosen = [...totals].sort((a, b) => b[1] - a[1]).slice(0, max).map(([k]) => k)
    .sort((a, b) => rank(a) - rank(b));
  const at = new Map(chosen.map((k, i) => [k, i]));
  return {
    color: (key) => (key != null && at.has(key) ? `var(--cat-${at.get(key)! + 1})` : OTHER),
    index: (key) => at.get(key) ?? MAX,
  };
}

export const parentOf = (categoryId: string | null | undefined) => (categoryId ?? "uncategorized").split(".")[0];

// Parent categories of these rows. "Uncategorized" always stays neutral.
export function categoryColors(meta: Meta, rows: { key: string; cents: number }[]): Colors {
  const rank = (k: string) => {
    const i = (meta.category_rank ?? []).indexOf(k);
    return i < 0 ? Number.MAX_SAFE_INTEGER : i;
  };
  const m = colorMap(rows.map((r) => ({ key: parentOf(r.key), cents: r.cents }))
                         .filter((r) => r.key !== "uncategorized"), rank);
  return { color: (k) => m.color(parentOf(k)), index: (k) => m.index(parentOf(k)) };
}

export const listColors = (ids: string[], rows: { key: string; cents: number }[]) =>
  colorMap(rows, (k) => ids.indexOf(k));
