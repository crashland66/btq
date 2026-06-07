from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import get_config
from field_capture.action_candidates import default_candidate_dir, default_semantic_dir
from field_capture.approved_job_drafts import default_draft_dir
from field_capture.draft_staging import default_status_dir
from processing_core.approved_job_drafts import DRAFT_STATUS_APPROVED, DRAFT_STATUS_FAILED
from processing_core.artifacts import resolve_within_root
from processing_core.review_status import iter_json_artifacts, path_exists, processed_index_records, queue_job_records


CANDIDATE_STATUSES = ("pending_review", "approved", "rejected", "failed")
STAGING_STATUSES = ("staged", "skipped", "failed")


def default_runtime_root() -> Path:
    return get_config().runtime_root


def review_status_report(
    *,
    runtime_root: Path,
    semantic_dir: Path | None = None,
    candidate_dir: Path | None = None,
    draft_dir: Path | None = None,
    staging_dir: Path | None = None,
    queue_dir: Path | None = None,
    processed_dir: Path | None = None,
    failed_dir: Path | None = None,
) -> dict[str, object]:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    roots = {
        "semantic_dir": (semantic_dir or default_semantic_dir(runtime_resolved)).expanduser().resolve(strict=False),
        "candidate_dir": (candidate_dir or default_candidate_dir(runtime_resolved)).expanduser().resolve(strict=False),
        "draft_dir": (draft_dir or default_draft_dir(runtime_resolved)).expanduser().resolve(strict=False),
        "staging_dir": (staging_dir or default_status_dir(runtime_resolved)).expanduser().resolve(strict=False),
        "queue_dir": (queue_dir or runtime_resolved / "queue").expanduser().resolve(strict=False),
        "processed_dir": (processed_dir or runtime_resolved / "processed").expanduser().resolve(strict=False),
        "failed_dir": (failed_dir or runtime_resolved / "failed").expanduser().resolve(strict=False),
    }
    for root in roots.values():
        resolve_within_root(root, runtime_resolved)

    semantics = dict(iter_json_artifacts(roots["semantic_dir"]))
    candidates = dict(iter_json_artifacts(roots["candidate_dir"]))
    drafts = dict(iter_json_artifacts(roots["draft_dir"]))
    staging_results = dict(iter_json_artifacts(roots["staging_dir"]))
    queue_records = queue_job_records(roots["queue_dir"], "queue")
    processed_records = queue_job_records(roots["processed_dir"], "processed")
    failed_records = queue_job_records(roots["failed_dir"], "failed")
    index_records, index_error = processed_index_records(runtime_resolved)

    candidate_by_id = {str(payload.get("candidate_id")): (path, payload) for path, payload in candidates.items() if payload.get("candidate_id")}
    draft_by_id = {str(payload.get("draft_id")): (path, payload) for path, payload in drafts.items() if payload.get("draft_id")}
    drafts_by_candidate: dict[str, list[Path]] = {}
    for path, payload in drafts.items():
        candidate_id = str(payload.get("candidate_id") or "")
        if candidate_id:
            drafts_by_candidate.setdefault(candidate_id, []).append(path)
    staging_by_draft = {str(payload.get("draft_id")): (path, payload) for path, payload in staging_results.items() if payload.get("draft_id")}

    queue_by_draft = records_by_draft(queue_records)
    processed_by_draft = records_by_draft(processed_records)
    failed_by_draft = records_by_draft(failed_records)
    index_job_ids = {str(record.get("computed_job_id")) for record in index_records if record.get("computed_job_id")}
    unresolved_failed_records = current_failed_records(
        failed_records,
        processed_records=processed_records,
        index_job_ids=index_job_ids,
    )
    replayed_failed_records = [
        record
        for record in failed_records
        if not record_is_current_failure(record, processed_records=processed_records, index_job_ids=index_job_ids)
    ]

    counts = {
        "semantic_with_action_candidates": count_semantics_with_candidates(semantics.values()),
        "candidates_pending_review": count_status(candidates.values(), "pending_review"),
        "candidates_approved": count_status(candidates.values(), "approved"),
        "candidates_rejected": count_status(candidates.values(), "rejected"),
        "candidates_failed": count_status(candidates.values(), "failed"),
        "approved_job_drafts": count_status(drafts.values(), DRAFT_STATUS_APPROVED),
        "failed_job_drafts": count_status(drafts.values(), DRAFT_STATUS_FAILED),
        "staging_staged": count_status(staging_results.values(), "staged"),
        "staging_skipped": count_status(staging_results.values(), "skipped"),
        "staging_failed": count_status(staging_results.values(), "failed"),
        "staged_queue_jobs_runtime_queue": len(queue_records),
        "staged_jobs_processed": len(processed_records),
        "staged_jobs_failed": len(unresolved_failed_records),
        "staged_jobs_failed_unresolved": len(unresolved_failed_records),
        "staged_jobs_failed_replayed_successfully": len(replayed_failed_records),
        "staged_jobs_failed_archive_total": len(failed_records),
        "processed_index_records": len(index_records),
    }

    gaps = lineage_gaps(
        candidates=candidates,
        drafts=drafts,
        staging_results=staging_results,
        queue_records=queue_records,
        candidate_by_id=candidate_by_id,
        draft_by_id=draft_by_id,
        drafts_by_candidate=drafts_by_candidate,
        staging_by_draft=staging_by_draft,
        queue_by_draft=queue_by_draft,
        processed_by_draft=processed_by_draft,
        failed_by_draft=failed_by_draft,
        index_job_ids=index_job_ids,
    )
    if index_error is not None:
        gaps.append({"type": "processed_index_unreadable", "artifact_path": str(runtime_resolved / "processed_index.jsonl"), "detail": index_error})

    return {
        "channel": "field_capture",
        "runtime_root": str(runtime_resolved),
        "paths": {key: str(value) for key, value in roots.items()},
        "counts": counts,
        "lineage_gaps": gaps,
    }


