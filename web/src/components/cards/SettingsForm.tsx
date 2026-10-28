// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

// What I tell the app that documents can't (docs/SPEC-cards.md §13.3): point valuations,
// pooling, memberships and enrollments, anniversary months. Saved as card_facts.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type CardSettings } from "@/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function SettingsForm() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["cardSettings"], queryFn: api.cardSettings });
  const [note, setNote] = useState("");
  const save = useMutation({
    mutationFn: (body: unknown) => api.putCardSettings(body),
    onSuccess: (s) => { qc.setQueryData(["cardSettings"], s); qc.invalidateQueries({ queryKey: ["best"] }); setNote("Saved."); },
    onError: (e) => setNote(String(e)),
  });
  if (!data) return null;
  return (
    <Card>
      <CardHeader><CardTitle>Your valuations and facts</CardTitle></CardHeader>
      <CardContent className="space-y-5 text-sm">
        <section>
          <h3 className="mb-1 font-medium">What a point is worth to you</h3>
          <p className="mb-2 text-xs text-ink-2">Cents per point. 1¢ is a cash redemption; transfers to airlines or hotels can be worth more.</p>
          <div className="flex flex-wrap gap-4">
            {data.valuations.filter((v) => v.currency !== "cash_back").map((v) => (
              <Valuation key={v.currency} v={v} onSave={(cents) => save.mutate({ valuations: [{ currency: v.currency, cents }] })} />
            ))}
          </div>
        </section>
        <section className="space-y-3">
          <h3 className="font-medium">Per card</h3>
          {data.cards.filter((c) => c.currency).map((c) => (
            <CardFacts key={c.card_id} c={c} currencies={data.valuations}
                       onSave={(patch) => save.mutate({ cards: [{ card_id: c.card_id, ...patch }] })} />
          ))}
        </section>
        {note && <p className="text-xs text-muted">{note}</p>}
      </CardContent>
    </Card>
  );
}

function Valuation({ v, onSave }: { v: CardSettings["valuations"][number]; onSave: (cents: number) => void }) {
  const [val, setVal] = useState(String(v.cents));
  return (
    <label className="flex items-center gap-2">
      <span>{v.name}</span>
      <input value={val} inputMode="decimal" onChange={(e) => setVal(e.target.value)}
             onBlur={() => { const n = parseFloat(val); if (n > 0 && n <= 10 && n !== v.cents) onSave(n); }}
             className="h-8 w-16 rounded-md border border-line bg-surface px-2 tabular" aria-label={`${v.name} cents`} />
      <span className="text-muted">¢</span>
    </label>
  );
}

function CardFacts({ c, currencies, onSave }: {
  c: CardSettings["cards"][number]; currencies: CardSettings["valuations"];
  onSave: (patch: Record<string, unknown>) => void;
}) {
  return (
    <div className="space-y-2 rounded-lg border border-line p-3">
      <div className="font-medium">{c.product}</div>
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2">
          Value its rewards as
          <Select value={c.pooled_into ?? ""} aria-label="Pooled into"
                  onChange={(e) => onSave({ pooled_into: e.target.value || null })}>
            <option value="">its own ({c.currency?.replace(/_/g, " ")})</option>
            {currencies.filter((x) => x.currency !== c.currency).map((x) => (
              <option key={x.currency} value={x.currency}>{x.name} (moved to another card)</option>
            ))}
          </Select>
        </label>
        <label className="flex items-center gap-2">
          Card anniversary
          <Select value={c.anniversary_month ?? ""} aria-label="Anniversary month"
                  onChange={(e) => onSave({ anniversary_month: e.target.value ? Number(e.target.value) : null })}>
            <option value="">not set</option>
            {MONTHS.map((m, i) => <option key={m} value={i + 1}>{m}</option>)}
          </Select>
        </label>
      </div>
      {c.requirements.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          {c.requirements.map((r) => (
            <label key={r.ref} className="flex items-center gap-2">
              <span>{r.label} <span className="text-xs text-muted">({r.for})</span></span>
              <Select value={r.value ?? ""} aria-label={r.label}
                      onChange={(e) => onSave({ requirements: [{ ref: r.ref, kind: r.kind, value: e.target.value || null }] })}>
                <option value="">not said</option>
                <option value="yes">{r.kind === "membership" ? "I have it" : "enrolled"}</option>
                <option value="no">{r.kind === "membership" ? "I don't" : "not enrolled"}</option>
              </Select>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
