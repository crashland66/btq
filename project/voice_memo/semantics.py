from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from field_capture import action_candidates
from processing_core.artifacts import write_json_object
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    RuleCaptureEngine,
    SemanticResult,
    build_semantic_engine,
    clean_internal_note,
    raw_text_hash,
    semantic_failure_fields,
    validate_semantic_result,
)


PROMPT_VERSION = "capture-actions-v1"
SEMANTIC_TYPE = "voice_memo_semantic_summary"
DEFAULT_OLLAMA_SEMANTIC_URL = "http://127.0.0.1:11434"
COMPLETE = "complete"
FAILED = "failed"


@dataclass(frozen=True)
class VoiceMemoTranscriptContext:
    capture_id: str
    routing_flag: str
    raw_text: str
    raw_transcript_path: Path
    audio_file: str
    site_id: str = ""
    site: str = ""
    employees: list[dict[str, str]] | None = None
    note: str = ""
    captured_at: str = ""


@dataclass(frozen=True)
class VoiceMemoSemanticPass:
    result: SemanticResult | None
    artifact_path: Path
    candidate_paths: tuple[Path, ...] = ()
    error: str = ""

    @property
    def candidate_path(self) -> Path | None:
        return self.candidate_paths[0] if self.candidate_paths else None


class VoiceMemoSemanticEngine(Protocol):
    engine_name: str

    def __call__(self, capture: CaptureSemanticInput) -> SemanticResult:
        ...


def default_semantic_engine_from_env() -> VoiceMemoSemanticEngine | None:
    enabled = os.environ.get("BTQ_VOICE_MEMO_SEMANTICS_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no", "off", "disabled"}:
        return None

    engine = os.environ.get("BTQ_VOICE_MEMO_SEMANTIC_ENGINE", "").strip().lower()
    if not engine:
        return build_semantic_engine()
    if engine in {"rule", "local_rule", "local-rule"}:
        return RuleCaptureEngine()
    if engine in {"mlx", "model", "local_model", "local-model"}:
        from vision_backends import MlxTextClient

        return _local_model_engine(MlxTextClient())
    if engine == "ollama":
        from vision_backends import OllamaTextClient

        model = os.environ.get("BTQ_VOICE_MEMO_SEMANTIC_MODEL", "").strip()
        if not model:
            raise ValueError("BTQ_VOICE_MEMO_SEMANTIC_MODEL is required when BTQ_VOICE_MEMO_SEMANTIC_ENGINE=ollama")
        base_url = os.environ.get("BTQ_VOICE_MEMO_SEMANTIC_URL", DEFAULT_OLLAMA_SEMANTIC_URL).strip() or DEFAULT_OLLAMA_SEMANTIC_URL
        timeout_raw = os.environ.get("BTQ_VOICE_MEMO_SEMANTIC_TIMEOUT_SECONDS", "180").strip()
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError as exc:
            raise ValueError("BTQ_VOICE_MEMO_SEMANTIC_TIMEOUT_SECONDS must be numeric") from exc
        keep_alive = os.environ.get("BTQ_VOICE_MEMO_SEMANTIC_KEEP_ALIVE", "10m").strip() or "10m"
        disable_thinking = _env_bool("BTQ_VOICE_MEMO_SEMANTIC_DISABLE_THINKING", default=True)
        return _local_model_engine(
            OllamaTextClient(
                model=model,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
                keep_alive=keep_alive,
                disable_thinking=disable_thinking,
            )
        )
    raise ValueError(f"BTQ_VOICE_MEMO_SEMANTIC_ENGINE={engine!r} is not a recognized value")


def _local_model_engine(client: object) -> VoiceMemoSemanticEngine:
    from processing_core.capture_semantics import LocalModelCaptureEngine

    return LocalModelCaptureEngine(client, fallback=RuleCaptureEngine())


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def clean_transcript(value: str) -> str:
    return clean_internal_note(value)


