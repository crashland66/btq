from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_pipeline.sites import SITES
from queue_processor.canonical_rmw import CanonicalEntityState, CanonicalMutation, CanonicalTarget, apply_canonical_rmw
from queue_spec import EDIT_RECORD_FIELD_ALLOWLISTS

from . import _shared
from ._shared import QueueJob, RunContext


def _resolve_record_doc_id(record_type: str, record_id: str) -> str:
    if record_id.startswith(f"{record_type}_"):
        return record_id
    store = _shared._vault_store()
    finder_name = {
        "site_issue": "find_site_issue_docs_by_issue_id",
        "supply_need": "find_supply_need_docs_by_supply_id",
        "equipment_request": "find_equipment_request_docs_by_equipment_id",
    }.get(record_type)
    if finder_name:
        try:
            docs = getattr(store, finder_name)(record_id)
        except Exception:
            docs = []
        ids = sorted(str(doc.get("_id")) for doc in docs if isinstance(doc, dict) and doc.get("_id"))
        if ids:
            return ids[0]
    return f"{record_type}_{record_id}"


def _site_name_for_site_id(site_id: object) -> str:
    normalized = str(site_id or "").strip()
    if not normalized:
        return ""
    return next(
        (
            str(site.get("canonical") or site.get("name") or "").strip()
            for site in SITES
            if str(site.get("site_id") or "").strip() == normalized
        ),
        "",
    )


def _edit_fields(payload: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    record_type = str(payload.get("record_type") or "").strip()
    record_id = str(payload.get("record_id") or "").strip()
    actor = str(payload.get("actor") or "").strip()
    raw_fields = payload.get("fields")
    if record_type not in EDIT_RECORD_FIELD_ALLOWLISTS or not record_id or not actor or not isinstance(raw_fields, dict):
        raise _shared.QueueProcessorError("edit_record_fields job requires valid record_type, record_id, fields, and actor")
    allowlist = EDIT_RECORD_FIELD_ALLOWLISTS[record_type]
    disallowed = sorted(set(raw_fields) - allowlist)
    if disallowed:
        raise _shared.QueueProcessorError(f"edit_record_fields rejected non-allowlisted fields: {', '.join(disallowed)}")
    fields = {key: value for key, value in raw_fields.items() if key in allowlist}
    if not fields:
        raise _shared.QueueProcessorError("edit_record_fields requires at least one allowlisted field")
    return record_type, record_id, actor, fields


def process_edit_record_fields_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    record_type, record_id, actor, fields = _edit_fields(job.payload)
    occurred_at = datetime.now(timezone.utc).isoformat()
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    doc_id = _resolve_record_doc_id(record_type, record_id)
    target = CanonicalTarget(doc_id=doc_id, doc_type=record_type, allow_create=False, require_existing=True)

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        if state.doc is None:
            raise _shared.QueueProcessorError(f"No canonical {record_type} with id {record_id} found")
        outgoing = dict(state.doc)
        outgoing.update(fields)
        if "site_id" in fields:
            outgoing["site_name"] = _site_name_for_site_id(fields["site_id"])
        outgoing["updated_at"] = occurred_at
        outgoing["edited_by"] = actor
        return CanonicalMutation(doc=outgoing, evidence_text=f"{record_type} {record_id} edited")

    if context.dry_run:
        _dry_run_edit(job, context, doc_id, record_type)
        return

    try:
        canonical_doc = apply_canonical_rmw(_shared._vault_store(), target, job.job_id, transform)
    except _shared.QueueProcessorError:
        raise
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={doc_id}: {exc}"
        ) from exc
    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {doc_id}")
    _shared.write_mutation_evidence(context, job, canonical_doc, f"{record_type} {record_id} edited")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=edit-record-fields status=success error=")


def _dry_run_edit(job: QueueJob, context: RunContext, doc_id: str, record_type: str) -> None:
    try:
        current_doc = _shared._vault_store().get_optional(doc_id)
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={doc_id}: {exc}"
        ) from exc
    if current_doc is None:
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={doc_id}: "
            f"CouchDB required document not found for canonical RMW: {doc_id}"
        )
    if job.job_id in [str(value).strip() for value in current_doc.get("btq_job_ids") or []]:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {doc_id}")
    print(f"Job {job.job_id}: would edit {record_type}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=edit-record-fields status=success error=")
