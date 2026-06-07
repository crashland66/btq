from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from queue_processor import cowork_drop_watcher as watcher


def _dirs(tmp_path: Path) -> dict[str, Path]:
    drop_root = tmp_path / "cowork_drop"
    dirs = {
        "incoming": drop_root / "incoming",
        "processed": drop_root / "processed",
        "failed": drop_root / "failed",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    return dirs


def _valid_job() -> dict:
    return {
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-06-02.md",
            "content": "Cowork drop watcher test note.",
            "destination": "journal",
        },
    }


def _valid_visit_create_job(site: str = "Summit Wire") -> dict:
    return {
        "job_type": "visit_create",
        "payload": {
            "site": site,
            "confidence": "high",
            "source": "cowork test",
            "evidence": "Arrived on site and completed walkthrough.",
        },
    }


def _logger() -> logging.Logger:
    return logging.getLogger("test_cowork_drop_watcher")


def test_cowork_drop_processes_valid_drop_through_enqueue_boundary(tmp_path: Path, monkeypatch) -> None:
    dirs = _dirs(tmp_path)
    source = dirs["incoming"] / "valid-drop.json"
    source.write_text(json.dumps(_valid_job()), encoding="utf-8")
    calls: list[dict] = []

    def fake_run(cmd: list[str], input: bytes, capture_output: bool) -> subprocess.CompletedProcess[bytes]:
        sent_job = json.loads(input.decode("utf-8"))
        calls.append({"cmd": cmd, "job": sent_job, "capture_output": capture_output})
        stdout = json.dumps(
            {
                "ok": True,
                "job_id": sent_job["job_id"],
                "couchdb": {"ok": True, "id": sent_job["job_id"], "rev": "1-test"},
            }
        ).encode("utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)

    watcher.process_file(source, dirs, "/test/python", _logger())

    assert len(calls) == 1
    assert calls[0]["cmd"] == ["/test/python", str(watcher.ENQUEUE_SCRIPT), "--created-by", "cowork"]
    assert calls[0]["capture_output"] is True
    assert calls[0]["job"]["job_id"].startswith("cw-")
    assert calls[0]["job"]["job_type"] == "append_to_note"
    assert not source.exists()
    assert (dirs["processed"] / "valid-drop.json").exists()
    ack = json.loads((dirs["processed"] / "valid-drop.ack.json").read_text(encoding="utf-8"))
    assert ack["ok"] is True
    assert ack["status"] == "enqueued"
    assert ack["job_id"] == calls[0]["job"]["job_id"]
    assert ack["couchdb"]["rev"] == "1-test"


@pytest.mark.parametrize(
    ("filename", "content", "expected_status"),
    [
        ("bad-json.json", "{not json", "invalid_json"),
        (
            "bad-spec.json",
            json.dumps({"job_type": "append_to_note", "payload": {"path": "Journal/missing-content.md", "destination": "journal"}}),
            "invalid",
        ),
    ],
)
def test_cowork_drop_rejects_invalid_payloads(
    tmp_path: Path,
    monkeypatch,
    filename: str,
    content: str,
    expected_status: str,
) -> None:
    dirs = _dirs(tmp_path)
    source = dirs["incoming"] / filename
    source.write_text(content, encoding="utf-8")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("invalid drops must not reach btq-enqueue")

    monkeypatch.setattr(watcher.subprocess, "run", fail_run)

    watcher.process_file(source, dirs, "/test/python", _logger())

    assert not source.exists()
    assert (dirs["failed"] / filename).exists()
    ack = json.loads((dirs["failed"] / f"{source.stem}.ack.json").read_text(encoding="utf-8"))
    assert ack["ok"] is False
    assert ack["status"] == expected_status
    assert ack["reasons"]


def test_cowork_drop_transient_enqueue_failure_leaves_source_for_retry(tmp_path: Path, monkeypatch) -> None:
    dirs = _dirs(tmp_path)
    source = dirs["incoming"] / "retry-me.json"
    source.write_text(json.dumps(_valid_job()), encoding="utf-8")

    def fake_run(cmd: list[str], input: bytes, capture_output: bool) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=b"",
            stderr=b"btq-enqueue: cannot reach CouchDB at http://127.0.0.1:5984/btq_queue: refused",
        )

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)

    watcher.process_file(source, dirs, "/test/python", _logger())

    assert source.exists()
    assert not any(dirs["processed"].iterdir())
    assert not any(dirs["failed"].iterdir())


def test_cowork_drop_permanent_enqueue_failure_quarantines_and_acknowledges(tmp_path: Path, monkeypatch) -> None:
    dirs = _dirs(tmp_path)
    source = dirs["incoming"] / "enqueue-error.json"
    source.write_text(json.dumps(_valid_job()), encoding="utf-8")

    def fake_run(cmd: list[str], input: bytes, capture_output: bool) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 2, stdout=b"", stderr=b"btq-enqueue: permanent validation failure")

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)

    watcher.process_file(source, dirs, "/test/python", _logger())

    assert not source.exists()
    assert (dirs["failed"] / "enqueue-error.json").exists()
    ack = json.loads((dirs["failed"] / "enqueue-error.ack.json").read_text(encoding="utf-8"))
    assert ack["ok"] is False
    assert ack["status"] == "enqueue_error"
    assert ack["job_id"].startswith("cw-")
    assert ack["reasons"] == ["btq-enqueue: permanent validation failure"]


