from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import get_config
from field_capture.action_candidates import default_candidate_dir
from field_capture.approved_job_drafts import default_draft_dir
from field_capture.draft_staging import default_status_dir
from field_capture.review_status import records_by_draft
from processing_core.approved_job_drafts import DRAFT_STATUS_APPROVED, DRAFT_STATUS_FAILED
from processing_core.artifacts import resolve_within_root
from processing_core.review_status import iter_json_artifacts, processed_index_records, queue_job_records


def default_runtime_root() -> Path:
    return get_config().runtime_root


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def artifact_timestamp(path: Path, payload: dict[str, object]) -> datetime:
    for key in ("reviewed_at", "approved_at", "created_at"):
        parsed = parse_timestamp(payload.get(key))
        if parsed is not None:
            return parsed
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def maybe_path(path: Path, include_paths: bool) -> dict[str, object]:
    return {"artifact_path": str(path)} if include_paths else {}


def finding(kind: str, path: Path, include_paths: bool, **fields: object) -> dict[str, object]:
    payload = {"type": kind}
    payload.update(fields)
    payload.update(maybe_path(path, include_paths))
    return payload


def directory_usage(root: Path) -> dict[str, int]:
    total = 0
    files = 0
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                files += 1
                total += path.stat().st_size
    return {"bytes": total, "files": files}


def oldest_newest(root: Path) -> tuple[str, str]:
    mtimes = [path.stat().st_mtime for path in root.rglob("*") if path.is_file()] if root.exists() else []
    if not mtimes:
        return "", ""
    oldest = datetime.fromtimestamp(min(mtimes), tz=timezone.utc).isoformat()
    newest = datetime.fromtimestamp(max(mtimes), tz=timezone.utc).isoformat()
    return oldest, newest


def count_status(payloads: object, status: str) -> int:
    return sum(1 for payload in payloads if isinstance(payload, dict) and payload.get("status") == status)


