from __future__ import annotations

from pathlib import Path

from event_pipeline import couchdb_config
from field_capture import approved_job_drafts
from field_capture import action_candidates as field_action_candidates
from field_capture import draft_staging
from processing_core.action_candidates import action_candidate_review_path
from processing_core.artifacts import read_json_object, write_json_object


def latest_draft_ids_for_candidate(runtime_root: Path, candidate_id: str) -> list[str]:
    draft_dir = approved_job_drafts.default_draft_dir(runtime_root)
    if not draft_dir.exists():
        return []
    matches: list[tuple[float, str]] = []
    for path in sorted(draft_dir.glob("*.json")):
        payload = read_json_object(path)
        if not payload or str(payload.get("candidate_id") or "") != candidate_id:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        matches.append((mtime, str(payload.get("draft_id") or path.stem)))
    matches.sort()
    return [draft_id for _mtime, draft_id in matches]


def materialize_candidate_for_direct_staging(runtime_root: Path, candidate_id: str) -> Path | None:
    config = field_action_candidates.couchdb_candidate_config_or_none()
    if config is None:
        return None
    doc = field_action_candidates.get_action_candidate(
        config,
        couchdb_config.field_captures_database(),
        candidate_id,
    )
    if doc is None:
        return None
    candidate_dir = field_action_candidates.default_candidate_dir(runtime_root)
    candidate_path = action_candidate_review_path(candidate_dir, candidate_id)
    payload = field_action_candidates.couchdb_action_candidate_to_review_payload(doc)
    field_action_candidates.require_candidate_human_text_basis(
        payload,
        context=f"direct staging materialization candidate_id={candidate_id}",
    )
    payload["status"] = "approved"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    write_json_object(candidate_path, payload)
    return candidate_path


def approved_candidate_for_staging(runtime_root: Path, candidate_id: str) -> dict[str, object] | None:
    candidate_dir = field_action_candidates.default_candidate_dir(runtime_root)
    for _path, candidate in field_action_candidates.find_candidate_artifacts(candidate_dir, candidate_id):
        if candidate.get("type") == "action_candidate_review" and candidate.get("status") == "approved":
            return candidate
    return None


def stage_candidate_job_after_approval(runtime_root: Path, candidate_id: str) -> str:
    candidate_dir = field_action_candidates.default_candidate_dir(runtime_root)
    draft_dir = approved_job_drafts.default_draft_dir(runtime_root)
    materialize_candidate_for_direct_staging(runtime_root, candidate_id)
    approved_candidate = approved_candidate_for_staging(runtime_root, candidate_id)
    if approved_candidate is not None:
        field_action_candidates.require_candidate_human_text_basis(
            approved_candidate,
            context=f"approved staging candidate_id={candidate_id}",
        )
    draft_counts = approved_job_drafts.create_approved_job_drafts(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        candidate_ids={candidate_id},
    )
    draft_ids = latest_draft_ids_for_candidate(runtime_root, candidate_id)
    if not draft_ids:
        if draft_counts.get("completed", 0) < 1:
            raise field_action_candidates.CandidateReviewError(f"approved candidate did not produce a queue draft: {draft_counts}")
        raise field_action_candidates.CandidateReviewError("approved candidate draft was created without a discoverable draft_id")
    staging_report = draft_staging.stage_field_capture_drafts_report(runtime_root=runtime_root, draft_ids=set(draft_ids), dry_run=False)
    results = staging_report.get("results") if isinstance(staging_report.get("results"), list) else []
    failed = [result for result in results if isinstance(result, dict) and result.get("status") == "failed"]
    if failed:
        raise field_action_candidates.CandidateReviewError(f"approved draft failed to stage: {failed[0].get('reason')}")
    staged_count = sum(1 for result in results if isinstance(result, dict) and result.get("status") == "staged")
    return f"draft_ids={','.join(draft_ids)} staged={staged_count}"
