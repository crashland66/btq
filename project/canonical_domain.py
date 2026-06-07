from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NewType


SiteId = NewType("SiteId", str)
PersonId = NewType("PersonId", str)


@dataclass(frozen=True)
class NoteRef:
    entity_type: str
    entity_id: str
    role: str
    storage_uri: str


@dataclass(frozen=True)
class Site:
    site_id: SiteId
    canonical_name: str
    account: str | None
    aliases: tuple[str, ...]
    address: str | None
    monthly_supply_budget: float | None
    budget_basis: str | None
    about_path: Path


@dataclass(frozen=True)
class Person:
    person_id: PersonId
    canonical_name: str
    aliases: tuple[str, ...]
    status: str | None
    site_ids: tuple[SiteId, ...]
    note_path: Path


@dataclass(frozen=True)
class Resolution:
    query: str
    matches: tuple[str, ...]
    reason: str

    @property
    def matched_id(self) -> str | None:
        return self.matches[0] if len(self.matches) == 1 else None

    @property
    def is_missing(self) -> bool:
        return not self.matches

    @property
    def is_ambiguous(self) -> bool:
        return len(self.matches) > 1

