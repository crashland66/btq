from __future__ import annotations

from pathlib import Path

from field_capture import approved_job_drafts
from field_capture import action_candidates as field_action_candidates
from field_capture import draft_staging
from processing_core.artifacts import read_json_object


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


def stage_candidate_job_after_approval(runtime_root: Path, candidate_id: str) -> str:
    candidate_dir = field_action_candidates.default_candidate_dir(runtime_root)
    draft_dir = approved_job_drafts.default_draft_dir(runtime_root)
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
