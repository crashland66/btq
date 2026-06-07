from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from event_pipeline import couchdb_config
from queue_processor import couchdb_queue_watcher


class FakeListener:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.processing: list[str] = []
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def listen(self) -> Any:
        yield from self.docs

    def mark_processing(self, doc: dict[str, Any]) -> dict[str, Any]:
        doc_id = str(doc.get("_id") or "")
        self.processing.append(doc_id)
        updated = dict(doc)
        updated["btq_state"] = "processing"
        updated["_rev"] = f"{doc.get('_rev')}-processing"
        return updated

    def mark_complete(self, doc: dict[str, Any]) -> None:
        self.completed.append(str(doc.get("_id") or ""))

    def mark_failed(self, doc: dict[str, Any], reason: str) -> None:
        self.failed.append((str(doc.get("_id") or ""), reason))


class FakeStaleSweepListener:
    state_field = "btq_state"

    def __init__(self, docs: list[dict[str, Any]], *, conflict_ids: set[str] | None = None) -> None:
        self.docs = docs
        self.conflict_ids = conflict_ids or set()
        self.reset_doc_ids: list[str] = []

    def find_stale_processing_docs(self, *, cutoff: datetime, limit: int) -> list[dict[str, Any]]:
        return self.docs[:limit]

    def reset_stale_processing_doc(self, doc: dict[str, Any], *, now: datetime) -> str:
        doc_id = str(doc.get("_id") or "")
        if doc_id in self.conflict_ids:
            return couchdb_queue_watcher.RESET_CONFLICT
        self.reset_doc_ids.append(doc_id)
        previous_started_at = str(doc.get(couchdb_queue_watcher.PROCESSING_STARTED_AT_FIELD) or "")
        doc["btq_state"] = "pending"
        doc.pop(couchdb_queue_watcher.PROCESSING_STARTED_AT_FIELD, None)
        doc[couchdb_queue_watcher.LAST_STALE_PROCESSING_STARTED_AT_FIELD] = previous_started_at
        doc[couchdb_queue_watcher.STALE_RESET_AT_FIELD] = now.isoformat()
        doc[couchdb_queue_watcher.STALE_RESET_COUNT_FIELD] = int(doc.get(couchdb_queue_watcher.STALE_RESET_COUNT_FIELD) or 0) + 1
        doc["_rev"] = f"{doc.get('_rev')}-reset"
        return couchdb_queue_watcher.RESET_APPLIED


def logger() -> logging.Logger:
    return logging.getLogger("test.queue_couchdb_watcher")


def queue_doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "abc123",
        "_rev": "1-a",
        "btq_state": "claimed",
        "type": "log_site_issue",
        "site_id": "site-1",
        "summary": "Gate latch is loose",
        "created_at": "2026-05-22T12:00:00Z",
        "created_by": "field-app",
    }
    doc.update(overrides)
    return doc


def processing_doc(doc_id: str, *, started_at: datetime, state: str = "processing") -> dict[str, Any]:
    return queue_doc(
        _id=doc_id,
        _rev="1-a",
        btq_state=state,
        **{couchdb_queue_watcher.PROCESSING_STARTED_AT_FIELD: started_at.isoformat()},
    )


def test_materialize_queue_job_writes_file_to_queue_dir(tmp_path: Path) -> None:
    path = couchdb_queue_watcher.materialize_queue_job(queue_doc(), tmp_path, logger())

    # File lives in the runtime queue dir, prefixed by job type and the
    # safe-rendered doc id. The safe rendering appends a short content
    # hash to make filenames collision-resistant under prefix truncation;
    # match on the readable portion rather than the exact name.
    assert path.parent == tmp_path / "queue"
    assert path.name.startswith("log_site_issue.abc123-")
    assert path.suffix == ".json"
    assert path.exists()


def test_materialize_queue_job_strips_couchdb_metadata_keys(tmp_path: Path) -> None:
    path = couchdb_queue_watcher.materialize_queue_job(queue_doc(), tmp_path, logger())

    payload = json.loads(path.read_text(encoding="utf-8"))

    for key in ("_id", "_rev", "btq_state", "created_at", "created_by"):
        assert key not in payload


def test_materialize_queue_job_preserves_job_payload_fields(tmp_path: Path) -> None:
    path = couchdb_queue_watcher.materialize_queue_job(queue_doc(), tmp_path, logger())

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["site_id"] == "site-1"
    assert payload["type"] == "log_site_issue"
    assert payload["summary"] == "Gate latch is loose"


