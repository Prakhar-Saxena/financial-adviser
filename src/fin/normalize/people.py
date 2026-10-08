"""Cardholder attribution (SPEC §9.2, as decided 2026-10-06).

Authorized users' names appear on their transactions. So:
- a name that matches a person's full_name or name_aliases -> that person
- no name at all -> the primary cardholder (self)
- a name that matches nobody -> None, plus an `unknown_person` review item (never guessed)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fin.config import CardsConfig


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z ]", " ", name.upper())).strip()


@dataclass(frozen=True)
class Attribution:
    person_id: str | None
    source: str  # "name" | "default_self" | "unknown_name"


class PeopleIndex:
    def __init__(self, cards: CardsConfig):
        self.self_id = next(p.id for p in cards.people if p.relationship == "self")
        self.by_name: dict[str, str] = {}
        for p in cards.people:
            for n in (p.full_name, *p.name_aliases):
                key = normalize_name(n)
                if key:
                    self.by_name[key] = p.id

    def attribute(self, name: str | None) -> Attribution:
        key = normalize_name(name or "")
        if not key:
            return Attribution(self.self_id, "default_self")
        if key in self.by_name:
            return Attribution(self.by_name[key], "name")
        return Attribution(None, "unknown_name")
