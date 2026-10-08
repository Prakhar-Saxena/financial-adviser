"""Config loading and validation (SPEC §3, §6).

- `config/cards.json`: people, issuers, item sources and cards (real, gitignored).
- `config/settings.toml`: tunables (committed).
- Data dir: `$FIN_DATA_DIR`, else `settings.general.data_dir`, else `~/FinancialAdviserData`.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_DATA_DIR = "~/FinancialAdviserData"
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- cards.json


class Person(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str
    full_name: str
    relationship: Literal["self", "spouse", "father", "mother", "child", "other"]
    name_aliases: list[str] = []


class Site(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    start_url: str = Field(pattern=r"^https://")
    allowed_domains: list[str] = Field(min_length=1)

    @field_validator("allowed_domains")
    @classmethod
    def _bare_domains(cls, v: list[str]) -> list[str]:
        for d in v:
            if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d):
                raise ValueError(f"allowed_domains entry must be a bare domain: {d!r}")
        return v


class CardHolder(_Strict):
    person: str
    card_ending: str = Field(default="", pattern=r"^(\d{4,5})?$")
    role: Literal["primary", "authorized_user"]


class Card(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    issuer: str
    product: str
    network: Literal["amex", "visa", "mastercard", "discover"]
    card_type: Literal["credit", "charge"]
    cardholders: list[CardHolder] = Field(min_length=1)
    expected_merchant_groups: list[str] = []
    reconcile_with: list[str] = []

    def expects_group(self, merchant_group: str | None) -> bool:
        """True if the card is general-purpose or the group is one it's reserved for.

        An entry matches the group itself or its family: "costco" covers
        "costco_warehouse", "costco_gas" and "costco_online".
        """
        if not self.expected_merchant_groups:
            return True
        if not merchant_group:
            return False
        return any(
            merchant_group == g or merchant_group.startswith(g + "_")
            for g in self.expected_merchant_groups
        )


class CardsConfig(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[1]
    notes: list[str] = Field(default=[], alias="_notes")
    people: list[Person]
    issuers: list[Site]
    item_sources: list[Site]
    cards: list[Card]

    @model_validator(mode="after")
    def _check_references(self) -> CardsConfig:
        def unique(ids: list[str], what: str) -> None:
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                raise ValueError(f"duplicate {what} ids: {sorted(dupes)}")

        unique([p.id for p in self.people], "people")
        unique([s.id for s in self.issuers + self.item_sources], "site")
        unique([c.id for c in self.cards], "card")
        if sum(p.relationship == "self" for p in self.people) != 1:
            raise ValueError("exactly one person must have relationship 'self'")

        people = {p.id for p in self.people}
        issuers = {i.id for i in self.issuers}
        sources = {s.id for s in self.item_sources}
        for card in self.cards:
            if card.issuer not in issuers:
                raise ValueError(f"card {card.id}: unknown issuer {card.issuer!r}")
            for src in card.reconcile_with:
                if src not in sources:
                    raise ValueError(f"card {card.id}: unknown item source {src!r}")
            holders = [h.person for h in card.cardholders]
            for h in holders:
                if h not in people:
                    raise ValueError(f"card {card.id}: unknown person {h!r}")
            if len(set(holders)) != len(holders):
                raise ValueError(f"card {card.id}: a person is listed twice")
            if sum(h.role == "primary" for h in card.cardholders) != 1:
                raise ValueError(f"card {card.id}: exactly one primary cardholder")
            endings = [h.card_ending for h in card.cardholders if h.card_ending]
            if len(set(endings)) != len(endings):
                raise ValueError(f"card {card.id}: duplicate card_ending")
        return self

    def site(self, site_id: str) -> Site:
        for s in self.issuers + self.item_sources:
            if s.id == site_id:
                return s
        raise KeyError(site_id)

    def cards_for_issuer(self, issuer_id: str) -> list[Card]:
        return [c for c in self.cards if c.issuer == issuer_id]

    def card(self, card_id: str) -> Card:
        for c in self.cards:
            if c.id == card_id:
                return c
        raise KeyError(card_id)


# ------------------------------------------------------------- settings.toml


class GeneralSettings(_Strict):
    data_dir: str = DEFAULT_DATA_DIR
    history_months: int = Field(default=3, ge=1, le=24)
    statements_per_card: int = Field(default=3, ge=1, le=24)


class SyncSettings(_Strict):
    sync_model: str = "sonnet"
    site_models: dict[str, str] = {}
    sync_overlap_days: int = Field(default=14, ge=0, le=60)
    trusted_sites: list[str] = []
    # "profile" = a separate Chrome profile per site (default). "extension" = the user's own
    # Chrome via the Playwright extension, for sites that block the separate profile (Costco).
    site_browser: dict[str, Literal["profile", "extension"]] = {}
    playwright_mcp_version: str = "0.0.83"

    @field_validator("playwright_mcp_version")
    @classmethod
    def _exact(cls, v: str) -> str:
        if not EXACT_VERSION.fullmatch(v):
            raise ValueError(f"playwright_mcp_version must be an exact version, got {v!r}")
        return v

    def browser_for(self, site: str) -> str:
        return self.site_browser.get(site, "profile")

    def model_for(self, site: str) -> str:
        return self.site_models.get(site, self.sync_model)


class AISettings(_Strict):
    classify_model: Literal["haiku"] = "haiku"
    batch_size: int = Field(default=40, ge=1, le=50)
    timeout_seconds: int = Field(default=180, ge=10, le=900)
    max_turns: int = Field(default=3, ge=1, le=10)
    low_confidence: float = Field(default=0.7, ge=0, le=1)
    ai_runs_retention_days: int = 90


class MatchSettings(_Strict):
    amazon_window_days: tuple[int, int] = (-1, 3)
    costco_warehouse_window_days: tuple[int, int] = (0, 2)
    unmatched_grace_days: int = 5


class ServerSettings(_Strict):
    host: Literal["127.0.0.1"] = "127.0.0.1"
    api_port: int = 8765
    web_dev_port: int = 5173


class Settings(_Strict):
    general: GeneralSettings = GeneralSettings()
    sync: SyncSettings = SyncSettings()
    ai: AISettings = AISettings()
    match: MatchSettings = MatchSettings()
    server: ServerSettings = ServerSettings()


# --------------------------------------------------------------- data paths


class DataPaths:
    """Layout of the data directory (SPEC §6). Nothing here touches the disk."""

    def __init__(self, root: Path):
        self.root = root
        self.db_file = root / "db" / "finance.sqlite"
        self.inbox = root / "inbox"
        self.raw = root / "raw"
        self.browser_profiles = root / "browser-profiles"
        self.sync_sessions = root / "sync-sessions"
        self.ai_runs = root / "ai-runs"
        self.secrets = root / "secrets"
        self.debug = root / "debug"
        self.logs = root / "logs"
        self.backups = root / "backups"

    @property
    def all_dirs(self) -> list[Path]:
        return [
            self.root, self.db_file.parent, self.inbox, self.raw, self.raw / "sync-logs",
            self.browser_profiles, self.sync_sessions, self.ai_runs, self.secrets,
            self.debug, self.logs, self.backups,
        ]

    def run_dir(self, site: str, date: str) -> Path:
        return self.inbox / site / date


ICLOUD_RISK_DIRS = ("Documents", "Desktop", "Library/Mobile Documents")


def icloud_risk(path: Path) -> str | None:
    """Return the risky ancestor if `path` sits in a folder that may sync to iCloud."""
    home = Path.home().resolve()
    p = path.expanduser().resolve()
    for d in ICLOUD_RISK_DIRS:
        risky = home / d
        if p == risky or risky in p.parents:
            return str(risky)
    return None


# ------------------------------------------------------------------ loaders


def load_cards(path: Path | None = None) -> CardsConfig:
    path = path or CONFIG_DIR / "cards.json"
    return CardsConfig.model_validate(json.loads(path.read_text()))


def load_settings(path: Path | None = None) -> Settings:
    path = path or CONFIG_DIR / "settings.toml"
    if not path.exists():
        return Settings()
    return Settings.model_validate(tomllib.loads(path.read_text()))


def data_dir(settings: Settings | None = None) -> Path:
    raw = os.environ.get("FIN_DATA_DIR") or (settings or load_settings()).general.data_dir
    return Path(raw).expanduser().resolve()


def data_paths(settings: Settings | None = None) -> DataPaths:
    return DataPaths(data_dir(settings))


# --------------------------------------------------------- categories.json


class Category(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)?$")
    parent_id: str | None
    name: str


class Taxonomy(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    version: int
    notes: list[str] = Field(default=[], alias="_notes")
    categories: list[Category]

    @model_validator(mode="after")
    def _check(self) -> Taxonomy:
        ids = [c.id for c in self.categories]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate category ids")
        for c in self.categories:
            if c.parent_id is not None and c.parent_id not in ids:
                raise ValueError(f"category {c.id}: unknown parent {c.parent_id!r}")
        if "uncategorized" not in ids:
            raise ValueError("taxonomy must include 'uncategorized'")
        return self

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.categories]


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    path = path or CONFIG_DIR / "categories.json"
    return Taxonomy.model_validate(json.loads(path.read_text()))
