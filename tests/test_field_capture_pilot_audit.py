from __future__ import annotations

import json
from pathlib import Path

import btq
from field_capture import pilot_audit, photo_vision


def write_capture(
    runtime_root: Path,
    *,
    capture_id: str,
    site_id: str = "7050",
    captured_at: str = "2026-05-05T18:00:00-04:00",
    area: str = "Restrooms",
    phase: str = "completed",
    submitter: str = "Jordan",
    photo_count: int = 1,
    audio_count: int = 0,
    text_note: str = "",
    missing_photo_index: int | None = None,
) -> None:
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads" / captured_at[:10] / capture_id
    photos: list[dict[str, object]] = []
    for index in range(photo_count):
        filename = f"photo-{index + 1}.jpg"
        image_path = upload_dir / filename
        if missing_photo_index != index:
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(f"image-{capture_id}-{index}".encode("utf-8"))
        photos.append(
            {
                "filename": filename,
                "mime_type": "image/jpeg",
                "stored_path": str(image_path),
                "upload_id": f"{captured_at[:10]}/{capture_id}/{filename}",
                "size_bytes": 20,
            }
        )
    audio: list[dict[str, object]] = []
    for index in range(audio_count):
        filename = f"voice-{index + 1}.webm"
        audio_path = upload_dir / filename
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")
        audio.append(
            {
                "media_type": "audio",
                "filename": filename,
                "mime_type": "audio/webm",
                "stored_path": str(audio_path),
                "upload_id": f"{captured_at[:10]}/{capture_id}/{filename}",
                "size_bytes": 5,
            }
        )
    intake_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": f"job-{capture_id}",
        "job_type": "photo_capture",
        "metadata": {
            "capture_id": capture_id,
            "site_id": site_id,
            "field_capture_token_label": submitter,
        },
        "payload": {
            "site": "Summit Wire",
            "qc_category": area,
            "phase": phase,
            "captured_at": captured_at,
            "exported_at": captured_at,
            "photos": photos,
            "audio": audio,
        },
    }
    if text_note:
        payload["payload"]["note"] = text_note
    (intake_dir / f"{capture_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def write_sidecar(
    runtime_root: Path,
    asset: photo_vision.FieldPhotoAsset,
    *,
    status: str = "completed",
    warning: str = "",
    area_guess: str = "Restroom",
) -> None:
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    vision_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_type": "field_capture_photo_vision",
        "status": status,
        "capture_id": asset.capture_id,
        "photo_asset_id": asset.photo_asset_id,
        "site_id": asset.site_id,
        "generated_at": "2026-05-06T12:00:00Z",
        "model_name": "ollama:qwen2.5vl:7b",
        "description": "Visible restroom fixtures.",
        "area_guess": area_guess,
        "visible_objects": ["toilet", "paper dispenser"],
        "possible_conditions": ["paper roll visible"],
        "possible_issues": ["possible low paper towels"],
        "confidence": 0.8,
        "warnings": [photo_vision.ADVISORY_WARNING] + ([warning] if warning else []),
    }
    if status == "failed":
        payload["description"] = ""
        payload["area_guess"] = ""
        payload["visible_objects"] = []
        payload["possible_conditions"] = []
        payload["possible_issues"] = []
        payload["error"] = {"type": "timeout", "message": "timed out", "can_retry": True}
    photo_vision.photo_vision_path_for(vision_dir, asset.photo_asset_id).write_text(json.dumps(payload), encoding="utf-8")


def build_runtime_fixture(runtime_root: Path) -> list[photo_vision.FieldPhotoAsset]:
    write_capture(runtime_root, capture_id="cap-one", submitter="Alice", photo_count=1, audio_count=1, text_note="CFO office reset", area="Offices")
    write_capture(runtime_root, capture_id="cap-large", submitter="Bob", photo_count=5, area="Other", phase="")
    write_capture(runtime_root, capture_id="cap-missing", submitter="Bob", photo_count=1, missing_photo_index=0, area="", phase="")
    write_capture(runtime_root, capture_id="cap-other-date", captured_at="2026-05-06T18:00:00-04:00", photo_count=1)
    (runtime_root / "field_capture" / "intake" / "malformed.json").write_text("{", encoding="utf-8")
    assets = photo_vision.discover_photo_assets(runtime_root / "field_capture" / "intake", runtime_root / "uploads", site_id="7050", date="2026-05-05")
    write_sidecar(runtime_root, assets[0], area_guess="Office", warning="possible_judgment_language_removed")
    write_sidecar(runtime_root, assets[1], status="failed")
    return assets


