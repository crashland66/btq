from __future__ import annotations

import json
import subprocess
from pathlib import Path

from field_capture import audio_transcription
from field_capture import photo_vision
from field_capture import pipeline_watcher
from field_capture.site_viewer import build_site_payload, render_site_page


def capture_docs_from_queue(queue_dir: Path) -> list[dict[str, object]]:
    docs: list[dict[str, object]] = []
    for path in sorted(queue_dir.glob("*.json")):
        job = json.loads(path.read_text(encoding="utf-8"))
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        docs.append(
            {
                "_id": str(metadata.get("capture_id") or path.stem),
                "type": "field_capture",
                "capture_id": str(metadata.get("capture_id") or ""),
                "site_id": str(metadata.get("site_id") or ""),
                "site": str(payload.get("site") or ""),
                "qc_category": str(payload.get("qc_category") or ""),
                "phase": str(payload.get("phase") or ""),
                "note": str(payload.get("note") or ""),
                "captured_at": str(payload.get("captured_at") or ""),
                "exported_at": str(payload.get("exported_at") or ""),
                "photos": payload.get("photos") if isinstance(payload.get("photos"), list) else [],
                "audio": payload.get("audio") if isinstance(payload.get("audio"), list) else [],
            }
        )
    return docs


def write_capture_with_audio(queue_dir: Path, upload_dir: Path, *, stored_path: Path | None = None, capture_id: str = "cap-audio", filename: str = "voice.webm") -> Path:
    audio_path = stored_path or upload_dir / "2026-05-02" / capture_id / filename
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"audio bytes")
    job = {
        "job_id": f"audio-job-{capture_id}",
        "job_type": "photo_capture",
        "metadata": {
            "capture_id": capture_id,
            "site_id": "7050",
        },
        "payload": {
            "site": "Summit Wire",
            "qc_category": "Restrooms",
            "phase": "issue",
            "captured_at": "2026-05-02T23:31:41-04:00",
            "exported_at": "2026-05-02T23:31:41-04:00",
            "photos": [],
            "audio": [
                {
                    "media_type": "audio",
                    "filename": audio_path.name,
                    "mime_type": "audio/webm",
                    "stored_path": str(audio_path),
                    "upload_id": f"2026-05-02/{capture_id}/{audio_path.name}",
                    "size_bytes": audio_path.stat().st_size,
                    "duration_seconds": "27",
                }
            ],
        },
    }
    queue_dir.mkdir(parents=True, exist_ok=True)
    (queue_dir / f"audio-job-{capture_id}.json").write_text(json.dumps(job), encoding="utf-8")
    return audio_path


def write_capture_with_photo(intake_dir: Path, upload_dir: Path, *, capture_id: str = "cap-photo", filename: str = "wide.jpg") -> Path:
    image_path = upload_dir / "2026-05-02" / capture_id / filename
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"image bytes")
    job = {
        "job_id": f"photo-job-{capture_id}",
        "job_type": "photo_capture",
        "metadata": {
            "capture_id": capture_id,
            "site_id": "7050",
        },
        "payload": {
            "site": "Summit Wire",
            "qc_category": "Offices / Classrooms / Exam Rooms",
            "phase": "completed",
            "captured_at": "2026-05-02T23:31:41-04:00",
            "exported_at": "2026-05-02T23:31:41-04:00",
            "photos": [
                {
                    "media_type": "image",
                    "filename": image_path.name,
                    "mime_type": "image/jpeg",
                    "stored_path": str(image_path),
                    "upload_id": f"2026-05-02/{capture_id}/{image_path.name}",
                    "size_bytes": image_path.stat().st_size,
                }
            ],
            "audio": [],
        },
    }
    intake_dir.mkdir(parents=True, exist_ok=True)
    (intake_dir / f"photo-job-{capture_id}.json").write_text(json.dumps(job), encoding="utf-8")
    return image_path


