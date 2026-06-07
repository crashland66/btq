from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from config import get_config
from processing_core.artifacts import read_json_object, resolve_within_root, write_json_object
from processing_core.capture_semantics import (
    AFTER_PHOTO_ACTION,
    GENERIC_REVIEW_ACTION,
    ISSUE_TYPES,
    SUPPLY_REVIEW_ACTION,
    URGENCIES,
    CaptureSemanticInput,
    CaptureSemanticResult,
    FieldAudioTranscript,
    LocalModelCaptureEngine,
    LocalModelSemanticEngine,
    LocalRuleSemanticEngine,
    RuleCaptureEngine,
    SemanticResult,
    _build_semantic_prompt,
    action_candidates_for,
    build_semantic_engine,
    classify_issue,
    classify_qc_visit,
    classify_urgency,
    classify_visit,
    clean_internal_note,
    client_safe_note,
    collapse_whitespace,
    is_non_field_operation_action,
    is_unclear_or_test_audio,
    needs_after_photo,
    operational_summary,
    raw_text_hash,
    semantic_failure_fields,
    strip_non_field_operation_actions,
    suggested_tags_for,
    validate_semantic_result,
)
from processing_core.results import semantic_result_counts
from processing_core.semantic_transform import SemanticTransformSpec
from processing_core.semantic_transform import semantic_engine_name as core_semantic_engine_name
from processing_core.semantic_transform import transform_semantic_payload
from processing_core.status import STATUS_COMPLETE, is_terminal_status


logger = logging.getLogger(__name__)


def default_runtime_root() -> Path:
    return get_config().runtime_root


def default_transcript_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "field_capture" / "audio_transcripts"


def default_semantic_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "field_capture" / "audio_semantics"


