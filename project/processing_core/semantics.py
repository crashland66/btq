from __future__ import annotations

from datetime import datetime, timezone

from processing_core.hashing import text_sha256
from processing_core.status import STATUS_COMPLETE, STATUS_FAILED


def semantic_base_payload(
    *,
    artifact_type: str,
    artifact_id_field: str,
    artifact_id: str,
    source_transcript_path: str,
    engine_field: str,
    engine_name: str,
    raw_text: str,
    provenance: dict[str, object],
    created_at: str | None = None,
) -> dict[str, object]:
    return {
        "type": artifact_type,
        **provenance,
        artifact_id_field: artifact_id,
        "source_transcript_path": source_transcript_path,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        engine_field: engine_name,
        "raw_text_hash": text_sha256(raw_text),
    }


def semantic_success_payload(base_payload: dict[str, object], result_fields: dict[str, object]) -> dict[str, object]:
    payload = dict(base_payload)
    payload.update({"status": STATUS_COMPLETE, **result_fields, "error": None})
    return payload


def semantic_failed_payload(
    base_payload: dict[str, object],
    *,
    error: Exception,
    failure_fields: dict[str, object],
) -> dict[str, object]:
    payload = dict(base_payload)
    payload.update(
        {
            "status": STATUS_FAILED,
            **failure_fields,
            "error": {"type": type(error).__name__, "message": str(error)},
        }
    )
    return payload