def test_cowork_drop_duplicate_enqueue_is_acknowledged_as_idempotent(tmp_path: Path, monkeypatch) -> None:
    dirs = _dirs(tmp_path)
    source = dirs["incoming"] / "already-enqueued.json"
    source.write_text(json.dumps(_valid_job()), encoding="utf-8")

    def fake_run(cmd: list[str], input: bytes, capture_output: bool) -> subprocess.CompletedProcess[bytes]:
        sent_job = json.loads(input.decode("utf-8"))
        stdout = json.dumps(
            {
                "ok": True,
                "job_id": sent_job["job_id"],
                "couchdb": {"ok": True, "duplicate": True, "id": sent_job["job_id"], "status": 409},
            }
        ).encode("utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)

    watcher.process_file(source, dirs, "/test/python", _logger())

    assert not source.exists()
    assert (dirs["processed"] / "already-enqueued.json").exists()
    ack = json.loads((dirs["processed"] / "already-enqueued.ack.json").read_text(encoding="utf-8"))
    assert ack["ok"] is True
    assert ack["status"] == "duplicate"
    assert ack["couchdb"]["status"] == 409


def test_visit_create_unregistered_site_acks_failed(tmp_path: Path, monkeypatch) -> None:
    dirs = _dirs(tmp_path)
    source = dirs["incoming"] / "unknown-site.json"
    source.write_text(json.dumps(_valid_visit_create_job("Totally Unknown Site")), encoding="utf-8")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("unregistered site drops must not reach btq-enqueue")

    monkeypatch.setattr(watcher.subprocess, "run", fail_run)
    monkeypatch.setattr(watcher.sites, "is_site_registered", lambda _site: False)

    watcher.process_file(source, dirs, "/test/python", _logger())

    assert not source.exists()
    assert (dirs["failed"] / "unknown-site.json").exists()
    assert not any(dirs["processed"].iterdir())
    ack = json.loads((dirs["failed"] / "unknown-site.ack.json").read_text(encoding="utf-8"))
    assert ack["ok"] is False
    assert ack["status"] == "unregistered_site"
    assert ack["job_type"] == "visit_create"
    assert ack["reasons"] == ["site not registered: Totally Unknown Site"]


def test_visit_create_registered_site_enqueues(tmp_path: Path, monkeypatch) -> None:
    dirs = _dirs(tmp_path)
    source = dirs["incoming"] / "registered-site.json"
    source.write_text(json.dumps(_valid_visit_create_job("Summit Wire")), encoding="utf-8")
    checked_sites: list[str] = []

    def fake_run(cmd: list[str], input: bytes, capture_output: bool) -> subprocess.CompletedProcess[bytes]:
        sent_job = json.loads(input.decode("utf-8"))
        stdout = json.dumps(
            {
                "ok": True,
                "job_id": sent_job["job_id"],
                "couchdb": {"ok": True, "id": sent_job["job_id"], "rev": "1-test"},
            }
        ).encode("utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=b"")

    def fake_is_site_registered(site: str) -> bool:
        checked_sites.append(site)
        return True

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    monkeypatch.setattr(watcher.sites, "is_site_registered", fake_is_site_registered)

    watcher.process_file(source, dirs, "/test/python", _logger())

    assert checked_sites == ["Summit Wire"]
    assert not source.exists()
    assert (dirs["processed"] / "registered-site.json").exists()
    ack = json.loads((dirs["processed"] / "registered-site.ack.json").read_text(encoding="utf-8"))
    assert ack["ok"] is True
    assert ack["status"] == "enqueued"


def test_non_site_keyed_job_skips_routability_check(tmp_path: Path, monkeypatch) -> None:
    dirs = _dirs(tmp_path)
    source = dirs["incoming"] / "journal-note.json"
    source.write_text(json.dumps(_valid_job()), encoding="utf-8")

    def fake_run(cmd: list[str], input: bytes, capture_output: bool) -> subprocess.CompletedProcess[bytes]:
        sent_job = json.loads(input.decode("utf-8"))
        stdout = json.dumps(
            {
                "ok": True,
                "job_id": sent_job["job_id"],
                "couchdb": {"ok": True, "id": sent_job["job_id"], "rev": "1-test"},
            }
        ).encode("utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=b"")

    def fail_site_lookup(_site: str) -> bool:
        raise AssertionError("non-site-keyed jobs must not check site routability")

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    monkeypatch.setattr(watcher.sites, "is_site_registered", fail_site_lookup)

    watcher.process_file(source, dirs, "/test/python", _logger())

    assert not source.exists()
    ack = json.loads((dirs["processed"] / "journal-note.ack.json").read_text(encoding="utf-8"))
    assert ack["ok"] is True
    assert ack["status"] == "enqueued"


def test_cowork_drop_run_ignores_acknowledgement_files(tmp_path: Path, monkeypatch) -> None:
    drop_root = tmp_path / "cowork_drop"
    incoming = drop_root / "incoming"
    incoming.mkdir(parents=True)
    ack_path = incoming / "already-processed.ack.json"
    ack_path.write_text(json.dumps({"ok": True, "status": "enqueued"}), encoding="utf-8")
    old_time = time.time() - watcher.STABLE_SECONDS - 10
    os.utime(ack_path, (old_time, old_time))
    processed: list[Path] = []

    def fake_process_file(path: Path, *_args, **_kwargs) -> None:
        processed.append(path)

    monkeypatch.setattr(watcher, "process_file", fake_process_file)
    monkeypatch.setattr(watcher, "get_config", lambda: SimpleNamespace(pipeline_dir=tmp_path / "unused"))
    monkeypatch.setattr(watcher, "_should_stop", False)

    result = watcher.run(0, once=True, logger=_logger(), drop_root_override=drop_root)

    assert result == 0
    assert processed == []
    assert ack_path.exists()
