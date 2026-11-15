// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

// Checking rows in words (docs/SPEC-checking.md §4). Keys are flow kinds, or the type when a
// row has none.
import type { Meta, Txn } from "@/api";

export const FLOW_LABELS: Record<string, string> = {
  purchase: "Purchases and bills",
  fee: "Bank fees",
  refund: "Refunds",
  check: "Checks",
  atm: "ATM cash",
  p2p: "Zelle, Venmo and the like",
  card_payment: "Card payments",
  card_payment_other: "Payments to other cards",
  own_transfer: "Between my accounts",
  investment: "Investments",
  income: "Pay",
  interest: "Interest and dividends",
  reward: "Debit rewards",
  tax_refund: "Tax refunds",
  other_in: "Other money in",
  transfer: "Transfers",
  deposit: "Deposits",
};

// One flow kind, as a short choice in a select ("" is a plain purchase).
export const FLOW_CHOICES: Record<string, string> = {
  "": "Plain purchase or bill",
  check: "Check",
  atm: "ATM cash",
  p2p: "Zelle, Venmo and the like",
  card_payment: "Payment to one of my cards",
  card_payment_other: "Payment to another card",
  own_transfer: "Between my own accounts",
  investment: "Investments",
  other_in: "Other money in",
  income: "Pay",
  interest: "Interest or dividends",
  reward: "Debit rewards",
  tax_refund: "Tax refund",
};

export function isAccount(meta: Meta, cardId: string) {
  return meta.cards.some((c) => c.id === cardId && c.kind === "account");
}

/** The badge on a row that isn't spending: why it's left out. */
export function excludedLabel(t: Pick<Txn, "type" | "flow_kind">) {
  const k = t.flow_kind ?? t.type;
  return {
    card_payment: "card payment", card_payment_other: "other card", own_transfer: "own transfer",
    investment: "investment", income: "pay", interest: "interest", reward: "reward",
    tax_refund: "tax refund", other_in: "money in", p2p: "money in",
  }[k] ?? t.type;
}
