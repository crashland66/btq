from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ops_dashboard import common
from queue_processor import main as queue_main


REPO_ROOT = Path(__file__).resolve().parents[1]
RETIRE_SCRIPT = REPO_ROOT / "scripts" / "btq-retire-dell-replicators"


@pytest.fixture(autouse=True)
def clear_media_inventory_cache() -> None:
    common.reset_field_capture_media_inventory_cache()
    yield
    common.reset_field_capture_media_inventory_cache()


def test_partial_inventory_query_returns_evidence_but_marks_it_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: list[object] = [
        {
            "docs": [{"_id": "capture-one", "photos": [{"upload_id": "one.jpg"}]}],
            "bookmark": "next-page",
        },
        RuntimeError("canonical inventory interrupted"),
    ]

    monkeypatch.setattr(common, "_field_capture_couchdb_config", lambda: object())

    def query(_config: object, _database: str, mango: dict[str, object]) -> dict[str, object]:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert mango.get("bookmark") in (None, "next-page")
        return response

    monkeypatch.setattr(common, "query_couchdb_find", query)

    docs, available = common._query_field_capture_docs_all_result(
        ["_id", "photos", "audio"], limit=1
    )

    assert docs == [{"_id": "capture-one", "photos": [{"upload_id": "one.jpg"}]}]
    assert available is False
    assert responses == []


def test_inventory_page_safety_bound_does_not_claim_partial_results_are_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    monkeypatch.setattr(common, "_field_capture_couchdb_config", lambda: object())

    def query(_config: object, _database: str, _mango: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "docs": [{"_id": f"capture-{calls}"}],
            "bookmark": f"page-{calls + 1}",
        }

    monkeypatch.setattr(common, "query_couchdb_find", query)

    docs, available = common._query_field_capture_docs_all_result(["_id"], limit=1)

    assert len(docs) == 400
    assert calls == 400
    assert available is False


