from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from queue_processor.canonical_rmw import CanonicalEntityState, CanonicalMutation, CanonicalTarget, apply_canonical_rmw

from . import _shared
from .record_edit import process_edit_record_fields_job
from ._shared import QueueJob, QueueProcessorError, RunContext
from .supplies_equipment import (
    equipment_request_status,
    supply_need_status,
)

def _resolve_supply_doc_id(supply_id: str) -> str:
    """Resolve the canonical supply_need ``_id`` for a bare ``supply_id``.

    Canonical docs are stored under path-derived ids
    (``supply_need_accounts_.._<supply_id>_<slug>``), so the legacy
    ``supply_need_<supply_id>`` construction misses them and every
    mark-supply-* transition fails the require-existing RMW lookup. Query the
    canonical store by the ``supply_id`` field; fall back to the legacy flat id
    when no doc matches (preserves behavior for older flat-id docs) or when the
    store is unreachable (the RMW itself then surfaces the real error).
    """
    legacy = f"supply_need_{supply_id}"
    try:
        docs = _shared._vault_store().find_supply_need_docs_by_supply_id(supply_id)
    except Exception:
        return legacy
    ids = sorted(str(doc.get("_id")) for doc in docs if doc.get("_id"))
    return ids[0] if ids else legacy


def _resolve_equipment_doc_id(equipment_id: str) -> str:
    """Resolve the canonical equipment_request ``_id`` for a bare ``equipment_id``.

    See ``_resolve_supply_doc_id`` — same path-derived id problem and fallback.
    """
    legacy = f"equipment_request_{equipment_id}"
    try:
        docs = _shared._vault_store().find_equipment_request_docs_by_equipment_id(equipment_id)
    except Exception:
        return legacy
    ids = sorted(str(doc.get("_id")) for doc in docs if doc.get("_id"))
    return ids[0] if ids else legacy


def _resolve_issue_doc_id(issue_id: str) -> str:
    """Resolve the canonical site_issue ``_id`` for a bare ``issue_id``."""
    legacy = f"site_issue_{issue_id}"
    try:
        docs = _shared._vault_store().find_site_issue_docs_by_issue_id(issue_id)
    except Exception:
        return legacy
    ids = sorted(str(doc.get("_id")) for doc in docs if doc.get("_id"))
    return ids[0] if ids else legacy


def _merge_transition_note(existing_notes: str, note: str) -> str:
    existing_notes = existing_notes.strip()
    note = note.strip()
    if not note:
        return existing_notes
    if not existing_notes:
        return note
    if note in existing_notes.splitlines():
        return existing_notes
    return f"{existing_notes}\n{note}"

def _validate_supply_transition(
    state_doc: dict[str, Any] | None,
    supply_id: str,
    target_status: str,
    valid_source_statuses: tuple[str, ...],
) -> str:
    if state_doc is None:
        raise _shared.QueueProcessorError(f"No canonical supply need with id {supply_id} found")
    current_status = supply_need_status(state_doc)
    if current_status not in valid_source_statuses:
        raise _shared.QueueProcessorError(
            f"Cannot transition supply {supply_id} to {target_status} from current status {current_status}; "
            f"expected one of {valid_source_statuses}"
        )
    return current_status

def _validate_equipment_transition(
    state_doc: dict[str, Any] | None,
    equipment_id: str,
    target_status: str,
    valid_source_statuses: tuple[str, ...],
) -> str:
    if state_doc is None:
        raise _shared.QueueProcessorError(f"No canonical equipment request with id {equipment_id} found")
    current_status = equipment_request_status(state_doc)
    if current_status not in valid_source_statuses:
        raise _shared.QueueProcessorError(
            f"Cannot transition equipment {equipment_id} to {target_status} from current status {current_status}; "
            f"expected one of {valid_source_statuses}"
        )
    return current_status