def review_maintenance_status_report(
    *,
    runtime_root: Path,
    candidate_dir: Path | None = None,
    draft_dir: Path | None = None,
    staging_dir: Path | None = None,
    queue_dir: Path | None = None,
    processed_dir: Path | None = None,
    failed_dir: Path | None = None,
    stale_days: int = 14,
    include_paths: bool = False,
) -> dict[str, object]:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    roots = {
        "candidate_dir": (candidate_dir or default_candidate_dir(runtime_resolved)).expanduser().resolve(strict=False),
        "draft_dir": (draft_dir or default_draft_dir(runtime_resolved)).expanduser().resolve(strict=False),
        "staging_dir": (staging_dir or default_status_dir(runtime_resolved)).expanduser().resolve(strict=False),
        "queue_dir": (queue_dir or runtime_resolved / "queue").expanduser().resolve(strict=False),
        "processed_dir": (processed_dir or runtime_resolved / "processed").expanduser().resolve(strict=False),
        "failed_dir": (failed_dir or runtime_resolved / "failed").expanduser().resolve(strict=False),
    }
    for root in roots.values():
        resolve_within_root(root, runtime_resolved)

    candidates = dict(iter_json_artifacts(roots["candidate_dir"]))
    drafts = dict(iter_json_artifacts(roots["draft_dir"]))
    staging_results = dict(iter_json_artifacts(roots["staging_dir"]))
    queue_records = queue_job_records(roots["queue_dir"], "queue")
    processed_records = queue_job_records(roots["processed_dir"], "processed")
    failed_records = queue_job_records(roots["failed_dir"], "failed")
    index_records, index_error = processed_index_records(runtime_resolved)

    drafts_by_candidate: dict[str, list[Path]] = {}
    for path, draft in drafts.items():
        candidate_id = str(draft.get("candidate_id") or "")
        if candidate_id:
            drafts_by_candidate.setdefault(candidate_id, []).append(path)
    draft_by_id = {str(payload.get("draft_id")): (path, payload) for path, payload in drafts.items() if payload.get("draft_id")}
    staging_by_draft = {str(payload.get("draft_id")): (path, payload) for path, payload in staging_results.items() if payload.get("draft_id")}
    queue_by_draft = records_by_draft(queue_records)
    processed_by_draft = records_by_draft(processed_records)
    failed_by_draft = records_by_draft(failed_records)
    index_job_ids = {str(record.get("computed_job_id")) for record in index_records if record.get("computed_job_id")}
    stale_before = datetime.now(timezone.utc) - timedelta(days=stale_days)

    findings: list[dict[str, object]] = []
    for path, candidate in candidates.items():
        candidate_id = str(candidate.get("candidate_id") or "")
        status = str(candidate.get("status") or "")
        timestamp = artifact_timestamp(path, candidate)
        if status == "pending_review" and timestamp < stale_before:
            findings.append(finding("stale_pending_candidate", path, include_paths, candidate_id=candidate_id, age_days=(datetime.now(timezone.utc) - timestamp).days))
        if status == "approved" and candidate_id not in drafts_by_candidate:
            findings.append(finding("approved_candidate_missing_draft", path, include_paths, candidate_id=candidate_id))
        if status == "rejected" and timestamp < stale_before:
            findings.append(finding("old_rejected_candidate", path, include_paths, candidate_id=candidate_id, age_days=(datetime.now(timezone.utc) - timestamp).days))
        if status == "failed":
            findings.append(finding("failed_candidate", path, include_paths, candidate_id=candidate_id))

    for path, draft in drafts.items():
        draft_id = str(draft.get("draft_id") or "")
        if draft.get("status") == DRAFT_STATUS_APPROVED and draft_id not in staging_by_draft:
            findings.append(finding("approved_draft_missing_staging_status", path, include_paths, draft_id=draft_id, candidate_id=str(draft.get("candidate_id") or "")))
        if draft.get("status") == DRAFT_STATUS_FAILED:
            findings.append(finding("failed_draft", path, include_paths, draft_id=draft_id, candidate_id=str(draft.get("candidate_id") or "")))

    for path, staging in staging_results.items():
        draft_id = str(staging.get("draft_id") or "")
        draft_path = staging.get("draft_artifact_path")
        if (draft_path and not Path(str(draft_path)).expanduser().exists()) or (draft_id and draft_id not in draft_by_id):
            findings.append(finding("orphaned_staging_status", path, include_paths, draft_id=draft_id, missing_path=str(draft_path or "")))
        if staging.get("status") == "staged":
            computed_job_id = str(staging.get("computed_job_id") or "")
            has_artifact = bool(queue_by_draft.get(draft_id) or processed_by_draft.get(draft_id) or failed_by_draft.get(draft_id))
            has_index = bool(computed_job_id and computed_job_id in index_job_ids)
            if not has_artifact and not has_index:
                findings.append(finding("staged_draft_missing_queue_processed_failed_evidence", path, include_paths, draft_id=draft_id, computed_job_id=computed_job_id))

    for record in queue_records:
        draft_id = str(record.get("draft_id") or "")
        draft_path = record.get("draft_artifact_path")
        missing = draft_path and not Path(str(draft_path)).expanduser().exists()
        if missing or (draft_id and draft_id not in draft_by_id):
            path = Path(str(record.get("path") or ""))
            findings.append(finding("queue_file_missing_draft", path, include_paths, draft_id=draft_id, missing_path=str(draft_path or "")))
    if index_error is not None:
        findings.append({"type": "processed_index_unreadable", **({"artifact_path": str(runtime_resolved / "processed_index.jsonl")} if include_paths else {}), "detail": index_error})

    disk_usage = {
        "action_candidates": directory_usage(roots["candidate_dir"]),
        "approved_job_drafts": directory_usage(roots["draft_dir"]),
        "staging": directory_usage(roots["staging_dir"]),
    }
    oldest: dict[str, str] = {}
    newest: dict[str, str] = {}
    for key in ("candidate_dir", "draft_dir", "staging_dir"):
        old, new = oldest_newest(roots[key])
        oldest[key] = old
        newest[key] = new

    counts = {
        "candidates_total": len(candidates),
        "candidates_pending_review": count_status(candidates.values(), "pending_review"),
        "candidates_approved": count_status(candidates.values(), "approved"),
        "candidates_rejected": count_status(candidates.values(), "rejected"),
        "candidates_failed": count_status(candidates.values(), "failed"),
        "stale_pending_candidates": sum(1 for item in findings if item["type"] == "stale_pending_candidate"),
        "approved_candidates_without_draft": sum(1 for item in findings if item["type"] == "approved_candidate_missing_draft"),
        "old_rejected_candidates": sum(1 for item in findings if item["type"] == "old_rejected_candidate"),
        "drafts_total": len(drafts),
        "drafts_approved": count_status(drafts.values(), DRAFT_STATUS_APPROVED),
        "drafts_failed": count_status(drafts.values(), DRAFT_STATUS_FAILED),
        "approved_drafts_without_staging_status": sum(1 for item in findings if item["type"] == "approved_draft_missing_staging_status"),
        "staged_drafts_without_queue_processed_failed_index_evidence": sum(1 for item in findings if item["type"] == "staged_draft_missing_queue_processed_failed_evidence"),
        "staging_total": len(staging_results),
        "staging_staged": count_status(staging_results.values(), "staged"),
        "staging_skipped": count_status(staging_results.values(), "skipped"),
        "staging_failed": count_status(staging_results.values(), "failed"),
        "orphaned_staging_statuses": sum(1 for item in findings if item["type"] == "orphaned_staging_status"),
        "queue_files_missing_drafts": sum(1 for item in findings if item["type"] == "queue_file_missing_draft"),
    }

    return {
        "channel": "field_capture",
        "runtime_root": str(runtime_resolved),
        "stale_days": stale_days,
        "counts": counts,
        "findings": findings,
        "disk_usage": disk_usage,
        "oldest": oldest,
        "newest": newest,
    }