def test_materialize_queue_job_raises_on_duplicate(tmp_path: Path) -> None:
    doc = queue_doc()

    couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())

    with pytest.raises(FileExistsError):
        couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())


def test_materialize_queue_job_treats_file_exists_as_ok(tmp_path: Path) -> None:
    doc = queue_doc()
    couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())

    result = couchdb_queue_watcher.process_one(doc=doc, runtime_root=tmp_path, logger=logger())

    assert result["ok"] is True
    assert result["doc_id"] == "abc123"


def test_materialize_queue_job_sanitizes_doc_id_in_filename(tmp_path: Path) -> None:
    path = couchdb_queue_watcher.materialize_queue_job(queue_doc(_id="abc/123 with spaces"), tmp_path, logger())

    assert path.name.startswith("log_site_issue.abc-123-with-spaces-")
    assert path.name.endswith(".json")
    assert "/" not in path.name
    assert " " not in path.name


def test_materialize_queue_job_avoids_prefix_collisions(tmp_path: Path) -> None:
    # Regression: two docs whose `_id`s share a 48-char readable prefix
    # used to collide on the materialized filename because `_safe_doc_id`
    # truncated before naming. The second materialize call raised
    # FileExistsError and run_listener swallowed it as
    # "queue job already materialized" -- silently acking the second doc
    # without its content ever reaching the runtime queue.
    long_prefix = "2026-05-28T13-50-48Z__accounts-glenco-locations-"
    doc_a = queue_doc(_id=f"{long_prefix}1204-central-glenwood-elementary-school")
    doc_b = queue_doc(_id=f"{long_prefix}1261-central-glenwood-high-school")

    path_a = couchdb_queue_watcher.materialize_queue_job(doc_a, tmp_path, logger())
    path_b = couchdb_queue_watcher.materialize_queue_job(doc_b, tmp_path, logger())

    assert path_a.name != path_b.name
    assert path_a.exists()
    assert path_b.exists()


def test_queue_watcher_delegates_to_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_change_worker(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(couchdb_queue_watcher, "run_change_worker", fake_run_change_worker)

    result = couchdb_queue_watcher.run(
        [
            "--runtime-root",
            str(tmp_path),
            "--log-path",
            str(tmp_path / "queue.log"),
            "--database",
            "queue-db",
            "--state-field",
            "btq_state",
            "--dry-run",
            "--once",
            "--json",
        ]
    )

    assert result == 0
    assert captured["database"] == "queue-db"
    assert captured["state_field"] == "btq_state"
    assert captured["stage"] == "queue"
    assert captured["state_processing"] == "processing"
    assert captured["state_done"] == "complete"
    assert captured["state_failed"] == "failed"
    assert captured["dry_run_updates_state"] is False
    assert captured["log_path"] == tmp_path / "queue.log"
    assert captured["default_log_path"](tmp_path) == tmp_path / "logs" / "queue_couchdb_watch.log"
    assert captured["listener_class"] is couchdb_queue_watcher.QueueCouchDBChangesListener
    assert captured["startup_task"] is None
    assert captured["periodic_task"] is None


def test_stale_sweep_does_not_reset_fresh_processing_doc() -> None:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    doc = processing_doc("fresh", started_at=now - timedelta(minutes=10))
    listener = FakeStaleSweepListener([doc])

    reset_count = couchdb_queue_watcher.sweep_stale_processing_docs(
        listener=listener,
        ttl_seconds=60 * 60,
        logger=logger(),
        now=now,
    )

    assert reset_count == 0
    assert doc["btq_state"] == "processing"
    assert listener.reset_doc_ids == []


def test_stale_sweep_resets_stale_processing_doc_to_pending() -> None:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    started_at = now - timedelta(hours=7)
    doc = processing_doc("stale", started_at=started_at)
    listener = FakeStaleSweepListener([doc])

    reset_count = couchdb_queue_watcher.sweep_stale_processing_docs(
        listener=listener,
        ttl_seconds=6 * 60 * 60,
        logger=logger(),
        now=now,
    )

    assert reset_count == 1
    assert doc["btq_state"] == "pending"
    assert couchdb_queue_watcher.PROCESSING_STARTED_AT_FIELD not in doc
    assert doc[couchdb_queue_watcher.LAST_STALE_PROCESSING_STARTED_AT_FIELD] == started_at.isoformat()
    assert doc[couchdb_queue_watcher.STALE_RESET_COUNT_FIELD] == 1
    assert listener.reset_doc_ids == ["stale"]


def test_stale_sweep_ignores_failed_and_completed_docs() -> None:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=7)
    failed = processing_doc("failed", started_at=old, state="failed")
    completed = processing_doc("complete", started_at=old, state="complete")
    listener = FakeStaleSweepListener([failed, completed])

    reset_count = couchdb_queue_watcher.sweep_stale_processing_docs(
        listener=listener,
        ttl_seconds=6 * 60 * 60,
        logger=logger(),
        now=now,
    )

    assert reset_count == 0
    assert failed["btq_state"] == "failed"
    assert completed["btq_state"] == "complete"
    assert listener.reset_doc_ids == []


