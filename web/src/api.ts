// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

// Typed client for the local API (src/fin/api/app.py). Amounts are integer cents.

export type Row = { key: string; cents: number; name?: string };
export type Summary = {
  month: string;
  total_cents: number;
  by_category: Row[];
  by_subcategory: Row[];
  by_person: Row[];
  by_card: Row[];
  top_merchants: Row[];
  credits_rewards_cents: number;
  trend: { month: string; cents: number; amazon_cents: number; costco_cents: number }[];
};
export type Meta = {
  months: string[];
  category_rank: string[];
  people: { id: string; name: string }[];
  cards: { id: string; name: string; issuer: string }[];
  categories: { id: string; name: string; parent_id: string | null }[];
};
export type Txn = {
  id: number;
  txn_date: string | null;
  post_date: string;
  card_id: string;
  person_id: string | null;
  person_source: string | null;
  amount_cents: number;
  description: string;
  description_raw: string;
  type: string;
  source: string;
  merchant: string | null;
  merchant_group: string | null;
  category_id: string | null;
  category_source: string | null;
  review_status: string;
  order_ref: string | null;
};
export type TxnDetail = Txn & {
  allocations: { category_id: string; amount_cents: number }[];
  match: null | {
    method: string;
    confidence: number;
    order: { id: number; merchant: string; external_id: string; order_date: string;
             total_cents: number; tax_cents: number; shipping_cents: number };
    items: { title: string; title_raw: string; quantity: number; line_total_cents: number;
             category_id: string | null; sku: string | null }[];
  };
  reviews: { id: number; kind: string; details: Record<string, unknown> }[];
};
export type ReviewItem = {
  id: number;
  kind: string;
  ref_table: string | null;
  ref_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
  transaction?: Txn;
  order?: { external_id: string; order_date: string; total_cents: number };
  item?: { id: number; title: string; title_raw: string; merchant: string; order_date: string;
           cents: number; category_id: string | null };
};

export type Order = {
  id: number; external_id: string; order_date: string; channel: string; location: string | null;
  total_cents: number; tax_cents: number; discounts_cents: number; payment_method: string | null;
  matched: boolean;
  items: { title: string; title_raw: string; sku: string | null; quantity: number;
           category_id: string | null; line_total_cents: number; discount_cents: number }[];
};
export type OrdersSummary = {
  orders: number; matched: number; total_cents: number;
  top_items: { title: string; category_id: string | null; cents: number; count: number }[];
  by_category: Row[];
};
export type Rule = { id: number; pattern: string; merchant_name: string | null;
                     merchant_group: string | null; category_id: string | null;
                     created_by: string; priority: number };
export type Statement = { id: number; card_id: string; period_start: string; period_end: string;
                          new_balance_cents: number; math_ok: boolean;
                          ledger_status: string | null; ledger_delta_cents: number | null };
export type SourceStatus = { site: string; last_run: string | null; status: string | null;
                             files_imported: number; guard_blocks: number; open_gaps: number };

// Card optimizer (docs/SPEC-cards.md §12).
export type Evidence = { doc: string; page: number | null; quote: string; doc_version_id: number | null };
export type EarnRule = {
  rate: number; is_base: boolean; categories: string[]; merchants: string[]; merchant_names: string[];
  channel: string; portal: string; geography: string; prepaid_only: boolean;
  requirements: { kind: string; name: string }[]; valid_from: string | null; valid_to: string | null;
  cap: { amount_cents: number; period: string; rate_after: number; shared_id: string | null } | null;
  excluded_categories: string[]; exclusions: string[]; description: string; evidence: Evidence[];
};
export type CardCredit = {
  name: string; amount_cents: number; period: string; merchant_names: string[]; categories: string[];
  enrollment_required: boolean; posts_as: string; valid_from: string | null; valid_to: string | null;
  description: string; evidence: Evidence[];
};
export type CardBenefit = {
  kind: string; name: string; summary: string; limits: string; coverage: string | null; evidence: Evidence[];
};
export type CardProfile = {
  card_id: string; currency: string; currency_name: string;
  annual_fee_cents: { value: number; evidence: Evidence[] };
  foreign_txn_fee_pct: { value: number; evidence: Evidence[] };
  earn_rules: EarnRule[]; credits: CardCredit[]; benefits: CardBenefit[]; notes: string[];
};
export type DiffItem = { section: string; item: string; material: boolean; fields?: Record<string, [unknown, unknown]> };
export type ProfileDiff = { added: DiffItem[]; removed: DiffItem[]; changed: DiffItem[];
                            reworded: { section: string; item: string }[]; material: boolean };
