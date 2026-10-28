// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

// The "Best card" tab (docs/SPEC-cards.md §13): Find a card, Missed rewards and Card terms.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { useState } from "react";
import {
  api, type CardAnswer, type CardDetail, type CardDoc, type CardProfile, type CardSummary, type DiffItem,
  type EarnRule, type Evidence, type ProfileDiff,
} from "@/api";
import { CardArt } from "@/components/cards/CardArt";
import { FindCard } from "@/components/cards/FindCard";
import { MissedRewards } from "@/components/cards/MissedRewards";
import { SettingsForm } from "@/components/cards/SettingsForm";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn, money } from "@/lib/utils";

type View = "find" | "missed" | "terms";
const VIEWS: { id: View; label: string; phase?: string }[] = [
  { id: "find", label: "Find a card" },
  { id: "missed", label: "Missed rewards" },
  { id: "terms", label: "Card terms" },
];

export function BestCard() {
  const [view, setView] = useState<View>(() => {
    const v = new URLSearchParams(window.location.search).get("view");
    return v === "missed" || v === "terms" ? v : "find";
  });
  return (
    <div className="space-y-4">
      <div role="tablist" aria-label="Best card views" className="inline-flex rounded-lg border border-line bg-surface p-0.5">
        {VIEWS.map((v) => (
          <button key={v.id} role="tab" aria-selected={view === v.id} disabled={!!v.phase}
                  title={v.phase ? `Arrives in Phase ${v.phase}` : undefined}
                  onClick={() => setView(v.id)}
                  className={cn("rounded-md px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-50",
                                view === v.id ? "bg-surface-2 font-medium text-ink" : "text-ink-2 hover:text-ink")}>
            {v.label}
          </button>
        ))}
      </div>
      {view === "find" && <FindCard />}
      {view === "missed" && <MissedRewards />}
      {view === "terms" && <CardTerms />}
    </div>
  );
}

// ------------------------------------------------------------------ Card terms

function CardTerms() {
  const { data: cards, isLoading } = useQuery({ queryKey: ["cardProfiles"], queryFn: api.cardProfiles });
  if (isLoading || !cards) return <p className="text-sm text-muted">Loading…</p>;
  const empty = cards.every((c) => !c.active_version && !c.pending_version && !Object.keys(c.docs).length);
  return (
    <div className="space-y-3">
      {empty && (
        <p className="text-sm text-ink-2">
          No card terms yet. Refresh one card below, or run <code>make cards-refresh CARD=…</code> in a terminal.
        </p>
      )}
      {cards.map((c) => <CardPanel key={c.card_id} summary={c} />)}
      <SettingsForm />
    </div>
  );
}

function daysAgo(stamp: string | null) {
  if (!stamp) return null;
  return Math.floor((Date.now() - Date.parse(stamp)) / 86_400_000);
}

function checkedLabel(stamp: string | null) {
  const d = daysAgo(stamp);
  if (d === null) return "never checked";
  return d === 0 ? "checked today" : `checked ${d} day${d === 1 ? "" : "s"} ago`;
}

