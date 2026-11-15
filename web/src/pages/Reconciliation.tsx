// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useQuery } from "@tanstack/react-query";
import { Check, Minus, X } from "lucide-react";
import { api, type BankReconciliation, type Meta } from "@/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { money, monthLabel } from "@/lib/utils";

function Status({ ok, label, neutral }: { ok: boolean; label: string; neutral?: boolean }) {
  // Status never by color alone: icon + label.
  if (neutral) {
    return <span className="inline-flex items-center gap-1 text-ink-2"><Minus className="h-4 w-4" />{label}</span>;
  }
  return (
    <span className={ok ? "inline-flex items-center gap-1 text-good" : "inline-flex items-center gap-1 text-critical"}>
      {ok ? <Check className="h-4 w-4" /> : <X className="h-4 w-4" />}{label}
    </span>
  );
}

export function Reconciliation({ meta }: { meta: Meta }) {
  const { data: statements = [] } = useQuery({ queryKey: ["reconciliation"], queryFn: api.reconciliation });
  const { data: sources = [] } = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const { data: bank } = useQuery({ queryKey: ["reconciliation-bank"], queryFn: api.bankReconciliation });
  const cardName = Object.fromEntries(meta.cards.map((c) => [c.id, c.name]));
  const byCard = statements.reduce<Record<string, typeof statements>>((acc, s) => {
    (acc[s.card_id] ??= []).push(s);
    return acc;
  }, {});
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      {bank && bank.accounts.length > 0 && <Checking bank={bank} cardName={cardName} />}
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

/** The checking account's proofs (docs/SPEC-checking.md §11, §13). */
function Checking({ bank, cardName }: { bank: BankReconciliation; cardName: Record<string, string> }) {
  const pays = bank.card_payments.reduce((a, r) => ({ n: a.n + r.card_payments, matched: a.matched + r.matched }),
                                         { n: 0, matched: 0 });
  const several = bank.accounts.length > 1;
  const ledger = (s: BankReconciliation["statements"][number]) =>
    s.ledger_status === "match" ? <Status ok label="matches" />
      : s.ledger_status === "incomplete" ? <Status ok neutral label="incomplete: the exports don't cover the whole period" />
        : s.ledger_status === "mismatch" ? <Status ok={false} label={`off${s.ledger_delta_cents ? ` (${money(s.ledger_delta_cents, { sign: true })})` : ""}`} />
          : <Status ok neutral label="not checked" />;
  return (
    <Card className="lg:col-span-3">
      <CardHeader><CardTitle>{several ? "Checking" : cardName[bank.accounts[0]] ?? "Checking"}</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                {several && <th className="py-2 font-medium">Account</th>}<th className="py-2 font-medium">Period</th>
                <th className="pl-4 text-right font-medium">Opening</th><th className="pl-4 text-right font-medium">Money in</th>
                <th className="pl-4 text-right font-medium">Money out</th><th className="pl-4 text-right font-medium">Closing</th>
                <th className="pl-4 font-medium">Statement math</th><th className="font-medium">Ledger</th>
              </tr>
            </thead>
            <tbody>
              {bank.statements.map((s) => (
                <tr key={s.id} className="border-b border-line last:border-0">
                  {several && <td className="py-2 text-ink">{cardName[s.card_id] ?? s.card_id}</td>}
                  <td className="whitespace-nowrap py-2 tabular text-ink-2">{s.period_start} → {s.period_end}</td>
                  <td className="pl-4 text-right tabular">{money(s.opening_cents)}</td>
                  <td className="pl-4 text-right tabular">{money(s.deposits_cents)}</td>
                  <td className="pl-4 text-right tabular">{money(s.withdrawals_cents)}</td>
                  <td className="pl-4 text-right tabular">{money(s.closing_cents)}</td>
                  <td className="pl-4"><Status ok={s.math_ok} label={s.math_ok ? "balances" : "off"} /></td>
                  <td>{ledger(s)}</td>
                </tr>
              ))}
              {!bank.statements.length && (
                <tr><td colSpan={several ? 8 : 7} className="py-3 text-muted">No checking statements yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        {bank.balance && (
          <div className="text-sm">
            <Status ok={!bank.balance.gaps.length}
                    label={bank.balance.gaps.length
                      ? `Running balance breaks on ${bank.balance.gaps.join(", ")}: a row is missing or doubled around these days`
                      : `Running balance: ${bank.balance.days} days checked, no gaps`} />
          </div>
        )}
        {pays.n > 0 && (
          <div className="text-sm">
            <p className="text-ink">
              Card payments: {pays.matched} of {pays.n} matched to checking;
              {" "}{pays.n - pays.matched} came from elsewhere or aren't synced yet.
            </p>
            <table className="mt-2 text-xs text-ink-2">
              <thead>
                <tr className="text-left text-muted">
                  <th className="pr-6 font-medium">Month</th><th className="pr-6 font-medium">Card payments matched</th>
                  <th className="font-medium">Checking payments linked</th>
                </tr>
              </thead>
              <tbody>
                {bank.card_payments.map((r) => (
                  <tr key={r.month}>
                    <td className="pr-6">{monthLabel(r.month)}</td>
                    <td className="pr-6 tabular">{r.matched} of {r.card_payments}</td>
                    <td className="tabular">{r.checking_linked} of {r.checking_payments}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {bank.checking_starts && <p className="mt-1 text-xs text-muted">Checking starts {bank.checking_starts}.</p>}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
