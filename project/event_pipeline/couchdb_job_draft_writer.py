from __future__ import annotations

from typing import Any
from urllib import error

from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import (
    CouchDBCandidateWriterError,
    _get_document,
    _put_document,
)


JOB_DRAFT_TYPE = "job_draft"
JOB_DRAFT_ID_PREFIX = "job_draft_"
JOB_DRAFT_REVIEW_STATUSES = frozenset({"pending_approval", "approved", "rejected"})
JOB_DRAFT_REVIEW_STATUS_DEFAULT = "pending_approval"


class CouchDBJobDraftWriterError(Exception):
    pass


def job_draft_doc_id(draft_id: str) -> str:
    draft_id_text = str(draft_id or "").strip()
    if not draft_id_text:
        raise CouchDBJobDraftWriterError("draft_id is required")
    return f"{JOB_DRAFT_ID_PREFIX}{draft_id_text}"


def get_job_draft_document(
    config: couchdb_config.CouchDBConfig,
    database: str,
    draft_id: str,
) -> dict[str, Any] | None:
    """Fetch a job_draft document by draft_id, returning None for 404."""
    try:
        return _get_document(config, database, job_draft_doc_id(draft_id))
    except CouchDBCandidateWriterError as exc:
        raise CouchDBJobDraftWriterError(str(exc)) from exc


def get_job_draft(
    config: couchdb_config.CouchDBConfig,
    db: str,
    draft_id: str,
) -> dict[str, Any] | None:
    """Fetch a job_draft document by draft_id, including _rev."""
    return get_job_draft_document(config, db, draft_id)


def upsert_job_draft(
    config: couchdb_config.CouchDBConfig,
    db: str,
    draft: dict[str, Any],
    *,
    expected_rev: str | None = None,
) -> dict[str, Any]:
    """
    Idempotently write a job_draft document to CouchDB.

    The draft_id determines the stable CouchDB _id, so re-emitting the same
    draft updates the existing document with _rev CAS instead of duplicating it.
    """
    doc = build_job_draft_document(draft)
    doc_id = str(doc["_id"])

    for attempt in range(1, 3):
        existing = _get_existing_document(config, db, doc_id)
        doc_to_put = dict(doc)
        if existing is not None and existing.get("_rev"):
            current_rev = str(existing["_rev"])
            if expected_rev is not None and str(expected_rev) != current_rev:
                raise CouchDBJobDraftWriterError(f"job_draft _rev changed: {doc['draft_id']}")
            doc_to_put["_rev"] = current_rev
        elif expected_rev is not None:
            raise CouchDBJobDraftWriterError(f"job_draft not found for expected _rev: {doc['draft_id']}")

        try:
            return _put_document(config, db, doc_id, doc_to_put)
        except CouchDBCandidateWriterError as exc:
            if _is_conflict_error(exc) and attempt < 2 and expected_rev is None:
                continue
            raise CouchDBJobDraftWriterError(str(exc)) from exc

    raise CouchDBJobDraftWriterError(f"CouchDB job_draft PUT exhausted retries for {doc_id}")


def build_job_draft_document(draft: dict[str, Any]) -> dict[str, Any]:
    draft_id = str(draft.get("draft_id") or "").strip()
    if not draft_id:
        raise CouchDBJobDraftWriterError("draft is missing draft_id")

    job_type = str(draft.get("job_type") or "").strip()
    if not job_type:
        raise CouchDBJobDraftWriterError(f"job_draft is missing job_type: {draft_id}")

    review_status = str(draft.get("review_status") or JOB_DRAFT_REVIEW_STATUS_DEFAULT).strip()
    if review_status not in JOB_DRAFT_REVIEW_STATUSES:
        raise CouchDBJobDraftWriterError(f"invalid job_draft review_status for {draft_id}: {review_status!r}")

    payload = draft.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    return {
        "_id": job_draft_doc_id(draft_id),
        "type": JOB_DRAFT_TYPE,
        "draft_id": draft_id,
        "job_type": job_type,
        "payload": dict(payload),
        "review_status": review_status,
        "validation_error": draft.get("validation_error"),
        "message": str(draft.get("message") or ""),
        "site_id": str(draft.get("site_id") or ""),
        "submitter_name": str(draft.get("submitter_name") or ""),
        "confidence": draft.get("confidence"),
        "source_capture_id": str(draft.get("source_capture_id") or ""),
        "source_kind": str(draft.get("source_kind") or ""),
        "group_id": str(draft.get("group_id") or draft_id),
        "reviewed_by": draft.get("reviewed_by"),
        "reviewed_at": draft.get("reviewed_at"),
        "review_rationale": draft.get("review_rationale"),
        "created_at": str(draft.get("created_at") or ""),
        "source": str(draft.get("source") or "field_capture_pipeline"),
    }


def _get_existing_document(
    config: couchdb_config.CouchDBConfig,
    db: str,
    doc_id: str,
) -> dict[str, Any] | None:
    try:
        return _get_document(config, db, doc_id)
    except CouchDBCandidateWriterError as exc:
        raise CouchDBJobDraftWriterError(str(exc)) from exc


def _is_conflict_error(exc: CouchDBCandidateWriterError) -> bool:
    cause = exc.__cause__
    return isinstance(cause, error.HTTPError) and cause.code == 409
