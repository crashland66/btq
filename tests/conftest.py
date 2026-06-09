from __future__ import annotations

import sys
from typing import Any, Callable

import pytest

from btq_vault.couch_store import CouchDBEntityStore
from test_helpers.queue_processor_stores import RecordingVaultStore


class RmwRecordingVaultStore(RecordingVaultStore):
    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                return dict(doc)
        return None

    def find_location_docs(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        locations = [dict(doc) for doc in self.docs if doc.get("type") == "location"]
        return locations[:limit]

    def job_id_applied_doc_id(self, job_id: str) -> str | None:
        for doc in self.docs:
            job_ids = doc.get("btq_job_ids")
            if isinstance(job_ids, list) and job_id in [str(item) for item in job_ids]:
                doc_id = doc.get("_id")
                return str(doc_id) if doc_id else None
        return None

    def find_unknown_capture_docs(self, status: str | None = "unresolved", *, limit: int = 10000) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for doc in self.docs:
            if doc.get("type") != "unknown_capture":
                continue
            if status is not None and doc.get("status") != status:
                continue
            results.append(dict(doc))
            if len(results) >= limit:
                break
        return results

    def update_doc(
        self,
        doc_id: str,
        transform: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        *,
        create: Callable[[], dict[str, Any]] | None = None,
        require_existing: bool = True,
        max_conflict_retries: int = 1,
    ) -> dict[str, Any]:
        current_index = next((index for index, doc in enumerate(self.docs) if doc.get("_id") == doc_id), None)
        current = dict(self.docs[current_index]) if current_index is not None else None
        if current is None:
            if require_existing:
                raise RuntimeError(f"missing required doc: {doc_id}")
            seed = create() if create is not None else None
        else:
            seed = current
        outgoing = transform(seed)
        if outgoing is None:
            if current is None:
                raise RuntimeError(f"no-op has no document: {doc_id}")
            return current
        stored = dict(outgoing)
        if current_index is None:
            self.docs.append(stored)
        else:
            self.docs[current_index] = stored
        return dict(stored)


@pytest.fixture
def recording_vault_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide an opt-in working canonical vault store for legacy handler tests.

    Prompt 175 made the canonical CouchDB write fail-closed, so handler tests
    that previously relied on warn-and-continue now need a working store. Tests
    must request this fixture explicitly so the rest of the suite exercises the
    real ``CouchDBEntityStore.from_env`` path by default.

    Patching ``queue_processor.main._VAULT_STORE`` alone is NOT enough:
    ``tests/test_queue_processor.py`` execs ``main.py`` into a *separate*
    synthetic module (``queue_processor_main_for_tests``) with its own
    ``_VAULT_STORE`` global. Both module instances import the same
    ``CouchDBEntityStore`` class from ``btq_vault.couch_store``, so we patch the
    class's ``from_env`` to hand back an in-memory store, and null out the
    cached ``_VAULT_STORE`` on every already-loaded queue-processor module so the
    next ``_vault_store()`` call lazily rebuilds via the patched ``from_env``.

    Tests that verify explicit store behavior (e.g. the couchdb-write fail tests)
    still override by setting ``_VAULT_STORE`` directly after this fixture runs.
    """
    monkeypatch.setattr(
        CouchDBEntityStore, "from_env", classmethod(lambda cls: RmwRecordingVaultStore())
    )
    monkeypatch.setattr(
        CouchDBEntityStore,
        "for_database_from_env",
        classmethod(lambda cls, database: RmwRecordingVaultStore()),
    )
    for name, module in list(sys.modules.items()):
        if name.startswith("queue_processor") or name.endswith("queue_processor_main_for_tests"):
            if module is not None and hasattr(module, "_VAULT_STORE"):
                monkeypatch.setattr(module, "_VAULT_STORE", None, raising=False)
            if module is not None and hasattr(module, "_PERSONAL_JOURNAL_STORE"):
                monkeypatch.setattr(module, "_PERSONAL_JOURNAL_STORE", None, raising=False)