def test_audit_counts_captures_media_submitters_behavior_and_vision(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    build_runtime_fixture(runtime_root)

    report = pilot_audit.build_report(runtime_root=runtime_root, site_id="7050", date="2026-05-05")

    assert report["site_name"] == "Summit Wire"
    assert report["capture_totals"]["total_captures"] == 3
    assert report["capture_totals"]["total_images"] == 7
    assert report["capture_totals"]["total_audio_notes"] == 1
    assert report["capture_totals"]["captures_with_audio"] == 1
    assert report["capture_totals"]["captures_without_audio"] == 2
    assert report["capture_totals"]["captures_with_text_notes"] == 1
    assert report["submitters"] == [
        {"submitter": "Bob", "captures": 2, "photos": 6, "audio_notes": 0, "text_notes": 0},
        {"submitter": "Alice", "captures": 1, "photos": 1, "audio_notes": 1, "text_notes": 1},
    ]
    assert report["areas"]["captures_by_area"]["Offices"] == 1
    assert report["areas"]["captures_by_area"]["missing/Other"] == 1
    assert report["phases"]["captures_with_phase_missing_or_blank"] == 2
    assert report["behavior_signals"]["captures_with_more_than_4_photos"] == 1
    assert report["behavior_signals"]["captures_with_exactly_1_photo"] == 2
    assert report["behavior_signals"]["likely_photo_only_captures"] == 2
    assert report["photo_vision"]["total_image_assets"] == 7
    assert report["photo_vision"]["sidecars_total"] == 2
    assert report["photo_vision"]["completed_sidecars"] == 1
    assert report["photo_vision"]["failed_sidecars"] == 1
    assert report["photo_vision"]["missing_sidecars"] == 5
    assert report["photo_vision"]["sidecars_with_warnings"] == 1
    assert report["photo_vision"]["area_guess_distribution"]["Office"] == 1
    assert report["photo_vision"]["failed_sidecar_details"][0]["error_type"] == "timeout"
    assert report["metadata_integrity"]["intake_records_missing_media"] == 1
    assert report["metadata_integrity"]["malformed_intake_record_count"] == 1


def test_audit_json_shape_and_default_command_writes_no_files(tmp_path: Path, capsys) -> None:
    runtime_root = tmp_path / "runtime"
    build_runtime_fixture(runtime_root)

    assert btq.run(["audit-field-capture-pilot", "--runtime-root", str(runtime_root), "--site-id", "7050", "--date", "2026-05-05", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert sorted(output.keys()) == [
        "areas",
        "behavior_signals",
        "capture_totals",
        "date",
        "generated_at",
        "metadata_integrity",
        "phases",
        "photo_vision",
        "recommendations",
        "review_state",
        "runtime_root",
        "site_id",
        "site_name",
        "submitters",
    ]
    assert not (runtime_root / "reports").exists()
    assert not (runtime_root / "queue").exists()


def test_output_markdown_and_json_write_only_when_requested(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    build_runtime_fixture(runtime_root)
    md_path = tmp_path / "reports" / "audit.md"
    json_path = tmp_path / "reports" / "audit.json"

    assert btq.run(
        [
            "audit-field-capture-pilot",
            "--runtime-root",
            str(runtime_root),
            "--site-id",
            "7050",
            "--date",
            "2026-05-05",
            "--output-md",
            str(md_path),
            "--output-json",
            str(json_path),
        ]
    ) == 0

    assert "# Summit Wire Field Capture Pilot Audit - 2026-05-05" in md_path.read_text(encoding="utf-8")
    assert "- [ ] What worked?" in md_path.read_text(encoding="utf-8")
    assert json.loads(json_path.read_text(encoding="utf-8"))["site_id"] == "7050"
    assert not (runtime_root / "queue").exists()


def test_audit_does_not_run_vision_or_create_review_or_queue_artifacts(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    build_runtime_fixture(runtime_root)

    def fail_ollama(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("audit must not construct or call local vision")

    monkeypatch.setattr(photo_vision, "OllamaVisionClient", fail_ollama)

    report = pilot_audit.build_report(runtime_root=runtime_root, site_id="7050", date="2026-05-05")

    assert report["review_state"]["runtime_queue_count"] == 0
    assert not (runtime_root / "queue").exists()
    assert not (runtime_root / "reviews" / "action_candidates").exists()
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()
