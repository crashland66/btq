from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btq_vault.markdown_export import render_equipment_request_markdown, render_supply_need_markdown
from queue_processor.canonical_rmw import CanonicalEntityState, CanonicalMutation, CanonicalTarget, apply_canonical_rmw
from site_equipment import EquipmentRequest, parse_site_equipment
from site_supplies import SupplyNeed, parse_site_supply

from . import _shared
from ._shared import QueueJob, QueueProcessorError, RunContext
from .supplies_equipment import (
    equipment_request_status,
    supply_need_status,
)

def locate_supply_file_by_id(context: RunContext, supply_id: str) -> Path | None:
    supply_id = supply_id.strip()
    if not supply_id:
        return None
    accounts_root = _shared.ensure_within_root(context.vault_root / "Accounts", context.vault_root, "Accounts root")
    matches = sorted(accounts_root.glob(f"*/Locations/*/Supplies/{supply_id}__*.md"))
    return _shared.ensure_within_root(matches[0], context.vault_root, "Supply target") if matches else None

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


def locate_equipment_file_by_id(context: RunContext, equipment_id: str) -> Path | None:
    equipment_id = equipment_id.strip()
    if not equipment_id:
        return None
    accounts_root = _shared.ensure_within_root(context.vault_root / "Accounts", context.vault_root, "Accounts root")
    matches = sorted(accounts_root.glob(f"*/Locations/*/Equipment/{equipment_id}__*.md"))
    return _shared.ensure_within_root(matches[0], context.vault_root, "Equipment target") if matches else None

def supply_as_payload(supply: SupplyNeed) -> dict:
    payload: dict[str, object] = {
        "supply_id": supply.supply_id,
        "site_id": supply.site_id,
        "site_name": supply.site_name,
        "account": supply.account,
        "item_name": supply.item_name,
        "quantity_needed": supply.quantity_needed,
        "urgency": supply.urgency,
        "requested_by": supply.requested_by,
        "observed_at": supply.observed_at,
        "source": supply.source,
        "status": supply.status,
        "notes": supply.notes,
        "related_capture_ids": list(supply.related_capture_ids),
        "related_candidate_ids": list(supply.related_candidate_ids),
        "created_at": supply.created_at,
        "ordered_at": supply.ordered_at,
        "ordered_by": supply.ordered_by,
        "ordered_note": supply.ordered_note,
        "delivered_at": supply.delivered_at,
        "delivered_by": supply.delivered_by,
        "delivered_note": supply.delivered_note,
        "stocked_at": supply.stocked_at,
        "stocked_by": supply.stocked_by,
        "stocked_note": supply.stocked_note,
    }
    return {key: value for key, value in payload.items() if value not in ("", [])}

def equipment_as_payload(equipment: EquipmentRequest) -> dict:
    payload: dict[str, object] = {
        "equipment_id": equipment.equipment_id,
        "site_id": equipment.site_id,
        "site_name": equipment.site_name,
        "account": equipment.account,
        "equipment_name": equipment.equipment_name,
        "reason": equipment.reason,
        "priority": equipment.priority,
        "requested_by": equipment.requested_by,
        "observed_at": equipment.observed_at,
        "source": equipment.source,
        "status": equipment.status,
        "notes": equipment.notes,
        "related_capture_ids": list(equipment.related_capture_ids),
        "related_candidate_ids": list(equipment.related_candidate_ids),
        "created_at": equipment.created_at,
        "approved_at": equipment.approved_at,
        "approved_by": equipment.approved_by,
        "approval_note": equipment.approval_note,
        "denied_at": equipment.denied_at,
        "denied_by": equipment.denied_by,
        "denial_note": equipment.denial_note,
        "ordered_at": equipment.ordered_at,
        "ordered_by": equipment.ordered_by,
        "ordered_note": equipment.ordered_note,
        "provided_at": equipment.provided_at,
        "provided_by": equipment.provided_by,
        "provided_note": equipment.provided_note,
    }
    return {key: value for key, value in payload.items() if value not in ("", [])}

