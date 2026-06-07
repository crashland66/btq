from __future__ import annotations

import json
from pathlib import Path

import pytest

import btq
from field_capture import pull_bundle
from queue_processor import main as queue_processor_main


CAPTURE_ID = "cap-photo-2026-05-03T18-25-20-04-00"


def write_bundle(root: Path, *, capture_id: str = CAPTURE_ID, missing_media: bool = False) -> tuple[Path, dict[str, object]]:
    upload_dir = root / "uploads" / "2026-05-03" / capture_id
    queue_dir = root / "queue"
    upload_dir.mkdir(parents=True)
    queue_dir.mkdir(parents=True)
    image_path = upload_dir / "img-0933-png-2026-05-03T18-25-00-04-00.jpg"
    audio_path = upload_dir / "voice-note-2026-05-03T18-25-16-04-00.webm"
    image_path.write_bytes(b"image bytes")
    if not missing_media:
        audio_path.write_bytes(b"audio bytes")
    job = {
        "job_id": "2026-05-03T18-25-20-04-00__photo-capture-summit-wire",
        "job_type": "photo_capture",
        "metadata": {
            "capture_id": capture_id,
            "site_id": "7050",
            "source": "field_capture_app",
            "person_id": "per_alice",
            "person_name": "Alice Example",
            "field_capture_token_label": "Alice phone",
        },
        "payload": {
            "site": "Summit Wire",
            "qc_category": "Other",
            "note": "Test note",
            "captured_at": "2026-05-03T18:25:20-04:00",
            "exported_at": "2026-05-03T18:25:20-04:00",
            "photos": [
                {
                    "filename": image_path.name,
                    "mime_type": "image/jpeg",
                    "stored_path": f"/srv/btq/runtime/uploads/2026-05-03/{capture_id}/{image_path.name}",
                    "upload_id": f"2026-05-03/{capture_id}/{image_path.name}",
                }
            ],
            "audio": [
                {
                    "media_type": "audio",
                    "filename": audio_path.name,
                    "mime_type": "audio/webm",
                    "stored_path": f"/srv/btq/runtime/uploads/2026-05-03/{capture_id}/{audio_path.name}",
                    "upload_id": f"2026-05-03/{capture_id}/{audio_path.name}",
                    "size_bytes": 11,
                    "duration_seconds": "13",
                }
            ],
        },
    }
    (queue_dir / "field-capture-job.json").write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return upload_dir, job


