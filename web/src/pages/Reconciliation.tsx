import { useQuery } from "@tanstack/react-query";
import { Check, X } from "lucide-react";
import { api, type Meta } from "@/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { money } from "@/lib/utils";

function Status({ ok, label }: { ok: boolean; label: string }) {
  // Status never by color alone: icon + label.
  return (
    <span className={ok ? "inline-flex items-center gap-1 text-good" : "inline-flex items-center gap-1 text-critical"}>
      {ok ? <Check className="h-4 w-4" /> : <X className="h-4 w-4" />}{label}
    </span>
  );
}

export function Reconciliation({ meta }: { meta: Meta }) {
  const { data: statements = [] } = useQuery({ queryKey: ["reconciliation"], queryFn: api.reconciliation });
  const { data: sources = [] } = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const cardName = Object.fromEntries(meta.cards.map((c) => [c.id, c.name]));
  const byCard = statements.reduce<Record<string, typeof statements>>((acc, s) => {
    (acc[s.card_id] ??= []).push(s);
    return acc;
  }, {});
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card className="lg:col-span-2">
        <CardHeader><CardTitle>Statements</CardTitle></CardHeader>
        <CardContent>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="py-2 font-medium">Card</th><th className="font-medium">Period</th>
                <th className="text-right font-medium">New balance</th>
                <th className="pl-4 font-medium">Statement math</th><th className="font-medium">Ledger</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(byCard).map(([card, rows]) => rows.map((s, i) => (
                <tr key={s.id} className="border-b border-line last:border-0">
                  <td className="py-2 text-ink">{i === 0 ? cardName[card] ?? card : ""}</td>
                  <td className="tabular text-ink-2">{s.period_start} → {s.period_end}</td>
                  <td className="text-right tabular">{money(s.new_balance_cents)}</td>
                  <td className="pl-4"><Status ok={s.math_ok} label={s.math_ok ? "balances" : "off"} /></td>
                  <td>
                    <Status ok={s.ledger_status === "match"}
                            label={s.ledger_status === "match" ? "matches"
                              : `${s.ledger_status ?? "not checked"}${s.ledger_delta_cents ? ` (${money(s.ledger_delta_cents, { sign: true })})` : ""}`} />
                  </td>
                </tr>
              )))}
            </tbody>
          </table>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Last sync per site</CardTitle></CardHeader>
        <CardContent className="divide-y divide-line text-sm">
          {sources.map((s) => (
            <div key={s.site} className="py-2">
              <div className="flex items-center justify-between">
                <span className="font-medium capitalize text-ink">{s.site}</span>
                <span className="text-xs text-ink-2">{s.last_run ? s.last_run.slice(0, 10) : "never"}</span>
              </div>
              {s.last_run && (
                <div className="mt-0.5 text-xs text-muted">
                  {s.status} · {s.files_imported} files · {s.guard_blocks} guard blocks
                  {s.open_gaps > 0 && <> · <span className="text-ink">{s.open_gaps} open gaps</span></>}
                </div>
              )}
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
