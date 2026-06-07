from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from processing_core.approved_job_drafts import DRAFT_STATUS_APPROVED
from processing_core.artifacts import read_json_object, resolve_within_root, write_json_object
from processing_core.results import result_counts
from queue_processor.idempotency import compute_job_id
from queue_processor.processed_index import processed_job_id_exists
from queue_spec import validate_job


STAGING_STATUS_STAGED = "staged"
STAGING_STATUS_SKIPPED = "skipped"
STAGING_STATUS_FAILED = "failed"
STAGING_STATUS_WOULD_STAGE = "would_stage"


def iter_draft_artifacts(draft_dir: Path) -> Iterable[tuple[Path, dict[str, object]]]:
    root = draft_dir.expanduser().resolve(strict=False)
    for path in sorted(root.glob("*.json")):
        resolved = path.resolve(strict=False)
        try:
            resolve_within_root(resolved, root)
        except ValueError:
            continue
        payload = read_json_object(resolved)
        if payload is not None:
            yield resolved, payload


def queue_job_from_draft(draft: dict[str, object], draft_artifact_path: Path) -> dict[str, object]:
    proposed_job_type = draft.get("proposed_job_type")
    proposed_payload = draft.get("proposed_payload")
    draft_id = str(draft.get("draft_id") or "")
    candidate_id = str(draft.get("candidate_id") or "")
    provenance = draft.get("provenance") if isinstance(draft.get("provenance"), dict) else {}
    return {
        "job_id": draft_id,
        "job_type": proposed_job_type,
        "payload": proposed_payload,
        "metadata": {
            "source": "approved_job_draft",
            "draft_id": draft_id,
            "candidate_id": candidate_id,
            "draft_artifact_path": str(draft_artifact_path),
            "candidate_artifact_path": str(draft.get("candidate_artifact_path") or provenance.get("candidate_artifact_path") or ""),
            "semantic_artifact_path": str(provenance.get("semantic_artifact_path") or ""),
            "source_transcript_path": str(provenance.get("source_transcript_path") or ""),
            "approval_metadata": dict(draft.get("approval_metadata") or {}),
        },
    }


def queue_filename_for(draft_id: str, computed_job_id: str) -> str:
    return f"{draft_id}__{computed_job_id[:16]}.json"


def staging_result_path(status_dir: Path, draft_id: str) -> Path:
    return status_dir / f"{draft_id}.json"


def stage_queue_path(queue_dir: Path, draft_id: str, computed_job_id: str) -> Path:
    return queue_dir / queue_filename_for(draft_id, computed_job_id)


def find_metadata_match(root: Path, draft_id: str, computed_job_id: str) -> Path | None:
    if not root.exists():
        return None
    for path in sorted(candidate for candidate in root.glob("*.json") if candidate.is_file()):
        payload = read_json_object(path)
        if not payload:
            continue
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("draft_id") == draft_id:
            return path
        try:
            if compute_job_id(payload) == computed_job_id:
                return path
        except Exception:
            continue
    return None


def duplicate_reason(runtime_root: Path, queue_dir: Path, draft_id: str, computed_job_id: str) -> tuple[str | None, str]:
    for label, root in (
        ("queue", queue_dir),
        ("processed", runtime_root / "processed"),
        ("failed", runtime_root / "failed"),
    ):
        match = find_metadata_match(root, draft_id, computed_job_id)
        if match is not None:
            return f"already present in runtime/{label}", str(match)
    processed_exists, source = processed_job_id_exists(runtime_root, runtime_root / "processed", computed_job_id)
    if processed_exists:
        return f"computed job already processed ({source})", ""
    return None, ""


def staging_result_payload(
    *,
    draft: dict[str, object],
    draft_artifact_path: Path,
    status: str,
    reason: str,
    queue_path: Path | None = None,
    job: dict[str, object] | None = None,
    computed_job_id: str = "",
    created_at: str | None = None,
) -> dict[str, object]:
    return {
        "type": "approved_draft_staging_result",
        "draft_id": str(draft.get("draft_id") or ""),
        "candidate_id": str(draft.get("candidate_id") or ""),
        "status": status,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "draft_artifact_path": str(draft_artifact_path),
        "queue_path": "" if queue_path is None else str(queue_path),
        "computed_job_id": computed_job_id,
        "job": job,
    }