class FakeTranscriber:
    engine_name = "stub"

    def __init__(self, text: str = "Raw field note transcript.\n") -> None:
        self.text = text
        self.calls: list[Path] = []
        self.last_run = None

    def __call__(self, path: Path) -> str:
        self.calls.append(path)
        return self.text


class FakeVisionClient:
    provider = "local-test"
    engine_name = "local-test:vision"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __call__(self, asset: photo_vision.FieldPhotoAsset) -> photo_vision.VisionDescription:
        self.calls.append(asset.photo_asset_id)
        return photo_vision.VisionDescription(
            description="A visible office area with a desk.",
            area_guess="office",
            visible_objects=["desk"],
            possible_conditions=["visible condition"],
            possible_issues=[],
            confidence=0.7,
            needs_human_review=True,
            warnings=[],
        )


def test_field_audio_asset_discovery(tmp_path: Path) -> None:
    queue_dir = tmp_path / "runtime" / "queue"
    upload_dir = tmp_path / "runtime" / "uploads"
    audio_path = write_capture_with_audio(queue_dir, upload_dir)

    assets = audio_transcription.discover_audio_assets(queue_dir, upload_dir)

    assert len(assets) == 1
    asset = assets[0]
    assert asset.site_id == "7050"
    assert asset.upload_id == "cap-audio"
    assert asset.area == "Restrooms"
    assert asset.phase == "issue"
    assert asset.audio_filename == "voice.webm"
    assert asset.audio_path == audio_path.resolve()
    assert asset.audio_media_url == "/media/2026-05-02/cap-audio/voice.webm"
    assert asset.audio_asset_id.startswith("fca_")


def test_field_audio_asset_carries_person_id_from_intake_metadata(tmp_path: Path) -> None:
    queue_dir = tmp_path / "runtime" / "queue"
    upload_dir = tmp_path / "runtime" / "uploads"
    write_capture_with_audio(queue_dir, upload_dir)
    job_path = queue_dir / "audio-job-cap-audio.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    job["metadata"]["person_id"] = "per_test001"
    job_path.write_text(json.dumps(job), encoding="utf-8")

    assets = audio_transcription.discover_audio_assets(queue_dir, upload_dir)

    assert len(assets) == 1
    assert assets[0].person_id == "per_test001"