def test_import_field_capture_bundle_copies_media_and_rewrites_queue_paths(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    runtime = tmp_path / "runtime"
    write_bundle(bundle)

    report = pull_bundle.import_field_capture_bundle(capture_id=CAPTURE_ID, bundle_path=bundle, runtime_root=runtime)

    assert report["ok"] is True
    assert report["counts"] == {"copied": 3, "skipped": 0, "failed": 0, "would_copy": 0}
    assert report["intake_destination_path"] == str(runtime / "field_capture" / "intake" / "field-capture-job.json")
    assert "queue_destination_path" not in report
    local_upload_dir = runtime / "uploads" / "2026-05-03" / CAPTURE_ID
    assert (local_upload_dir / "img-0933-png-2026-05-03T18-25-00-04-00.jpg").read_bytes() == b"image bytes"
    assert (local_upload_dir / "voice-note-2026-05-03T18-25-16-04-00.webm").read_bytes() == b"audio bytes"
    intake_payload = json.loads((runtime / "field_capture" / "intake" / "field-capture-job.json").read_text(encoding="utf-8"))
    assert not (runtime / "queue").exists()
    assert intake_payload["metadata"]["capture_id"] == CAPTURE_ID
    assert intake_payload["metadata"]["person_id"] == "per_alice"
    assert intake_payload["metadata"]["person_name"] == "Alice Example"
    assert intake_payload["metadata"]["field_capture_token_label"] == "Alice phone"
    assert intake_payload["payload"]["photos"][0]["stored_path"] == str(local_upload_dir / "img-0933-png-2026-05-03T18-25-00-04-00.jpg")
    assert intake_payload["payload"]["audio"][0]["stored_path"] == str(local_upload_dir / "voice-note-2026-05-03T18-25-16-04-00.webm")
    assert not (runtime / "field_capture" / "audio_transcripts").exists()
    assert not (runtime / "field_capture" / "audio_semantics").exists()
    assert not (runtime / "processed").exists()
    assert not (runtime / "failed").exists()


def test_import_field_capture_bundle_is_idempotent(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    runtime = tmp_path / "runtime"
    write_bundle(bundle)
    pull_bundle.import_field_capture_bundle(capture_id=CAPTURE_ID, bundle_path=bundle, runtime_root=runtime)

    report = pull_bundle.import_field_capture_bundle(capture_id=CAPTURE_ID, bundle_path=bundle, runtime_root=runtime)

    assert report["ok"] is True
    assert report["counts"] == {"copied": 0, "skipped": 3, "failed": 0, "would_copy": 0}


def test_import_field_capture_bundle_dry_run_reports_without_writing(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    runtime = tmp_path / "runtime"
    write_bundle(bundle)

    report = pull_bundle.import_field_capture_bundle(capture_id=CAPTURE_ID, bundle_path=bundle, runtime_root=runtime, dry_run=True)

    assert report["ok"] is True
    assert report["counts"] == {"copied": 0, "skipped": 0, "failed": 0, "would_copy": 3}
    assert report["intake_destination_path"] == str(runtime / "field_capture" / "intake" / "field-capture-job.json")
    assert report["results"][-1]["type"] == "intake_json"
    assert report["results"][-1]["destination"] == str(runtime / "field_capture" / "intake" / "field-capture-job.json")
    assert not (runtime / "uploads").exists()
    assert not (runtime / "queue").exists()
    assert not (runtime / "field_capture" / "intake").exists()


def test_import_field_capture_bundle_fails_closed_for_capture_id_mismatch(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    runtime = tmp_path / "runtime"
    write_bundle(bundle, capture_id="cap-photo-2026-05-03T18-25-20-04-00")

    with pytest.raises(pull_bundle.PullBundleError, match="missing upload directory"):
        pull_bundle.import_field_capture_bundle(capture_id="cap-photo-2026-05-03T18-99-99-04-00", bundle_path=bundle, runtime_root=runtime)

    assert not (runtime / "uploads").exists()
    assert not (runtime / "queue").exists()
    assert not (runtime / "field_capture" / "intake").exists()


def test_import_field_capture_bundle_requires_matching_queue_json_capture_id(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    runtime = tmp_path / "runtime"
    write_bundle(bundle)
    queue_path = bundle / "queue" / "field-capture-job.json"
    queue_payload = json.loads(queue_path.read_text(encoding="utf-8"))
    queue_payload["metadata"]["capture_id"] = "cap-photo-2026-05-03T18-00-00-04-00"
    queue_path.write_text(json.dumps(queue_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(pull_bundle.PullBundleError, match="missing matching photo_capture queue JSON"):
        pull_bundle.import_field_capture_bundle(capture_id=CAPTURE_ID, bundle_path=bundle, runtime_root=runtime)

    assert not (runtime / "uploads").exists()
    assert not (runtime / "queue").exists()
    assert not (runtime / "field_capture" / "intake").exists()


def test_import_field_capture_bundle_fails_when_referenced_media_is_missing(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    runtime = tmp_path / "runtime"
    write_bundle(bundle, missing_media=True)

    with pytest.raises(pull_bundle.PullBundleError, match="referenced media file is missing"):
        pull_bundle.import_field_capture_bundle(capture_id=CAPTURE_ID, bundle_path=bundle, runtime_root=runtime)

    assert not (runtime / "uploads").exists()
    assert not (runtime / "queue").exists()


def test_import_field_capture_bundle_fails_closed_for_non_identical_existing_file(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    runtime = tmp_path / "runtime"
    write_bundle(bundle)
    local_upload_dir = runtime / "uploads" / "2026-05-03" / CAPTURE_ID
    local_upload_dir.mkdir(parents=True)
    existing = local_upload_dir / "voice-note-2026-05-03T18-25-16-04-00.webm"
    existing.write_bytes(b"different audio")

    report = pull_bundle.import_field_capture_bundle(capture_id=CAPTURE_ID, bundle_path=bundle, runtime_root=runtime)

    assert report["ok"] is False
    assert report["counts"]["failed"] == 1
    assert "different content" in report["results"][0]["error"]
    assert existing.read_bytes() == b"different audio"
    assert not (runtime / "queue").exists()
    assert not (runtime / "field_capture" / "intake").exists()


def test_pull_field_capture_cli_json_dry_run_without_queue_processor_or_vault_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    runtime = tmp_path / "runtime"
    vault = tmp_path / "vault"
    vault.mkdir()
    sentinel = vault / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    write_bundle(bundle)
    queue_processor_called = {"called": False}

    def fail_if_queue_processor_runs(*_args: object, **_kwargs: object) -> None:
        queue_processor_called["called"] = True
        raise AssertionError("queue processor must not run during field-capture bundle pull")

    monkeypatch.setattr(queue_processor_main, "process_all", fail_if_queue_processor_runs)

    exit_code = btq.run(
        [
            "pull-field-capture",
            "--capture-id",
            CAPTURE_ID,
            "--bundle-path",
            str(bundle),
            "--runtime-root",
            str(runtime),
            "--dry-run",
            "--json",
        ]
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["counts"] == {"copied": 0, "skipped": 0, "failed": 0, "would_copy": 3}
    assert not (runtime / "uploads").exists()
    assert not (runtime / "queue").exists()
    assert not (runtime / "field_capture" / "intake").exists()
    assert not (runtime / "processed").exists()
    assert not (runtime / "failed").exists()
    assert queue_processor_called["called"] is False
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_imported_bundle_is_discoverable_from_field_capture_intake_without_runtime_queue(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    runtime = tmp_path / "runtime"
    write_bundle(bundle)

    pull_bundle.import_field_capture_bundle(capture_id=CAPTURE_ID, bundle_path=bundle, runtime_root=runtime)

    from field_capture import audio_transcription

    assets = audio_transcription.discover_audio_assets(runtime / "field_capture" / "intake", runtime / "uploads")

    assert len(assets) == 1
    assert assets[0].upload_id == CAPTURE_ID
    assert assets[0].audio_filename == "voice-note-2026-05-03T18-25-16-04-00.webm"
    assert assets[0].audio_path == (runtime / "uploads" / "2026-05-03" / CAPTURE_ID / "voice-note-2026-05-03T18-25-16-04-00.webm").resolve()
    assert not (runtime / "queue").exists()


def test_imported_bundle_can_be_transcribed_from_field_capture_intake(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    runtime = tmp_path / "runtime"
    transcript_dir = runtime / "field_capture" / "audio_transcripts"
    write_bundle(bundle)
    pull_bundle.import_field_capture_bundle(capture_id=CAPTURE_ID, bundle_path=bundle, runtime_root=runtime)

    from field_capture import audio_transcription

    calls: list[Path] = []

    def transcribe(path: Path) -> str:
        calls.append(path)
        return "Imported field note."

    counts = audio_transcription.process_audio_assets(
        runtime / "field_capture" / "intake",
        runtime / "uploads",
        transcript_dir,
        transcribe,
    )

    assert counts == {"pending": 1, "transcribed": 1, "failed": 0, "skipped": 0}
    assert calls == [(runtime / "uploads" / "2026-05-03" / CAPTURE_ID / "voice-note-2026-05-03T18-25-16-04-00.webm").resolve()]
    assert len(sorted(transcript_dir.glob("*.json"))) == 1
    assert not (runtime / "queue").exists()
    assert not (runtime / "failed").exists()