export type CardSummary = {
  card_id: string; product: string; active_version: number | null; pending_version: number | null;
  docs: Record<string, number>; last_checked: string | null; last_changed: string | null; stale: boolean;
  open_items: number; currency: string | null; annual_fee_cents: number | null;
};
export type CardDoc = {
  id: number; url: string; doc_type: string; title: string | null; origin: string; status: string;
  note: string | null; last_checked_at: string | null; last_changed_at: string | null;
  version_id: number | null; content_type: string | null;
};
export type CardDetail = {
  card_id: string; product: string; issuer: string; network: string;
  active: { version: number; decided_at: string | null; profile: CardProfile } | null;
  pending: { version: number; created_at: string; first: boolean; profile: CardProfile; diff: ProfileDiff } | null;
  valuation_cents: number | null;
  documents: CardDoc[];
  changelog: { version: number; status: string; material: boolean; created_at: string;
               decided_at: string | null; summary: string; diff: ProfileDiff }[];
  review: { id: number; kind: string; details: Record<string, unknown> }[];
};
export type RefreshStatus = {
  run_id: number; status: string; card: string | null; step: string | null;
  docs_checked: number; docs_changed: number; ai_calls: number;
  profiles_pending: number; profiles_published: number;
  cards: { card_id: string; outcome: string; error: string | null }[]; errors: string[];
};

export type MerchantInfo = {
  key: string; display_name: string; reward_category: string | null;
  acceptance: Record<string, string>; source: string; confidence: number;
  mcc_guess: string | null; notes: string | null; unsure?: boolean;
};
export type OfferLine = {
  name: string; merchant: string; text: string; notes: string[]; value_cents: number;
  counted: boolean; added: boolean | null; expires_on: string | null; captured_on: string;
};
export type BestEvidence = { quote: string; page: number | null; doc_version_id: number | null } | null;
export type BestResult = {
  card_id: string; product: string; rank: number; currency: string; valuation_cents: number;
  total_cents: number; rule_cents: number; credit_cents: number; offer_cents: number;
  fee_cents: number; cents_per_dollar: number; units: number;
  rule: { rate: number; rate_text: string; text: string; is_base: boolean; evidence: BestEvidence } | null;
  caveats: string[];
  credit: { name: string; text: string; counted: boolean; needs_enrollment: boolean; evidence: BestEvidence } | null;
  offer: OfferLine | null;
  benefits: { kind: string; name: string; limits: string; coverage: string | null; evidence: BestEvidence }[];
  alternatives: { text: string; cents: number; caveats: string[]; evidence: BestEvidence }[];
};
export type BestAnswer = {
  query: string; resolution: string | null; needs_lookup: boolean;
  suggestions: { key: string; display_name: string; alias: string }[];
  kb: { last_checked: string | null; stale: string[]; without_terms: string[] };
  merchant: MerchantInfo | null;
  amount_cents?: number; results?: BestResult[];
  not_accepted?: { card_id: string; product: string; network: string }[];
  break_even?: string | null; asks_channel?: boolean; asks_abroad?: boolean;
};
export type CardSettings = {
  valuations: { currency: string; name: string; cents: number }[];
  count_unused_credits: boolean;
  reward_categories: { id: string; name: string }[];
  cards: { card_id: string; product: string; currency: string | null; pooled_into: string | null;
           anniversary_month: string | null;
           requirements: { ref: string; kind: string; label: string; for: string; value: string | null }[] }[];
};