def test_field_audio_transcript_json_artifact_is_written(tmp_path: Path) -> None:
    queue_dir = tmp_path / "runtime" / "queue"
    upload_dir = tmp_path / "runtime" / "uploads"
    transcript_dir = tmp_path / "runtime" / "field_capture" / "audio_transcripts"
    write_capture_with_audio(queue_dir, upload_dir)
    transcriber = FakeTranscriber("There was a spill near the restroom sink.\n")

    counts = audio_transcription.process_audio_assets(queue_dir, upload_dir, transcript_dir, transcriber)

    assert counts == {"pending": 1, "transcribed": 1, "failed": 0, "skipped": 0}
    assert len(transcriber.calls) == 1
    artifacts = sorted(transcript_dir.glob("*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["type"] == "field_audio_transcript"
    assert payload["status"] == "complete"
    assert payload["transcription_engine"] == "stub"
    assert payload["site_id"] == "7050"
    assert payload["upload_id"] == "cap-audio"
    assert payload["area"] == "Restrooms"
    assert payload["phase"] == "issue"
    assert payload["audio_filename"] == "voice.webm"
    assert payload["audio_media_url"] == "/media/2026-05-02/cap-audio/voice.webm"
    assert payload["raw_text"] == "There was a spill near the restroom sink."


def test_already_transcribed_audio_is_skipped(tmp_path: Path) -> None:
    queue_dir = tmp_path / "runtime" / "queue"
    upload_dir = tmp_path / "runtime" / "uploads"
    transcript_dir = tmp_path / "runtime" / "field_capture" / "audio_transcripts"
    write_capture_with_audio(queue_dir, upload_dir)
    first = FakeTranscriber("First transcript.\n")
    audio_transcription.process_audio_assets(queue_dir, upload_dir, transcript_dir, first)
    second = FakeTranscriber("Second transcript.\n")

    counts = audio_transcription.process_audio_assets(queue_dir, upload_dir, transcript_dir, second)

    assert counts == {"pending": 1, "transcribed": 0, "failed": 0, "skipped": 1}
    assert second.calls == []
    payload = json.loads(next(transcript_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["raw_text"] == "First transcript."


def test_field_audio_transcription_limit_processes_one_pending_item(tmp_path: Path) -> None:
    intake_dir = tmp_path / "runtime" / "field_capture" / "intake"
    upload_dir = tmp_path / "runtime" / "uploads"
    transcript_dir = tmp_path / "runtime" / "field_capture" / "audio_transcripts"
    first_audio = write_capture_with_audio(intake_dir, upload_dir, capture_id="cap-audio-1")
    second_audio = write_capture_with_audio(intake_dir, upload_dir, capture_id="cap-audio-2")
    transcriber = FakeTranscriber("Limited transcript.\n")

    counts = audio_transcription.process_audio_assets(intake_dir, upload_dir, transcript_dir, transcriber, limit=1)

    assert counts == {"pending": 2, "transcribed": 1, "failed": 0, "skipped": 1}
    assert transcriber.calls == [first_audio.resolve()]
    assert len(list(transcript_dir.glob("*.json"))) == 1

    second = FakeTranscriber("Second limited transcript.\n")
    counts = audio_transcription.process_audio_assets(intake_dir, upload_dir, transcript_dir, second, limit=1)

    assert counts == {"pending": 2, "transcribed": 1, "failed": 0, "skipped": 1}
    assert second.calls == [second_audio.resolve()]
    assert len(list(transcript_dir.glob("*.json"))) == 2


def test_failed_field_audio_transcription_writes_terminal_failed_status(tmp_path: Path) -> None:
    queue_dir = tmp_path / "runtime" / "queue"
    upload_dir = tmp_path / "runtime" / "uploads"
    transcript_dir = tmp_path / "runtime" / "field_capture" / "audio_transcripts"
    write_capture_with_audio(queue_dir, upload_dir)

    class FailingTranscriber:
        engine_name = "stub"
        last_run = None

        def __call__(self, _path: Path) -> str:
            raise RuntimeError("mic noise")

    counts = audio_transcription.process_audio_assets(queue_dir, upload_dir, transcript_dir, FailingTranscriber())

    assert counts == {"pending": 1, "transcribed": 0, "failed": 1, "skipped": 0}
    payload = json.loads(next(transcript_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["raw_text"] == ""
    assert payload["error"]["type"] == "RuntimeError"
    assert payload["error"]["message"] == "mic noise"


def test_unsafe_audio_path_is_not_discovered_or_written(tmp_path: Path) -> None:
    queue_dir = tmp_path / "runtime" / "queue"
    upload_dir = tmp_path / "runtime" / "uploads"
    transcript_dir = tmp_path / "runtime" / "field_capture" / "audio_transcripts"
    outside_audio = tmp_path / "outside" / "voice.webm"
    write_capture_with_audio(queue_dir, upload_dir, stored_path=outside_audio)

    assets = audio_transcription.discover_audio_assets(queue_dir, upload_dir)
    counts = audio_transcription.process_audio_assets(queue_dir, upload_dir, transcript_dir, FakeTranscriber())

    assert assets == []
    assert counts == {"pending": 0, "transcribed": 0, "failed": 0, "skipped": 0}
    assert not transcript_dir.exists()


def test_site_viewer_shows_raw_internal_transcript_when_artifact_exists(tmp_path: Path) -> None:
    queue_dir = tmp_path / "runtime" / "queue"
    upload_dir = tmp_path / "runtime" / "uploads"
    transcript_dir = tmp_path / "runtime" / "field_capture" / "audio_transcripts"
    write_capture_with_audio(queue_dir, upload_dir)
    audio_transcription.process_audio_assets(queue_dir, upload_dir, transcript_dir, FakeTranscriber("Raw sink note.\n"))

    payload = build_site_payload("7050", capture_docs_from_queue(queue_dir), upload_dir, transcript_dir)
    html = render_site_page("7050", payload)

    audio = payload["dates"][0]["uploads"][0]["audio"][0]
    assert audio["transcript"]["status"] == "complete"
    assert audio["transcript"]["raw_text"] == "Raw sink note."
    assert "Internal transcript - raw/unreviewed" in html
    assert "Raw sink note." in html


def test_field_capture_pipeline_cycle_runs_safe_steps_in_order_and_limits_transcription(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    calls: list[str] = []
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    write_capture_with_audio(intake_dir, upload_dir, capture_id="cap-audio-1")
    write_capture_with_audio(intake_dir, upload_dir, capture_id="cap-audio-2")
    write_capture_with_photo(intake_dir, upload_dir, capture_id="cap-photo-1")
    write_capture_with_photo(intake_dir, upload_dir, capture_id="cap-photo-2")

    class OrderedTranscriber(FakeTranscriber):
        def __call__(self, path: Path) -> str:
            if not self.calls:
                calls.append("transcribe")
            return super().__call__(path)

    def factory(_runtime_root: Path, _logger: object) -> OrderedTranscriber:
        return OrderedTranscriber("Sink water by restroom.\n")

    def vision_factory(_model: str, _ollama_url: str, _timeout_seconds: float) -> FakeVisionClient:
        calls.append("vision")
        return FakeVisionClient([])

    logger = pipeline_watcher.configure_logger(runtime_root / "logs" / "watch.log")
    cycle = pipeline_watcher.run_cycle(
        runtime_root=runtime_root,
        transcribe_limit=1,
        vision_limit=1,
        vision_model="qwen2.5vl:7b",
        ollama_url="http://127.0.0.1:11434",
        vision_timeout_seconds=180,
        transcriber_factory=factory,
        logger=logger,
        vision_describe_factory=vision_factory,
    )

    assert cycle["ok"] is True
    assert calls == ["transcribe", "vision"]
    assert [step["step"] for step in cycle["steps"]] == [
        "transcribe_field_audio",
        "process_field_audio_semantics",
        "route_field_reported_issues",
        "describe_field_photos",
        "process_cat_vision",
        "collect_action_candidates",
    ]
    assert cycle["steps"][0]["counts"] == {"pending": 2, "transcribed": 1, "failed": 0, "skipped": 1}
    assert cycle["steps"][2]["counts"] == {"discovered": 0, "routed": 0, "skipped": 0, "failed": 0}
    assert cycle["steps"][3]["counts"] == {"discovered": 2, "would_create": 0, "would_replace": 0, "completed": 1, "failed": 0, "skipped": 1}
    assert len(list((runtime_root / "field_capture" / "audio_transcripts").glob("*.json"))) == 1
    assert len(list((runtime_root / "field_capture" / "audio_semantics").glob("*.json"))) == 1
    assert len(list((runtime_root / "field_capture" / "photo_vision").glob("*.json"))) == 1
    assert len(list((runtime_root / "reviews" / "action_candidates" / "field_capture").glob("*.json"))) == 1
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_field_capture_pipeline_repeated_cycles_are_idempotent(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_capture_with_audio(runtime_root / "field_capture" / "intake", runtime_root / "uploads", capture_id="cap-audio-1")

    def factory(_runtime_root: Path, _logger: object) -> FakeTranscriber:
        return FakeTranscriber("Sink water by restroom.\n")

    logger = pipeline_watcher.configure_logger(runtime_root / "logs" / "watch.log")
    first = pipeline_watcher.run_cycle(
        runtime_root=runtime_root,
        transcribe_limit=1,
        vision_limit=1,
        vision_model="qwen2.5vl:7b",
        ollama_url="http://127.0.0.1:11434",
        vision_timeout_seconds=180,
        transcriber_factory=factory,
        logger=logger,
        run_vision=False,
    )
    second = pipeline_watcher.run_cycle(
        runtime_root=runtime_root,
        transcribe_limit=1,
        vision_limit=1,
        vision_model="qwen2.5vl:7b",
        ollama_url="http://127.0.0.1:11434",
        vision_timeout_seconds=180,
        transcriber_factory=factory,
        logger=logger,
        run_vision=False,
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["steps"][0]["counts"]["transcribed"] == 0
    assert second["steps"][0]["counts"]["skipped"] == 1
    assert second["steps"][1]["counts"]["skipped"] == 1
    assert len(list((runtime_root / "reviews" / "action_candidates" / "field_capture").glob("*.json"))) == 1
    assert not (runtime_root / "queue").exists()


def test_field_capture_pipeline_transcription_failure_is_reported_without_queue_or_vault_mutation(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    write_capture_with_audio(runtime_root / "field_capture" / "intake", runtime_root / "uploads", capture_id="cap-audio-1")

    def factory(_runtime_root: Path, _logger: object) -> FakeTranscriber:
        raise RuntimeError("transcriber unavailable")

    logger = pipeline_watcher.configure_logger(runtime_root / "logs" / "watch.log")
    cycle = pipeline_watcher.run_cycle(
        runtime_root=runtime_root,
        transcribe_limit=1,
        vision_limit=1,
        vision_model="qwen2.5vl:7b",
        ollama_url="http://127.0.0.1:11434",
        vision_timeout_seconds=180,
        transcriber_factory=factory,
        logger=logger,
        run_vision=False,
    )

    assert cycle["ok"] is False
    assert cycle["steps"][0]["status"] == "failed"
    assert cycle["steps"][0]["error"] == "transcriber unavailable"
    assert [step["step"] for step in cycle["steps"]] == [
        "transcribe_field_audio",
        "process_field_audio_semantics",
        "route_field_reported_issues",
        "describe_field_photos",
        "process_cat_vision",
        "collect_action_candidates",
    ]
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_field_capture_pipeline_no_vision_skips_photo_vision(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_capture_with_photo(runtime_root / "field_capture" / "intake", runtime_root / "uploads")

    def factory(_runtime_root: Path, _logger: object) -> FakeTranscriber:
        return FakeTranscriber("unused\n")

    def vision_factory(_model: str, _ollama_url: str, _timeout_seconds: float) -> FakeVisionClient:
        raise AssertionError("vision should be skipped")

    logger = pipeline_watcher.configure_logger(runtime_root / "logs" / "watch.log")
    cycle = pipeline_watcher.run_cycle(
        runtime_root=runtime_root,
        transcribe_limit=1,
        vision_limit=1,
        vision_model="qwen2.5vl:7b",
        ollama_url="http://127.0.0.1:11434",
        vision_timeout_seconds=180,
        transcriber_factory=factory,
        logger=logger,
        vision_describe_factory=vision_factory,
        run_transcribe=False,
        run_semantics=False,
        run_vision=False,
        run_candidates=False,
    )

    assert cycle["ok"] is True
    assert cycle["steps"][2] == {"step": "route_field_reported_issues", "status": "completed", "counts": {"discovered": 0, "routed": 0, "skipped": 0, "failed": 0}, "error": ""}
    assert cycle["steps"][3] == {"step": "describe_field_photos", "status": "skipped", "counts": {}, "error": "disabled"}
    assert not (runtime_root / "field_capture" / "photo_vision").exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_pipeline_vision_limit_zero_skips_photo_vision(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_capture_with_photo(runtime_root / "field_capture" / "intake", runtime_root / "uploads")

    def factory(_runtime_root: Path, _logger: object) -> FakeTranscriber:
        return FakeTranscriber("unused\n")

    def vision_factory(_model: str, _ollama_url: str, _timeout_seconds: float) -> FakeVisionClient:
        raise AssertionError("vision should be skipped")

    logger = pipeline_watcher.configure_logger(runtime_root / "logs" / "watch.log")
    cycle = pipeline_watcher.run_cycle(
        runtime_root=runtime_root,
        transcribe_limit=1,
        vision_limit=0,
        vision_model="qwen2.5vl:7b",
        ollama_url="http://127.0.0.1:11434",
        vision_timeout_seconds=180,
        transcriber_factory=factory,
        logger=logger,
        vision_describe_factory=vision_factory,
        run_transcribe=False,
        run_semantics=False,
        run_candidates=False,
    )

    assert cycle["ok"] is True
    assert cycle["steps"][2] == {"step": "route_field_reported_issues", "status": "completed", "counts": {"discovered": 0, "routed": 0, "skipped": 0, "failed": 0}, "error": ""}
    assert cycle["steps"][3] == {"step": "describe_field_photos", "status": "skipped", "counts": {}, "error": "limit=0"}
    assert not (runtime_root / "field_capture" / "photo_vision").exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_pipeline_vision_failure_is_reported_without_queue_or_vault_mutation(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    write_capture_with_photo(runtime_root / "field_capture" / "intake", runtime_root / "uploads")

    def factory(_runtime_root: Path, _logger: object) -> FakeTranscriber:
        return FakeTranscriber("unused\n")

    def failing_photo_vision(*_args: object, **_kwargs: object) -> dict[str, int]:
        raise RuntimeError("ollama unavailable")

    logger = pipeline_watcher.configure_logger(runtime_root / "logs" / "watch.log")
    cycle = pipeline_watcher.run_cycle(
        runtime_root=runtime_root,
        transcribe_limit=1,
        vision_limit=1,
        vision_model="qwen2.5vl:7b",
        ollama_url="http://127.0.0.1:11434",
        vision_timeout_seconds=180,
        transcriber_factory=factory,
        logger=logger,
        photo_vision_func=failing_photo_vision,
        run_transcribe=False,
        run_semantics=False,
    )

    assert cycle["ok"] is False
    assert cycle["steps"][2]["step"] == "route_field_reported_issues"
    assert cycle["steps"][2]["status"] == "completed"
    assert cycle["steps"][3]["step"] == "describe_field_photos"
    assert cycle["steps"][3]["status"] == "failed"
    assert cycle["steps"][3]["error"] == "ollama unavailable"
    assert cycle["steps"][4] == {"step": "process_cat_vision", "status": "skipped", "counts": {}, "error": "disabled"}
    assert cycle["steps"][5]["step"] == "collect_action_candidates"
    assert cycle["steps"][5]["status"] == "completed"
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_isolated_mlx_photo_vision_timeout_writes_retryable_failed_sidecar(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    write_capture_with_photo(runtime_root / "field_capture" / "intake", runtime_root / "uploads")
    asset = photo_vision.discover_photo_assets(
        runtime_root / "field_capture" / "intake",
        runtime_root / "uploads",
    )[0]

    def timeout_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["python", "-m", "field_capture.photo_vision"], timeout=240)

    monkeypatch.setattr(pipeline_watcher.subprocess, "run", timeout_run)

    describe = pipeline_watcher.isolated_mlx_vision_describe_factory(
        "mlx-community/test-vlm",
        "http://127.0.0.1:11434",
        120.0,
    )
    counts = pipeline_watcher.process_mlx_photo_assets_isolated(
        runtime_root / "field_capture" / "intake",
        runtime_root / "uploads",
        runtime_root / "field_capture" / "photo_vision",
        describe,
        runtime_root=runtime_root,
        limit=1,
        model="mlx-community/test-vlm",
    )

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 0, "failed": 1, "skipped": 0}
    sidecar = json.loads(
        (runtime_root / "field_capture" / "photo_vision" / f"{asset.photo_asset_id}.json").read_text(encoding="utf-8")
    )
    assert sidecar["status"] == "failed"
    assert sidecar["model_provider"] == "mlx"
    assert sidecar["error"]["type"] == "timeout"
    assert sidecar["error"]["can_retry"] is True