def _validate_issue_transition(
    state_doc: dict[str, Any] | None,
    issue_id: str,
    target_status: str,
    valid_source_statuses: tuple[str, ...],
) -> str:
    if state_doc is None:
        raise _shared.QueueProcessorError(f"No canonical site issue with id {issue_id} found")
    current_status = str(state_doc.get("status") or "open").strip()
    if current_status not in valid_source_statuses:
        raise _shared.QueueProcessorError(
            f"Cannot transition issue {issue_id} to {target_status} from current status {current_status}; "
            f"expected one of {valid_source_statuses}"
        )
    return current_status

def _process_mark_supply_job(
    job_path: Path,
    job: _shared.QueueJob,
    context: _shared.RunContext,
    processed_dir: Path,
    *,
    target_status: str,
    valid_source_statuses: tuple[str, ...],
    timestamp_field: str | None,
    actor_field: str | None,
    note_field: str | None,
) -> None:
    payload = job.payload
    supply_id = str(payload["supply_id"])
    actor = str(payload["actor"])
    note = str(payload.get("note") or "")
    occurred_at = str(payload.get("occurred_at") or datetime.now(timezone.utc).isoformat())
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    doc_id = _resolve_supply_doc_id(supply_id)
    target = CanonicalTarget(
        doc_id=doc_id,
        doc_type="supply_need",
        allow_create=False,
        require_existing=True,
    )

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        _validate_supply_transition(state.doc, supply_id, target_status, valid_source_statuses)
        outgoing = dict(state.doc or {})
        outgoing["status"] = target_status
        if timestamp_field:
            outgoing[timestamp_field] = occurred_at
        if actor_field:
            outgoing[actor_field] = actor
        if note_field and note:
            outgoing[note_field] = note
        elif note:
            outgoing["notes"] = _merge_transition_note(str(outgoing.get("notes") or ""), note)
        return CanonicalMutation(doc=outgoing, evidence_text=f"supply_need {supply_id} marked {target_status}")

    if context.dry_run:
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
        _validate_supply_transition(current_doc, supply_id, target_status, valid_source_statuses)
        print(f"Job {job.job_id}: validated")
        print(f"Job {job.job_id}: target {doc_id}")
        print(f"Job {job.job_id}: would mark supply {target_status}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=mark-supply-{target_status} status=success error=")
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
    _shared.write_mutation_evidence(
        context,
        job,
        canonical_doc,
        f"supply_need {supply_id} marked {target_status}",
    )

    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=mark-supply-{target_status} status=success error=")

def _process_mark_equipment_job(
    job_path: Path,
    job: _shared.QueueJob,
    context: _shared.RunContext,
    processed_dir: Path,
    *,
    target_status: str,
    valid_source_statuses: tuple[str, ...],
    timestamp_field: str | None,
    actor_field: str | None,
    note_field: str | None,
) -> None:
    payload = job.payload
    equipment_id = str(payload["equipment_id"])
    actor = str(payload["actor"])
    note = str(payload.get("note") or "")
    occurred_at = str(payload.get("occurred_at") or datetime.now(timezone.utc).isoformat())
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    doc_id = _resolve_equipment_doc_id(equipment_id)
    target = CanonicalTarget(
        doc_id=doc_id,
        doc_type="equipment_request",
        allow_create=False,
        require_existing=True,
    )

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        _validate_equipment_transition(state.doc, equipment_id, target_status, valid_source_statuses)
        outgoing = dict(state.doc or {})
        outgoing["status"] = target_status
        if timestamp_field:
            outgoing[timestamp_field] = occurred_at
        if actor_field:
            outgoing[actor_field] = actor
        if note_field and note:
            outgoing[note_field] = note
        elif note:
            outgoing["notes"] = _merge_transition_note(str(outgoing.get("notes") or ""), note)
        return CanonicalMutation(doc=outgoing, evidence_text=f"equipment_request {equipment_id} marked {target_status}")

    if context.dry_run:
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
        _validate_equipment_transition(current_doc, equipment_id, target_status, valid_source_statuses)
        print(f"Job {job.job_id}: validated")
        print(f"Job {job.job_id}: target {doc_id}")
        print(f"Job {job.job_id}: would mark equipment {target_status}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=mark-equipment-{target_status} status=success error=")
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
    _shared.write_mutation_evidence(
        context,
        job,
        canonical_doc,
        f"equipment_request {equipment_id} marked {target_status}",
    )

    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=mark-equipment-{target_status} status=success error=")