export type TipRow = {
  merchant: string; key: string; category: string; unsure: boolean; purchases: number;
  spend_cents: number; cards_used: { card_id: string; product: string; purchases: number }[];
  better_card_id: string; better_product: string; earned_cents: number; possible_cents: number;
  difference_cents: number; text: string | null;
  examples: { transaction_id: number; date: string; amount_cents: number; card_id: string;
              used_cents: number; best_card_id: string; best_cents: number }[];
};
export type Tips = {
  months: number; since: string; until: string; estimate: string; top: TipRow[]; unsure: TipRow[];
  total_difference_cents: number; unknown_descriptors: number; unknown_purchases: number;
};
export type CreditUse = {
  card_id: string; product: string; name: string; identity: string; amount_cents: number;
  period: string; period_key: string; start: string; end: string; days_left: number;
  used_cents: number; known: boolean; how: string; matched_transactions: number;
  enrollment_required: boolean; enrolled: boolean | null; enroll_ref: string; posts_as: string;
  approximate: boolean;
  as_of: string | null; fresh: boolean;
};
export type CheatRow = { label: string; key: string; card_id: string | null; product: string | null;
                         cents_per_dollar: number; rule: string | null; caveats: string[];
                         runner_up: string | null; runner_up_cents_per_dollar: number | null };
export type OffersSummary = {
  cards: { card_id: string; product: string; captured_on: string; status: string; found: number;
           verified: number; current: number; added: number; stale: boolean }[];
  at_my_merchants: (OfferLine & { card_id: string; product: string; merchant_key: string;
                                  purchases: number; typical_cents: number; amount_cents: number })[];
};
export type RewardsCheck = {
  months: number; since: string; note: string; without_summary: string[];
  statements: { card_id: string; product: string; statement_id: number; period_start: string;
                period_end: string; kind: "points" | "cash"; stated: number; predicted: number;
                difference: number; flagged: boolean; purchases: number; refunds: number;
                unknown_merchants: number;
                stated_lines: { text: string; value: number }[];
                predicted_lines: { text: string; value: number }[] }[];
};
export type CardAnswer = {
  card_id: string; question: string; answer: string; found: boolean; cached: boolean;
  status: "verified" | "partial" | "unverified"; warnings: string[];
  citations: { quote: string; page: number | null; ok: boolean; doc_title: string;
               doc_version_id: number | null }[];
};
export type Cheatsheet = { amount_cents: number; categories: CheatRow[]; merchants: CheatRow[] };

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

