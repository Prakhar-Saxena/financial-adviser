# Copyright (C) 2026 Prakhar Saxena
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Card profiles and merchant profiles (docs/SPEC-cards.md §4).

A card profile is what the knowledge base knows about one card: its currency, fees, earn rules,
statement credits and benefits, each backed by verbatim quotes from stored documents. Profiles
are stored as JSON in `card_profiles.profile_json`; money is integer cents (SPEC §8).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SLUG = r"^[a-z][a-z0-9_]*$"

CHANNELS = ("any", "direct", "issuer_portal", "online", "in_store", "in_app")
GEOGRAPHIES = ("any", "us", "outside_us")
REQUIREMENTS = ("activation", "enrollment", "membership")
CAP_PERIODS = ("month", "quarter", "calendar_year", "cardmember_year", "statement")
CREDIT_PERIODS = (
    "month", "quarter", "half_year", "calendar_year", "cardmember_year", "multi_year", "one_time",
)
CREDIT_POSTING = ("statement_credit", "in_app_balance", "other")
BENEFIT_KINDS = (
    "purchase_protection", "extended_warranty", "return_protection", "cell_phone_protection",
    "trip_cancellation_interruption", "trip_delay", "baggage_delay_loss", "rental_car_cdw",
    "travel_accident", "roadside_assistance", "lounge_access", "other",
)
NETWORKS = ("amex", "visa", "mastercard", "discover")
ACCEPTANCE = ("yes", "no", "unknown")
MERCHANT_SOURCES = ("seed", "kb", "ai", "ai_web", "user")
DOC_TYPES = (
    "product_page", "rewards_terms", "program_terms", "pricing_terms", "cardmember_agreement",
    "benefits_guide", "benefit_terms", "other",
)

MAX_QUOTE = 300


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(_Strict):
    """A verbatim quote from one stored document version. `doc` is the ref the extraction saw
    ("D1"); compose replaces it with the document version id."""

    doc: str
    page: int | None = Field(default=None, ge=1)
    quote: str = Field(min_length=3, max_length=MAX_QUOTE)
    doc_version_id: int | None = None


Evidenced = list[Evidence]


class Requirement(_Strict):
    kind: Literal["activation", "enrollment", "membership"]
    name: str = ""  # e.g. "Prime membership"


class Cap(_Strict):
    amount_cents: int = Field(gt=0)  # spend, not rewards
    period: Literal["month", "quarter", "calendar_year", "cardmember_year", "statement"]
    rate_after: float = Field(ge=0, le=25)
    shared_id: str | None = None  # rules with the same shared_id share one cap


class EarnRule(_Strict):
    """N units per $1 (points, miles, or cents for cash back)."""

    rate: float = Field(ge=0, le=25)
    is_base: bool = False  # "all other purchases"
    categories: list[str] = []  # reward category ids
    merchants: list[str] = []  # merchant profile keys (compose maps names to keys)
    merchant_names: list[str] = []  # as the documents name them
    channel: Literal["any", "direct", "issuer_portal", "online", "in_store", "in_app"] = "any"
    portal: str = ""  # e.g. "Chase Travel", when channel is issuer_portal
    geography: Literal["any", "us", "outside_us"] = "any"
    prepaid_only: bool = False
    requirements: list[Requirement] = []
    valid_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    valid_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    cap: Cap | None = None
    excluded_categories: list[str] = []
    exclusions: list[str] = []  # in the documents' words, e.g. "superstores", "gift cards"
    description: str = ""
    evidence: Evidenced = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def _scope(self) -> EarnRule:
        if not self.is_base and not (self.categories or self.merchants or self.merchant_names):
            raise ValueError("a non-base rule needs at least one category or merchant")
        return self

    def identity(self) -> str:
        """Stable identity for diffs (§7.5): its scope, not its numbers."""
        if self.is_base:
            return "base"
        parts = [",".join(sorted(self.categories)), ",".join(sorted(self.merchants)),
                 self.channel, self.geography, "prepaid" if self.prepaid_only else ""]
        return "|".join(parts)


class Credit(_Strict):
    name: str
    amount_cents: int = Field(gt=0)  # per period
    period: Literal[
        "month", "quarter", "half_year", "calendar_year", "cardmember_year", "multi_year",
        "one_time",
    ]
    merchants: list[str] = []
    merchant_names: list[str] = []
    categories: list[str] = []
    enrollment_required: bool = False
    posts_as: Literal["statement_credit", "in_app_balance", "other"] = "statement_credit"
    valid_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    valid_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    description: str = ""
    evidence: Evidenced = Field(min_length=1, max_length=3)

    def identity(self) -> str:
        return f"{normalize_name(self.name)}|{self.period}"


class Benefit(_Strict):
    kind: Literal[
        "purchase_protection", "extended_warranty", "return_protection", "cell_phone_protection",
        "trip_cancellation_interruption", "trip_delay", "baggage_delay_loss", "rental_car_cdw",
        "travel_accident", "roadside_assistance", "lounge_access", "other",
    ]
    name: str
    summary: str = ""
    limits: str = ""  # e.g. "up to $10,000 per claim, $50,000 per year"
    coverage: Literal["primary", "secondary"] | None = None
    evidence: Evidenced = Field(min_length=1, max_length=3)

    def identity(self) -> str:
        return self.kind if self.kind != "other" else f"other|{normalize_name(self.name)}"


class Fee(_Strict):
    value: float = Field(ge=0)  # annual fee in cents, or foreign transaction fee in percent
    evidence: Evidenced = Field(min_length=1, max_length=3)


class CardProfile(_Strict):
    card_id: str = Field(pattern=SLUG)
    currency: str = Field(pattern=SLUG)  # amex_mr, chase_ur, citi_typ, cash_back, ...
    currency_name: str = ""  # as the documents say it, e.g. "Membership Rewards points"
    annual_fee_cents: Fee
    foreign_txn_fee_pct: Fee
    earn_rules: list[EarnRule]
    credits: list[Credit] = []
    benefits: list[Benefit] = []
    notes: list[str] = []

    @model_validator(mode="after")
    def _one_base(self) -> CardProfile:
        if sum(r.is_base for r in self.earn_rules) != 1:
            raise ValueError("a profile needs exactly one base rule")
        return self


class MerchantProfile(_Strict):
    key: str = Field(pattern=SLUG)
    display_name: str
    reward_category: str | None = None
    mcc_guess: str | None = Field(default=None, pattern=r"^\d{4}$")
    acceptance: dict[Literal["amex", "visa", "mastercard", "discover"],
                     Literal["yes", "no", "unknown"]] = {}
    aliases: list[str] = []
    notes: str = ""
    source: Literal["seed", "kb", "ai", "ai_web", "user"]
    confidence: float = Field(default=1.0, ge=0, le=1)


def normalize_name(name: str) -> str:
    """Lower case, '&' -> 'and', punctuation dropped, spaces collapsed (§8 alias rule)."""
    s = name.lower().replace("&", " and ").replace("®", "").replace("™", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())
