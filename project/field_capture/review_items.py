from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import CouchDBCandidateWriterError, get_action_candidate
from field_capture.action_candidates import (
    candidate_list_item,
    couchdb_action_candidate_to_review_payload,
    couchdb_candidate_config_or_none,
    default_candidate_dir,
)
from field_capture.approved_job_drafts import (
    default_draft_dir,
    draft_list_item,
    iter_draft_artifacts,
)
from field_capture.draft_staging import default_status_dir
from processing_core.artifacts import read_json_object, resolve_within_root
from processing_core.draft_staging import staging_result_path


class ReviewItemError(RuntimeError):
    """Raised when one review item cannot be shown safely."""


def default_runtime_root() -> Path:
    return get_config().runtime_root


def find_draft_artifacts(draft_dir: Path, draft_id: str) -> list[tuple[Path, dict[str, object]]]:
    matches: list[tuple[Path, dict[str, object]]] = []
    for path, payload in iter_draft_artifacts(draft_dir):
        if payload.get("draft_id") == draft_id:
            matches.append((path, payload))
    return matches


def candidate_detail_item(path: Path, payload: dict[str, object]) -> dict[str, object]:
    item = candidate_list_item(path, payload, include_source=True)
    item.update(
        {
            "provenance": payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {},
            "channel_metadata": payload.get("channel_metadata") if isinstance(payload.get("channel_metadata"), dict) else {},
            "prior_status": str(payload.get("prior_status") or ""),
            "review_history": payload.get("review_history") if isinstance(payload.get("review_history"), list) else [],
            "error": payload.get("error"),
        }
    )
    return item


def draft_detail_item(
    path: Path,
    payload: dict[str, object],
    *,
    runtime_root: Path,
    staging_dir: Path,
    queue_dir: Path,
    processed_dir: Path,
    failed_dir: Path,
) -> dict[str, object]:
    item = draft_list_item(
        path,
        payload,
        runtime_root=runtime_root,
        staging_dir=staging_dir,
        queue_dir=queue_dir,
        processed_dir=processed_dir,
        failed_dir=failed_dir,
        include_payload=True,
        include_source=True,
    )
    draft_id = str(payload.get("draft_id") or "")
    status_path = staging_result_path(staging_dir, draft_id) if draft_id else None
    item.update(
        {
            "staging_status_artifact_path": str(status_path) if status_path is not None and status_path.exists() else "",
            "error": payload.get("error"),
        }
    )
    if status_path is not None and status_path.exists():
        item["staging_status"] = read_json_object(status_path)
    return item


def show_candidate(
    *,
    candidate_id: str,
    candidate_dir: Path,
    runtime_root: Path,
) -> dict[str, object]:
    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    resolve_within_root(candidate_root, runtime_root.expanduser().resolve(strict=False))
    config = couchdb_candidate_config_or_none()
    if config is None:
        raise ReviewItemError("CouchDB action candidate store is not configured")
    try:
        doc = get_action_candidate(config, couchdb_config.field_captures_database(), candidate_id)
    except CouchDBCandidateWriterError as exc:
        raise ReviewItemError(str(exc)) from exc
    if doc is None:
        raise ReviewItemError(f"candidate not found: {candidate_id}")
    path = Path("couchdb") / couchdb_config.field_captures_database() / str(doc.get("_id") or "")
    payload = couchdb_action_candidate_to_review_payload(doc)
    return {"channel": "field_capture", "item_type": "candidate", "item": candidate_detail_item(path, payload)}


def show_draft(
    *,
    draft_id: str,
    draft_dir: Path,
    runtime_root: Path,
    staging_dir: Path | None = None,
    queue_dir: Path | None = None,
    processed_dir: Path | None = None,
    failed_dir: Path | None = None,
) -> dict[str, object]:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    draft_root = draft_dir.expanduser().resolve(strict=False)
    staging_root = (staging_dir or default_status_dir(runtime_resolved)).expanduser().resolve(strict=False)
    queue_root = (queue_dir or runtime_resolved / "queue").expanduser().resolve(strict=False)
    processed_root = (processed_dir or runtime_resolved / "processed").expanduser().resolve(strict=False)
    failed_root = (failed_dir or runtime_resolved / "failed").expanduser().resolve(strict=False)
    for root in (draft_root, staging_root, queue_root, processed_root, failed_root):
        resolve_within_root(root, runtime_resolved)
    matches = find_draft_artifacts(draft_root, draft_id)
    if not matches:
        raise ReviewItemError(f"draft not found: {draft_id}")
    if len(matches) > 1:
        raise ReviewItemError(f"multiple draft artifacts found for draft_id: {draft_id}")
    path, payload = matches[0]
    return {
        "channel": "field_capture",
        "item_type": "draft",
        "item": draft_detail_item(
            path,
            payload,
            runtime_root=runtime_resolved,
            staging_dir=staging_root,
            queue_dir=queue_root,
            processed_dir=processed_root,
            failed_dir=failed_root,
        ),
    }


