from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib import error

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb.backfill_legacy_captures import build_capture_document, main, run_backfill


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


def request_payload(req: object) -> dict[str, Any]:
    data = getattr(req, "data", None)
    if not data:
        return {}
    return json.loads(data.decode("utf-8"))


def http_error(url: str, code: int) -> error.HTTPError:
    return error.HTTPError(url, code, "error", hdrs=None, fp=None)


def write_queue_file(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def sample_queue_job(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "job_id": "job-1",
        "job_type": "photo_capture",
        "metadata": {
            "capture_id": "cap-legacy-1",
            "source": "field_capture_app",
            "person_id": "person-1",
            "person_name": "Example Person",
            "field_capture_token_id": "token-1",
            "field_capture_token_label": "Shift token",
            "site_id": "7050",
        },
        "payload": {
            "site": "Summit Wire",
            "qc_category": "completed_area",
            "note": "Lobby complete.",
            "captured_at": "2026-05-09T14:30:00Z",
            "exported_at": "2026-05-09T14:31:00Z",
            "photos": [
                {
                    "filename": "photo_001.jpg",
                    "mime_type": "image/jpeg",
                    "stored_path": "/srv/btq/runtime/uploads/2026-05-09/cap-legacy-1/photo_001.jpg",
                    "upload_id": "2026-05-09/cap-legacy-1/photo_001.jpg",
                }
            ],
            "audio": [],
        },
    }
    job.update(overrides)
    return job


def fake_config() -> couchdb_config.CouchDBConfig:
    return couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)


def install_create_urlopen(monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]] | None = None) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method")
        url = getattr(req, "full_url")
        if method == "GET":
            raise http_error(url, 404)
        if method == "PUT":
            if sent is not None:
                sent.append(request_payload(req))
            return FakeResponse({"ok": True, "id": request_payload(req).get("_id"), "rev": "1-a"})
        raise AssertionError(method)

    monkeypatch.setattr("event_pipeline.couchdb.backfill_legacy_captures.request.urlopen", fake_urlopen)


def test_builds_couchdb_document_from_queue_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    install_create_urlopen(monkeypatch, sent)
    queue_path = tmp_path / "job.json"
    write_queue_file(queue_path, sample_queue_job())

    report = run_backfill(queue_dir=tmp_path, database="btq_field_captures", config=fake_config())

    assert report["created"] == 1
    doc = sent[0]
    assert doc["_id"] == "cap-legacy-1"
    assert doc["type"] == "field_capture"
    assert doc["processing_state"] == "complete"
    assert doc["created_at"] == "2026-05-09T14:30:00Z"
    assert doc["site_id"] == "7050"
    assert doc["photos"][0]["upload_id"] == "2026-05-09/cap-legacy-1/photo_001.jpg"
    assert doc["backfill_source"] == "queue_file"
    assert doc["backfill_source_path"] == str(queue_path)


def test_skips_non_photo_capture_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    install_create_urlopen(monkeypatch, sent)
    write_queue_file(tmp_path / "job.json", sample_queue_job(job_type="log_site_issue"))

    report = run_backfill(queue_dir=tmp_path, database="btq_field_captures", config=fake_config())

    assert report["skipped_not_photo_capture"] == 1
    assert sent == []


def test_skips_existing_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        calls.append(getattr(req, "method"))
        if getattr(req, "method") == "GET":
            return FakeResponse({"_id": "cap-legacy-1", "_rev": "1-a"})
        raise AssertionError("PUT should not be issued for existing documents")

    monkeypatch.setattr("event_pipeline.couchdb.backfill_legacy_captures.request.urlopen", fake_urlopen)
    write_queue_file(tmp_path / "job.json", sample_queue_job())

    report = run_backfill(queue_dir=tmp_path, database="btq_field_captures", config=fake_config())

    assert report["skipped_already_exists"] == 1
    assert calls == ["GET"]


def test_failed_files_dont_abort_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    install_create_urlopen(monkeypatch, sent)
    write_queue_file(tmp_path / "good.json", sample_queue_job())
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")

    exit_code = main(["--queue-dir", str(tmp_path), "--database", "btq_field_captures"])

    assert exit_code == 1
    assert len(sent) == 1


def test_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    methods: list[str] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method")
        methods.append(method)
        if method == "GET":
            raise http_error(getattr(req, "full_url"), 404)
        raise AssertionError("dry run should not write")

    monkeypatch.setattr("event_pipeline.couchdb.backfill_legacy_captures.request.urlopen", fake_urlopen)
    write_queue_file(tmp_path / "job.json", sample_queue_job())

    report = run_backfill(queue_dir=tmp_path, database="btq_field_captures", config=fake_config(), dry_run=True)

    assert report["created"] == 1
    assert methods == ["GET"]


def test_audio_field_propagated_when_present(tmp_path: Path) -> None:
    queue_path = tmp_path / "job.json"
    job = sample_queue_job()
    job["payload"]["audio"] = [
        {
            "media_type": "audio",
            "filename": "audio_001.webm",
            "mime_type": "audio/webm",
            "stored_path": "/srv/btq/runtime/uploads/2026-05-09/cap-legacy-1/audio_001.webm",
            "upload_id": "2026-05-09/cap-legacy-1/audio_001.webm",
            "duration_seconds": "8.4",
        }
    ]

    doc = build_capture_document(queue_path, job)

    assert doc["audio"] == job["payload"]["audio"]


def test_audio_field_omitted_when_absent(tmp_path: Path) -> None:
    queue_path = tmp_path / "job.json"
    job = sample_queue_job()
    del job["payload"]["audio"]

    doc = build_capture_document(queue_path, job)

    assert "audio" not in doc


def test_missing_capture_id_fails_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    install_create_urlopen(monkeypatch, sent)
    job = sample_queue_job()
    del job["metadata"]["capture_id"]
    write_queue_file(tmp_path / "job.json", job)

    report = run_backfill(queue_dir=tmp_path, database="btq_field_captures", config=fake_config())

    assert report["failed"] == 1
    assert report["failed_paths"] == [str(tmp_path / "job.json")]
    assert sent == []