def preserve_frontmatter_lists(payload: dict, existing_text: str, fields: tuple[str, ...]) -> None:
    frontmatter, _body, has_frontmatter = _shared.parse_frontmatter_text(existing_text)
    if not has_frontmatter:
        return
    for field in fields:
        value = frontmatter.get(field)
        if isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            if cleaned and not payload.get(field):
                payload[field] = cleaned

def _job_ids_with(existing_text: str, job_id: str) -> list[str]:
    frontmatter, _body, has_frontmatter = _shared.parse_frontmatter_text(existing_text)
    values = frontmatter.get("btq_job_ids") if has_frontmatter else None
    job_ids = [str(value).strip() for value in values if str(value).strip()] if isinstance(values, list) else []
    if job_id not in job_ids:
        job_ids.append(job_id)
    return job_ids

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

def _canonical_projection_payload(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in doc.items()
        if key not in {"_id", "_rev", "type", "operator", "btq_job_ids", "created_at"}
    }

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
    target_path = locate_supply_file_by_id(context, supply_id)
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    doc_id = _resolve_supply_doc_id(supply_id)
    target = CanonicalTarget(
        doc_id=doc_id,
        doc_type="supply_need",
        projection_path=target_path,
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
        print(f"Job {job.job_id}: target {target_path or doc_id}")
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
    print(f"Job {job.job_id}: target {target_path or doc_id}")
    if target_path is not None:
        existing_text = target_path.read_text(encoding="utf-8")
        projection_payload = _canonical_projection_payload(canonical_doc)
        final_text = render_supply_need_markdown(
            payload=projection_payload,
            site_id=str(canonical_doc.get("site_id") or ""),
            site_name=str(canonical_doc.get("site_name") or ""),
            account=str(canonical_doc.get("account") or ""),
            supply_id=str(canonical_doc.get("supply_id") or supply_id),
            job_id=job.job_id,
            created_at=str(canonical_doc.get("created_at") or ""),
            existing_text=existing_text,
        )
        for canonical_job_id in canonical_doc.get("btq_job_ids") or []:
            final_text = _shared.upsert_job_id_frontmatter(final_text, str(canonical_job_id))
        _shared.atomic_write_text(target_path, final_text)
        _shared.write_mutation_evidence(
            context,
            job,
            target_path,
            existing_text,
            final_text,
            f"supply_need {supply_id} marked {target_status}",
        )

    moved_path = _shared.move_job_file(job_path, processed_dir)
    if target_path is not None:
        print(f"Job {job.job_id}: updated {target_path}")
    else:
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
    target_path = locate_equipment_file_by_id(context, equipment_id)
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    doc_id = _resolve_equipment_doc_id(equipment_id)
    target = CanonicalTarget(
        doc_id=doc_id,
        doc_type="equipment_request",
        projection_path=target_path,
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
        print(f"Job {job.job_id}: target {target_path or doc_id}")
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
    print(f"Job {job.job_id}: target {target_path or doc_id}")
    if target_path is not None:
        existing_text = target_path.read_text(encoding="utf-8")
        projection_payload = _canonical_projection_payload(canonical_doc)
        final_text = render_equipment_request_markdown(
            payload=projection_payload,
            site_id=str(canonical_doc.get("site_id") or ""),
            site_name=str(canonical_doc.get("site_name") or ""),
            account=str(canonical_doc.get("account") or ""),
            equipment_id=str(canonical_doc.get("equipment_id") or equipment_id),
            job_id=job.job_id,
            created_at=str(canonical_doc.get("created_at") or ""),
            existing_text=existing_text,
        )
        for canonical_job_id in canonical_doc.get("btq_job_ids") or []:
            final_text = _shared.upsert_job_id_frontmatter(final_text, str(canonical_job_id))
        _shared.atomic_write_text(target_path, final_text)
        _shared.write_mutation_evidence(
            context,
            job,
            target_path,
            existing_text,
            final_text,
            f"equipment_request {equipment_id} marked {target_status}",
        )

    moved_path = _shared.move_job_file(job_path, processed_dir)
    if target_path is not None:
        print(f"Job {job.job_id}: updated {target_path}")
    else:
        print(f"Job {job.job_id}: updated {doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=mark-equipment-{target_status} status=success error=")

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
