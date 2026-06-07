from __future__ import annotations

import json
from urllib import error

import pytest

from event_pipeline import sites
from event_pipeline.couchdb_registry import CouchDBRegistryError, CouchDBSiteRegistry


NOTE_PATH = "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md"
VISION_CONTEXT = {
    "context_id": "summit_wire",
    "label": "Summit Wire",
    "summary": "Industrial wire manufacturing facility.",
    "environment": "industrial_manufacturing",
}


@pytest.fixture(autouse=True)
def reset_sites_registry_singleton() -> None:
    sites._registry_instance = None
    yield
    sites._registry_instance = None


def alias_payload() -> dict[str, object]:
    return {
        "total_rows": 2,
        "offset": 0,
        "rows": [
            {
                "id": "site_7050",
                "key": "summit wire",
                "value": {
                    "site_id": "7050",
                    "canonical": "Summit Wire",
                    "note_path": NOTE_PATH,
                    "vision_context": VISION_CONTEXT,
                },
            },
            {
                "id": "site_7050",
                "key": "summit",
                "value": {
                    "site_id": "7050",
                    "canonical": "Summit Wire",
                    "note_path": NOTE_PATH,
                    "vision_context": VISION_CONTEXT,
                },
            },
        ],
    }


def site_id_payload(site_id: str) -> dict[str, object]:
    if site_id != "7050":
        return {"total_rows": 0, "offset": 0, "rows": []}
    return {
        "total_rows": 1,
        "offset": 0,
        "rows": [
            {
                "id": "site_7050",
                "key": "7050",
                "value": {
                    "site_id": "7050",
                    "canonical": "Summit Wire",
                    "note_path": NOTE_PATH,
                    "vision_context": VISION_CONTEXT,
                },
            }
        ],
    }


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def fake_urlopen_for_views(calls: list[str]):
    def fake_urlopen(req: object, timeout: float = 10.0) -> FakeResponse:
        url = getattr(req, "full_url")
        calls.append(url)
        if "_view/by_site_id" in url:
            if "7050" in url:
                return FakeResponse(site_id_payload("7050"))
            return FakeResponse(site_id_payload("0000"))
        return FakeResponse(alias_payload())

    return fake_urlopen


def test_resolve_site_exact_alias_high(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_views([]))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_site("summit") == ("Summit Wire", "high")


def test_resolve_site_substring_medium(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_views([]))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_site("i was at the summit wire plant today") == ("Summit Wire", "medium")


def test_resolve_site_unknown_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_views([]))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_site("somewhere else entirely") == ("unknown", "low")


def test_resolve_site_raises_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_req: object, timeout: float = 10.0) -> FakeResponse:
        return FakeResponse({"error": "server_error"}, status=500)

    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen)
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    with pytest.raises(CouchDBRegistryError):
        registry.resolve_site("summit")


def test_resolve_site_raises_on_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_req: object, timeout: float = 10.0) -> FakeResponse:
        raise error.URLError("connection refused")

    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen)
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    with pytest.raises(CouchDBRegistryError):
        registry.resolve_site("summit")


def test_resolve_site_uses_cache_on_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_views(calls))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_site("summit") == ("Summit Wire", "high")
    assert registry.resolve_site("summit wire plant") == ("Summit Wire", "medium")

    assert len(calls) == 1


def test_resolve_site_note_path_known_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_views([]))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_site_note_path("Summit Wire") == NOTE_PATH


def test_get_vision_context_known_site_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_views([]))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.get_vision_context("7050") == VISION_CONTEXT


def test_get_vision_context_unknown_site_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_views([]))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.get_vision_context("9999") is None


def test_resolve_canonical_known_site_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_views([]))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_canonical("7050") == "Summit Wire"


def test_resolve_canonical_unknown_site_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_views([]))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_canonical("9999") is None


def test_invalidate_causes_resolve_site_to_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_views(calls))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_site("summit") == ("Summit Wire", "high")
    registry.invalidate()
    assert registry.resolve_site("summit") == ("Summit Wire", "high")

    assert len(calls) == 2


def test_sites_resolve_site_delegates_when_couchdb_url_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRegistry:
        def resolve_site(self, text: str) -> tuple[str, str]:
            assert text == "summit"
            return "Delegated Site", "high"

    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")
    monkeypatch.setattr(sites, "CouchDBSiteRegistry", FakeRegistry)

    assert sites.resolve_site("summit") == ("Delegated Site", "high")


def test_sites_resolve_site_uses_hardcoded_sites_without_couchdb_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    assert sites.resolve_site("summit") == ("Summit Wire", "high")


def test_sites_resolve_site_reuses_singleton_when_couchdb_url_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    instances: list[object] = []

    class FakeRegistry:
        def __init__(self) -> None:
            instances.append(self)

        def resolve_site(self, text: str) -> tuple[str, str]:
            return f"Delegated {text}", "high"

    sites._registry_instance = None
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")
    monkeypatch.setattr(sites, "CouchDBSiteRegistry", FakeRegistry)

    assert sites.resolve_site("one") == ("Delegated one", "high")
    assert sites.resolve_site("two") == ("Delegated two", "high")

    assert len(instances) == 1


def test_get_registry_clears_singleton_when_couchdb_url_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRegistry:
        pass

    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")
    monkeypatch.setattr(sites, "CouchDBSiteRegistry", FakeRegistry)
    registry = sites._get_registry()

    assert registry is not None

    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    assert sites._get_registry() is None
    assert sites._registry_instance is None


def test_get_registry_creates_new_singleton_after_env_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRegistry:
        pass

    monkeypatch.setattr(sites, "CouchDBSiteRegistry", FakeRegistry)
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")
    first = sites._get_registry()

    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    assert sites._get_registry() is None

    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")
    second = sites._get_registry()

    assert second is not None
    assert second is not first