function CardPanel({ summary }: { summary: CardSummary }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(summary.pending_version !== null);
  const [runId, setRunId] = useState<number | null>(null);
  const { data: detail } = useQuery({
    queryKey: ["cardProfile", summary.card_id], queryFn: () => api.cardProfile(summary.card_id), enabled: open,
  });
  const start = useMutation({
    mutationFn: () => api.refreshCards(summary.card_id),
    onSuccess: (r) => { setRunId(r.run_id); setOpen(true); },
  });
  const { data: run } = useQuery({
    queryKey: ["cardRefresh", runId], queryFn: () => api.refreshStatus(runId!), enabled: runId !== null,
    refetchInterval: (q) => {
      const st = q.state.data?.status;
      if (st && st !== "running") {
        qc.invalidateQueries({ queryKey: ["cardProfiles"] });
        qc.invalidateQueries({ queryKey: ["cardProfile", summary.card_id] });
        qc.invalidateQueries({ queryKey: ["review"] });
        return false;
      }
      return 1500;
    },
  });
  const running = start.isPending || run?.status === "running";
  return (
    <Card>
      <CardHeader>
        <button className="flex items-center gap-2 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? <ChevronDown className="h-4 w-4 text-muted" /> : <ChevronRight className="h-4 w-4 text-muted" />}
          <CardArt cardId={summary.card_id} product={summary.product} size="md" />
          <CardTitle>{summary.product}</CardTitle>
        </button>
        <div className="flex flex-wrap items-center gap-2">
          {summary.active_version ? <Badge>v{summary.active_version} in use</Badge> : <Badge>No terms yet</Badge>}
          {summary.pending_version && (
            <Badge className="border-transparent bg-warning-bg text-ink">v{summary.pending_version} waiting for you</Badge>
          )}
          {summary.open_items > 0 && <Badge>{summary.open_items} to look at</Badge>}
          <span className={cn("text-xs", summary.stale ? "text-critical" : "text-muted")}>
            {summary.stale && summary.last_checked ? "Stale: " : ""}{checkedLabel(summary.last_checked)}
          </span>
          <Button size="sm" variant="outline" disabled={running} onClick={() => start.mutate()}>
            <RefreshCw className={cn("h-3.5 w-3.5", running && "animate-spin")} />
            {running ? "Refreshing…" : "Refresh this card"}
          </Button>
        </div>
      </CardHeader>
      {(running || (run && run.status !== "running") || start.error) && (
        <CardContent className="pt-0 text-xs text-ink-2">
          {start.error ? `Couldn't start: ${String(start.error)}` : running
            ? `${run?.step ?? "starting"}… (${run?.ai_calls ?? 0} AI calls so far)`
            : `Refresh ${run!.status}: ${run!.docs_checked} documents checked, ${run!.docs_changed} changed, ` +
              `${run!.ai_calls} AI calls.${run!.errors.length ? ` ${run!.errors.join("; ")}` : ""}`}
        </CardContent>
      )}
      {open && detail && <CardBody detail={detail} />}
    </Card>
  );
}

function CardBody({ detail }: { detail: CardDetail }) {
  const qc = useQueryClient();
  const decide = useMutation({
    mutationFn: (d: "accept" | "reject") => api.decideCard(detail.card_id, detail.pending!.version, d),
    onSuccess: () => qc.invalidateQueries(),
  });
  const docs = Object.fromEntries(detail.documents.filter((d) => d.version_id).map((d) => [d.version_id!, d]));
  const shown = detail.active?.profile;
  return (
    <CardContent className="space-y-5">
      {detail.pending && (
        <section className="space-y-3 rounded-lg border border-line bg-warning-bg/40 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-medium">
              {detail.pending.first ? "First terms for this card" : `Changes in v${detail.pending.version}`}
            </h3>
            <span className="text-xs text-ink-2">
              {detail.pending.first
                ? "Check each line against its quote, then accept."
                : "The current version stays in use until you accept."}
            </span>
            <div className="ml-auto flex gap-2">
              <Button size="sm" disabled={decide.isPending} onClick={() => decide.mutate("accept")}>Accept</Button>
              <Button size="sm" variant="outline" disabled={decide.isPending} onClick={() => decide.mutate("reject")}>Reject</Button>
            </div>
          </div>
          {detail.pending.first
            ? <ProfileView p={detail.pending.profile} valuation={detail.valuation_cents} docs={docs} />
            : <DiffView d={detail.pending.diff} />}
        </section>
      )}
      {shown && <ProfileView p={shown} valuation={detail.valuation_cents} docs={docs} />}
      {shown && <AskCard cardId={detail.card_id} />}
      {detail.review.length > 0 && <Problems items={detail.review} />}
      <Documents docs={detail.documents} />
      {detail.changelog.length > 0 && (
        <section>
          <h3 className="mb-1.5 text-sm font-medium">Changelog</h3>
          <ul className="space-y-1 text-xs text-ink-2">
            {detail.changelog.map((v) => (
              <li key={v.version}>
                v{v.version} · {v.status} · {v.created_at.slice(0, 10)}{v.summary ? ` · ${v.summary}` : ""}
              </li>
            ))}
          </ul>
        </section>
      )}
    </CardContent>
  );
}

// ------------------------------------------------------------------ ask (Phase C5)

const ANSWER_STATUS: Record<CardAnswer["status"], string> = {
  verified: "Checked: every quote is in the documents",
  partial: "Partly checked: see the notes below",
  unverified: "Not checked: its quotes weren't found in the documents. Don't rely on it.",
};

