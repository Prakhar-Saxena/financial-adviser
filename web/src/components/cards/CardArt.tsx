// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

// A small picture of a card: the issuer's official one when `fin cards art` has stored it
// (served from the data dir by the local API, never loaded from the issuer's site), otherwise
// a drawn one: a colour picked from the product name, the issuer, a chip and the network mark.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api";
import { cn } from "@/lib/utils";

type Look = { bg: string; dark?: boolean };

// First match wins, so the more specific names come first.
const LOOKS: [RegExp, Look][] = [
  [/rose gold/i, { bg: "linear-gradient(135deg,#f1c9b8,#c98f7c)", dark: true }],
  [/platinum/i, { bg: "linear-gradient(135deg,#e6e6e3,#a3a5a8)", dark: true }],
  [/gold/i, { bg: "linear-gradient(135deg,#ecc977,#b3842b)", dark: true }],
  [/green/i, { bg: "linear-gradient(135deg,#7f9b68,#4e6a3d)" }],
  [/sapphire reserve/i, { bg: "linear-gradient(135deg,#27344f,#0c1322)" }],
  [/sapphire/i, { bg: "linear-gradient(135deg,#2f5a9a,#183262)" }],
  [/freedom/i, { bg: "linear-gradient(135deg,#3b8ede,#1c5aa3)" }],
  [/prime|amazon/i, { bg: "linear-gradient(135deg,#2c3a4c,#10161f)" }],
  [/costco/i, { bg: "linear-gradient(135deg,#55687c,#202c3a)" }],
  [/blue/i, { bg: "linear-gradient(135deg,#4aa0e6,#1f6bb5)" }],
  [/double cash|citi/i, { bg: "linear-gradient(135deg,#3e7fb8,#1d4a78)" }],
];

const ISSUERS: Record<string, string> = { amex: "AMEX", chase: "CHASE", citi: "citi", capital_one: "Capital One", discover: "DISCOVER" };

function look(id: string, product: string): Look {
  for (const [re, l] of LOOKS) if (re.test(product)) return l;
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return { bg: `linear-gradient(135deg,hsl(${h} 45% 45%),hsl(${(h + 30) % 360} 50% 25%))` };
}

// "The Platinum Card from American Express" -> "Platinum"; "Prime Visa" -> "Prime".
export function shortName(product: string) {
  return product
    .replace(/\b(the|card|from|by|american express|chase|citi|visa|mastercard)\b/gi, "")
    .replace(/\s+/g, " ").trim() || product;
}

function Network({ network, dark }: { network: string; dark?: boolean }) {
  const ink = dark ? "text-black/75" : "text-white/90";
  if (network === "mastercard") {
    return (
      <span className="flex" aria-hidden>
        <span className="h-[0.9em] w-[0.9em] rounded-full bg-[#eb001b]" />
        <span className="-ml-[0.35em] h-[0.9em] w-[0.9em] rounded-full bg-[#f79e1b]/90" />
      </span>
    );
  }
  if (network === "amex") {
    return <span className={cn("rounded-[2px] border px-[0.2em] text-[0.55em] font-bold leading-tight", dark ? "border-black/60" : "border-white/80", ink)}>AMEX</span>;
  }
  const label = { visa: "VISA", discover: "DISCOVER" }[network];
  return label ? <span className={cn("text-[0.75em] font-extrabold italic leading-none", ink)}>{label}</span> : null;
}

export function CardArt({ cardId, product, size = "sm", className }: {
  cardId: string; product: string; size?: "sm" | "md" | "lg"; className?: string;
}) {
  const { data: meta } = useQuery({ queryKey: ["meta"], queryFn: api.meta, staleTime: Infinity });
  const card = meta?.cards.find((c) => c.id === cardId);
  const [broken, setBroken] = useState<string | null>(null);
  const l = look(cardId, product);
  const w = { sm: "w-10 text-[7px]", md: "w-16 text-[9px]", lg: "w-28 text-[12px]" }[size];
  if (card?.art && broken !== card.art) {
    return (
      <img src={`/api/cards/art/${encodeURIComponent(cardId)}?v=${card.art}`} alt={`${product} card`}
           title={product} loading="lazy" onError={() => setBroken(card.art)}
           className={cn("aspect-[1.586] shrink-0 object-contain", w, className)} />
    );
  }
  return (
    <div role="img" aria-label={`${product} card`} title={product}
         style={{ background: l.bg }}
         className={cn("relative aspect-[1.586] shrink-0 overflow-hidden rounded-[0.6em] border border-black/10 shadow-sm",
                       l.dark ? "text-black/75" : "text-white/90", w, className)}>
      {size !== "sm" && card && (
        <span className="absolute left-[0.6em] top-[0.4em] font-semibold leading-none tracking-wide">
          {ISSUERS[card.issuer] ?? card.issuer}
        </span>
      )}
      <span className="absolute left-[0.6em] top-[38%] h-[1.4em] w-[1.9em] rounded-[0.25em] bg-gradient-to-br from-[#f3e2a6] to-[#bfa35a] opacity-90" aria-hidden />
      {size === "lg" && (
        <span className="absolute bottom-[0.45em] left-[0.6em] max-w-[60%] truncate text-[0.8em] leading-none">{shortName(product)}</span>
      )}
      {card && (
        <span className="absolute bottom-[0.4em] right-[0.5em] flex items-end">
          <Network network={card.network} dark={l.dark} />
        </span>
      )}
    </div>
  );
}
