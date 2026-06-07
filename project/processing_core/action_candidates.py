from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from processing_core.artifacts import resolve_within_root, write_json_object
from processing_core.ids import deterministic_artifact_id
from processing_core.json import canonical_json


STATUS_PENDING_REVIEW = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"

REVIEW_STATUSES = {
    STATUS_PENDING_REVIEW,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_FAILED,
}

RESOLUTION_OPEN = "open"
RESOLUTION_CLIENT_NOTIFIED = "client_notified"
RESOLUTION_RESOLVED = "resolved"
RESOLUTION_DISMISSED = "dismissed"
RESOLUTION_STATUSES = frozenset(
    {
        RESOLUTION_OPEN,
        RESOLUTION_CLIENT_NOTIFIED,
        RESOLUTION_RESOLVED,
        RESOLUTION_DISMISSED,
    }
)


def action_candidate_id(
    *,
    candidate_type: str,
    summary: str,
    provenance: dict[str, object] | None = None,
    source_text: str = "",
    source_context: str = "",
    channel_metadata: dict[str, object] | None = None,
    id_key: object | None = None,
) -> str:
    if id_key is not None:
        return deterministic_artifact_id(
            "ac",
            canonical_json(
                {
                    "id_key": id_key,
                }
            ),
        )
    return deterministic_artifact_id(
        "ac",
        canonical_json(
            {
                "candidate_type": candidate_type,
                "summary": summary,
                "provenance": provenance or {},
                "source_text": source_text,
                "source_context": source_context,
                "channel_metadata": channel_metadata or {},
            }
        ),
    )


def action_candidate_review_path(candidate_dir: Path, candidate_id: str) -> Path:
    return candidate_dir / f"{candidate_id}.json"


def malformed_candidate_reason(candidate_type: object, summary: object, status: str) -> str | None:
    if status not in REVIEW_STATUSES:
        return f"unsupported status: {status}"
    if not isinstance(candidate_type, str) or not candidate_type.strip():
        return "candidate_type is required"
    if not isinstance(summary, str) or not summary.strip():
        return "summary is required"
    return None


def action_candidate_payload(
    *,
    candidate_type: object,
    summary: object,
    rationale: object = "",
    confidence: object = "unknown",
    source_text: object = "",
    source_context: object = "",
    provenance: dict[str, object] | None = None,
    channel_metadata: dict[str, object] | None = None,
    id_key: object | None = None,
    status: str = STATUS_PENDING_REVIEW,
    created_at: str | None = None,
    error: dict[str, object] | None = None,
) -> dict[str, object]:
    candidate_type_text = candidate_type if isinstance(candidate_type, str) else ""
    summary_text = summary if isinstance(summary, str) else ""
    rationale_text = rationale if isinstance(rationale, str) else str(rationale or "")
    confidence_value = confidence if isinstance(confidence, (str, int, float, bool)) or confidence is None else str(confidence)
    source_text_text = source_text if isinstance(source_text, str) else str(source_text or "")
    source_context_text = source_context if isinstance(source_context, str) else str(source_context or "")
    provenance_payload = dict(provenance or {})
    channel_payload = dict(channel_metadata or {})

    failure_reason = malformed_candidate_reason(candidate_type, summary, status)
    payload_status = STATUS_FAILED if failure_reason is not None else status
    payload_error = error
    if failure_reason is not None:
        payload_error = {"type": "ValueError", "message": failure_reason}

    candidate_id = action_candidate_id(
        candidate_type=candidate_type_text.strip(),
        summary=summary_text.strip(),
        provenance=provenance_payload,
        source_text=source_text_text,
        source_context=source_context_text,
        channel_metadata=channel_payload,
        id_key=id_key,
    )

    return {
        "type": "action_candidate_review",
        "candidate_id": candidate_id,
        "status": payload_status,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "candidate_type": candidate_type_text.strip(),
        "summary": summary_text.strip(),
        "rationale": rationale_text,
        "confidence": confidence_value,
        "source_text": source_text_text,
        "source_context": source_context_text,
        "provenance": provenance_payload,
        "channel_metadata": channel_payload,
        "error": payload_error,
    }


def failed_action_candidate_payload(
    *,
    error: Exception | str,
    candidate_type: object = "",
    summary: object = "",
    rationale: object = "",
    confidence: object = "unknown",
    source_text: object = "",
    source_context: object = "",
    provenance: dict[str, object] | None = None,
    channel_metadata: dict[str, object] | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    error_payload: dict[str, object]
    if isinstance(error, Exception):
        error_payload = {"type": type(error).__name__, "message": str(error)}
    else:
        error_payload = {"type": "ValueError", "message": str(error)}
    payload = action_candidate_payload(
        candidate_type=candidate_type,
        summary=summary,
        rationale=rationale,
        confidence=confidence,
        source_text=source_text,
        source_context=source_context,
        provenance=provenance,
        channel_metadata=channel_metadata,
        status=STATUS_FAILED,
        created_at=created_at,
        error=error_payload,
    )
    if payload["error"] is None:
        payload["error"] = error_payload
    return payload


def write_action_candidate_review(candidate_dir: Path, payload: dict[str, object]) -> Path:
    candidate_id = str(payload.get("candidate_id") or "")
    if not candidate_id:
        raise ValueError("candidate_id is required")
    path = action_candidate_review_path(candidate_dir, candidate_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_object(path, payload)
    return path


def candidate_resolution_status(payload: dict[str, object]) -> str:
    resolution = payload.get("resolution") if isinstance(payload.get("resolution"), dict) else {}
    return str(resolution.get("status") or "")


def patch_candidate_resolution(
    candidate_dir: Path,
    candidate_id: str,
    *,
    resolution_status: str,
    by: str = "",
    note: str = "",
    at: str | None = None,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    if resolution_status not in RESOLUTION_STATUSES:
        raise ValueError(f"unsupported resolution status: {resolution_status}")

    from field_capture.action_candidates import find_candidate_artifacts

    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    if runtime_root is not None:
        resolve_within_root(candidate_root, runtime_root.expanduser().resolve(strict=False))
    matches = find_candidate_artifacts(candidate_root, candidate_id)
    if not matches:
        raise ValueError(f"candidate not found: {candidate_id}")
    if len(matches) > 1:
        raise ValueError(f"multiple candidate artifacts found for candidate_id: {candidate_id}")

    path, payload = matches[0]
    existing_resolution = payload.get("resolution") if isinstance(payload.get("resolution"), dict) else {}
    resolution = dict(existing_resolution)
    timestamp = at or datetime.now(timezone.utc).isoformat()

    if resolution_status == RESOLUTION_OPEN:
        resolution["opened_at"] = timestamp
        resolution["opened_by"] = by
    elif resolution_status == RESOLUTION_CLIENT_NOTIFIED:
        resolution["client_notified_at"] = timestamp
        resolution["client_notified_by"] = by
        resolution["client_notified_note"] = note
    elif resolution_status == RESOLUTION_RESOLVED:
        resolution["resolved_at"] = timestamp
        resolution["resolved_by"] = by
        resolution["resolved_note"] = note
    elif resolution_status == RESOLUTION_DISMISSED:
        resolution["dismissed_at"] = timestamp
        resolution["dismissed_by"] = by
        resolution["dismissed_note"] = note
    resolution["status"] = resolution_status

    write_json_object(path, {**payload, "resolution": resolution})
    return {"candidate_id": candidate_id, "resolution_status": resolution_status, "changed": True}