def _process_mark_issue_job(
    job_path: Path,
    job: _shared.QueueJob,
    context: _shared.RunContext,
    processed_dir: Path,
    *,
    target_status: str,
    valid_source_statuses: tuple[str, ...],
    timestamp_field: str | None,
    actor_field: str | None,
    note_field: str | None,
) -> None:
    payload = job.payload
    issue_id = str(payload["issue_id"])
    actor = str(payload["actor"])
    note = str(payload.get("note") or "")
    occurred_at = str(payload.get("occurred_at") or datetime.now(timezone.utc).isoformat())
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    doc_id = _resolve_issue_doc_id(issue_id)
    target = CanonicalTarget(
        doc_id=doc_id,
        doc_type="site_issue",
        allow_create=False,
        require_existing=True,
    )

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        _validate_issue_transition(state.doc, issue_id, target_status, valid_source_statuses)
        outgoing = dict(state.doc or {})
        outgoing["status"] = target_status
        if timestamp_field:
            outgoing[timestamp_field] = occurred_at
        if actor_field:
            outgoing[actor_field] = actor
        if note_field and note:
            outgoing[note_field] = note
        return CanonicalMutation(doc=outgoing, evidence_text=f"site_issue {issue_id} marked {target_status}")

    if context.dry_run:
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
        _validate_issue_transition(current_doc, issue_id, target_status, valid_source_statuses)
        print(f"Job {job.job_id}: validated")
        print(f"Job {job.job_id}: target {doc_id}")
        print(f"Job {job.job_id}: would mark issue {target_status}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=mark-issue-{target_status} status=success error=")
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
    _shared.write_mutation_evidence(
        context,
        job,
        canonical_doc,
        f"site_issue {issue_id} marked {target_status}",
    )

    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=mark-issue-{target_status} status=success error=")

ARCHIVABLE_RECORD_TYPES = {"site_issue", "supply_need", "equipment_request", "visit"}


def _resolve_record_doc_id(record_type: str, record_id: str) -> str:
    # Visits have no short-id form; archive jobs must use the full visit_* _id.
    if record_id.startswith(f"{record_type}_"):
        return record_id
    if record_type == "site_issue":
        return _resolve_issue_doc_id(record_id)
    if record_type == "supply_need":
        return _resolve_supply_doc_id(record_id)
    if record_type == "equipment_request":
        return _resolve_equipment_doc_id(record_id)
    if record_type == "visit":
        raise _shared.QueueProcessorError(f"Visit records must be referenced by full canonical _id starting with visit_: {record_id}")
    raise _shared.QueueProcessorError(f"Unsupported archivable record_type: {record_type}")


