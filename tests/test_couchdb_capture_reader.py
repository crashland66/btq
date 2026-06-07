from __future__ import annotations

import json
from urllib import error

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_reader import (
    CouchDBCaptureReaderError,
    query_captures_by_site_id,
    query_capture_target_by_upload_id,
    query_site_id_by_upload_id,
)


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")


def test_query_captures_by_site_id_reads_view_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        calls.append((req, timeout))
        return FakeResponse(
            {
                "rows": [
                    {"key": ["7050", "2026-05-09T10:00:00-04:00", "cap-new"], "value": {"capture_id": "cap-new", "site_id": "7050"}},
                    {"key": ["7050", "2026-05-08T10:00:00-04:00", "cap-old"], "value": {"capture_id": "cap-old", "site_id": "7050"}},
                ]
            }
        )

    monkeypatch.setattr("event_pipeline.couchdb_capture_reader.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "jordan", "secret", 12.0, 10000)

    rows = query_captures_by_site_id(config, "7050", database="btq_field_captures")

    req, timeout = calls[0]
    assert rows == [{"capture_id": "cap-new", "site_id": "7050"}, {"capture_id": "cap-old", "site_id": "7050"}]
    assert timeout == 12.0
    assert getattr(req, "full_url").startswith("http://couchdb.test/btq_field_captures/_design/btq_field_captures/_view/by_site_id?")
    assert "descending=true" in getattr(req, "full_url")
    assert req.get_header("Authorization", "").startswith("Basic ")


def test_query_capture_target_by_upload_id_returns_location_target_for_post_155_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        return FakeResponse({"rows": [{"key": "2026-05-09/cap/photo.jpg", "value": {"target_type": "location", "target_id": "7050", "site_id": "7050"}}]})

    monkeypatch.setattr("event_pipeline.couchdb_capture_reader.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)

    target = query_capture_target_by_upload_id(config, "2026-05-09/cap/photo.jpg", database="btq_field_captures")

    assert target == {"target_type": "location", "target_id": "7050", "site_id": "7050"}


def test_query_capture_target_by_upload_id_returns_prospect_target(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        return FakeResponse({"rows": [{"key": "2026-05-09/cap/photo.jpg", "value": {"target_type": "prospect", "target_id": "kmf-birch-1", "site_id": ""}}]})

    monkeypatch.setattr("event_pipeline.couchdb_capture_reader.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)

    target = query_capture_target_by_upload_id(config, "2026-05-09/cap/photo.jpg", database="btq_field_captures")

    assert target == {"target_type": "prospect", "target_id": "kmf-birch-1", "site_id": ""}


def test_query_capture_target_by_upload_id_promotes_pre_155_string_value_to_location(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        return FakeResponse({"rows": [{"key": "2026-05-09/cap/photo.jpg", "value": "7050"}]})

    monkeypatch.setattr("event_pipeline.couchdb_capture_reader.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)

    target = query_capture_target_by_upload_id(config, "2026-05-09/cap/photo.jpg", database="btq_field_captures")

    assert target == {"target_type": "location", "target_id": "7050", "site_id": "7050"}


def test_query_capture_target_by_upload_id_returns_none_on_empty_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        return FakeResponse({"rows": []})

    monkeypatch.setattr("event_pipeline.couchdb_capture_reader.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)

    assert query_capture_target_by_upload_id(config, "missing.jpg", database="btq_field_captures") is None


def test_query_site_id_by_upload_id_returns_none_for_prospect_target(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        return FakeResponse({"rows": [{"key": "2026-05-09/cap/photo.jpg", "value": {"target_type": "prospect", "target_id": "kmf-birch-1", "site_id": ""}}]})

    monkeypatch.setattr("event_pipeline.couchdb_capture_reader.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)

    assert query_site_id_by_upload_id(config, "2026-05-09/cap/photo.jpg", database="btq_field_captures") is None


def test_query_site_id_by_upload_id_returns_site_id_for_location_target(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        return FakeResponse({"rows": [{"key": "2026-05-09/cap/photo.jpg", "value": {"target_type": "location", "target_id": "7050", "site_id": "7050"}}]})

    monkeypatch.setattr("event_pipeline.couchdb_capture_reader.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)

    assert query_site_id_by_upload_id(config, "2026-05-09/cap/photo.jpg", database="btq_field_captures") == "7050"


def test_capture_reader_raises_on_couchdb_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        raise error.URLError("connection refused")

    monkeypatch.setattr("event_pipeline.couchdb_capture_reader.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)

    with pytest.raises(CouchDBCaptureReaderError, match="capture view failed"):
        query_captures_by_site_id(config, "7050", database="btq_field_captures")
