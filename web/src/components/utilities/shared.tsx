// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

import { AlertTriangle, Info, Megaphone } from "lucide-react";
import type { UtilityInsight } from "@/api";
import { Badge } from "@/components/ui/badge";
import { listColors } from "@/lib/palette";

// One palette slot per service, in this fixed order, so a service keeps its colour on every
// chart (docs/SPEC-utilities.md §10).
export const SERVICES = ["electric", "gas", "water", "trash", "internet", "phone", "other"];
export const SERVICE_LABELS: Record<string, string> = {
  electric: "Electricity", gas: "Gas", water: "Water & sewer", trash: "Trash",
  internet: "Internet", phone: "Phone", other: "Other",
};

export function serviceColors(rows: { key: string; cents: number }[]) {
  return listColors(SERVICES, rows);
}

export const shortDate = (iso: string) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric" });

const SEVERITY = {
  warning: { icon: AlertTriangle, word: "Warning", cls: "text-critical" },
  notice: { icon: Megaphone, word: "Notice", cls: "text-ink" },
  info: { icon: Info, word: "Info", cls: "text-ink-2" },
} as const;

/** Severity as an icon and a word, never colour alone. */
export function Severity({ level }: { level: UtilityInsight["severity"] }) {
  const s = SEVERITY[level];
  const Icon = s.icon;
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 text-xs font-medium ${s.cls}`}>
      <Icon className="h-3.5 w-3.5" aria-hidden /> {s.word}
    </span>
  );
}

export function Chip({ children, tone }: { children: React.ReactNode; tone?: "warning" }) {
  return (
    <Badge className={tone === "warning" ? "bg-warning-bg text-ink" : undefined}>{children}</Badge>
  );
}