def format_report(report: dict[str, object], *, include_paths: bool = False) -> str:
    counts = report["counts"]
    lines = [f"field-capture review maintenance runtime={report['runtime_root']} stale_days={report['stale_days']}", ""]
    for key in sorted(counts):
        lines.append(f"{key}: {counts[key]}")
    lines.append("")
    lines.append("disk_usage:")
    for key, value in report["disk_usage"].items():
        lines.append(f"- {key}: {value['bytes']} bytes across {value['files']} files")
    lines.append("")
    lines.append(f"findings: {len(report['findings'])}")
    for item in report["findings"]:
        detail = item.get("candidate_id") or item.get("draft_id") or item.get("detail") or ""
        path = f" {item['artifact_path']}" if include_paths and item.get("artifact_path") else ""
        lines.append(f"- {item['type']}: {detail}{path}".rstrip())
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Report read-only field-capture review artifact maintenance status.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--draft-dir", type=Path)
    parser.add_argument("--staging-dir", type=Path)
    parser.add_argument("--queue-dir", type=Path)
    parser.add_argument("--processed-dir", type=Path)
    parser.add_argument("--failed-dir", type=Path)
    parser.add_argument("--stale-days", type=int, default=14)
    parser.add_argument("--include-paths", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = review_maintenance_status_report(
        runtime_root=args.runtime_root.expanduser(),
        candidate_dir=args.candidate_dir.expanduser() if args.candidate_dir is not None else None,
        draft_dir=args.draft_dir.expanduser() if args.draft_dir is not None else None,
        staging_dir=args.staging_dir.expanduser() if args.staging_dir is not None else None,
        queue_dir=args.queue_dir.expanduser() if args.queue_dir is not None else None,
        processed_dir=args.processed_dir.expanduser() if args.processed_dir is not None else None,
        failed_dir=args.failed_dir.expanduser() if args.failed_dir is not None else None,
        stale_days=args.stale_days,
        include_paths=args.include_paths,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_report(report, include_paths=args.include_paths))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