def semantic_artifact_path(runtime_root: Path, capture_id: str) -> Path:
    safe_capture_id = re.sub(r"[^A-Za-z0-9._-]+", "-", capture_id).strip("-") or "unknown"
    return runtime_root / "voice_memo" / "semantics" / f"{safe_capture_id}.json"


def capture_semantic_input(context: VoiceMemoTranscriptContext) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id=context.capture_id,
        source_kind="voice_memo",
        source_text=clean_transcript(context.raw_text),
        site_id=context.site_id,
        site_label=context.site,
        selected_employees=[dict(employee) for employee in (context.employees or [])],
        source_transcript_path=context.raw_transcript_path,
        captured_at=context.captured_at,
        audio_asset_id=context.audio_file,
    )


def legacy_primary_fields(result: SemanticResult) -> dict[str, object]:
    actions = list(result.extracted_actions or [])
    primary = actions[0] if actions else None
    if primary is None:
        return {
            "intent": "unknown",
            "target_type": "general",
            "target_id": "",
            "action": "",
            "confidence": "normal",
            "review_required": False,
            "proposed_queue_job": None,
            "warnings": [],
        }

    intent = primary.action_key
    action = primary.action_key
    if primary.job_type == "set_entity_status":
        status = ""
        if isinstance(primary.payload_fields, dict):
            status = str(primary.payload_fields.get("status") or "")
        action = "set_inactive" if status == "inactive" else "status_change"
        intent = "site_status_change" if primary.target_type == "site" else "employee_status_change"
    return {
        "intent": intent,
        "target_type": primary.target_type,
        "target_id": primary.target_id,
        "action": action,
        "confidence": primary.confidence if isinstance(primary.confidence, str) else str(primary.confidence),
        "review_required": True,
        "proposed_queue_job": primary.proposed_queue_job,
        "warnings": [],
    }