def _process_mark_record_archive_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path, *, archived: bool) -> None:
    payload = job.payload
    record_type = str(payload.get("record_type") or "").strip()
    record_id = str(payload.get("record_id") or "").strip()
    actor = str(payload.get("actor") or "").strip()
    note = str(payload.get("note") or "").strip()
    occurred_at = datetime.now(timezone.utc).isoformat()
    if record_type not in ARCHIVABLE_RECORD_TYPES or not record_id or not actor:
        raise _shared.QueueProcessorError("mark_record archive job requires valid record_type, record_id, and actor")
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    doc_id = _resolve_record_doc_id(record_type, record_id)
    target = CanonicalTarget(doc_id=doc_id, doc_type=record_type, allow_create=False, require_existing=True)

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        if state.doc is None:
            raise _shared.QueueProcessorError(f"No canonical {record_type} with id {record_id} found")
        outgoing = dict(state.doc)
        if archived:
            outgoing["archived"] = True
            outgoing.setdefault("archived_at", occurred_at)
            outgoing.setdefault("archived_by", actor)
            if note:
                outgoing.setdefault("archive_note", note)
        else:
            outgoing["archived"] = False
            for field in ("archived_at", "archived_by", "archive_note"):
                outgoing.pop(field, None)
        action = "archived" if archived else "unarchived"
        return CanonicalMutation(doc=outgoing, evidence_text=f"{record_type} {record_id} marked {action}")

    if context.dry_run:
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
        print(f"Job {job.job_id}: would {'archive' if archived else 'unarchive'} {record_type}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=mark-record-{'archive' if archived else 'unarchive'} status=success error=")
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
    action = "archive" if archived else "unarchive"
    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {doc_id}")
    _shared.write_mutation_evidence(context, job, canonical_doc, f"{record_type} {record_id} marked {action}d")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=mark-record-{action} status=success error=")


def process_mark_record_archived_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_record_archive_job(job_path, job, context, processed_dir, archived=True)


def process_mark_record_unarchived_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_record_archive_job(job_path, job, context, processed_dir, archived=False)

def process_mark_supply_ordered_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_supply_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="ordered",
        valid_source_statuses=("open",),
        timestamp_field="ordered_at",
        actor_field="ordered_by",
        note_field="ordered_note",
    )

def process_mark_supply_delivered_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_supply_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="delivered",
        valid_source_statuses=("ordered",),
        timestamp_field="delivered_at",
        actor_field="delivered_by",
        note_field="delivered_note",
    )

def process_mark_supply_stocked_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_supply_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="stocked",
        valid_source_statuses=("delivered",),
        timestamp_field="stocked_at",
        actor_field="stocked_by",
        note_field="stocked_note",
    )

def process_mark_supply_no_action_needed_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_supply_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="no_action_needed",
        valid_source_statuses=("open", "ordered", "delivered"),
        timestamp_field=None,
        actor_field=None,
        note_field=None,
    )

def process_mark_equipment_approved_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_equipment_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="approved",
        valid_source_statuses=("open",),
        timestamp_field="approved_at",
        actor_field="approved_by",
        note_field="approval_note",
    )

def process_mark_equipment_denied_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_equipment_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="denied",
        valid_source_statuses=("open", "approved"),
        timestamp_field="denied_at",
        actor_field="denied_by",
        note_field="denial_note",
    )

def process_mark_equipment_ordered_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_equipment_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="ordered",
        valid_source_statuses=("approved",),
        timestamp_field="ordered_at",
        actor_field="ordered_by",
        note_field="ordered_note",
    )

def process_mark_equipment_provided_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_equipment_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="provided",
        valid_source_statuses=("ordered",),
        timestamp_field="provided_at",
        actor_field="provided_by",
        note_field="provided_note",
    )

def process_mark_equipment_no_action_needed_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_equipment_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="no_action_needed",
        valid_source_statuses=("open", "approved", "ordered"),
        timestamp_field=None,
        actor_field=None,
        note_field=None,
    )

def process_mark_issue_monitoring_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_issue_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="monitoring",
        valid_source_statuses=("open",),
        timestamp_field="monitoring_at",
        actor_field="monitoring_by",
        note_field="monitoring_note",
    )

def process_mark_issue_resolved_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_issue_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="resolved",
        valid_source_statuses=("open", "monitoring"),
        timestamp_field="resolved_at",
        actor_field="resolved_by",
        note_field="resolved_note",
    )

def process_mark_issue_open_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    _process_mark_issue_job(
        job_path,
        job,
        context,
        processed_dir,
        target_status="open",
        valid_source_statuses=("monitoring", "resolved"),
        timestamp_field="open_at",
        actor_field="open_by",
        note_field="open_note",
    )
