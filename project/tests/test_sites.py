from __future__ import annotations

import pytest

from event_pipeline import sites
from event_pipeline.couchdb_registry import CouchDBRegistryError


class FailingRegistry:
    def resolve_site(self, text: str) -> tuple[str, str]:
        raise CouchDBRegistryError(f"registry unavailable for {text}")

    def resolve_site_id(self, site_name: str) -> str | None:
        raise CouchDBRegistryError(f"registry unavailable for {site_name}")

    def resolve_site_note_path(self, site_name: str) -> str | None:
        raise CouchDBRegistryError(f"registry unavailable for {site_name}")


def test_hardcoded_site_fallback_when_couchdb_registry_unset(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    monkeypatch.setattr(sites, "_registry_instance", None)

    assert sites.resolve_site("Summit Wire") == ("Summit Wire", "high")
    assert sites.resolve_site_id("Summit Wire") == "7050"
    assert sites.resolve_site_note_path("Summit Wire") == (
        "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md"
    )


def test_resolve_site_kmf_iron_st_7040(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    monkeypatch.setattr(sites, "_registry_instance", None)

    for query in ("KMF Industries- Oak St", "kmf oak st", "oak st"):
        canonical, confidence = sites.resolve_site(query)
        assert canonical == "KMF Industries- Oak St"
        assert confidence in {"high", "medium"}


def test_resolve_site_id_7040_7041(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    monkeypatch.setattr(sites, "_registry_instance", None)

    assert sites.resolve_site_id("KMF Industries- Oak St") == "7040"
    assert sites.resolve_site_id("KMF Industries- Maple Pike") == "7041"


def test_resolve_site_note_path_7040_7041(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    monkeypatch.setattr(sites, "_registry_instance", None)

    assert sites.resolve_site_note_path("KMF Industries- Oak St") == (
        "Accounts/Kmf/Locations/7040 - KMF Industries- Oak St/about.md"
    )
    assert sites.resolve_site_note_path("KMF Industries- Maple Pike") == (
        "Accounts/Kmf/Locations/7041 - KMF Industries- Maple Pike/about.md"
    )


def test_is_site_registered_true_false(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    monkeypatch.setattr(sites, "_registry_instance", None)

    assert sites.is_site_registered("Summit Wire") is True
    assert sites.is_site_registered("kmf oak st") is True
    assert sites.is_site_registered("Totally Unknown Site") is False


def test_resolve_site_logs_and_reraises_registry_error(monkeypatch, caplog) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.example")
    monkeypatch.setattr(sites, "_get_registry", lambda: FailingRegistry())
    caplog.set_level("ERROR", logger="event_pipeline.sites")

    with pytest.raises(CouchDBRegistryError):
        sites.resolve_site("Summit Wire")

    assert "CouchDB site registry unavailable; failing closed (resolve_site)" in caplog.text
    assert "do not edit sites.py" in caplog.text


def test_resolve_site_id_logs_and_reraises_registry_error(monkeypatch, caplog) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.example")
    monkeypatch.setattr(sites, "_get_registry", lambda: FailingRegistry())
    caplog.set_level("ERROR", logger="event_pipeline.sites")

    with pytest.raises(CouchDBRegistryError):
        sites.resolve_site_id("Summit Wire")

    assert "CouchDB site registry unavailable; failing closed (resolve_site_id)" in caplog.text
    assert "do not edit sites.py" in caplog.text


def test_resolve_site_note_path_logs_and_reraises_registry_error(monkeypatch, caplog) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.example")
    monkeypatch.setattr(sites, "_get_registry", lambda: FailingRegistry())
    caplog.set_level("ERROR", logger="event_pipeline.sites")

    with pytest.raises(CouchDBRegistryError):
        sites.resolve_site_note_path("Summit Wire")

    assert "CouchDB site registry unavailable; failing closed (resolve_site_note_path)" in caplog.text
    assert "do not edit sites.py" in caplog.text
