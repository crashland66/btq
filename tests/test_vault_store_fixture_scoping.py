from __future__ import annotations

import pytest

from btq_vault.couch_store import CouchDBEntityStore
from event_pipeline import couchdb_config


class FakeCouchConfig:
    base_url = "http://couchdb.test"
    timeout = 3.5

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": "Basic test-token"}


def test_real_from_env_used_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(couchdb_config, "from_env", lambda: FakeCouchConfig())
    monkeypatch.setattr(couchdb_config, "vault_database", lambda: "btq_vault_test")

    store = CouchDBEntityStore.from_env()

    assert type(store) is CouchDBEntityStore
    assert store.base_url == "http://couchdb.test"
    assert store.auth_headers == {"Authorization": "Basic test-token"}
    assert store.database == "btq_vault_test"
    assert store.timeout == 3.5


@pytest.mark.usefixtures("recording_vault_store")
def test_recording_store_available_where_requested() -> None:
    store = CouchDBEntityStore.from_env()
    database_store = CouchDBEntityStore.for_database_from_env("btq_other")

    assert type(store).__name__ == "RmwRecordingVaultStore"
    assert type(database_store).__name__ == "RmwRecordingVaultStore"
