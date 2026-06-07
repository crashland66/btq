from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from config import get_config
from field_capture.site_viewer import UnsafeMediaPath, resolve_media_path, upload_id_for_path
from processing_core.artifacts import read_json_object, resolve_within_root, write_json_object
from processing_core.ids import deterministic_artifact_id
from processing_core.results import result_counts
from processing_core.status import is_terminal_status
from processing_core.transcripts import transcript_base_payload, transcript_failed_payload, transcript_success_payload
from transcription_pipeline.main import (
    DEFAULT_INITIAL_PROMPT,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    SubprocessWhisperTranscriber,
    configure_logging,
    ensure_ffmpeg_on_path,
    transcription_log_payload,
)


@dataclass(frozen=True)
class FieldAudioAsset:
    site_id: str
    upload_id: str
    area: str
    phase: str
    audio_asset_id: str
    audio_filename: str
    audio_path: Path
    audio_media_url: str
    mime_type: str
    size_bytes: int
    person_id: str = ""


def default_runtime_root() -> Path:
    return get_config().runtime_root


def default_intake_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "field_capture" / "intake"


def default_upload_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "uploads"


def default_transcript_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "field_capture" / "audio_transcripts"


def default_log_path(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "logs" / "field_capture_audio_transcription.log"


def transcript_path_for(transcript_dir: Path, asset_id: str) -> Path:
    return transcript_dir / f"{asset_id}.json"


def read_json(path: Path) -> dict[str, object] | None:
    return read_json_object(path)


def transcript_is_terminal(path: Path) -> bool:
    payload = read_json(path)
    if not payload:
        return False
    return is_terminal_status(payload.get("status"))


def discover_audio_assets(intake_dir: Path, upload_dir: Path) -> list[FieldAudioAsset]:
    assets: dict[str, FieldAudioAsset] = {}
    for job_path in sorted(intake_dir.expanduser().glob("**/*.json")):
        job = read_json(job_path)
        if not job or job.get("job_type") != "photo_capture":
            continue
        metadata = job.get("metadata")
        payload = job.get("payload")
        if not isinstance(metadata, dict) or not isinstance(payload, dict):
            continue
        audio_records = payload.get("audio")
        if not isinstance(audio_records, list):
            continue
        capture_id = str(metadata.get("capture_id") or "").strip()
        site_id = str(metadata.get("site_id") or payload.get("site") or "").strip()
        area = str(payload.get("area") or payload.get("qc_category") or "").strip()
        phase = str(payload.get("phase") or metadata.get("phase") or payload.get("status") or "").strip()
        person_id = str(metadata.get("person_id") or "").strip()
        for audio in audio_records:
            if not isinstance(audio, dict):
                continue
            stored_path = str(audio.get("stored_path", "")).strip()
            filename = str(audio.get("filename", "")).strip()
            if not stored_path or not filename:
                continue
            try:
                media_path = resolve_media_path(stored_path, upload_dir)
            except UnsafeMediaPath:
                continue
            if not media_path.exists() or not media_path.is_file():
                continue
            upload_id = str(audio.get("upload_id") or "").strip()
            if not upload_id:
                upload_id = upload_id_for_path(media_path, upload_dir)
            asset_id = deterministic_artifact_id("fca", capture_id or upload_id, filename, str(media_path))
            assets.setdefault(
                asset_id,
                FieldAudioAsset(
                    site_id=site_id,
                    upload_id=capture_id or str(Path(upload_id).parent),
                    area=area,
                    phase=phase,
                    person_id=person_id,
                    audio_asset_id=asset_id,
                    audio_filename=filename,
                    audio_path=media_path,
                    audio_media_url=f"/media/{quote(upload_id)}",
                    mime_type=str(audio.get("mime_type") or ""),
                    size_bytes=int(audio.get("size_bytes") or media_path.stat().st_size),
                ),
            )
    return sorted(assets.values(), key=lambda asset: (asset.upload_id, asset.audio_filename, asset.audio_asset_id))


def base_transcript_payload(asset: FieldAudioAsset, engine_name: str) -> dict[str, object]:
    return transcript_base_payload(
        artifact_type="field_audio_transcript",
        artifact_id_field="audio_asset_id",
        artifact_id=asset.audio_asset_id,
        engine_field="transcription_engine",
        engine_name=engine_name,
        provenance={
            "site_id": asset.site_id,
            "upload_id": asset.upload_id,
            "area": asset.area,
            "phase": asset.phase,
            "person_id": asset.person_id,
            "audio_filename": asset.audio_filename,
            "audio_media_url": asset.audio_media_url,
            "audio_path": str(asset.audio_path),
            "audio_mime_type": asset.mime_type,
            "audio_size_bytes": asset.size_bytes,
        },
    )


def write_success_transcript(
    transcript_dir: Path,
    asset: FieldAudioAsset,
    raw_text: str,
    engine_name: str,
    transcription_payload: dict[str, object] | None,
) -> Path:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_path_for(transcript_dir, asset.audio_asset_id)
    payload = transcript_success_payload(
        base_transcript_payload(asset, engine_name),
        raw_text=raw_text,
        transcription_payload=transcription_payload,
    )
    write_json_object(path, payload)
    return path


def write_failed_transcript(transcript_dir: Path, asset: FieldAudioAsset, engine_name: str, error: Exception) -> Path:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_path_for(transcript_dir, asset.audio_asset_id)
    payload = transcript_failed_payload(base_transcript_payload(asset, engine_name), error)
    write_json_object(path, payload)
    return path


def transcriber_engine_name(transcribe: Callable[[Path], str]) -> str:
    explicit = getattr(transcribe, "engine_name", None)
    if explicit:
        return str(explicit)
    return type(transcribe).__name__ if hasattr(transcribe, "__class__") else "transcriber"


def process_audio_assets(
    intake_dir: Path,
    upload_dir: Path,
    transcript_dir: Path,
    transcribe: Callable[[Path], str],
    logger: logging.Logger | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    counts = result_counts("pending", "transcribed", "failed", "skipped")
    engine_name = transcriber_engine_name(transcribe)
    upload_root = upload_dir.expanduser().resolve(strict=False)
    transcript_root = transcript_dir.expanduser().resolve(strict=False)
    assets = discover_audio_assets(intake_dir, upload_root)
    counts["pending"] = len(assets)
    for asset in assets:
        transcript_path = transcript_path_for(transcript_root, asset.audio_asset_id)
        if transcript_is_terminal(transcript_path):
            counts["skipped"] += 1
            if logger is not None:
                logger.info("field audio transcript exists; skipping asset_id=%s path=%s", asset.audio_asset_id, transcript_path)
            continue
        if limit is not None and counts["transcribed"] + counts["failed"] >= limit:
            counts["skipped"] += 1
            if logger is not None:
                logger.info("field audio transcription cycle limit reached; deferring asset_id=%s", asset.audio_asset_id)
            continue
        try:
            resolve_within_root(asset.audio_path, upload_root)
        except ValueError as exc:
            write_failed_transcript(transcript_root, asset, engine_name, exc)
            counts["failed"] += 1
            continue
        try:
            if logger is not None:
                logger.info("field audio transcription started asset_id=%s audio=%s", asset.audio_asset_id, asset.audio_path)
            raw_text = transcribe(asset.audio_path)
            write_success_transcript(
                transcript_root,
                asset,
                raw_text,
                engine_name,
                transcription_log_payload(transcribe),
            )
            counts["transcribed"] += 1
            if logger is not None:
                logger.info("field audio transcription completed asset_id=%s", asset.audio_asset_id)
        except Exception as exc:  # noqa: BLE001
            write_failed_transcript(transcript_root, asset, engine_name, exc)
            counts["failed"] += 1
            if logger is not None:
                logger.exception("field audio transcription failed asset_id=%s audio=%s", asset.audio_asset_id, asset.audio_path)
    return counts


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Transcribe uploaded field-capture voice notes after upload.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--intake-dir", type=Path, help="Field-capture intake metadata directory.")
    parser.add_argument("--upload-dir", type=Path)
    parser.add_argument("--transcript-dir", type=Path)
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--model", default=config.whisper_model)
    parser.add_argument("--initial-prompt", default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument("--no-initial-prompt", action="store_true")
    parser.add_argument("--worker-timeout-seconds", type=float, default=DEFAULT_WORKER_TIMEOUT_SECONDS)
    parser.add_argument("--limit", type=int, help="Maximum number of not-yet-terminal audio assets to transcribe in this pass.")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_ffmpeg_on_path()
    runtime_root = args.runtime_root.expanduser()
    intake_dir = (args.intake_dir or default_intake_dir(runtime_root)).expanduser()
    upload_dir = (args.upload_dir or default_upload_dir(runtime_root)).expanduser()
    transcript_dir = (args.transcript_dir or default_transcript_dir(runtime_root)).expanduser()
    log_path = (args.log_path or default_log_path(runtime_root)).expanduser()
    logger = configure_logging(log_path)
    transcriber = SubprocessWhisperTranscriber(
        args.model,
        runtime_root / "local",
        compare_mode=False,
        initial_prompt=None if args.no_initial_prompt else args.initial_prompt,
        timeout_seconds=args.worker_timeout_seconds,
        logger=logger,
    )
    counts = process_audio_assets(intake_dir, upload_dir, transcript_dir, transcriber, logger, limit=args.limit)
    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
    else:
        print(
            "field audio transcription: "
            f"pending={counts['pending']} transcribed={counts['transcribed']} "
            f"failed={counts['failed']} skipped={counts['skipped']}"
        )
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
