from __future__ import annotations

import pytest

from event_pipeline.couchdb_registry import CouchDBRegistryError
from queue_processor.handlers import _shared as shared


def test_site_id_registered_offline_uses_hardcoded_sites(monkeypatch) -> None:
    """With BTQ_COUCHDB_URL unset, the offline fallback checks the hardcoded SITES.

    Regression guard for a latent NameError: the fallback branch references the
    module-level ``SITES`` symbol, which must be imported into ``_shared``.
    """
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    # "7050" is the Summit Wire site_id in event_pipeline.sites.SITES.
    assert shared._site_id_registered("7050") is True
    assert shared._site_id_registered("  7050  ") is True
    assert shared._site_id_registered("not-a-real-site") is False


def test_site_id_registered_offline_empty_is_false(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    assert shared._site_id_registered("") is False
    assert shared._site_id_registered("   ") is False


def test_site_id_registered_couchdb_branch_resolves_via_registry(monkeypatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.example")

    seen: list[str] = []

    class FakeRegistry:
        def resolve_canonical(self, normalized: str):
            seen.append(normalized)
            return "Summit Wire" if normalized == "7050" else None

    monkeypatch.setattr(shared, "CouchDBSiteRegistry", lambda: FakeRegistry())

    assert shared._site_id_registered("7050") is True
    assert shared._site_id_registered("9999") is False
    assert seen == ["7050", "9999"]


def test_site_id_registered_couchdb_failure_raises_queue_error(monkeypatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.example")

    class FailingRegistry:
        def resolve_canonical(self, normalized: str):
            raise CouchDBRegistryError("registry unavailable")

    monkeypatch.setattr(shared, "CouchDBSiteRegistry", lambda: FailingRegistry())

    with pytest.raises(shared.QueueProcessorError, match="site registry unavailable"):
        shared._site_id_registered("7050")
