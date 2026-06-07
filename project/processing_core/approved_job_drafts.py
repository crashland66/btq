from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from processing_core.action_candidates import STATUS_APPROVED
from processing_core.artifacts import write_json_object
from processing_core.ids import deterministic_artifact_id
from processing_core.json import canonical_json


DRAFT_STATUS_APPROVED = "approved_draft"
DRAFT_STATUS_FAILED = "failed"


def approved_job_draft_id(
    *,
    candidate_id: str,
    proposed_job_type: str,
    proposed_payload: dict[str, object],
) -> str:
    return deterministic_artifact_id(
        "ajd",
        canonical_json(
            {
                "candidate_id": candidate_id,
                "proposed_job_type": proposed_job_type,
                "proposed_payload": proposed_payload,
            }
        ),
    )


def approved_job_draft_path(draft_dir: Path, draft_id: str) -> Path:
    return draft_dir / f"{draft_id}.json"


def approved_job_draft_payload(
    *,
    candidate: dict[str, object],
    candidate_artifact_path: str,
    proposed_job_type: object,
    proposed_payload: object,
    rationale: object = "",
    confidence: object = "unknown",
    reviewer: object = "",
    approved_at: object = "",
    approval_metadata: dict[str, object] | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    candidate_id = str(candidate.get("candidate_id") or "")
    job_type_text = proposed_job_type if isinstance(proposed_job_type, str) else ""
    payload = proposed_payload if isinstance(proposed_payload, dict) else {}
    provenance = dict(candidate.get("provenance") or {})
    source_context = str(candidate.get("source_context") or "")
    error = draft_failure_reason(candidate, proposed_job_type, proposed_payload)
    status = DRAFT_STATUS_FAILED if error is not None else DRAFT_STATUS_APPROVED
    draft_id = approved_job_draft_id(
        candidate_id=candidate_id,
        proposed_job_type=job_type_text.strip(),
        proposed_payload=payload,
    )

    return {
        "type": "approved_queue_job_draft",
        "draft_id": draft_id,
        "status": status,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "candidate_id": candidate_id,
        "candidate_artifact_path": candidate_artifact_path,
        "provenance": {
            **provenance,
            "candidate_artifact_path": candidate_artifact_path,
        },
        "proposed_job_type": job_type_text.strip(),
        "proposed_payload": payload,
        "rationale": str(rationale or candidate.get("rationale") or ""),
        "confidence": confidence if isinstance(confidence, (str, int, float, bool)) or confidence is None else str(confidence),
        "reviewer": str(reviewer or candidate.get("reviewer") or ""),
        "approved_at": str(approved_at or candidate.get("approved_at") or ""),
        "approval_metadata": dict(approval_metadata or candidate.get("approval_metadata") or {}),
        "source_context": source_context,
        "error": None if error is None else {"type": "ValueError", "message": error},
    }


def draft_failure_reason(candidate: dict[str, object], proposed_job_type: object, proposed_payload: object) -> str | None:
    if candidate.get("status") != STATUS_APPROVED:
        return f"candidate status must be approved, got {candidate.get('status')}"
    if not str(candidate.get("candidate_id") or "").strip():
        return "candidate_id is required"
    if not isinstance(proposed_job_type, str) or not proposed_job_type.strip():
        return "proposed_job_type is required"
    if not isinstance(proposed_payload, dict) or not proposed_payload:
        return "proposed_payload is required"
    return None


def write_approved_job_draft(draft_dir: Path, payload: dict[str, object]) -> Path:
    draft_id = str(payload.get("draft_id") or "")
    if not draft_id:
        raise ValueError("draft_id is required")
    path = approved_job_draft_path(draft_dir, draft_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_object(path, payload)
    return path
