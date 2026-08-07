from __future__ import annotations

import json
from urllib import error

import pytest

from event_pipeline.couchdb_registry import CouchDBSiteRegistry


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


def site_doc_payload(
    *,
    capture_guidance: object = None,
    display_categories: object = None,
    include_capture_guidance: bool = False,
    include_display_categories: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "_id": "location_7050",
        "type": "location",
        "site_id": "7050",
        "location": "Summit Wire",
        "capture_active": True,
    }
    if include_capture_guidance:
        payload["capture_guidance"] = capture_guidance
    if include_display_categories:
        payload["display_categories"] = display_categories
    return payload


def fake_urlopen_for_site_doc(payload: dict[str, object] | None):
    def fake_urlopen(req: object, timeout: float = 10.0) -> FakeResponse:
        url = getattr(req, "full_url")
        # The registry reads registration off the canonical btq_vault location
        # doc; a btq_sites site_<id> GET would be the retired path.
        if url.endswith("/btq_vault/location_7050") and payload is not None:
            return FakeResponse(payload)
        raise error.HTTPError(url, 404, "not found", hdrs=None, fp=None)

    return fake_urlopen


def test_get_capture_guidance_returns_value_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "event_pipeline.couchdb_registry.request.urlopen",
        fake_urlopen_for_site_doc(site_doc_payload(capture_guidance="  Focus north dock today  ", include_capture_guidance=True)),
    )
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.get_capture_guidance("7050") == "Focus north dock today"


def test_get_capture_guidance_returns_none_when_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "event_pipeline.couchdb_registry.request.urlopen",
        fake_urlopen_for_site_doc(site_doc_payload(capture_guidance="   ", include_capture_guidance=True)),
    )
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.get_capture_guidance("7050") is None


def test_get_capture_guidance_returns_none_when_field_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_site_doc(site_doc_payload()))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.get_capture_guidance("7050") is None


def test_get_capture_guidance_returns_none_for_unknown_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_site_doc(None))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.get_capture_guidance("9999") is None


def test_get_display_categories_returns_list_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    categories = [{"label": "Mill floor", "canonical": "Common / Open Areas"}]
    monkeypatch.setattr(
        "event_pipeline.couchdb_registry.request.urlopen",
        fake_urlopen_for_site_doc(site_doc_payload(display_categories=categories, include_display_categories=True)),
    )
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.get_display_categories("7050") == categories


def test_get_display_categories_drops_malformed_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "event_pipeline.couchdb_registry.request.urlopen",
        fake_urlopen_for_site_doc(
            site_doc_payload(
                display_categories=[
                    {"label": "Mill floor", "canonical": "Common / Open Areas"},
                    {"label": "", "canonical": "Trash"},
                    {"label": "Restroom", "canonical": ""},
                    "bad",
                ],
                include_display_categories=True,
            )
        ),
    )
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.get_display_categories("7050") == [{"label": "Mill floor", "canonical": "Common / Open Areas"}]


def test_get_display_categories_returns_none_when_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "event_pipeline.couchdb_registry.request.urlopen",
        fake_urlopen_for_site_doc(site_doc_payload(display_categories=[], include_display_categories=True)),
    )
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.get_display_categories("7050") is None


def test_get_display_categories_returns_none_when_field_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("event_pipeline.couchdb_registry.request.urlopen", fake_urlopen_for_site_doc(site_doc_payload()))
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.get_display_categories("7050") is None
