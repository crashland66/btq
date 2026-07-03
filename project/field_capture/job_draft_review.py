from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from field_capture.action_candidates import couchdb_candidate_config_or_none
from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import AlreadyDecided
from event_pipeline.couchdb_job_draft_writer import (
    CouchDBJobDraftWriterError,
    get_job_draft,
    list_job_drafts,
    set_job_draft_approval_route_and_review_status,
    set_job_draft_payload,
    set_job_draft_review_status,
)
from field_capture.approval_routes import (
    build_approval_route_job,
    is_valid_approval_route,
    normalize_approval_route,
)
from queue_spec import validate_job


@dataclass(frozen=True)
class JobDraftReviewResult:
    draft_id: str
    review_status: str
    already_decided: bool
    error: str | None
    new_rev: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())


def job_draft_to_review_payload(doc: dict[str, Any]) -> dict[str, object]:
    return {
        "type": "job_draft_review",
        "draft_id": str(doc.get("draft_id") or ""),
        "review_status": str(doc.get("review_status") or ""),
        "job_type": str(doc.get("job_type") or ""),
        "payload": doc.get("payload") if isinstance(doc.get("payload"), dict) else {},
        "validation_error": doc.get("validation_error"),
        "message": str(doc.get("message") or ""),
        "site_id": str(doc.get("site_id") or ""),
        "submitter_name": str(doc.get("submitter_name") or ""),
        "confidence": doc.get("confidence"),
        "source_capture_id": str(doc.get("source_capture_id") or ""),
        "source_kind": str(doc.get("source_kind") or ""),
        "group_id": str(doc.get("group_id") or ""),
        "reviewed_by": str(doc.get("reviewed_by") or doc.get("reviewer") or ""),
        "reviewed_at": str(doc.get("reviewed_at") or ""),
        "review_rationale": str(doc.get("review_rationale") or ""),
        "created_at": str(doc.get("created_at") or ""),
        "_id": str(doc.get("_id") or ""),
        "_rev": str(doc.get("_rev") or ""),
    }


def couchdb_job_draft_payloads(
    *,
    review_status: str | None = None,
    group_id: str | None = None,
) -> list[tuple[Path, dict[str, object]]]:
    config = couchdb_candidate_config_or_none()
    if config is None:
        return []
    db = couchdb_config.field_captures_database()
    docs = list_job_drafts(
        config,
        db,
        review_status=review_status,
        group_id=group_id,
    )
    return [
        (Path("couchdb") / db / str(doc.get("_id") or ""), job_draft_to_review_payload(doc))
        for doc in docs
    ]


def apply_job_draft_review(
    config: couchdb_config.CouchDBConfig,
    db: str,
    draft_id: str,
    action: str,
    reviewer: str,
    *,
    expected_rev: str | None = None,
    rationale: str | None = None,
    route: str | None = None,
) -> JobDraftReviewResult:
    draft_id_text = str(draft_id or "").strip()
    reviewer_text = str(reviewer or "").strip()
    action_text = str(action or "").strip().lower()
    if not draft_id_text:
        return JobDraftReviewResult("", "", False, "draft_id is required", None)
    if not reviewer_text:
        return JobDraftReviewResult(draft_id_text, "", False, "reviewer is required", None)
    if action_text not in {"approve", "reject"}:
        return JobDraftReviewResult(draft_id_text, "", False, f"unsupported review action: {action}", None)

    try:
        doc = get_job_draft(config, db, draft_id_text)
    except CouchDBJobDraftWriterError as exc:
        return JobDraftReviewResult(draft_id_text, "", False, str(exc), None)
    if doc is None:
        return JobDraftReviewResult(draft_id_text, "", False, f"job_draft not found: {draft_id_text}", None)

    current_rev = str(doc.get("_rev") or "")
    prior_status = str(doc.get("review_status") or "")
    if expected_rev is not None and str(expected_rev) != current_rev:
        return JobDraftReviewResult(draft_id_text, prior_status, True, None, current_rev or None)
    if prior_status != "pending_approval":
        return JobDraftReviewResult(draft_id_text, prior_status, True, None, current_rev or None)
    if str(doc.get("type") or "") != "job_draft":
        return JobDraftReviewResult(draft_id_text, prior_status, False, "document type is not job_draft", current_rev or None)

    route_text = normalize_approval_route(route)
    if route_text and action_text == "reject":
        return JobDraftReviewResult(draft_id_text, prior_status, False, "reject action cannot use approval route", current_rev or None)
    if route_text and not is_valid_approval_route(route_text):
        return JobDraftReviewResult(draft_id_text, prior_status, False, f"invalid approval route: {route_text}", current_rev or None)

    if action_text == "approve":
        if route_text:
            job_type, payload, error = build_approval_route_job(doc, route_text, reviewer=reviewer_text)
            if error:
                return JobDraftReviewResult(draft_id_text, prior_status, False, error, current_rev or None)
            route_rationale = str(rationale if rationale is not None else f"Approved via inbox route: {route_text}")
            try:
                updated = set_job_draft_approval_route_and_review_status(
                    config,
                    db,
                    draft_id_text,
                    route=route_text,
                    job_type=job_type,
                    payload=payload,
                    reviewed_by=reviewer_text,
                    rationale=str(rationale if rationale is not None else "No rationale provided."),
                    route_rationale=route_rationale,
                    expected_rev=expected_rev,
                )
            except AlreadyDecided:
                latest = get_job_draft(config, db, draft_id_text)
                latest_status = str(latest.get("review_status") or prior_status) if latest else prior_status
                latest_rev = str(latest.get("_rev") or "") if latest else current_rev
                return JobDraftReviewResult(draft_id_text, latest_status, True, None, latest_rev or None)
            except CouchDBJobDraftWriterError as exc:
                return JobDraftReviewResult(draft_id_text, prior_status, False, str(exc), current_rev or None)
            return JobDraftReviewResult(draft_id_text, "approved", False, None, str(updated.get("_rev") or "") or None)
        else:
            error = _validate_current_job_draft(doc)
            if error:
                return JobDraftReviewResult(draft_id_text, prior_status, False, error, current_rev or None)

    target_status = "approved" if action_text == "approve" else "rejected"
    try:
        updated = set_job_draft_review_status(
            config,
            db,
            draft_id_text,
            review_status=target_status,
            reviewed_by=reviewer_text,
            rationale=str(rationale if rationale is not None else "No rationale provided."),
            expected_rev=expected_rev,
        )
    except AlreadyDecided:
        latest = get_job_draft(config, db, draft_id_text)
        latest_status = str(latest.get("review_status") or prior_status) if latest else prior_status
        latest_rev = str(latest.get("_rev") or "") if latest else current_rev
        return JobDraftReviewResult(draft_id_text, latest_status, True, None, latest_rev or None)
    except CouchDBJobDraftWriterError as exc:
        return JobDraftReviewResult(draft_id_text, prior_status, False, str(exc), current_rev or None)
    return JobDraftReviewResult(draft_id_text, target_status, False, None, str(updated.get("_rev") or "") or None)


