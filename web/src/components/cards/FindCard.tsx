// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

// "Find a card" (docs/SPEC-cards.md §13.1): type where you're paying, get your cards ranked.
// Searching never calls Claude; only "Ask Claude" / "Look it up on the web" do, once per merchant.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, type BestAnswer, type BestEvidence, type BestResult, type MerchantInfo } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { cn, money } from "@/lib/utils";

const words = (id: string) => id.replace(/_/g, " ");
const NETWORKS = ["visa", "mastercard", "amex", "discover"];
const NETWORK_NAMES: Record<string, string> = { visa: "Visa", mastercard: "Mastercard", amex: "Amex", discover: "Discover" };

export function FindCard() {
  // ?q=costco+gas opens with that search (a bookmark, or a link from elsewhere).
  const initial = new URLSearchParams(window.location.search).get("q")?.trim() || "";
  const [text, setText] = useState(initial);
  const [query, setQuery] = useState<{ q: string; key?: string } | null>(initial ? { q: initial } : null);
  const [amount, setAmount] = useState("100");
  const [channel, setChannel] = useState("");
  const [abroad, setAbroad] = useState(false);
  const [open, setOpen] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const cents = Math.round(parseFloat(amount || "0") * 100) || undefined;
  const { data: suggestions = [] } = useQuery({
    queryKey: ["merchantSearch", text], queryFn: () => api.merchantSearch(text),
    enabled: open && text.trim().length >= 2,
  });
  const { data: answer, isFetching, error } = useQuery({
    queryKey: ["best", query, cents, channel, abroad],
    queryFn: () => api.best({ q: query!.q, key: query!.key, amount_cents: cents, channel, abroad }),
    enabled: !!query,
  });
  const pick = (q: string, key?: string) => { setText(q); setQuery({ q, key }); setOpen(false); };

  return (
    <div className="space-y-4">
      <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); if (text.trim()) pick(text.trim()); }}>
        <div className="relative min-w-[16rem] flex-1">
          <label className="mb-1 block text-xs text-muted" htmlFor="where">Where are you paying?</label>
          <Search className="pointer-events-none absolute bottom-3 left-3 h-4 w-4 text-muted" />
          <input id="where" ref={input} value={text} autoComplete="off"
                 placeholder="Whole Foods, Costco gas, Delta flight…   (press / to search)"
                 onChange={(e) => { setText(e.target.value); setOpen(true); }}
                 onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 150)}
                 className="h-11 w-full rounded-lg border border-line bg-surface pl-9 pr-3 text-base text-ink outline-none focus:border-series-1" />
          {open && suggestions.length > 0 && (
            <ul className="absolute z-10 mt-1 w-full overflow-hidden rounded-lg border border-line bg-surface shadow-lg">
              {suggestions.map((sug) => (
                <li key={sug.key}>
                  <button type="button" className="flex w-full justify-between px-3 py-2 text-left text-sm hover:bg-surface-2"
                          onMouseDown={() => pick(sug.display_name, sug.key)}>
                    <span>{sug.display_name}</span><span className="text-xs text-muted">{sug.alias}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted" htmlFor="amount">Amount ($)</label>
          <input id="amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)}
                 className="h-11 w-28 rounded-lg border border-line bg-surface px-3 text-base tabular text-ink outline-none focus:border-series-1" />
        </div>
        <Button type="submit" className="h-11">Find</Button>
      </form>

      {answer?.results && (answer.asks_channel || answer.asks_abroad || channel || abroad) && (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          {(answer.asks_channel || channel) && (
            <Select value={channel} onChange={(e) => setChannel(e.target.value)} aria-label="How you're paying">
              <option value="">Any way of paying</option>
              <option value="in_store">In store</option>
              <option value="online">Online</option>
              <option value="in_app">In the app</option>
            </Select>
          )}
          {(answer.asks_abroad || abroad) && (
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={abroad} onChange={(e) => setAbroad(e.target.checked)} />
              Outside the U.S.
            </label>
          )}
        </div>
      )}

      {error && <p className="text-sm text-critical">{String(error)}</p>}
      {isFetching && !answer && <p className="text-sm text-muted">Looking…</p>}
      {answer && answer.needs_lookup && (
        <Unknown answer={answer} onPick={pick} />
      )}
      {answer && !answer.needs_lookup && answer.results && <Answer answer={answer} />}
      {!query && (
        <p className="text-sm text-ink-2">
          Searches use the card terms stored on this Mac. Nothing goes online unless you ask Claude about a merchant it doesn't know.
        </p>
      )}
    </div>
  );
}

function Unknown({ answer, onPick }: { answer: BestAnswer; onPick: (q: string, key?: string) => void }) {
  const [note, setNote] = useState("");
  const look = useMutation({
    mutationFn: (web: boolean) => api.lookupMerchant(answer.query, web),
    onSuccess: (r) => {
      if (r.not_a_merchant || !r.merchant) setNote("That doesn't look like a merchant.");
      else onPick(answer.query, r.merchant.key);
    },
    onError: (e) => setNote(String(e)),
  });
  return (
    <Card>
      <CardContent className="space-y-3 pt-4">
        <p className="font-medium">I don't know “{answer.query}” yet.</p>
        {answer.suggestions.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-ink-2">Did you mean</span>
            {answer.suggestions.map((s) => (
              <Button key={s.key} size="sm" variant="outline" onClick={() => onPick(s.display_name, s.key)}>{s.display_name}</Button>
            ))}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled={look.isPending} onClick={() => look.mutate(false)}>
            {look.isPending && look.variables === false ? "Asking Claude…" : "Ask Claude"}
          </Button>
          <Button size="sm" variant="outline" disabled={look.isPending} onClick={() => look.mutate(true)}>
            {look.isPending && look.variables === true ? "Searching the web…" : "Look it up on the web"}
          </Button>
          <span className="text-xs text-muted">Sends only the name, once (haiku). About 10 seconds; the web lookup is slower.</span>
        </div>
        {note && <p className="text-sm text-critical">{note}</p>}
      </CardContent>
    </Card>
  );
}

function Quote({ ev }: { ev: BestEvidence }) {
  if (!ev) return null;
  return (
    <details className="text-xs">
      <summary className="cursor-pointer text-ink-2 underline-offset-2 hover:underline">Source</summary>
      <blockquote className="mt-1 max-w-md rounded-md border border-line bg-surface-2 p-2 text-ink">
        “{ev.quote}”
        {ev.doc_version_id && (
          <div className="mt-0.5 text-muted">
            <a className="underline" target="_blank" rel="noreferrer"
               href={`/api/cards/documents/${ev.doc_version_id}${ev.page ? `#page=${ev.page}` : ""}`}>open the document</a>
          </div>
        )}
      </blockquote>
    </details>
  );
}

function Caveats({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((c) => <Badge key={c}>{c}</Badge>)}
    </div>
  );
}

function Answer({ answer }: { answer: BestAnswer }) {
  const results = answer.results ?? [];
  const [top, ...rest] = results;
  const amount = answer.amount_cents ?? 10_000;
  // Protections of the top three cards only: the list is advice, not an inventory.
  const benefits = results.slice(0, 3).flatMap((r) => r.benefits.map((b) => ({ ...b, product: r.product })));
  return (
    <div className="space-y-4">
      {answer.merchant && <MerchantLine m={answer.merchant} />}
      {top ? <Winner r={top} amount={amount} /> : <p className="text-sm text-ink-2">No card terms yet.</p>}
      {answer.break_even && <p className="text-sm text-ink-2">{answer.break_even}</p>}
      {rest.length > 0 && (
        <Card>
          <CardContent className="overflow-x-auto pt-3">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted">
                <tr><th className="py-1 pr-3 font-normal">#</th><th className="pr-3 font-normal">Card</th>
                    <th className="pr-3 text-right font-normal">Value</th><th className="pr-3 text-right font-normal">¢/$</th>
                    <th className="font-normal">Why</th></tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rest.map((r) => (
                  <tr key={r.card_id} className="align-top">
                    <td className="py-1.5 pr-3 tabular text-muted">{r.rank}</td>
                    <td className="py-1.5 pr-3">{r.product}</td>
                    <td className="py-1.5 pr-3 text-right tabular">{money(r.total_cents)}</td>
                    <td className="py-1.5 pr-3 text-right tabular">{r.cents_per_dollar.toFixed(1)}</td>
                    <td className="space-y-1 py-1.5 text-ink-2">
                      <div>{r.rule?.text}</div>
                      {r.credit && <div className="text-xs">{r.credit.text}</div>}
                      {r.alternatives.map((a) => <div key={a.text} className="text-xs">{a.text} = {money(a.cents)}</div>)}
                      <Caveats items={r.caveats} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
      {(answer.not_accepted?.length ?? 0) > 0 && (
        <p className="text-sm text-ink-2">
          <span className="font-medium text-ink">Not accepted here: </span>
          {answer.not_accepted!.map((x) => `${x.product} (${NETWORK_NAMES[x.network] ?? x.network})`).join(", ")}
        </p>
      )}
      {benefits.length > 0 && (
        <section>
          <h3 className="mb-1 text-sm font-medium">Also consider</h3>
          <ul className="space-y-1 text-sm text-ink-2">
            {benefits.map((b) => (
              <li key={b.product + b.kind}>
                <span className="text-ink">{b.product}</span>: {b.name}{b.coverage ? ` (${b.coverage})` : ""}{b.limits ? `, ${b.limits}` : ""}
              </li>
            ))}
          </ul>
        </section>
      )}
      <Freshness kb={answer.kb} />
    </div>
  );
}

function Winner({ r, amount }: { r: BestResult; amount: number }) {
  return (
    <Card className="border-series-1">
      <CardContent className="space-y-2 pt-4">
        <div className="text-xs uppercase tracking-wide text-muted">Use</div>
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h2 className="text-xl font-semibold">{r.product}</h2>
          <span className="text-2xl font-semibold tabular">{r.cents_per_dollar.toFixed(1)}¢ per $1</span>
          <span className="text-ink-2 tabular">{money(r.total_cents)} back on {money(amount)}</span>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span>{r.rule?.text}</span>
          {r.rule && <Quote ev={r.rule.evidence} />}
        </div>
        {r.credit && (
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span>{r.credit.text}{r.credit.needs_enrollment ? " · needs enrollment" : ""}</span>
            <Quote ev={r.credit.evidence} />
          </div>
        )}
        {r.alternatives.map((a) => (
          <div key={a.text} className="text-sm text-ink-2">Or: {a.text} = {money(a.cents)}</div>
        ))}
        <Caveats items={r.caveats} />
      </CardContent>
    </Card>
  );
}

function MerchantLine({ m }: { m: MerchantInfo }) {
  const qc = useQueryClient();
  const [edit, setEdit] = useState(false);
  const { data: settings } = useQuery({ queryKey: ["cardSettings"], queryFn: api.cardSettings, enabled: edit });
  const save = useMutation({
    mutationFn: (body: { category?: string; acceptance?: Record<string, string> }) => api.patchMerchant(m.key, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["best"] }),
  });
  const source = { seed: "built-in list", ai: "Claude", ai_web: "Claude + web", user: "your correction", kb: "card documents" }[m.source] ?? m.source;
  return (
    <div className="space-y-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{m.display_name}</span>
        <span className="text-ink-2">· {words(m.reward_category ?? "unknown")} · from {source}</span>
        {m.unsure && <Badge className="border-transparent bg-warning-bg text-ink">Not sure about this one</Badge>}
        <Button size="sm" variant="ghost" onClick={() => setEdit(!edit)}>{edit ? "Done" : "Change"}</Button>
      </div>
      {edit && settings && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-line p-3">
          <Select value={m.reward_category ?? ""} aria-label="Reward category"
                  onChange={(e) => save.mutate({ category: e.target.value })}>
            {settings.reward_categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </Select>
          {NETWORKS.map((n) => (
            <label key={n} className="flex items-center gap-1.5">
              {NETWORK_NAMES[n]}
              <Select value={m.acceptance[n] ?? "unknown"} aria-label={`${NETWORK_NAMES[n]} accepted`}
                      onChange={(e) => save.mutate({ acceptance: { [n]: e.target.value } })}>
                <option value="yes">accepted</option>
                <option value="no">not accepted</option>
                <option value="unknown">don't know</option>
              </Select>
            </label>
          ))}
          <span className="text-xs text-muted">Your changes are kept through refreshes.</span>
        </div>
      )}
    </div>
  );
}

function Freshness({ kb }: { kb: BestAnswer["kb"] }) {
  const days = kb.last_checked ? Math.floor((Date.now() - Date.parse(kb.last_checked)) / 86_400_000) : null;
  return (
    <p className={cn("text-xs", kb.stale.length ? "text-critical" : "text-muted")}>
      {days === null ? "Card terms never checked." : `Terms checked ${days === 0 ? "today" : `${days} day${days === 1 ? "" : "s"} ago`}.`}
      {kb.stale.length > 0 && ` Stale: ${kb.stale.join(", ")}. Refresh them under Card terms.`}
      {kb.without_terms.length > 0 && ` No terms yet: ${kb.without_terms.join(", ")}.`}
    </p>
  );
}