def test_stale_sweep_tolerates_and_logs_reset_conflict(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    doc = processing_doc("conflict", started_at=now - timedelta(hours=7))
    listener = FakeStaleSweepListener([doc], conflict_ids={"conflict"})

    with caplog.at_level(logging.WARNING):
        reset_count = couchdb_queue_watcher.sweep_stale_processing_docs(
            listener=listener,
            ttl_seconds=6 * 60 * 60,
            logger=logger(),
            now=now,
        )

    assert reset_count == 0
    assert doc["btq_state"] == "processing"
    assert "queue stale processing reset conflict doc_id=conflict" in caplog.text


def test_run_listener_invokes_stale_sweep_on_startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    listener = FakeListener([])
    calls: list[int] = []

    def fake_sweep_stale_processing_docs(**kwargs: Any) -> int:
        calls.append(int(kwargs["ttl_seconds"]))
        return 0

    monkeypatch.setattr(couchdb_queue_watcher, "sweep_stale_processing_docs", fake_sweep_stale_processing_docs)

    couchdb_queue_watcher.run_listener(
        listener=listener,  # type: ignore[arg-type]
        runtime_root=tmp_path,
        dry_run=False,
        json_output=False,
        logger=logger(),
        stale_processing_ttl_seconds=123,
    )

    assert calls == [123]


def test_run_listener_marks_complete_on_successful_materialization(tmp_path: Path) -> None:
    listener = FakeListener([queue_doc()])

    couchdb_queue_watcher.run_listener(
        listener=listener,  # type: ignore[arg-type]
        runtime_root=tmp_path,
        dry_run=False,
        json_output=False,
        logger=logger(),
    )

    assert listener.processing == ["abc123"]
    assert listener.completed == ["abc123"]
    assert listener.failed == []


def test_run_listener_marks_failed_on_os_error(monkeypatch, tmp_path: Path) -> None:
    listener = FakeListener([queue_doc()])

    def raise_os_error(*_args: Any, **_kwargs: Any) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(couchdb_queue_watcher, "materialize_queue_job", raise_os_error)

    couchdb_queue_watcher.run_listener(
        listener=listener,  # type: ignore[arg-type]
        runtime_root=tmp_path,
        dry_run=False,
        json_output=False,
        logger=logger(),
    )

    assert listener.completed == []
    assert listener.failed == [("abc123", "disk full")]


def test_run_listener_skips_state_update_on_file_exists_error(monkeypatch, tmp_path: Path) -> None:
    listener = FakeListener([queue_doc()])

    def raise_file_exists(*_args: Any, **_kwargs: Any) -> Path:
        raise FileExistsError("already staged")

    monkeypatch.setattr(couchdb_queue_watcher, "materialize_queue_job", raise_file_exists)

    couchdb_queue_watcher.run_listener(
        listener=listener,  # type: ignore[arg-type]
        runtime_root=tmp_path,
        dry_run=False,
        json_output=False,
        logger=logger(),
    )

    assert listener.completed == ["abc123"]
    assert listener.failed == []


def test_run_exits_on_bad_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class BadConfig:
        def assert_can_authenticate(self) -> None:
            raise couchdb_config.CouchDBConfigError("bad credentials")

    listener_created = False

    def fail_if_listener_created(*_args: Any, **_kwargs: Any) -> object:
        nonlocal listener_created
        listener_created = True
        raise AssertionError("listener should not be created")

    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")
    monkeypatch.setattr(couchdb_queue_watcher, "configure_logger", lambda _path: logger())
    monkeypatch.setattr(couchdb_queue_watcher.couchdb_config, "from_env", lambda: BadConfig())
    monkeypatch.setattr(couchdb_queue_watcher, "CouchDBChangesListener", fail_if_listener_created)

    with pytest.raises(SystemExit) as excinfo:
        couchdb_queue_watcher.run(["--runtime-root", str(tmp_path), "--json"])

    assert excinfo.value.code == 2
    assert listener_created is False