def edit_job_draft_payload(
    config: couchdb_config.CouchDBConfig,
    db: str,
    draft_id: str,
    new_payload: dict[str, Any],
    editor: str,
    *,
    expected_rev: str | None = None,
) -> JobDraftReviewResult:
    draft_id_text = str(draft_id or "").strip()
    editor_text = str(editor or "").strip()
    if not draft_id_text:
        return JobDraftReviewResult("", "", False, "draft_id is required", None)
    if not editor_text:
        return JobDraftReviewResult(draft_id_text, "", False, "editor is required", None)

    try:
        doc = get_job_draft(config, db, draft_id_text)
    except CouchDBJobDraftWriterError as exc:
        return JobDraftReviewResult(draft_id_text, "", False, str(exc), None)
    if doc is None:
        return JobDraftReviewResult(draft_id_text, "", False, f"job_draft not found: {draft_id_text}", None)

    current_rev = str(doc.get("_rev") or "")
    prior_status = str(doc.get("review_status") or "")
    if expected_rev is not None and str(expected_rev) != current_rev:
        return JobDraftReviewResult(draft_id_text, prior_status, True, None, current_rev or None)
    if prior_status != "pending_approval":
        return JobDraftReviewResult(draft_id_text, prior_status, True, None, current_rev or None)
    if str(doc.get("type") or "") != "job_draft":
        return JobDraftReviewResult(draft_id_text, prior_status, False, "document type is not job_draft", current_rev or None)

    job_type = str(doc.get("job_type") or "")
    candidate_job = {"job_type": job_type, "payload": new_payload}
    if not validate_job(candidate_job):
        return JobDraftReviewResult(
            draft_id_text,
            prior_status,
            False,
            "edited job_draft payload fails queue_spec validation",
            current_rev or None,
        )

    try:
        updated = set_job_draft_payload(
            config,
            db,
            draft_id_text,
            payload=new_payload,
            validation_error=None,
            edited_by=editor_text,
            expected_rev=expected_rev,
        )
    except AlreadyDecided:
        latest = get_job_draft(config, db, draft_id_text)
        latest_status = str(latest.get("review_status") or prior_status) if latest else prior_status
        latest_rev = str(latest.get("_rev") or "") if latest else current_rev
        return JobDraftReviewResult(draft_id_text, latest_status, True, None, latest_rev or None)
    except CouchDBJobDraftWriterError as exc:
        return JobDraftReviewResult(draft_id_text, prior_status, False, str(exc), current_rev or None)
    return JobDraftReviewResult(
        draft_id_text,
        str(updated.get("review_status") or prior_status),
        False,
        None,
        str(updated.get("_rev") or "") or None,
    )


def _validate_current_job_draft(doc: dict[str, Any]) -> str:
    validation_error = doc.get("validation_error")
    if validation_error:
        return str(validation_error)
    job = {"job_type": str(doc.get("job_type") or ""), "payload": doc.get("payload")}
    if not validate_job(job):
        return "job_draft fails queue_spec validation"
    return ""
