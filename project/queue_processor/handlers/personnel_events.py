from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btq_vault.entity_types import current_operator_id
from queue_processor.canonical_rmw import (
    CanonicalEntityState,
    CanonicalMutation,
    CanonicalTarget,
    apply_canonical_rmw,
)

from . import _shared
from ._shared import QueueJob, RunContext

def personnel_event_id(payload: dict) -> str:
    explicit = str(payload.get("event_id") or "").strip()
    if explicit:
        return explicit
    employee = str(payload.get("employee") or "").strip()
    canonical_employee = _shared.person_file_name(employee)[:-3] if employee else ""
    seed = {
        "employee": canonical_employee,
        "event_type": str(payload.get("event_type") or "").strip(),
        "occurred_at": str(payload.get("occurred_at") or "").strip(),
        "reported_by": str(payload.get("reported_by") or "").strip(),
    }
    digest = hashlib.sha256(json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return f"evt_{digest}"

def personnel_event_doc_id(payload: dict) -> str:
    employee = str(payload.get("employee") or "").strip()
    if not employee:
        raise _shared.QueueProcessorError("Personnel event requires employee")
    return f"personnel_event_{personnel_event_id(payload)}"

def _canonical_availability_person(payload: dict) -> str:
    employee = str(payload.get("employee") or "").strip()
    if not employee:
        raise _shared.QueueProcessorError("Availability constraint requires employee")
    return _shared.person_file_name(employee)[:-3]

def availability_constraint_id(payload: dict) -> str:
    explicit = str(payload.get("event_id") or "").strip()
    if explicit:
        return explicit
    seed = {
        "person": _canonical_availability_person(payload),
        "constraint_type": str(payload.get("constraint_type") or "").strip(),
        "date": str(payload.get("date") or "").strip(),
    }
    digest = hashlib.sha256(json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return f"avail_{digest}"

def availability_constraint_doc_id(payload: dict) -> str:
    return f"availability_constraint_{availability_constraint_id(payload)}"

def _build_personnel_event_entity_doc(payload: dict, job: QueueJob, event_id: str) -> dict:
    return {
        "_id": f"personnel_event_{event_id}",
        "type": "personnel_event",
        "operator": current_operator_id(),
        "event_id": event_id,
        "btq_job_ids": [job.job_id],
        **{k: v for k, v in payload.items()},
    }

def process_log_personnel_event_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    event_id = personnel_event_id(payload)
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    created_at = datetime.now(timezone.utc).isoformat()
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target personnel_event_{event_id}")
    if context.dry_run:
        print(f"Job {job.job_id}: would log personnel event")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=log-personnel-event status=success error=")
        return

    target = CanonicalTarget(
        doc_id=f"personnel_event_{event_id}",
        doc_type="personnel_event",
        allow_create=True,
        require_existing=False,
    )

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        is_new_doc = state.doc is None or set(state.doc) <= {"_id", "type"}
        if is_new_doc:
            doc = {
                "_id": target.doc_id,
                "type": "personnel_event",
                "operator": current_operator_id(),
                "event_id": event_id,
                **{k: v for k, v in payload.items()},
                "created_at": created_at,
            }
        else:
            doc = dict(state.doc)
            existing_created_at = doc.get("created_at") or created_at
            doc.update(payload)
            doc["created_at"] = existing_created_at
            doc.setdefault("operator", current_operator_id())
            doc["event_id"] = event_id
        return CanonicalMutation(doc=doc, evidence_text=f"personnel_event {event_id}")

    try:
        canonical_doc = apply_canonical_rmw(_shared._vault_store(), target, job.job_id, transform)
    except Exception as exc:
        entity_id = target.doc_id
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}"
        ) from exc

    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return

    _shared.write_mutation_evidence(context, job, canonical_doc, f"personnel_event {event_id}")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=log-personnel-event status=success error=")

def process_log_availability_constraint_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    constraint_id = availability_constraint_id(payload)
    person_id = _canonical_availability_person(payload)
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    created_at = datetime.now(timezone.utc).isoformat()
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target availability_constraint_{constraint_id}")
    if context.dry_run:
        print(f"Job {job.job_id}: would log availability constraint")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=log-availability-constraint status=success error=")
        return

    target = CanonicalTarget(
        doc_id=f"availability_constraint_{constraint_id}",
        doc_type="availability_constraint",
        allow_create=True,
        require_existing=False,
    )

    def doc_fields() -> dict[str, Any]:
        fields = {
            "person_id": person_id,
            "constraint_type": str(payload.get("constraint_type") or "").strip(),
            "date": str(payload.get("date") or "").strip(),
            "reported_by": payload["reported_by"],
            "source_text": payload["source_text"],
        }
        for field in ("related_site", "reported_at"):
            if field in payload:
                fields[field] = payload[field]
        return fields

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        is_new_doc = state.doc is None or set(state.doc) <= {"_id", "type"}
        if is_new_doc:
            doc = {
                "_id": target.doc_id,
                "type": "availability_constraint",
                "operator": current_operator_id(),
                **doc_fields(),
                "created_at": created_at,
            }
        else:
            doc = dict(state.doc)
            existing_created_at = doc.get("created_at") or created_at
            doc.update(doc_fields())
            doc["created_at"] = existing_created_at
            doc.setdefault("operator", current_operator_id())
        return CanonicalMutation(doc=doc, evidence_text=f"availability_constraint {constraint_id}")

    try:
        canonical_doc = apply_canonical_rmw(_shared._vault_store(), target, job.job_id, transform)
    except Exception as exc:
        entity_id = target.doc_id
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}"
        ) from exc

    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return

    _shared.write_mutation_evidence(context, job, canonical_doc, f"availability_constraint {constraint_id}")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=log-availability-constraint status=success error=")

