import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useState } from "react";
import { api, type Meta } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";

export function Rules({ meta }: { meta: Meta }) {
  const qc = useQueryClient();
  const { data: rules = [] } = useQuery({ queryKey: ["rules"], queryFn: api.rules });
  const [pattern, setPattern] = useState("");
  const [category, setCategory] = useState("");
  const [note, setNote] = useState("");
  const catName = Object.fromEntries(meta.categories.map((c) => [c.id, c.name]));
  const { data: preview } = useQuery({
    queryKey: ["rulePreview", pattern], queryFn: () => api.previewRule(pattern),
    enabled: pattern.trim().length >= 2, retry: false,
  });
  const refresh = () => qc.invalidateQueries();
  const add = useMutation({
    mutationFn: async () => {
      await api.createRule({ pattern, category_id: category });
      return api.reprocess();
    },
    onSuccess: () => { setNote("Rule saved and applied."); setPattern(""); refresh(); },
    onError: (e) => setNote(String(e)),
  });
  const del = useMutation({
    mutationFn: async (id: number) => { await api.deleteRule(id); return api.reprocess(); },
    onSuccess: refresh,
  });
  const mine = rules.filter((r) => r.created_by === "user");
  const seed = rules.filter((r) => r.created_by === "seed");
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader><CardTitle>New rule</CardTitle></CardHeader>
        <CardContent className="space-y-3 text-sm">
          <p className="text-xs text-muted">
            Matches the start of the cleaned description, case-insensitive (a regular expression).
            Your rules win over the seed rules.
          </p>
          <div className="flex flex-wrap gap-2">
            <input className="h-9 flex-1 rounded-md border border-line bg-surface px-2 font-mono text-sm"
                   placeholder="e.g. TRADER JOE" value={pattern} onChange={(e) => setPattern(e.target.value)}
                   aria-label="Pattern" />
            <Select value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
              <option value="" disabled>Category…</option>
              {meta.categories.map((c) => (
                <option key={c.id} value={c.id}>{c.parent_id ? `${catName[c.parent_id]} › ${c.name}` : c.name}</option>
              ))}
            </Select>
            <Button disabled={!pattern || !category} onClick={() => add.mutate()}>Save &amp; apply</Button>
          </div>
          {preview && (
            <div className="rounded-md bg-surface-2 p-2 text-xs">
              <div className="mb-1 text-ink-2">Matches {preview.transactions} transactions</div>
              {preview.descriptions.map(([d, n]) => (
                <div key={d} className="flex justify-between"><span className="font-mono">{d}</span><span className="tabular text-muted">{n}</span></div>
              ))}
            </div>
          )}
          {note && <p className="text-xs text-ink-2">{note}</p>}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Your rules <Badge className="ml-1.5">{mine.length}</Badge></CardTitle></CardHeader>
        <CardContent className="divide-y divide-line text-sm">
          {mine.length === 0 && <p className="text-muted">None yet. Corrections with "Create rule" also land here.</p>}
          {mine.map((r) => (
            <div key={r.id} className="flex items-center gap-3 py-1.5">
              <span className="font-mono text-xs">{r.pattern}</span>
              <span className="text-xs text-ink-2">→ {catName[r.category_id ?? ""] ?? r.category_id}</span>
              <Button className="ml-auto" variant="ghost" size="icon" aria-label="Delete rule"
                      onClick={() => del.mutate(r.id)}><Trash2 className="h-4 w-4" /></Button>
            </div>
          ))}
        </CardContent>
      </Card>
      <Card className="lg:col-span-2">
        <CardHeader><CardTitle>Seed rules <span className="text-xs font-normal text-muted">(config/rules.yaml)</span></CardTitle></CardHeader>
        <CardContent>
          <table className="w-full text-xs">
            <tbody>
              {seed.map((r) => (
                <tr key={r.id} className="border-b border-line last:border-0">
                  <td className="py-1 pr-3 font-mono">{r.pattern}</td>
                  <td className="pr-3 text-ink-2">{r.merchant_name}</td>
                  <td className="pr-3 text-muted">{r.merchant_group ?? ""}</td>
                  <td className="text-ink-2">{catName[r.category_id ?? ""] ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  );
}