def complete_artifact_payload(
    context: VoiceMemoTranscriptContext,
    result: SemanticResult,
    *,
    semantic_engine: str,
    prompt_version: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    legacy = legacy_primary_fields(result)
    extracted_actions = [serialized_extracted_action(action) for action in (result.extracted_actions or [])]
    review_summaries = [str(action.get("summary") or "").strip() for action in extracted_actions if str(action.get("summary") or "").strip()]
    return {
        "type": SEMANTIC_TYPE,
        "status": COMPLETE,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "capture_id": context.capture_id,
        "upload_id": context.capture_id,
        "source_kind": "voice_memo",
        "source_text": context.raw_text,
        "source_transcript_path": str(context.raw_transcript_path),
        "raw_transcript_path": str(context.raw_transcript_path),
        "audio_file": context.audio_file,
        "audio_asset_id": context.audio_file,
        "raw_text_hash": raw_text_hash(context.raw_text),
        "site_id": context.site_id,
        "site_label": context.site,
        "cleaned_transcript": result.cleaned_internal_note,
        "cleaned_internal_note": result.cleaned_internal_note,
        "client_safe_note": result.client_safe_note,
        "summary": result.operational_summary,
        "operational_summary": result.operational_summary,
        "issue_detected": result.issue_detected,
        "issue_type": result.issue_type,
        "urgency": result.urgency,
        "suggested_tags": list(result.suggested_tags),
        "action_candidates": review_summaries,
        "extracted_actions": extracted_actions,
        "action_count": len(extracted_actions),
        "visit_proposed": result.visit_proposed,
        "visit_type": result.visit_type,
        "submitter_person_id": result.submitter_person_id,
        "semantic_engine": semantic_engine,
        "prompt_version": prompt_version,
        **legacy,
        "provenance": provenance_payload(context),
    }


def serialized_extracted_action(action: object) -> dict[str, object]:
    payload = asdict(action)
    evidence_terms = payload.get("evidence_terms")
    if isinstance(evidence_terms, tuple):
        payload["evidence_terms"] = list(evidence_terms)
    return payload


def failed_artifact_payload(
    context: VoiceMemoTranscriptContext,
    error: Exception | str,
    *,
    semantic_engine: str,
    prompt_version: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "type": SEMANTIC_TYPE,
        "status": FAILED,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "capture_id": context.capture_id,
        "upload_id": context.capture_id,
        "source_kind": "voice_memo",
        "source_text": context.raw_text,
        "source_transcript_path": str(context.raw_transcript_path),
        "raw_transcript_path": str(context.raw_transcript_path),
        "audio_file": context.audio_file,
        "audio_asset_id": context.audio_file,
        "raw_text_hash": raw_text_hash(context.raw_text),
        "site_id": context.site_id,
        "site_label": context.site,
        "semantic_engine": semantic_engine,
        "prompt_version": prompt_version,
        "error": {
            "type": type(error).__name__ if isinstance(error, Exception) else "ValueError",
            "message": str(error),
        },
        **semantic_failure_fields(),
        "provenance": provenance_payload(context),
    }


def provenance_payload(context: VoiceMemoTranscriptContext) -> dict[str, Any]:
    return {
        "capture_id": context.capture_id,
        "routing_flag": context.routing_flag,
        "sidecar": {
            "site_id": context.site_id,
            "site": context.site,
            "employees": context.employees or [],
            "note": context.note,
            "captured_at": context.captured_at,
        },
        "raw_text": context.raw_text,
        "raw_transcript_path": str(context.raw_transcript_path),
    }


def default_candidate_dir(runtime_root: Path) -> Path:
    return action_candidates.default_candidate_dir(runtime_root)


def candidate_paths_for_artifact(artifact_path: Path, payload: dict[str, object], candidate_dir: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for candidate in action_candidates.payloads_from_semantic(artifact_path, payload):
        candidate_id = str(candidate.get("candidate_id") or "")
        if candidate_id:
            paths.append(action_candidates.action_candidate_review_path(candidate_dir, candidate_id))
    return tuple(paths)


def run_semantic_pass(
    context: VoiceMemoTranscriptContext,
    runtime_root: Path,
    *,
    engine: VoiceMemoSemanticEngine | None = None,
    created_at: str | None = None,
) -> VoiceMemoSemanticPass:
    artifact_path = semantic_artifact_path(runtime_root, context.capture_id)
    try:
        semantic_engine = engine if engine is not None else default_semantic_engine_from_env()
        if semantic_engine is None:
            return VoiceMemoSemanticPass(result=None, artifact_path=artifact_path)
        capture_input = capture_semantic_input(context)
        result = semantic_engine(capture_input)
        validate_semantic_result(result)
        payload = complete_artifact_payload(
            context,
            result,
            semantic_engine=getattr(semantic_engine, "engine_name", type(semantic_engine).__name__),
            prompt_version=getattr(semantic_engine, "prompt_version", PROMPT_VERSION),
            created_at=created_at,
        )
        write_json_object(artifact_path, payload)
        candidate_dir = default_candidate_dir(runtime_root)
        action_candidates.collect_action_candidates(artifact_path.parent, candidate_dir, runtime_root=runtime_root)
        candidate_paths = candidate_paths_for_artifact(artifact_path, payload, candidate_dir)
        return VoiceMemoSemanticPass(result=result, artifact_path=artifact_path, candidate_paths=candidate_paths)
    except Exception as exc:  # noqa: BLE001
        semantic_engine = engine if engine is not None else None
        semantic_engine_name = getattr(semantic_engine, "engine_name", "voice-memo-semantics")
        prompt_version = getattr(semantic_engine, "prompt_version", PROMPT_VERSION)
        write_json_object(
            artifact_path,
            failed_artifact_payload(
                context,
                exc,
                semantic_engine=semantic_engine_name,
                prompt_version=prompt_version,
                created_at=created_at,
            ),
        )
        return VoiceMemoSemanticPass(result=None, artifact_path=artifact_path, error=str(exc))
