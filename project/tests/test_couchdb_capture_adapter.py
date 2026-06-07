from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from event_pipeline.couchdb_capture_adapter import CaptureAdapterError, build_intake_job, import_couchdb_capture, run_command
from queue_spec import validate_job


REMOTE_PATH = "/srv/btq/runtime/uploads/cap-2026-05-09-abc123/photo_001.jpg"


class FakeRegistry:
    def __init__(self, canonical: str | None = "Summit Wire") -> None:
        self.canonical = canonical
        self.site_ids: list[str] = []

    def resolve_canonical(self, site_id: str) -> str | None:
        self.site_ids.append(site_id)
        return self.canonical


def sample_doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "cap-2026-05-09-abc123",
        "_rev": "2-claimed",
        "type": "field_capture",
        "capture_id": "cap-2026-05-09-abc123",
        "site_id": "7050",
        "person_id": "person-1",
        "person_name": "Person One",
        "field_capture_token_id": "fct_test123",
        "field_capture_token_label": "Summit porter",
        "captured_at": "2026-05-09T14:30:00Z",
        "qc_category": "completed_area",
        "note": "Lobby complete. Floor mopped.",
        "photos": [
            {
                "filename": "photo_001.jpg",
                "mime_type": "image/jpeg",
                "stored_path": REMOTE_PATH,
            }
        ],
        "processing_state": "claimed",
    }
    doc.update(overrides)
    return doc


class CopyingRunner:
    def __init__(self, remote_sizes: dict[str, int] | None = None, fail_scp: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.remote_sizes = remote_sizes if remote_sizes is not None else {REMOTE_PATH: 5}
        self.fail_scp = fail_scp

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        if args[0] == "ssh":
            remote_path = args[-1]
            size = self.remote_sizes.get(remote_path, 5)
            return subprocess.CompletedProcess(args, 0, stdout=f"{size}\n", stderr="")
        if args[0] == "scp":
            if self.fail_scp:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="scp exploded")
            local_path = Path(args[-1])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"photo")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)


def read_intake(runtime_root: Path, capture_id: str) -> dict[str, Any]:
    path = runtime_root / "field_capture" / "intake" / f"{capture_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def build_sample_job(doc: dict[str, Any]) -> dict[str, object]:
    return build_intake_job(
        doc=doc,
        site="Summit Wire",
        captured_at="2026-05-09T14:30:00Z",
        exported_at="2026-05-09T14:31:00Z",
        local_photos=[
            {
                "filename": "photo_001.jpg",
                "mime_type": "image/jpeg",
                "stored_path": "/tmp/photo_001.jpg",
            }
        ],
        local_audio=[],
    )


def test_build_intake_job_propagates_site_id() -> None:
    job = build_sample_job(sample_doc())

    assert job["metadata"]["site_id"] == "7050"


def test_build_intake_job_propagates_person_id_and_name() -> None:
    job = build_sample_job(sample_doc())

    assert job["metadata"]["person_id"] == "person-1"
    assert job["metadata"]["person_name"] == "Person One"


def test_build_intake_job_propagates_token_id_and_label() -> None:
    job = build_sample_job(sample_doc())

    assert job["metadata"]["field_capture_token_id"] == "fct_test123"
    assert job["metadata"]["field_capture_token_label"] == "Summit porter"


def test_build_intake_job_omits_missing_identity_fields() -> None:
    doc = sample_doc(
        site_id="",
        person_name="",
        field_capture_token_id="",
        field_capture_token_label="",
    )

    job = build_sample_job(doc)

    assert job["metadata"] == {
        "capture_id": "cap-2026-05-09-abc123",
        "source": "couchdb",
        "person_id": "person-1",
    }


def test_build_intake_job_empty_string_identity_is_treated_as_missing() -> None:
    job = build_sample_job(sample_doc(person_name=""))

    assert "person_name" not in job["metadata"]


def test_import_couchdb_capture_copies_file_and_writes_valid_intake(tmp_path: Path) -> None:
    runner = CopyingRunner()

    result = import_couchdb_capture(
        doc=sample_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=runner,
    )

    local_photo = tmp_path / "uploads" / "2026-05-09" / "cap-2026-05-09-abc123" / "photo_001.jpg"
    intake = read_intake(tmp_path, "cap-2026-05-09-abc123")

    assert result["ok"] is True
    assert result["counts"] == {"copied": 2, "skipped": 0, "failed": 0, "would_copy": 0}
    assert local_photo.read_bytes() == b"photo"
    assert runner.commands[0] == ["scp", f"vps.example:{REMOTE_PATH}", str(local_photo)]
    assert validate_job(intake)
    assert intake["payload"]["photos"][0]["stored_path"] == str(local_photo)