def records_by_draft(records: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    by_draft: dict[str, list[dict[str, object]]] = {}
    for record in records:
        draft_id = str(record.get("draft_id") or "")
        if draft_id:
            by_draft.setdefault(draft_id, []).append(record)
    return by_draft


def record_is_current_failure(
    failed_record: dict[str, object],
    *,
    processed_records: list[dict[str, object]],
    index_job_ids: set[str],
) -> bool:
    draft_id = str(failed_record.get("draft_id") or "")
    computed_job_id = str(failed_record.get("computed_job_id") or "")
    for record in processed_records:
        if draft_id and draft_id == str(record.get("draft_id") or ""):
            return False
        if computed_job_id and computed_job_id == str(record.get("computed_job_id") or ""):
            return False
    if computed_job_id and computed_job_id in index_job_ids:
        return False
    return True


def current_failed_records(
    failed_records: list[dict[str, object]],
    *,
    processed_records: list[dict[str, object]],
    index_job_ids: set[str],
) -> list[dict[str, object]]:
    return [
        record
        for record in failed_records
        if record_is_current_failure(record, processed_records=processed_records, index_job_ids=index_job_ids)
    ]


def count_status(payloads: object, status: str) -> int:
    return sum(1 for payload in payloads if isinstance(payload, dict) and payload.get("status") == status)


def count_semantics_with_candidates(payloads: object) -> int:
    count = 0
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        candidates = payload.get("action_candidates")
        if payload.get("status") == "complete" and isinstance(candidates, list) and candidates:
            count += 1
    return count


def lineage_gaps(
    *,
    candidates: dict[Path, dict[str, object]],
    drafts: dict[Path, dict[str, object]],
    staging_results: dict[Path, dict[str, object]],
    queue_records: list[dict[str, object]],
    candidate_by_id: dict[str, tuple[Path, dict[str, object]]],
    draft_by_id: dict[str, tuple[Path, dict[str, object]]],
    drafts_by_candidate: dict[str, list[Path]],
    staging_by_draft: dict[str, tuple[Path, dict[str, object]]],
    queue_by_draft: dict[str, list[dict[str, object]]],
    processed_by_draft: dict[str, list[dict[str, object]]],
    failed_by_draft: dict[str, list[dict[str, object]]],
    index_job_ids: set[str],
) -> list[dict[str, object]]:
    gaps: list[dict[str, object]] = []
    for path, candidate in candidates.items():
        provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
        semantic_path = provenance.get("semantic_artifact_path")
        if semantic_path and not path_exists(semantic_path):
            gaps.append(gap("candidate_missing_semantic", path, missing_path=str(semantic_path), candidate_id=str(candidate.get("candidate_id") or "")))
        candidate_id = str(candidate.get("candidate_id") or "")
        if candidate.get("status") == "approved" and candidate_id not in drafts_by_candidate:
            gaps.append(gap("approved_candidate_missing_draft", path, candidate_id=candidate_id))

    for path, draft in drafts.items():
        candidate_path = draft.get("candidate_artifact_path")
        candidate_id = str(draft.get("candidate_id") or "")
        if candidate_path and not path_exists(candidate_path):
            gaps.append(gap("draft_missing_candidate_artifact", path, missing_path=str(candidate_path), candidate_id=candidate_id, draft_id=str(draft.get("draft_id") or "")))
        elif candidate_id and candidate_id not in candidate_by_id:
            gaps.append(gap("draft_missing_candidate_id", path, candidate_id=candidate_id, draft_id=str(draft.get("draft_id") or "")))
        draft_id = str(draft.get("draft_id") or "")
        if draft.get("status") == DRAFT_STATUS_APPROVED and draft_id not in staging_by_draft:
            gaps.append(gap("approved_draft_missing_staging_status", path, draft_id=draft_id, candidate_id=candidate_id))

    for path, staging in staging_results.items():
        draft_path = staging.get("draft_artifact_path")
        draft_id = str(staging.get("draft_id") or "")
        if draft_path and not path_exists(draft_path):
            gaps.append(gap("staging_missing_draft_artifact", path, missing_path=str(draft_path), draft_id=draft_id))
        elif draft_id and draft_id not in draft_by_id:
            gaps.append(gap("staging_missing_draft_id", path, draft_id=draft_id))
        if staging.get("status") == "staged":
            computed_job_id = str(staging.get("computed_job_id") or "")
            has_artifact = bool(queue_by_draft.get(draft_id) or processed_by_draft.get(draft_id) or failed_by_draft.get(draft_id))
            has_index = bool(computed_job_id and computed_job_id in index_job_ids)
            if not has_artifact and not has_index:
                gaps.append(gap("staged_draft_missing_queue_processed_failed_evidence", path, draft_id=draft_id, computed_job_id=computed_job_id))

    for record in queue_records:
        draft_path = record.get("draft_artifact_path")
        draft_id = str(record.get("draft_id") or "")
        if draft_path and not path_exists(draft_path):
            gaps.append(gap("queue_file_missing_draft_artifact", Path(str(record.get("path"))), missing_path=str(draft_path), draft_id=draft_id))
        elif draft_id and draft_id not in draft_by_id:
            gaps.append(gap("queue_file_missing_draft_id", Path(str(record.get("path"))), draft_id=draft_id))
    return gaps


def gap(kind: str, artifact_path: Path, **fields: object) -> dict[str, object]:
    payload = {"type": kind, "artifact_path": str(artifact_path)}
    payload.update(fields)
    return payload


def format_report(report: dict[str, object]) -> str:
    counts = report["counts"]
    gaps = report["lineage_gaps"]
    lines = [f"field-capture review status runtime={report['runtime_root']}", ""]
    for key in sorted(counts):
        lines.append(f"{key}: {counts[key]}")
    lines.append("")
    lines.append(f"lineage_gaps: {len(gaps)}")
    for item in gaps:
        detail = item.get("missing_path") or item.get("draft_id") or item.get("candidate_id") or ""
        lines.append(f"- {item['type']}: {item['artifact_path']} {detail}".rstrip())
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Report field-capture review pipeline status.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--semantic-dir", type=Path)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--draft-dir", type=Path)
    parser.add_argument("--staging-dir", type=Path)
    parser.add_argument("--queue-dir", type=Path)
    parser.add_argument("--processed-dir", type=Path)
    parser.add_argument("--failed-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = review_status_report(
        runtime_root=args.runtime_root.expanduser(),
        semantic_dir=args.semantic_dir.expanduser() if args.semantic_dir is not None else None,
        candidate_dir=args.candidate_dir.expanduser() if args.candidate_dir is not None else None,
        draft_dir=args.draft_dir.expanduser() if args.draft_dir is not None else None,
        staging_dir=args.staging_dir.expanduser() if args.staging_dir is not None else None,
        queue_dir=args.queue_dir.expanduser() if args.queue_dir is not None else None,
        processed_dir=args.processed_dir.expanduser() if args.processed_dir is not None else None,
        failed_dir=args.failed_dir.expanduser() if args.failed_dir is not None else None,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_report(report))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
