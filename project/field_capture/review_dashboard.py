from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import get_config
from field_capture.action_candidates import default_candidate_dir, list_action_candidates_report
from field_capture.approved_job_drafts import default_draft_dir, list_approved_drafts_report
from field_capture.review_maintenance import review_maintenance_status_report
from field_capture.review_status import review_status_report


# The active review pipeline is job_draft-based: the field-capture pipeline emits
# CouchDB `job_draft` records that operators triage at /swipe or /candidates, then
# `./scripts/job-draft-queue-watch` materializes approvals into the queue. The
# action-candidate CLI (collect/list/generate) is retired (prompt 370), so these
# suggestions point at the live surfaces instead.
EMIT_JOB_DRAFTS_COMMAND = "./scripts/btq watch-field-capture-pipeline --once --json"
REVIEW_JOB_DRAFTS_GUIDANCE = "Review pending job_draft records at /swipe or /candidates on the ops dashboard."


def default_runtime_root() -> Path:
    return get_config().runtime_root


def queue_state_counts(drafts: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for draft in drafts:
        state = "unknown"
        queue_state = draft.get("queue_state")
        if isinstance(queue_state, dict):
            state = str(queue_state.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return counts


def preview_candidate(item: dict[str, object]) -> dict[str, object]:
    return {
        "candidate_id": item.get("candidate_id"),
        "status": item.get("status"),
        "summary": item.get("summary"),
        "confidence": item.get("confidence"),
        "artifact_path": item.get("artifact_path"),
    }


def preview_draft(item: dict[str, object]) -> dict[str, object]:
    queue_state = item.get("queue_state") if isinstance(item.get("queue_state"), dict) else {}
    return {
        "draft_id": item.get("draft_id"),
        "status": item.get("status"),
        "candidate_id": item.get("candidate_id"),
        "proposed_job_type": item.get("proposed_job_type"),
        "queue_state": queue_state.get("state", "unknown"),
        "artifact_path": item.get("artifact_path"),
    }


def suggested_next_command(
    *,
    review_status: dict[str, object],
    candidate_report: dict[str, object],
    draft_report: dict[str, object],
    maintenance_report: dict[str, object],
) -> str:
    status_counts = review_status.get("counts") if isinstance(review_status.get("counts"), dict) else {}
    candidate_counts = candidate_report.get("counts") if isinstance(candidate_report.get("counts"), dict) else {}
    maintenance_counts = maintenance_report.get("counts") if isinstance(maintenance_report.get("counts"), dict) else {}
    draft_states = queue_state_counts(draft_report.get("drafts", []))

    if int(candidate_counts.get("total", 0)) == 0 and int(status_counts.get("semantic_with_action_candidates", 0)) > 0:
        return EMIT_JOB_DRAFTS_COMMAND
    if int(candidate_counts.get("pending_review", 0)) > 0:
        return REVIEW_JOB_DRAFTS_GUIDANCE
    if int(maintenance_counts.get("approved_candidates_without_draft", 0)) > 0:
        return REVIEW_JOB_DRAFTS_GUIDANCE
    if int(draft_states.get("not_staged", 0)) > 0 or int(maintenance_counts.get("approved_drafts_without_staging_status", 0)) > 0:
        return "./scripts/btq stage-approved-drafts --channel field_capture --dry-run"
    if int(maintenance_counts.get("candidates_failed", 0)) > 0 or int(maintenance_counts.get("drafts_failed", 0)) > 0 or len(maintenance_report.get("findings", [])) > 0:
        return "./scripts/btq review-maintenance-status --channel field_capture"
    if int(status_counts.get("staged_queue_jobs_runtime_queue", 0)) > 0:
        return "Queue jobs are staged; run the deterministic queue processor deliberately when ready."
    return "./scripts/btq review-status --channel field_capture"


def review_dashboard_report(
    *,
    runtime_root: Path,
    stale_days: int = 14,
    limit: int = 5,
    include_paths: bool = False,
) -> dict[str, object]:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    candidate_dir = default_candidate_dir(runtime_resolved)
    draft_dir = default_draft_dir(runtime_resolved)
    review_status = review_status_report(runtime_root=runtime_resolved)
    candidate_report = list_action_candidates_report(candidate_dir, runtime_root=runtime_resolved)
    pending_report = list_action_candidates_report(candidate_dir, status="pending_review", limit=limit, runtime_root=runtime_resolved)
    draft_report = list_approved_drafts_report(draft_dir, runtime_root=runtime_resolved)
    approved_draft_report = list_approved_drafts_report(draft_dir, status="approved_draft", limit=limit, runtime_root=runtime_resolved)
    maintenance_report = review_maintenance_status_report(runtime_root=runtime_resolved, stale_days=stale_days, include_paths=include_paths)

    candidate_preview = [preview_candidate(item) for item in pending_report["candidates"]]
    draft_preview = [preview_draft(item) for item in approved_draft_report["drafts"]]
    draft_summary = {
        "approved_draft": draft_report["counts"].get("approved_draft", 0),
        "failed_draft": draft_report["counts"].get("failed_draft", 0),
        "queue_state_counts": queue_state_counts(draft_report["drafts"]),
        "failed_unresolved": review_status["counts"].get("staged_jobs_failed_unresolved", 0),
        "failed_replayed_successfully": review_status["counts"].get("staged_jobs_failed_replayed_successfully", 0),
        "failed_archive_total": review_status["counts"].get("staged_jobs_failed_archive_total", 0),
    }
    maintenance_summary = {
        "finding_count": len(maintenance_report["findings"]),
        "stale_pending_candidates": maintenance_report["counts"].get("stale_pending_candidates", 0),
        "orphan_or_lineage_issue_count": sum(
            1
            for item in maintenance_report["findings"]
            if item.get("type")
            in {
                "approved_candidate_missing_draft",
                "approved_draft_missing_staging_status",
                "staged_draft_missing_queue_processed_failed_evidence",
                "orphaned_staging_status",
                "queue_file_missing_draft",
                "processed_index_unreadable",
            }
        ),
        "disk_usage": maintenance_report["disk_usage"],
        "findings": maintenance_report["findings"],
    }
    report = {
        "channel": "field_capture",
        "runtime_root": str(runtime_resolved),
        "review_status": review_status,
        "candidate_summary": {
            "pending_review": candidate_report["counts"].get("pending_review", 0),
            "approved": candidate_report["counts"].get("approved", 0),
            "rejected": candidate_report["counts"].get("rejected", 0),
            "failed": candidate_report["counts"].get("failed", 0),
        },
        "candidate_preview": candidate_preview,
        "draft_summary": draft_summary,
        "draft_preview": draft_preview,
        "maintenance": maintenance_summary,
        "suggested_next_command": "",
    }
    report["suggested_next_command"] = suggested_next_command(
        review_status=review_status,
        candidate_report=candidate_report,
        draft_report=draft_report,
        maintenance_report=maintenance_report,
    )
    return report


def format_report(report: dict[str, object]) -> str:
    review_counts = report["review_status"]["counts"]
    candidates = report["candidate_summary"]
    drafts = report["draft_summary"]
    maintenance = report["maintenance"]
    lines = [f"field-capture review dashboard runtime={report['runtime_root']}", ""]
    lines.append("Overall:")
    lines.append(
        f"semantic_with_action_candidates={review_counts['semantic_with_action_candidates']} "
        f"staged_queue={review_counts['staged_queue_jobs_runtime_queue']} "
        f"processed={review_counts['staged_jobs_processed']} "
        f"failed_unresolved={review_counts['staged_jobs_failed_unresolved']} "
        f"failed_replayed_successfully={review_counts['staged_jobs_failed_replayed_successfully']} "
        f"failed_archive_total={review_counts['staged_jobs_failed_archive_total']}"
    )
    lines.append("")
    lines.append("Candidates:")
    lines.append(
        f"pending_review={candidates['pending_review']} approved={candidates['approved']} "
        f"rejected={candidates['rejected']} failed={candidates['failed']}"
    )
    for item in report["candidate_preview"]:
        lines.append(f"- {item['candidate_id']} [{item['status']}] {item['summary']} confidence={item['confidence']}")
    lines.append("")
    lines.append("Drafts:")
    lines.append(f"approved_draft={drafts['approved_draft']} failed_draft={drafts['failed_draft']}")
    queue_states = drafts["queue_state_counts"]
    if queue_states:
        lines.append("queue_states: " + ", ".join(f"{key}={queue_states[key]}" for key in sorted(queue_states)))
    for item in report["draft_preview"]:
        lines.append(
            f"- {item['draft_id']} [{item['status']}] candidate={item['candidate_id']} "
            f"job_type={item['proposed_job_type']} queue_state={item['queue_state']}"
        )
    lines.append("")
    lines.append("Maintenance:")
    lines.append(
        f"findings={maintenance['finding_count']} stale_pending={maintenance['stale_pending_candidates']} "
        f"orphan_or_lineage={maintenance['orphan_or_lineage_issue_count']}"
    )
    for key, usage in maintenance["disk_usage"].items():
        lines.append(f"- {key}: {usage['bytes']} bytes across {usage['files']} files")
    lines.append("")
    lines.append(f"Suggested next command: {report['suggested_next_command']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Show a read-only field-capture review dashboard.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--stale-days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--include-paths", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = review_dashboard_report(
        runtime_root=args.runtime_root.expanduser(),
        stale_days=args.stale_days,
        limit=args.limit,
        include_paths=args.include_paths,
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
