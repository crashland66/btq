from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from typing import Any

from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import (
    CouchDBCandidateWriterError,
    _put_document as put_candidate_document,
)
from event_pipeline.couchdb_job_draft_writer import (
    JOB_DRAFT_REVIEW_STATUS_DEFAULT,
    set_job_draft_queue_materialized_at,
    set_job_draft_review_status,
    upsert_job_draft,
)
from field_capture.action_candidates import (
    couchdb_action_candidate_to_review_payload,
    list_action_candidates,
)
from field_capture.approved_job_drafts import proposed_queue_jobs


LOGGER = logging.getLogger(__name__)
REPORT_KEYS = (
    "candidates_scanned",
    "drafts_created",
    "approved_marked_materialized",
    "rejected_migrated",
    "pending_migrated",
    "skipped_no_job",
    "skipped_already_migrated",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def migrate_candidates_to_drafts(
    config: couchdb_config.CouchDBConfig,
    db: str,
    *,
    dry_run: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Migrate existing CouchDB action_candidate docs into job_draft docs."""
    report: dict[str, Any] = {key: 0 for key in REPORT_KEYS}
    report["errors"] = []
    migration_timestamp = utc_now_iso()

    candidates = list_action_candidates(config, db, include_archived=True)
    scanned = 0
    for doc in candidates:
        if limit is not None and scanned >= limit:
            break
        scanned += 1
        report["candidates_scanned"] += 1

        candidate_id = str(doc.get("candidate_id") or "")
        if doc.get("migrated_to_draft_at"):
            report["skipped_already_migrated"] += 1
            continue

        try:
            migration = _candidate_migration_plan(doc, migration_timestamp=migration_timestamp)
            drafts = migration["drafts"]
            if not drafts:
                report["skipped_no_job"] += 1
                if not dry_run:
                    _mark_candidate_migrated(config, db, doc, migration_timestamp)
                continue

            if not dry_run:
                for draft in drafts:
                    draft_id = str(draft["draft_id"])
                    review_status = str(draft["review_status"])
                    draft_to_write = dict(draft)
                    if review_status in {"approved", "rejected"}:
                        draft_to_write["review_status"] = JOB_DRAFT_REVIEW_STATUS_DEFAULT
                    upsert_job_draft(config, db, draft_to_write)
                    if review_status == "approved":
                        set_job_draft_queue_materialized_at(
                            config,
                            db,
                            draft_id,
                            materialized_at=str(draft["queue_materialized_at"]),
                        )
                    if review_status in {"approved", "rejected"}:
                        set_job_draft_review_status(
                            config,
                            db,
                            draft_id,
                            review_status=review_status,
                            reviewed_by=str(draft.get("reviewed_by") or ""),
                            rationale=str(draft.get("review_rationale") or ""),
                            expected_prior_statuses=("pending_approval", review_status),
                            reason="migrated_from_action_candidate",
                        )
                _mark_candidate_migrated(config, db, doc, migration_timestamp)

            report["drafts_created"] += len(drafts)
            if migration["review_status"] == "approved":
                report["approved_marked_materialized"] += len(drafts)
            elif migration["review_status"] == "rejected":
                report["rejected_migrated"] += len(drafts)
            elif migration["review_status"] == JOB_DRAFT_REVIEW_STATUS_DEFAULT:
                report["pending_migrated"] += len(drafts)
        except Exception as exc:  # noqa: BLE001 - per-candidate failures should be reported and migration continues.
            _record_error(report, candidate_id, exc)

    return report


def _candidate_migration_plan(doc: dict[str, Any], *, migration_timestamp: str) -> dict[str, Any]:
    payload = couchdb_action_candidate_to_review_payload(doc)
    candidate_status = str(payload.get("status") or "").strip()
    if candidate_status == "failed":
        return {"review_status": "", "drafts": []}

    review_status = _mapped_review_status(payload)
    if not review_status:
        raise ValueError(f"unsupported action_candidate status: {candidate_status}")

    jobs = proposed_queue_jobs(payload)
    valid_jobs = [(job_type, job_payload, error) for job_type, job_payload, error in jobs if str(job_type or "").strip()]
    if not valid_jobs:
        return {"review_status": review_status, "drafts": []}

    group_id = _group_id(payload)
    created_at = migration_timestamp
    drafts: list[dict[str, Any]] = []
    for ordinal, (job_type, job_payload, error) in enumerate(valid_jobs):
        job_type_text = str(job_type or "").strip()
        draft: dict[str, Any] = {
            "draft_id": f"{group_id}-{job_type_text}-{ordinal}",
            "job_type": job_type_text,
            "payload": job_payload if isinstance(job_payload, dict) else {},
            "review_status": review_status,
            "validation_error": str(error) if error else None,
            "message": _message(payload),
            "site_id": _site_id(payload),
            "source_kind": _source_kind(payload),
            "source_capture_id": _capture_id(payload),
            "submitter_name": _submitter_name(payload),
            "confidence": payload.get("confidence"),
            "group_id": group_id,
            "created_at": created_at,
            "source": "field_capture_pipeline",
            "reviewed_by": str(payload.get("reviewed_by") or payload.get("reviewer") or ""),
            "reviewed_at": str(payload.get("reviewed_at") or ""),
            "review_rationale": _review_rationale(payload),
        }
        if review_status == "approved":
            draft["queue_materialized_at"] = str(doc.get("staged_at") or migration_timestamp)
        drafts.append(draft)
    return {"review_status": review_status, "drafts": drafts}


def _mapped_review_status(payload: dict[str, object]) -> str:
    status = str(payload.get("status") or "").strip()
    archived = bool(payload.get("archived") is True)
    if status == "pending_review":
        return "rejected" if archived else JOB_DRAFT_REVIEW_STATUS_DEFAULT
    if status == "approved":
        return "approved"
    if status == "rejected":
        return "rejected"
    return ""


def _mark_candidate_migrated(
    config: couchdb_config.CouchDBConfig,
    db: str,
    doc: dict[str, Any],
    migrated_at: str,
) -> None:
    doc_id = str(doc.get("_id") or "")
    if not doc_id:
        raise CouchDBCandidateWriterError("action_candidate document has no _id")
    updated = dict(doc)
    updated["migrated_to_draft_at"] = migrated_at
    put_candidate_document(config, db, doc_id, updated, conflict_as_already_decided=True)


def _record_error(report: dict[str, Any], candidate_id: str, exc: Exception) -> None:
    errors = report.get("errors")
    if not isinstance(errors, list):
        errors = []
        report["errors"] = errors
    message = str(exc)
    errors.append({"candidate_id": candidate_id, "error": message})
    LOGGER.error(
        "candidate-to-draft migration failed: candidate_id=%s error=%s",
        candidate_id,
        message,
    )


def _dict_field(candidate: dict[str, object], field: str) -> dict[str, object]:
    value = candidate.get(field)
    return value if isinstance(value, dict) else {}


def _capture_id(candidate: dict[str, object]) -> str:
    channel_metadata = _dict_field(candidate, "channel_metadata")
    provenance = _dict_field(candidate, "provenance")
    return str(
        candidate.get("capture_id")
        or channel_metadata.get("capture_id")
        or channel_metadata.get("upload_id")
        or provenance.get("capture_id")
        or provenance.get("upload_id")
        or ""
    )


def _group_id(candidate: dict[str, object]) -> str:
    capture_id = _capture_id(candidate).strip()
    if capture_id:
        return capture_id
    return str(candidate.get("candidate_id") or "").strip()


def _message(candidate: dict[str, object]) -> str:
    return str(
        candidate.get("summary")
        or candidate.get("source_text")
        or candidate.get("operational_summary")
        or candidate.get("source_context")
        or ""
    )


def _site_id(candidate: dict[str, object]) -> str:
    channel_metadata = _dict_field(candidate, "channel_metadata")
    provenance = _dict_field(candidate, "provenance")
    return str(
        candidate.get("site_id")
        or channel_metadata.get("site_id")
        or provenance.get("site_id")
        or ""
    )


def _source_kind(candidate: dict[str, object]) -> str:
    channel_metadata = _dict_field(candidate, "channel_metadata")
    provenance = _dict_field(candidate, "provenance")
    return str(
        candidate.get("source_kind")
        or channel_metadata.get("source_kind")
        or provenance.get("source_kind")
        or provenance.get("semantic_artifact_type")
        or ""
    )


def _submitter_name(candidate: dict[str, object]) -> str:
    channel_metadata = _dict_field(candidate, "channel_metadata")
    return str(channel_metadata.get("person_name") or channel_metadata.get("person_id") or candidate.get("person_id") or "")


def _review_rationale(candidate: dict[str, object]) -> str:
    rationale = str(candidate.get("review_rationale") or "")
    if candidate.get("archived") is True:
        archived_by = str(candidate.get("archived_by") or "")
        archived_at = str(candidate.get("archived_at") or "")
        archive_note = "Archived action candidate migrated to job_draft."
        if archived_by or archived_at:
            archive_note = f"{archive_note} archived_by={archived_by} archived_at={archived_at}"
        return f"{rationale}\n{archive_note}".strip()
    return rationale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate CouchDB action_candidate docs into job_draft docs.")
    parser.add_argument(
        "--database",
        default=couchdb_config.field_captures_database(),
        help="CouchDB database containing action_candidate and job_draft documents.",
    )
    parser.add_argument("--apply", action="store_true", help="Write migrated job_draft docs and mark source candidates.")
    parser.add_argument("--dry-run", action="store_true", help="Preview migration without writing. This is the default.")
    parser.add_argument("--limit", type=int, help="Maximum number of candidate documents to scan.")
    parser.add_argument("--json", action="store_true", help="Print the migration report as JSON.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dry_run = not bool(args.apply)
    config = couchdb_config.from_env()
    report = migrate_candidates_to_drafts(
        config,
        args.database,
        dry_run=dry_run,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        mode = "dry-run" if dry_run else "apply"
        parts = " ".join(f"{key}={report[key]}" for key in REPORT_KEYS)
        print(f"candidate-to-draft migration ({mode}): {parts} errors={len(report['errors'])}")
        for item in report["errors"]:
            print(f"error: {item['candidate_id']}: {item['error']}")
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