def test_inventory_unavailability_is_ttl_cached_and_reset_forces_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    replies = [
        ([], False),
        ([{"_id": "capture-two", "audio": [{"upload_id": "voice.webm"}]}], True),
        ([{"_id": "capture-three", "photos": [{"upload_id": "three.jpg"}]}], True),
    ]
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(common.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(common, "_FIELD_CAPTURE_MEDIA_INVENTORY_CACHE_TTL_SECONDS", 10.0)

    def inventory_query(fields: list[str], limit: int = 5000) -> tuple[list[dict[str, object]], bool]:
        calls.append(tuple(fields))
        return replies.pop(0)

    monkeypatch.setattr(common, "_query_field_capture_docs_all_result", inventory_query)

    unavailable = common.field_capture_media_inventory()
    assert unavailable == {
        "available": False,
        "total_count": 0,
        "image_count": 0,
        "audio_count": 0,
        "capture_count": 0,
    }

    now[0] = 109.999
    assert common.field_capture_media_inventory() is unavailable
    assert len(calls) == 1

    now[0] = 110.0
    refreshed = common.field_capture_media_inventory()
    assert refreshed["available"] is True
    assert refreshed["audio_count"] == 1
    assert len(calls) == 2

    common.reset_field_capture_media_inventory_cache()
    reset_result = common.field_capture_media_inventory()
    assert reset_result["image_count"] == 1
    assert len(calls) == 3


def _queue_fixture(tmp_path: Path) -> tuple[Path, Path, Path, queue_main.RunContext]:
    runtime = (tmp_path / "runtime").resolve()
    queue_dir = runtime / "queue"
    processed_dir = runtime / "processed"
    failed_dir = runtime / "failed"
    log_path = runtime / "logs" / "queue-processor.log"
    for directory in (queue_dir, processed_dir, failed_dir, log_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    context = queue_main.RunContext(
        project_root=(tmp_path / "project").resolve(),
        runtime_root=runtime,
        log_path=log_path,
        dry_run=False,
    )
    return queue_dir, processed_dir, failed_dir, context


def _isolate_queue_decisions(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    job = queue_main.QueueJob(
        job_id="verifier-job",
        job_type="verifier_job_type",
        payload={},
        metadata={},
        intent={},
    )
    monkeypatch.setattr(queue_main, "load_job", lambda _path: job)
    monkeypatch.setattr(queue_main, "processed_job_id_exists", lambda *_args: (False, "none"))
    monkeypatch.setattr(queue_main, "_job_has_applied_marker", lambda *_args: (False, None))
    monkeypatch.setattr(queue_main, "append_processed_index_record", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(queue_main, "target_path_hint", lambda *_args: "verifier-target")
    monkeypatch.setitem(queue_main.JOB_HANDLERS, job.job_type, handler)


def test_identical_processed_archive_skips_without_handler_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_dir, processed_dir, failed_dir, context = _queue_fixture(tmp_path)
    queue_file = queue_dir / "same-name.json"
    archive_file = processed_dir / queue_file.name
    content = b'{"revision":"identical"}\n'
    queue_file.write_bytes(content)
    archive_file.write_bytes(content)
    handler_calls: list[Path] = []

    def handler(path: Path, *_args: object) -> None:
        handler_calls.append(path)
        raise AssertionError("identical replay must not execute its handler")

    _isolate_queue_decisions(monkeypatch, handler)

    queue_main.process_job(queue_file, context, processed_dir, failed_dir)

    assert handler_calls == []
    assert archive_file.read_bytes() == content
    assert not queue_file.exists()
    assert list(failed_dir.iterdir()) == []


def test_changed_same_filename_uses_content_digest_and_preserves_old_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_dir, processed_dir, failed_dir, context = _queue_fixture(tmp_path)
    queue_file = queue_dir / "same-name.json"
    old_content = b'{"revision":"old"}\n'
    new_content = b'{"revision":"new"}\n'
    queue_file.write_bytes(new_content)
    (processed_dir / queue_file.name).write_bytes(old_content)
    digest = hashlib.sha256(new_content).hexdigest()[:12]
    expected_name = f"same-name.{digest}.json"
    handler_calls: list[Path] = []

    def handler(path: Path, _job: object, _context: object, destination: Path) -> None:
        handler_calls.append(path)
        queue_main.move_job_file(path, destination)

    _isolate_queue_decisions(monkeypatch, handler)

    queue_main.process_job(queue_file, context, processed_dir, failed_dir)

    assert [path.name for path in handler_calls] == [expected_name]
    assert (processed_dir / "same-name.json").read_bytes() == old_content
    assert (processed_dir / expected_name).read_bytes() == new_content
    assert list(queue_dir.iterdir()) == []
    assert list(failed_dir.iterdir()) == []


def test_conflicting_digest_destination_fails_closed_before_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_dir, processed_dir, failed_dir, context = _queue_fixture(tmp_path)
    queue_file = queue_dir / "same-name.json"
    old_content = b'{"revision":"old"}\n'
    new_content = b'{"revision":"new"}\n'
    queue_file.write_bytes(new_content)
    (processed_dir / queue_file.name).write_bytes(old_content)
    digest = hashlib.sha256(new_content).hexdigest()[:12]
    conflicting_revision = queue_dir / f"same-name.{digest}.json"
    conflicting_revision.write_bytes(b"unrelated existing bytes\n")
    handler_calls: list[Path] = []

    def handler(path: Path, *_args: object) -> None:
        handler_calls.append(path)

    _isolate_queue_decisions(monkeypatch, handler)

    queue_main.process_job(queue_file, context, processed_dir, failed_dir)

    assert handler_calls == []
    assert (processed_dir / queue_file.name).read_bytes() == old_content
    assert conflicting_revision.read_bytes() == b"unrelated existing bytes\n"
    assert (failed_dir / queue_file.name).read_bytes() == new_content
    assert not (processed_dir / conflicting_revision.name).exists()


def test_replicator_retirement_defaults_to_dry_run_and_apply_is_narrow_and_secret_safe(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, str, str | None]] = []
    rows = [
        {"doc": {"_id": "dell_to_pro_main", "_rev": "1-a"}},
        {"doc": {"_id": "pro_to_dell_events", "_rev": "2-b"}},
        {"doc": {"_id": "production_to_archive", "_rev": "3-c"}},
        {"doc": {"_id": "dell_to_pro_missing_rev"}},
        {"doc": {"_id": "unrelated", "_rev": "4-d"}},
    ]

    class FakeCouchHandler(BaseHTTPRequestHandler):
        def _write_json(self, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            requests.append(("GET", self.path, self.headers.get("Authorization")))
            self._write_json({"rows": rows})

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
            requests.append(("DELETE", self.path, self.headers.get("Authorization")))
            self._write_json({"ok": True})

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCouchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake_user = "verifier-user"
    fake_password = "verifier-password"
    expected_auth = "Basic " + base64.b64encode(
        f"{fake_user}:{fake_password}".encode("utf-8")
    ).decode("ascii")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "BTQ_COUCHDB_URL": f"http://127.0.0.1:{server.server_port}",
        "BTQ_COUCHDB_USER": fake_user,
        "BTQ_COUCHDB_PASSWORD": fake_password,
    }

    try:
        dry_run = subprocess.run(
            [str(RETIRE_SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        dry_requests = list(requests)
        requests.clear()
        applied = subprocess.run(
            [str(RETIRE_SCRIPT), "--apply"],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        apply_requests = list(requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert dry_run.returncode == 0, dry_run.stderr
    assert dry_requests == [
        ("GET", "/_replicator/_all_docs?include_docs=true", expected_auth)
    ]
    assert dry_run.stdout.splitlines() == [
        "dell_to_pro_main: would retire",
        "pro_to_dell_events: would retire",
        "mode=dry-run matched=2 errors=0",
    ]

    assert applied.returncode == 0, applied.stderr
    assert apply_requests == [
        ("GET", "/_replicator/_all_docs?include_docs=true", expected_auth),
        ("DELETE", "/_replicator/dell_to_pro_main?rev=1-a", expected_auth),
        ("DELETE", "/_replicator/pro_to_dell_events?rev=2-b", expected_auth),
    ]
    assert applied.stdout.splitlines() == [
        "dell_to_pro_main: retired",
        "pro_to_dell_events: retired",
        "mode=applied matched=2 errors=0",
    ]

    combined_output = "\n".join(
        (dry_run.stdout, dry_run.stderr, applied.stdout, applied.stderr)
    )
    assert fake_user not in combined_output
    assert fake_password not in combined_output
    assert expected_auth not in combined_output
    assert "production_to_archive" not in combined_output
    assert "unrelated" not in combined_output