def default_log_path(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "logs" / "field_capture_audio_semantics.log"


def semantic_path_for(semantic_dir: Path, audio_asset_id: str) -> Path:
    return semantic_dir / f"{audio_asset_id}.json"


def read_json(path: Path) -> dict[str, object] | None:
    return read_json_object(path)


def semantic_is_terminal(path: Path) -> bool:
    payload = read_json(path)
    return bool(payload and is_terminal_status(payload.get("status")))


def transcript_from_payload(path: Path, payload: dict[str, object]) -> FieldAudioTranscript | None:
    if payload.get("type") != "field_audio_transcript" or payload.get("status") != STATUS_COMPLETE:
        return None
    raw_text = str(payload.get("raw_text") or "").strip()
    audio_asset_id = str(payload.get("audio_asset_id") or "").strip()
    if not raw_text or not audio_asset_id:
        return None
    return FieldAudioTranscript(
        site_id=str(payload.get("site_id") or ""),
        upload_id=str(payload.get("upload_id") or ""),
        area=str(payload.get("area") or ""),
        phase=str(payload.get("phase") or ""),
        person_id=str(payload.get("person_id") or ""),
        captured_at=str(payload.get("captured_at") or payload.get("created_at") or ""),
        audio_asset_id=audio_asset_id,
        source_transcript_path=path,
        raw_text=raw_text,
    )


def discover_completed_transcripts(transcript_dir: Path) -> list[FieldAudioTranscript]:
    root = transcript_dir.expanduser().resolve(strict=False)
    transcripts: list[FieldAudioTranscript] = []
    for path in sorted(root.glob("*.json")):
        resolved = path.resolve(strict=False)
        try:
            resolve_within_root(resolved, root)
        except ValueError:
            continue
        payload = read_json(resolved)
        if not payload:
            continue
        transcript = transcript_from_payload(resolved, payload)
        if transcript is not None:
            transcripts.append(transcript)
    return transcripts


def semantic_transform_spec(
    transcript: FieldAudioTranscript,
    engine: Callable[[FieldAudioTranscript], SemanticResult],
    engine_name: str,
) -> SemanticTransformSpec[FieldAudioTranscript, SemanticResult]:
    return SemanticTransformSpec(
        artifact_type="field_audio_semantic_summary",
        artifact_id_field="audio_asset_id",
        artifact_id=transcript.audio_asset_id,
        source_transcript_path=str(transcript.source_transcript_path),
        engine_field="semantic_engine",
        engine_name=engine_name,
        raw_text=transcript.raw_text,
        provenance={
            "site_id": transcript.site_id,
            "upload_id": transcript.upload_id,
            "area": transcript.area,
            "phase": transcript.phase,
            "submitter_person_id": transcript.person_id,
            "prompt_version": getattr(engine, "prompt_version", ""),
        },
        engine_input=transcript,
        engine=engine,
        result_fields=lambda result: asdict(result),
        failure_fields=semantic_failure_fields(),
        validate_result=validate_semantic_result,
    )


def write_failed_semantic(semantic_dir: Path, transcript: FieldAudioTranscript, engine_name: str, error: Exception) -> Path:
    semantic_dir.mkdir(parents=True, exist_ok=True)
    path = semantic_path_for(semantic_dir, transcript.audio_asset_id)
    payload = transform_semantic_payload(
        semantic_transform_spec(transcript, lambda _transcript: (_ for _ in ()).throw(error), engine_name)
    ).payload
    write_json_object(path, payload)
    return path


def semantic_engine_name(engine: Callable[[FieldAudioTranscript], SemanticResult]) -> str:
    return core_semantic_engine_name(engine)


def process_semantics(
    transcript_dir: Path,
    semantic_dir: Path,
    engine: Callable[[FieldAudioTranscript], SemanticResult],
    *,
    runtime_root: Path | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, int]:
    counts = semantic_result_counts()
    transcript_root = transcript_dir.expanduser().resolve(strict=False)
    semantic_root = semantic_dir.expanduser().resolve(strict=False)
    if runtime_root is not None:
        runtime_resolved = runtime_root.expanduser().resolve(strict=False)
        resolve_within_root(semantic_root, runtime_resolved)
    transcripts = discover_completed_transcripts(transcript_root)
    counts["discovered"] = len(transcripts)
    engine_name = semantic_engine_name(engine)
    for transcript in transcripts:
        semantic_path = semantic_path_for(semantic_root, transcript.audio_asset_id)
        if semantic_is_terminal(semantic_path):
            counts["skipped"] += 1
            if logger is not None:
                logger.info("field audio semantic artifact exists; skipping asset_id=%s", transcript.audio_asset_id)
            continue
        try:
            resolve_within_root(transcript.source_transcript_path, transcript_root)
            outcome = transform_semantic_payload(semantic_transform_spec(transcript, engine, engine_name))
            write_json_object(semantic_path, outcome.payload)
            if outcome.error is None:
                counts["completed"] += 1
                continue
            counts["failed"] += 1
            if logger is not None:
                logger.error(
                    "field audio semantic processing failed asset_id=%s",
                    transcript.audio_asset_id,
                    exc_info=(type(outcome.error), outcome.error, outcome.error.__traceback__),
                )
        except Exception as exc:  # noqa: BLE001
            write_failed_semantic(semantic_root, transcript, engine_name, exc)
            counts["failed"] += 1
            if logger is not None:
                logger.exception("field audio semantic processing failed asset_id=%s", transcript.audio_asset_id)
    return counts


def configure_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("field_capture.audio_semantics")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Create review-needed semantic artifacts for field-capture audio transcripts.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--transcript-dir", type=Path)
    parser.add_argument("--semantic-dir", type=Path)
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    transcript_dir = (args.transcript_dir or default_transcript_dir(runtime_root)).expanduser()
    semantic_dir = (args.semantic_dir or default_semantic_dir(runtime_root)).expanduser()
    log_path = (args.log_path or default_log_path(runtime_root)).expanduser()
    logger = configure_logger(log_path)
    counts = process_semantics(transcript_dir, semantic_dir, build_semantic_engine(), runtime_root=runtime_root, logger=logger)
    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
    else:
        print(
            "field audio semantics: "
            f"discovered={counts['discovered']} skipped={counts['skipped']} "
            f"completed={counts['completed']} failed={counts['failed']}"
        )
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