def test_written_intake_json_passes_validate_job(tmp_path: Path) -> None:
    import_couchdb_capture(
        doc=sample_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=CopyingRunner(),
    )

    assert validate_job(read_intake(tmp_path, "cap-2026-05-09-abc123"))


def test_site_uses_resolved_canonical_name(tmp_path: Path) -> None:
    registry = FakeRegistry("Summit Wire")

    import_couchdb_capture(
        doc=sample_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=registry,
        runner=CopyingRunner(),
    )

    intake = read_intake(tmp_path, "cap-2026-05-09-abc123")
    assert registry.site_ids == ["7050"]
    assert intake["payload"]["site"] == "Summit Wire"


def test_site_falls_back_to_site_id_when_registry_returns_none(tmp_path: Path) -> None:
    import_couchdb_capture(
        doc=sample_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(None),
        runner=CopyingRunner(),
    )

    intake = read_intake(tmp_path, "cap-2026-05-09-abc123")
    assert intake["payload"]["site"] == "7050"


def test_second_identical_import_skips_file_and_intake_json(tmp_path: Path) -> None:
    first_runner = CopyingRunner()
    second_runner = CopyingRunner()

    import_couchdb_capture(
        doc=sample_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=first_runner,
    )
    result = import_couchdb_capture(
        doc=sample_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=second_runner,
    )

    assert result["counts"] == {"copied": 0, "skipped": 2, "failed": 0, "would_copy": 0}
    assert second_runner.commands == []


def test_import_with_existing_file_and_missing_intake_checks_remote_size(tmp_path: Path) -> None:
    import_couchdb_capture(
        doc=sample_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=CopyingRunner(),
    )
    intake = tmp_path / "field_capture" / "intake" / "cap-2026-05-09-abc123.json"
    intake.unlink()
    local_photo = tmp_path / "uploads" / "2026-05-09" / "cap-2026-05-09-abc123" / "photo_001.jpg"
    local_photo.write_bytes(b"different-size")

    with pytest.raises(CaptureAdapterError):
        import_couchdb_capture(
            doc=sample_doc(),
            runtime_root=tmp_path,
            remote_host="vps.example",
            registry=FakeRegistry(),
            runner=CopyingRunner(remote_sizes={REMOTE_PATH: 5}),
        )


def test_scp_failure_raises_capture_adapter_error(tmp_path: Path) -> None:
    with pytest.raises(CaptureAdapterError, match="scp exploded"):
        import_couchdb_capture(
            doc=sample_doc(),
            runtime_root=tmp_path,
            remote_host="vps.example",
            registry=FakeRegistry(),
            runner=CopyingRunner(fail_scp=True),
        )


def test_dry_run_reports_would_copy_without_writes_or_scp(tmp_path: Path) -> None:
    runner = CopyingRunner()

    result = import_couchdb_capture(
        doc=sample_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=runner,
        dry_run=True,
    )

    assert result["counts"] == {"copied": 0, "skipped": 0, "failed": 0, "would_copy": 2}
    assert runner.commands == []
    assert not (tmp_path / "uploads").exists()
    assert not (tmp_path / "field_capture").exists()


def test_missing_captured_at_uses_today_utc_for_upload_directory(tmp_path: Path) -> None:
    doc = sample_doc(captured_at="")
    today = datetime.now(timezone.utc).date().isoformat()

    result = import_couchdb_capture(
        doc=doc,
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=CopyingRunner(),
    )

    assert result["upload_destination_dir"] == str(tmp_path / "uploads" / today / "cap-2026-05-09-abc123")


def test_exported_at_is_iso_timestamp_distinct_from_captured_at(tmp_path: Path) -> None:
    import_couchdb_capture(
        doc=sample_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=CopyingRunner(),
    )

    intake = read_intake(tmp_path, "cap-2026-05-09-abc123")
    exported_at = intake["payload"]["exported_at"]

    assert exported_at != intake["payload"]["captured_at"]
    parsed = datetime.fromisoformat(exported_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_run_command_returns_timeout_completed_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["ssh", "vps.example", "stat"], timeout=1)

    monkeypatch.setattr("event_pipeline.couchdb_capture_adapter.subprocess.run", fake_run)
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_REMOTE_COMMAND_TIMEOUT_SECONDS", "1")

    result = run_command(["ssh", "vps.example", "stat"])

    assert result.returncode == 124
    assert "timed out" in result.stderr