export const api = {
  meta: () => req<Meta>("/api/meta"),
  summary: (month?: string) => req<Summary>(`/api/summary${month ? `?month=${month}` : ""}`),
  transactions: (params: Record<string, string | undefined>) => {
    const q = new URLSearchParams(
      Object.entries(params).filter((e): e is [string, string] => !!e[1]),
    ).toString();
    return req<Txn[]>(`/api/transactions${q ? `?${q}` : ""}`);
  },
  transaction: (id: number) => req<TxnDetail>(`/api/transactions/${id}`),
  patchTransaction: (id: number, body: { category_id?: string; person_id?: string }) =>
    req<TxnDetail>(`/api/transactions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  patchItem: (id: number, category_id: string) =>
    req(`/api/order_items/${id}`, { method: "PATCH", body: JSON.stringify({ category_id }) }),
  createRule: (body: { pattern: string; category_id: string; merchant_name?: string }) =>
    req<{ id: number; note: string }>("/api/rules", { method: "POST", body: JSON.stringify(body) }),
  review: () => req<ReviewItem[]>("/api/review"),
  orders: (merchant: string, month?: string) =>
    req<Order[]>(`/api/orders?merchant=${merchant}${month ? `&month=${month}` : ""}`),
  ordersSummary: (merchant: string, month?: string) =>
    req<OrdersSummary>(`/api/orders/summary?merchant=${merchant}${month ? `&month=${month}` : ""}`),
  rules: () => req<Rule[]>("/api/rules"),
  deleteRule: (id: number) => req(`/api/rules/${id}`, { method: "DELETE" }),
  previewRule: (pattern: string) =>
    req<{ transactions: number; descriptions: [string, number][] }>(
      `/api/rules/preview?pattern=${encodeURIComponent(pattern)}`),
  reprocess: () => req<{ rule_matched: number }>("/api/process", { method: "POST", body: "{}" }),
  resolveMany: (ids: number[], status: "resolved" | "ignored") =>
    req<{ updated: number }>("/api/review/resolve", { method: "POST", body: JSON.stringify({ ids, status }) }),
  reconciliation: () => req<Statement[]>("/api/reconciliation"),
  sources: () => req<SourceStatus[]>("/api/sources/status"),
  cardProfiles: () => req<CardSummary[]>("/api/cards/profiles"),
  cardProfile: (id: string) => req<CardDetail>(`/api/cards/profiles/${id}`),
  decideCard: (id: string, version: number, decision: "accept" | "reject") =>
    req(`/api/cards/profiles/${id}/${decision}`, { method: "POST", body: JSON.stringify({ version }) }),
  refreshCards: (card_id: string | null) =>
    req<{ run_id: number }>("/api/cards/refresh", { method: "POST", body: JSON.stringify({ card_id }) }),
  refreshStatus: (run_id: number) => req<RefreshStatus>(`/api/cards/refresh/${run_id}`),
  best: (p: { q?: string; key?: string; amount_cents?: number; channel?: string; abroad?: boolean }) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== "" && v !== false) q.set(k, String(v));
    return req<BestAnswer>(`/api/cards/best?${q}`);
  },
  merchantSearch: (q: string) =>
    req<{ key: string; display_name: string; alias: string }[]>(`/api/cards/merchants?q=${encodeURIComponent(q)}`),
  lookupMerchant: (q: string, web: boolean) =>
    req<{ not_a_merchant: boolean; merchant: MerchantInfo | null }>("/api/cards/merchants/lookup",
      { method: "POST", body: JSON.stringify({ q, web }) }),
  patchMerchant: (key: string, body: { category?: string; acceptance?: Record<string, string>; alias?: string }) =>
    req<MerchantInfo>(`/api/cards/merchants/${key}`, { method: "PATCH", body: JSON.stringify(body) }),
  cardSettings: () => req<CardSettings>("/api/cards/settings"),
  putCardSettings: (body: unknown) =>
    req<CardSettings>("/api/cards/settings", { method: "PUT", body: JSON.stringify(body) }),
  tips: (months: number) => req<Tips>(`/api/cards/tips?months=${months}`),
  credits: () => req<CreditUse[]>("/api/cards/credits"),
  markCredit: (body: { card_id: string; identity: string; period_key?: string; used?: string | null;
                       alias?: string; clear?: boolean }) =>
    req<CreditUse[]>("/api/cards/credits", { method: "PUT", body: JSON.stringify(body) }),
  cheatsheet: () => req<Cheatsheet>("/api/cards/cheatsheet"),
  offers: () => req<OffersSummary>("/api/cards/offers"),
  askCard: (card_id: string, question: string) =>
    req<CardAnswer>("/api/cards/question", { method: "POST", body: JSON.stringify({ card_id, question }) }),
  rewardsCheck: (months: number) => req<RewardsCheck>(`/api/cards/rewards-check?months=${months}`),
  resolve: (id: number, status: "resolved" | "ignored") =>
    req(`/api/review/${id}/resolve`, { method: "POST", body: JSON.stringify({ status }) }),
};
