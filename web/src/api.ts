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
  reprocess: () => req<{ rule_matched: number }>("/api/process", { method: "POST" }),
  resolveMany: (ids: number[], status: "resolved" | "ignored") =>
    req<{ updated: number }>("/api/review/resolve", { method: "POST", body: JSON.stringify({ ids, status }) }),
  reconciliation: () => req<Statement[]>("/api/reconciliation"),
  sources: () => req<SourceStatus[]>("/api/sources/status"),
  resolve: (id: number, status: "resolved" | "ignored") =>
    req(`/api/review/${id}/resolve`, { method: "POST", body: JSON.stringify({ status }) }),
};