def write_staging_result(status_dir: Path, payload: dict[str, object]) -> Path:
    draft_id = str(payload.get("draft_id") or "")
    if not draft_id:
        raise ValueError("draft_id is required")
    path = staging_result_path(status_dir, draft_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_object(path, payload)
    return path


def stage_draft(
    draft_artifact_path: Path,
    draft: dict[str, object],
    *,
    runtime_root: Path,
    queue_dir: Path,
    status_dir: Path,
) -> dict[str, object]:
    draft_id = str(draft.get("draft_id") or "")
    if draft.get("type") != "approved_queue_job_draft":
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_FAILED,
            reason="not an approved queue-job draft artifact",
        )
    if draft.get("status") != DRAFT_STATUS_APPROVED:
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_SKIPPED,
            reason=f"draft status is not approved: {draft.get('status')}",
        )
    if not draft_id:
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_FAILED,
            reason="draft_id is required",
        )

    existing_status = read_json_object(staging_result_path(status_dir, draft_id))
    if existing_status and existing_status.get("status") == STAGING_STATUS_STAGED:
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_SKIPPED,
            reason="draft already staged by staging status artifact",
            queue_path=Path(str(existing_status.get("queue_path") or "")) if existing_status.get("queue_path") else None,
            computed_job_id=str(existing_status.get("computed_job_id") or ""),
        )

    job = queue_job_from_draft(draft, draft_artifact_path)
    if not validate_job(job):
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_FAILED,
            reason="proposed queue job does not match queue_spec",
            job=job,
        )

    computed_job_id = compute_job_id(job)
    duplicate, duplicate_path = duplicate_reason(runtime_root, queue_dir, draft_id, computed_job_id)
    if duplicate is not None:
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_SKIPPED,
            reason=duplicate,
            queue_path=Path(duplicate_path) if duplicate_path else None,
            job=job,
            computed_job_id=computed_job_id,
        )

    queue_path = stage_queue_path(queue_dir, draft_id, computed_job_id)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_object(queue_path, job)
    return staging_result_payload(
        draft=draft,
        draft_artifact_path=draft_artifact_path,
        status=STAGING_STATUS_STAGED,
        reason="staged approved draft into runtime queue",
        queue_path=queue_path,
        job=job,
        computed_job_id=computed_job_id,
    )


def plan_stage_draft(
    draft_artifact_path: Path,
    draft: dict[str, object],
    *,
    runtime_root: Path,
    queue_dir: Path,
    status_dir: Path,
) -> dict[str, object]:
    draft_id = str(draft.get("draft_id") or "")
    if draft.get("type") != "approved_queue_job_draft":
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_FAILED,
            reason="not an approved queue-job draft artifact",
        )
    if draft.get("status") != DRAFT_STATUS_APPROVED:
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_SKIPPED,
            reason=f"draft status is not approved: {draft.get('status')}",
        )
    if not draft_id:
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_FAILED,
            reason="draft_id is required",
        )

    existing_status = read_json_object(staging_result_path(status_dir, draft_id))
    if existing_status and existing_status.get("status") == STAGING_STATUS_STAGED:
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_SKIPPED,
            reason="draft already staged by staging status artifact",
            queue_path=Path(str(existing_status.get("queue_path") or "")) if existing_status.get("queue_path") else None,
            computed_job_id=str(existing_status.get("computed_job_id") or ""),
        )

    job = queue_job_from_draft(draft, draft_artifact_path)
    if not validate_job(job):
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_FAILED,
            reason="proposed queue job does not match queue_spec",
            job=job,
        )

    computed_job_id = compute_job_id(job)
    duplicate, duplicate_path = duplicate_reason(runtime_root, queue_dir, draft_id, computed_job_id)
    if duplicate is not None:
        return staging_result_payload(
            draft=draft,
            draft_artifact_path=draft_artifact_path,
            status=STAGING_STATUS_SKIPPED,
            reason=duplicate,
            queue_path=Path(duplicate_path) if duplicate_path else None,
            job=job,
            computed_job_id=computed_job_id,
        )

    queue_path = stage_queue_path(queue_dir, draft_id, computed_job_id)
    return staging_result_payload(
        draft=draft,
        draft_artifact_path=draft_artifact_path,
        status=STAGING_STATUS_WOULD_STAGE,
        reason="would stage approved draft into runtime queue",
        queue_path=queue_path,
        job=job,
        computed_job_id=computed_job_id,
    )