function AskCard({ cardId }: { cardId: string }) {
  const [q, setQ] = useState("");
  const ask = useMutation({ mutationFn: (question: string) => api.askCard(cardId, question) });
  const a = ask.data;
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-medium">Ask about this card</h3>
      <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); if (q.trim()) ask.mutate(q); }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} maxLength={500}
               placeholder="Does it cover rental car damage abroad?" aria-label="Question"
               className="min-w-0 flex-1 rounded-md border border-line bg-surface px-3 py-1.5 text-sm" />
        <Button size="sm" type="submit" disabled={ask.isPending || !q.trim()}>{ask.isPending ? "Asking…" : "Ask"}</Button>
      </form>
      <p className="text-xs text-muted">Your question and this card's public documents go to Claude. Answers quote the documents; code checks each quote.</p>
      {ask.error && <p className="text-sm text-critical">{String(ask.error.message).replace(/^\d+ /, "")}</p>}
      {a && (
        <div className="space-y-2 rounded-lg border border-line p-3 text-sm">
          <p>{a.answer}</p>
          <p className={cn("text-xs", a.status === "verified" ? "text-ink-2" : "text-critical")}>
            {ANSWER_STATUS[a.status]}{a.cached ? " · answered before" : ""}
          </p>
          <ul className="space-y-1.5">
            {a.citations.map((c, i) => (
              <li key={i} className={cn("text-xs", !c.ok && "line-through opacity-60")}>
                “{c.quote}” <span className="text-muted">
                  {c.doc_version_id
                    ? <a className="underline" target="_blank" rel="noreferrer"
                         href={`/api/cards/documents/${c.doc_version_id}${c.page ? `#page=${c.page}` : ""}`}>{c.doc_title}{c.page ? `, p. ${c.page}` : ""}</a>
                    : c.doc_title}
                </span>
              </li>
            ))}
          </ul>
          {a.warnings.map((w) => <p key={w} className="text-xs text-ink-2">{w}</p>)}
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ profile

const GEO: Record<string, string> = { us: "U.S. only", outside_us: "outside the U.S." };
const CHANNEL: Record<string, string> = {
  direct: "booked directly", online: "online", in_store: "in store", in_app: "in the app",
};
const words = (id: string) => id.replace(/_/g, " ");
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

function rate(r: number, currency: string) {
  return currency === "cash_back" ? `${r}%` : `${r}x`;
}

function conditions(r: EarnRule, currency: string) {
  const out: string[] = [];
  if (r.channel === "issuer_portal") out.push(`through ${r.portal || "the issuer's portal"}`);
  else if (CHANNEL[r.channel]) out.push(CHANNEL[r.channel]);
  if (GEO[r.geography]) out.push(GEO[r.geography]);
  if (r.prepaid_only) out.push("prepaid only");
  if (r.cap) out.push(`up to ${money(r.cap.amount_cents, { whole: true })} per ${words(r.cap.period)}, then ${rate(r.cap.rate_after, currency)}`);
  for (const q of r.requirements) out.push(q.name || words(q.kind));
  if (r.valid_from) out.push(`from ${r.valid_from}`);
  if (r.valid_to) out.push(`until ${r.valid_to}`);
  const not = [...r.excluded_categories.map(words), ...r.exclusions];
  if (not.length) out.push(`not: ${not.join(", ")}`);
  return out.join(" · ");
}

function ProfileView({ p, valuation, docs }: { p: CardProfile; valuation: number | null; docs: Record<number, CardDoc> }) {
  const v = valuation ?? 1;
  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Fact label="Earns" value={`${p.currency_name || words(p.currency)}`} sub={`${words(p.currency)} · ${v}¢ each`} />
        <Fact label="Annual fee" value={money(p.annual_fee_cents.value, { whole: true })} ev={p.annual_fee_cents.evidence} docs={docs} />
        <Fact label="Foreign transaction fee" value={`${p.foreign_txn_fee_pct.value}%`} ev={p.foreign_txn_fee_pct.evidence} docs={docs} />
        <Fact label="Rules" value={`${p.earn_rules.length} earn · ${plural(p.credits.length, "credit")}`} sub={plural(p.benefits.length, "benefit")} />
      </dl>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-muted">
            <tr><th className="py-1 pr-3 font-normal">Rate</th><th className="pr-3 font-normal">Where</th>
                <th className="pr-3 font-normal">Conditions</th><th className="font-normal">Source</th></tr>
          </thead>
          <tbody className="divide-y divide-line">
            {p.earn_rules.map((r, i) => (
              <tr key={i} className="align-top">
                <td className="py-1.5 pr-3 tabular whitespace-nowrap">
                  <span className="font-medium">{rate(r.rate, p.currency)}</span>
                  <span className="ml-1.5 text-xs text-muted">≈{(r.rate * v).toFixed(1)}¢/$</span>
                </td>
                <td className="py-1.5 pr-3">{r.is_base ? "Everything else" : [...r.categories.map(words), ...r.merchant_names].join(", ")}</td>
                <td className="py-1.5 pr-3 text-ink-2">{conditions(r, p.currency)}</td>
                <td className="py-1.5"><Source ev={r.evidence} docs={docs} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {p.credits.length > 0 && (
        <List title="Statement credits" items={p.credits.map((c) => ({
          main: `${c.name}: ${money(c.amount_cents)} per ${words(c.period)}`,
          sub: [c.enrollment_required && "enrollment required", c.valid_to && `until ${c.valid_to}`, c.description].filter(Boolean).join(" · "),
          ev: c.evidence,
        }))} docs={docs} />
      )}
      {p.benefits.length > 0 && (
        <List title="Benefits (not counted in the value)" items={p.benefits.map((b) => ({
          main: b.name + (b.coverage ? ` (${b.coverage})` : ""),
          sub: [b.limits, b.summary].filter(Boolean).join(" · "),
          ev: b.evidence,
        }))} docs={docs} />
      )}
      {p.notes.length > 0 && (
        <section>
          <h3 className="mb-1 text-sm font-medium">Notes</h3>
          <ul className="list-disc space-y-0.5 pl-5 text-xs text-ink-2">{p.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </section>
      )}
    </div>
  );
}

function Fact({ label, value, sub, ev, docs }: { label: string; value: string; sub?: string; ev?: Evidence[]; docs?: Record<number, CardDoc> }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="font-medium">{value}</dd>
      {sub && <dd className="text-xs text-ink-2">{sub}</dd>}
      {ev && docs && <dd><Source ev={ev} docs={docs} /></dd>}
    </div>
  );
}

function List({ title, items, docs }: { title: string; items: { main: string; sub: string; ev: Evidence[] }[]; docs: Record<number, CardDoc> }) {
  return (
    <section>
      <h3 className="mb-1 text-sm font-medium">{title}</h3>
      <ul className="divide-y divide-line text-sm">
        {items.map((it, i) => (
          <li key={i} className="flex flex-wrap items-start gap-x-3 py-1.5">
            <div className="min-w-0 flex-1">
              <div>{it.main}</div>
              {it.sub && <div className="text-xs text-ink-2">{it.sub}</div>}
            </div>
            <Source ev={it.ev} docs={docs} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function docHref(e: Evidence, docs: Record<number, CardDoc>) {
  if (!e.doc_version_id) return null;
  const d = docs[e.doc_version_id];
  const pdf = d?.content_type === "application/pdf";
  return `/api/cards/documents/${e.doc_version_id}${pdf && e.page ? `#page=${e.page}` : ""}`;
}

function Source({ ev, docs }: { ev: Evidence[]; docs: Record<number, CardDoc> }) {
  return (
    <details className="text-xs">
      <summary className="cursor-pointer text-ink-2 underline-offset-2 hover:underline">Source</summary>
      <div className="mt-1 max-w-md space-y-1.5 rounded-md border border-line bg-surface-2 p-2">
        {ev.map((e, i) => {
          const href = docHref(e, docs);
          const d = e.doc_version_id ? docs[e.doc_version_id] : undefined;
          return (
            <blockquote key={i} className="text-ink">
              “{e.quote}”
              <div className="mt-0.5 text-muted">
                {d ? words(d.doc_type) : e.doc}{e.page ? `, ${d?.content_type === "application/pdf" ? "page" : "section"} ${e.page}` : ""}
                {href && <> · <a className="underline" href={href} target="_blank" rel="noreferrer">open</a></>}
              </div>
            </blockquote>
          );
        })}
      </div>
    </details>
  );
}

// ------------------------------------------------------------------ diff, problems, documents

function fmt(field: string, v: unknown): string {
  if (v === null || v === undefined) return "none";
  if (field === "amount_cents" && typeof v === "number") return money(v);
  if (field === "cap" && typeof v === "object") {
    const c = v as { amount_cents: number; period: string; rate_after: number };
    return `${money(c.amount_cents, { whole: true })} per ${words(c.period)}, then ${c.rate_after}`;
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function DiffView({ d }: { d: ProfileDiff }) {
  const row = (label: string, x: DiffItem) => (
    <li key={label + x.section + x.item} className="flex flex-wrap gap-x-2">
      <span className="font-medium">{label}</span>
      <span className="text-ink-2">{words(x.section)}:</span>
      <span>{x.item}</span>
      {x.fields && Object.entries(x.fields).map(([k, [a, b]]) => (
        <span key={k} className="tabular text-ink-2">{words(k)} {fmt(k, a)} → {fmt(k, b)}</span>
      ))}
      {x.material && <Badge>needs approval</Badge>}
    </li>
  );
  return (
    <ul className="space-y-1 text-sm">
      {d.added.map((x) => row("Added", x))}
      {d.removed.map((x) => row("Removed", x))}
      {d.changed.map((x) => row("Changed", x))}
      {d.reworded.length > 0 && <li className="text-xs text-muted">{d.reworded.length} reworded</li>}
    </ul>
  );
}

const PROBLEM_TITLES: Record<string, string> = {
  card_terms_pending: "Waiting for approval",
  card_terms_unverified: "Terms that didn't check out",
  card_source_problem: "Document problem",
};

function Problems({ items }: { items: CardDetail["review"] }) {
  const shown = items.filter((i) => i.kind !== "card_terms_pending");
  if (!shown.length) return null;
  return (
    <section>
      <h3 className="mb-1 text-sm font-medium">To look at</h3>
      <ul className="space-y-1.5 text-sm">
        {shown.map((i) => (
          <li key={i.id}>
            <span className="font-medium">{PROBLEM_TITLES[i.kind]}: </span>
            <span className="text-ink-2">{problemText(i.details)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function problemText(d: Record<string, unknown>): string {
  if (typeof d.message === "string") return d.message;
  const problems = d.problems as ({ item: string; reasons: string[] } | string)[] | undefined;
  if (problems?.length) {
    return problems.map((p) => (typeof p === "string" ? p : `${p.item}: ${p.reasons.join("; ")}`)).join(" · ");
  }
  return JSON.stringify(d);
}

function Documents({ docs }: { docs: CardDoc[] }) {
  if (!docs.length) return null;
  return (
    <section>
      <h3 className="mb-1 text-sm font-medium">Documents</h3>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-left text-muted">
            <tr><th className="py-1 pr-3 font-normal">Type</th><th className="pr-3 font-normal">Status</th>
                <th className="pr-3 font-normal">Checked</th><th className="pr-3 font-normal">Changed</th>
                <th className="font-normal">Document</th></tr>
          </thead>
          <tbody className="divide-y divide-line">
            {docs.map((d) => (
              <tr key={d.id} className="align-top">
                <td className="py-1 pr-3">{words(d.doc_type)}</td>
                <td className={cn("pr-3", d.status !== "active" && "text-critical")}>{words(d.status)}</td>
                <td className="pr-3 tabular">{d.last_checked_at?.slice(0, 10) ?? "never"}</td>
                <td className="pr-3 tabular">{d.last_changed_at?.slice(0, 10) ?? "-"}</td>
                <td className="break-all">
                  {d.version_id
                    ? <a className="underline" href={`/api/cards/documents/${d.version_id}`} target="_blank" rel="noreferrer">{d.title || d.url}</a>
                    : <span>{d.title || d.url}</span>}
                  {!d.url.startsWith("manual:") && (
                    <> · <a className="text-muted underline" href={d.url} target="_blank" rel="noreferrer noopener">original</a></>
                  )}
                  {d.note && <div className="text-muted">{d.note}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
