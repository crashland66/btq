from __future__ import annotations

from datetime import date
from typing import Any, Callable

import pytest

from btq_vault.strip_vault_path import StripVaultPathError, strip_vault_path


class FakeStore:
    def __init__(self, database: str, docs: list[dict[str, Any]], events: list[str]) -> None:
        self.database = database
        self.docs = [dict(doc) for doc in docs]
        self.events = events

    def find_docs_with_field(self, field_name: str) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self.docs if field_name in doc]

    def update_doc(
        self,
        doc_id: str,
        transform: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        *,
        create: Callable[[], dict[str, Any]] | None = None,
        require_existing: bool = True,
        max_conflict_retries: int = 1,
    ) -> dict[str, Any]:
        index = next((idx for idx, doc in enumerate(self.docs) if doc.get("_id") == doc_id), None)
        current = dict(self.docs[index]) if index is not None else None
        outgoing = transform(current)
        if outgoing is None:
            if current is None:
                return {}
            return current
        self.events.append(f"mutate:{self.database}:{doc_id}")
        if index is None:
            self.docs.append(dict(outgoing))
        else:
            self.docs[index] = dict(outgoing)
        return dict(outgoing)


def fake_store_factory(seed: dict[str, list[dict[str, Any]]], events: list[str]) -> Callable[[str], FakeStore]:
    stores: dict[str, FakeStore] = {}

    def factory(database: str) -> FakeStore:
        if database not in stores:
            stores[database] = FakeStore(database, seed.get(database, []), events)
        return stores[database]

    return factory


def test_strip_vault_path_dry_run_counts_and_writes_nothing() -> None:
    events: list[str] = []
    backups: list[tuple[str, str]] = []
    seed = {
        "btq_vault": [{"_id": "v1", "vault_path": "Accounts/A.md"}, {"_id": "v2"}],
        "btq_sites": [{"_id": "s1", "vault_path": "Sites/S.md"}],
        "btq_people": [{"_id": "p1"}],
    }

    result = strip_vault_path(
        apply=False,
        databases=("btq_vault", "btq_sites", "btq_people"),
        store_factory=fake_store_factory(seed, events),
        backup_fn=lambda source, target: backups.append((source, target)),
        backup_day=date(2026, 6, 9),
    )

    assert result.as_dict()["matched"] == 2
    assert [item.matched for item in result.databases] == [1, 1, 0]
    assert events == []
    assert backups == []
    assert seed["btq_vault"][0]["vault_path"] == "Accounts/A.md"


def test_strip_vault_path_apply_backs_up_then_removes_field_and_is_idempotent() -> None:
    events: list[str] = []
    seed = {
        "btq_vault": [{"_id": "v1", "_rev": "1-a", "vault_path": "Accounts/A.md"}, {"_id": "v2"}],
        "btq_sites": [{"_id": "s1", "_rev": "1-b", "vault_path": "Sites/S.md"}],
        "btq_people": [{"_id": "p1", "_rev": "1-c", "vault_path": "People/P.md"}],
    }
    factory = fake_store_factory(seed, events)

    def backup(source: str, target: str) -> None:
        events.append(f"backup:{source}:{target}")

    first = strip_vault_path(
        apply=True,
        databases=("btq_vault", "btq_sites", "btq_people"),
        store_factory=factory,
        backup_fn=backup,
        backup_day=date(2026, 6, 9),
    )
    second = strip_vault_path(
        apply=True,
        databases=("btq_vault", "btq_sites", "btq_people"),
        store_factory=factory,
        backup_fn=backup,
        backup_day=date(2026, 6, 9),
    )

    assert first.changed == 3
    assert second.changed == 0
    assert events[:3] == [
        "backup:btq_vault:btq_vault_bak_20260609",
        "backup:btq_sites:btq_sites_bak_20260609",
        "backup:btq_people:btq_people_bak_20260609",
    ]
    assert all(not event.startswith("mutate:") for event in events[:3])
    for database in ("btq_vault", "btq_sites", "btq_people"):
        store = factory(database)
        assert all("vault_path" not in doc for doc in store.docs)


def test_strip_vault_path_refuses_mutation_when_backup_fails() -> None:
    events: list[str] = []
    seed = {"btq_vault": [{"_id": "v1", "_rev": "1-a", "vault_path": "Accounts/A.md"}]}

    def backup(_source: str, _target: str) -> None:
        events.append("backup")
        raise StripVaultPathError("backup failed")

    with pytest.raises(StripVaultPathError):
        strip_vault_path(
            apply=True,
            databases=("btq_vault",),
            store_factory=fake_store_factory(seed, events),
            backup_fn=backup,
            backup_day=date(2026, 6, 9),
        )

    assert events == ["backup"]