def stage_approved_drafts(
    draft_dir: Path,
    *,
    runtime_root: Path,
    queue_dir: Path | None = None,
    status_dir: Path | None = None,
    draft_ids: Iterable[str] | None = None,
) -> dict[str, int]:
    counts = result_counts("discovered", "skipped", "completed", "failed")
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    draft_root = draft_dir.expanduser().resolve(strict=False)
    queue_root = (queue_dir or runtime_resolved / "queue").expanduser().resolve(strict=False)
    status_root = (status_dir or runtime_resolved / "reviews" / "staging").expanduser().resolve(strict=False)
    resolve_within_root(draft_root, runtime_resolved)
    resolve_within_root(queue_root, runtime_resolved)
    resolve_within_root(status_root, runtime_resolved)
    draft_filter = {str(item) for item in draft_ids or [] if str(item)}

    for draft_path, draft in iter_draft_artifacts(draft_root):
        counts["discovered"] += 1
        if draft_filter and str(draft.get("draft_id") or "") not in draft_filter:
            counts["skipped"] += 1
            continue
        result = stage_draft(
            draft_path,
            draft,
            runtime_root=runtime_resolved,
            queue_dir=queue_root,
            status_dir=status_root,
        )
        write_staging_result(status_root, result)
        if result["status"] == STAGING_STATUS_STAGED:
            counts["completed"] += 1
        elif result["status"] == STAGING_STATUS_FAILED:
            counts["failed"] += 1
        else:
            counts["skipped"] += 1
    return counts


def stage_approved_drafts_report(
    draft_dir: Path,
    *,
    runtime_root: Path,
    queue_dir: Path | None = None,
    status_dir: Path | None = None,
    dry_run: bool = False,
    draft_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    counts = result_counts("discovered", "skipped", "completed", "failed")
    results: list[dict[str, object]] = []
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    draft_root = draft_dir.expanduser().resolve(strict=False)
    queue_root = (queue_dir or runtime_resolved / "queue").expanduser().resolve(strict=False)
    status_root = (status_dir or runtime_resolved / "reviews" / "staging").expanduser().resolve(strict=False)
    resolve_within_root(draft_root, runtime_resolved)
    resolve_within_root(queue_root, runtime_resolved)
    resolve_within_root(status_root, runtime_resolved)
    draft_filter = {str(item) for item in draft_ids or [] if str(item)}

    for draft_path, draft in iter_draft_artifacts(draft_root):
        counts["discovered"] += 1
        if draft_filter and str(draft.get("draft_id") or "") not in draft_filter:
            counts["skipped"] += 1
            continue
        result = plan_stage_draft(
            draft_path,
            draft,
            runtime_root=runtime_resolved,
            queue_dir=queue_root,
            status_dir=status_root,
        ) if dry_run else stage_draft(
            draft_path,
            draft,
            runtime_root=runtime_resolved,
            queue_dir=queue_root,
            status_dir=status_root,
        )
        if not dry_run:
            write_staging_result(status_root, result)
        results.append(result)
        if result["status"] in {STAGING_STATUS_STAGED, STAGING_STATUS_WOULD_STAGE}:
            counts["completed"] += 1
        elif result["status"] == STAGING_STATUS_FAILED:
            counts["failed"] += 1
        else:
            counts["skipped"] += 1
    return {"dry_run": dry_run, "counts": counts, "results": results}
