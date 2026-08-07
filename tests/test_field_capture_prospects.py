from __future__ import annotations

import json
from urllib import error

import pytest

from btq_vault.entity_types import OPERATOR_ID_GREG
from event_pipeline import couchdb_config
from field_capture import prospects


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_build_prospect_document_minimum_fields() -> None:
    doc = prospects.build_prospect_document(prospect_id="kmf-birch-1", name="KMF Birch Ave", created_at="2026-05-27T12:00:00Z")

    assert doc["_id"] == "prospect_kmf-birch-1"
    assert doc["doc_type"] == "prospect"
    assert doc["type"] == "prospect"
    assert doc["status"] == "open"
    assert doc["promoted_to_site_id"] is None


def test_build_prospect_document_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="invalid prospect status"):
        prospects.build_prospect_document(prospect_id="x", name="Prospect", status="signed")


def test_build_prospect_document_includes_operator() -> None:
    doc = prospects.build_prospect_document(prospect_id="x", name="Prospect")

    assert doc["operator"] == OPERATOR_ID_GREG


def test_write_prospect_round_trips_via_load_prospect(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[object] = []
    stored = {
        "_id": "prospect_x",
        "_rev": "1-test",
        "doc_type": "prospect",
        "type": "prospect",
        "prospect_id": "x",
        "name": "Prospect X",
        "status": "open",
    }

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        requests.append(req)
        if getattr(req, "method", "GET") == "GET":
            if len(requests) == 1:
                raise error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
            return FakeResponse(stored)
        return FakeResponse({"ok": True, "id": "prospect_x", "rev": "1-test"}, status=201)

    monkeypatch.setattr("field_capture.prospects.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)
    doc = prospects.build_prospect_document(prospect_id="x", name="Prospect X")

    assert prospects.write_prospect(config, prospect=doc)["ok"] is True
    assert prospects.load_prospect(config, "x") == stored


def test_write_prospect_targets_vault_database(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        requests.append(req)
        if getattr(req, "method", "GET") == "GET":
            raise error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        return FakeResponse({"ok": True, "id": "prospect_x", "rev": "1-test"}, status=201)

    monkeypatch.setattr("field_capture.prospects.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)
    doc = prospects.build_prospect_document(prospect_id="x", name="Prospect X")

    assert prospects.write_prospect(config, prospect=doc)["ok"] is True
    assert requests
    urls = [getattr(req, "full_url", "") for req in requests]
    # Prospects live in the canonical vault; btq_sites is retired.
    assert any("/btq_vault/" in url for url in urls)
    assert not any("/btq_sites/" in url for url in urls)


def test_load_prospect_returns_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        raise error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr("field_capture.prospects.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)

    assert prospects.load_prospect(config, "missing") is None