def format_candidate(item: dict[str, object]) -> str:
    lines = [
        f"field review candidate {item['candidate_id']}",
        f"status: {item['status']}",
        f"type: {item['candidate_type']}",
        f"summary: {item['summary']}",
        f"confidence: {item['confidence']}",
        f"rationale: {item['rationale']}",
        f"artifact: {item['artifact_path']}",
    ]
    for key in ("source_text", "source_context", "semantic_artifact_path", "reviewer", "reviewed_at", "review_rationale", "prior_status"):
        value = item.get(key)
        if value:
            lines.append(f"{key}: {value}")
    if item.get("provenance"):
        lines.append("provenance:")
        lines.append(json.dumps(item["provenance"], indent=2, sort_keys=True))
    if item.get("channel_metadata"):
        lines.append("channel_metadata:")
        lines.append(json.dumps(item["channel_metadata"], indent=2, sort_keys=True))
    if item.get("review_history"):
        lines.append("review_history:")
        lines.append(json.dumps(item["review_history"], indent=2, sort_keys=True))
    if item.get("error"):
        lines.append("error:")
        lines.append(json.dumps(item["error"], indent=2, sort_keys=True))
    return "\n".join(lines)


def format_draft(item: dict[str, object]) -> str:
    lines = [
        f"field approved draft {item['draft_id']}",
        f"status: {item['status']}",
        f"candidate_id: {item['candidate_id']}",
        f"proposed_job_type: {item['proposed_job_type']}",
        f"confidence: {item['confidence']}",
        f"rationale: {item['rationale']}",
        f"artifact: {item['artifact_path']}",
        f"candidate_artifact_path: {item['candidate_artifact_path']}",
        f"semantic_artifact_path: {item['semantic_artifact_path']}",
        f"source_transcript_path: {item['source_transcript_path']}",
        f"queue_state: {json.dumps(item['queue_state'], sort_keys=True)}",
    ]
    if item.get("staging_status_artifact_path"):
        lines.append(f"staging_status_artifact_path: {item['staging_status_artifact_path']}")
    lines.append("proposed_payload:")
    lines.append(json.dumps(item.get("proposed_payload") or {}, indent=2, sort_keys=True))
    if item.get("provenance"):
        lines.append("provenance:")
        lines.append(json.dumps(item["provenance"], indent=2, sort_keys=True))
    if item.get("approval_metadata"):
        lines.append("approval_metadata:")
        lines.append(json.dumps(item["approval_metadata"], indent=2, sort_keys=True))
    if item.get("staging_status"):
        lines.append("staging_status:")
        lines.append(json.dumps(item["staging_status"], indent=2, sort_keys=True))
    if item.get("error"):
        lines.append("error:")
        lines.append(json.dumps(item["error"], indent=2, sort_keys=True))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Show full detail for one field-capture review candidate or draft.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--draft-dir", type=Path)
    parser.add_argument("--staging-dir", type=Path)
    parser.add_argument("--queue-dir", type=Path)
    parser.add_argument("--processed-dir", type=Path)
    parser.add_argument("--failed-dir", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--candidate-id")
    group.add_argument("--draft-id")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    try:
        if args.candidate_id is not None:
            report = show_candidate(
                candidate_id=args.candidate_id,
                candidate_dir=args.candidate_dir.expanduser() if args.candidate_dir is not None else default_candidate_dir(runtime_root),
                runtime_root=runtime_root,
            )
        else:
            report = show_draft(
                draft_id=args.draft_id,
                draft_dir=args.draft_dir.expanduser() if args.draft_dir is not None else default_draft_dir(runtime_root),
                runtime_root=runtime_root,
                staging_dir=args.staging_dir.expanduser() if args.staging_dir is not None else None,
                queue_dir=args.queue_dir.expanduser() if args.queue_dir is not None else None,
                processed_dir=args.processed_dir.expanduser() if args.processed_dir is not None else None,
                failed_dir=args.failed_dir.expanduser() if args.failed_dir is not None else None,
            )
    except ReviewItemError as exc:
        error = {"channel": "field_capture", "error": str(exc)}
        if args.json:
            print(json.dumps(error, indent=2, sort_keys=True))
        else:
            print(f"field review item failed: {exc}")
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif report["item_type"] == "candidate":
        print(format_candidate(report["item"]))
    else:
        print(format_draft(report["item"]))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
