from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from queue_processor.canonical_rmw import (
    CanonicalEntityState,
    CanonicalMutation,
    CanonicalTarget,
    apply_canonical_rmw,
    resolve_employee_target,
)

from . import _shared
from ._shared import QueueJob, RunContext


def process_set_employee_contact_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    person = str(payload["person"]).strip()
    actor = str(payload["actor"]).strip()
    contact = payload["contact"]
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    store = _shared._vault_store()
    target = resolve_employee_target(store, person)
    updates = {
        field: (None if value is None else str(value).strip())
        for field, value in contact.items()
        if field in {"phone", "email"}
    }
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        if store.get_optional(target.doc_id) is None:
            raise _shared.QueueJobError(f"No canonical employee found for {person}")
        print(f"Job {job.job_id}: would update employee contact")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=set-employee-contact status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc or {})
        outgoing.update(updates)
        outgoing["updated_at"] = datetime.now(timezone.utc).isoformat()
        outgoing["edited_by"] = actor
        return CanonicalMutation(doc=outgoing, evidence_text=f"employee contact updated on {target.doc_id}")

    try:
        canonical_doc = apply_canonical_rmw(store, target, job.job_id, transform)
    except _shared.QueueProcessorError:
        raise
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}"
        ) from exc

    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return

    _shared.write_mutation_evidence(context, job, canonical_doc, f"employee contact updated on {target.doc_id}")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=set-employee-contact status=success error=")


def process_set_employee_home_address_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    person = str(payload["person"]).strip()
    actor = str(payload["actor"]).strip()
    action = str(payload.get("action") or "set").strip()
    address = _shared.normalized_home_address(payload.get("home_address")) if action == "set" else None
    source = str(payload.get("source") or "").strip()
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    store = _shared._vault_store()
    target = resolve_employee_target(store, person)
    # Evidence snapshots are the audit trail and carry the full doc; log lines
    # and printed output must never contain the address itself.
    evidence_text = f"employee home address {'cleared' if action == 'clear' else 'updated'} on {target.doc_id}"
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        if store.get_optional(target.doc_id) is None:
            raise _shared.QueueJobError(f"No canonical employee found for {person}")
        print(f"Job {job.job_id}: would {'clear' if action == 'clear' else 'update'} employee home address")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=set-employee-home-address status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc or {})
        if action == "clear":
            outgoing.pop("home_address", None)
            outgoing.pop("home_address_source", None)
        else:
            outgoing["home_address"] = address
            if source:
                outgoing["home_address_source"] = source
        outgoing["updated_at"] = datetime.now(timezone.utc).isoformat()
        outgoing["edited_by"] = actor
        return CanonicalMutation(doc=outgoing, evidence_text=evidence_text)

    try:
        canonical_doc = apply_canonical_rmw(store, target, job.job_id, transform)
    except _shared.QueueProcessorError:
        raise
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}"
        ) from exc

    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return

    _shared.write_mutation_evidence(context, job, canonical_doc, evidence_text)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=set-employee-home-address status=success error=")


def process_set_employee_uniform_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    person = str(payload["person"]).strip()
    actor = str(payload["actor"]).strip()
    uniform = payload["uniform"]
    source = str(payload.get("source") or "").strip()
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    store = _shared._vault_store()
    target = resolve_employee_target(store, person)
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        if store.get_optional(target.doc_id) is None:
            raise _shared.QueueJobError(f"No canonical employee found for {person}")
        print(f"Job {job.job_id}: would update employee uniform status")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=set-employee-uniform status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc or {})
        outgoing["uniform_status"] = str(uniform["status"]).strip()
        if "shirt_count" in uniform:
            outgoing["uniform_shirt_count"] = int(uniform["shirt_count"])
        else:
            outgoing.pop("uniform_shirt_count", None)
        if "shirt_size" in uniform:
            outgoing["uniform_shirt_size"] = str(uniform["shirt_size"]).strip()
        else:
            outgoing.pop("uniform_shirt_size", None)
        outgoing["uniform_updated_at"] = datetime.now(timezone.utc).isoformat()
        outgoing["uniform_updated_by"] = actor
        if source:
            outgoing["uniform_source"] = source
        else:
            outgoing.pop("uniform_source", None)
        outgoing["updated_at"] = outgoing["uniform_updated_at"]
        outgoing["edited_by"] = actor
        return CanonicalMutation(doc=outgoing, evidence_text=f"employee uniform status updated on {target.doc_id}")

    try:
        canonical_doc = apply_canonical_rmw(store, target, job.job_id, transform)
    except _shared.QueueProcessorError:
        raise
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}"
        ) from exc

    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return

    evidence_text = f"employee uniform status updated on {target.doc_id}"
    _shared.write_mutation_evidence(context, job, canonical_doc, evidence_text)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=set-employee-uniform status=success error=")


def _ensure_employee_id_available_for_target(store, employee_id: str, target_doc_id: str, job: QueueJob) -> None:
    try:
        docs = store.find_employee_docs()
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb employee_id duplicate check failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={target_doc_id}: {exc}"
        ) from exc

    target_person_id = target_doc_id.removeprefix("employee_")
    for doc in docs:
        existing_employee_id = str(doc.get("employee_id") or "").strip()
        if not existing_employee_id or existing_employee_id != employee_id:
            continue
        doc_id = str(doc.get("_id") or "").strip()
        person_id = str(doc.get("person_id") or "").strip()
        if doc_id == target_doc_id or (person_id and person_id == target_person_id):
            continue
        existing_entity_id = doc_id or (f"employee_{person_id}" if person_id else "<unknown>")
        raise _shared.QueueJobError(
            "employee_id already assigned to another person "
            f"job_type={job.job_type} job_id={job.job_id} employee_id={employee_id} "
            f"existing_entity_id={existing_entity_id} target_entity_id={target_doc_id}"
        )


def process_set_employee_id_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    person = str(payload["person"]).strip()
    employee_id = str(payload["employee_id"]).strip()
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    store = _shared._vault_store()
    try:
        resolved_target = resolve_employee_target(store, person)
    except _shared.QueueProcessorError:
        raise
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={person}: {exc}"
        ) from exc
    target = CanonicalTarget(
        doc_id=resolved_target.doc_id,
        doc_type=resolved_target.doc_type,
        allow_create=False,
        require_existing=True,
    )
    _ensure_employee_id_available_for_target(store, employee_id, target.doc_id, job)

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        try:
            if store.get_optional(target.doc_id) is None:
                raise _shared.QueueJobError(
                    "canonical couchdb write failed "
                    f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: "
                    f"CouchDB required document not found for canonical RMW: {target.doc_id}"
                )
        except _shared.QueueJobError:
            raise
        except Exception as exc:
            raise _shared.QueueJobError(
                "canonical couchdb write failed "
                f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}"
            ) from exc
        print(f"Job {job.job_id}: would set employee_id={employee_id} on {target.doc_id}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=set-employee-id status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        _ensure_employee_id_available_for_target(store, employee_id, target.doc_id, job)
        outgoing = dict(state.doc or {})
        outgoing["employee_id"] = employee_id
        for field in ("employment_type", "hire_date", "status"):
            if field in payload:
                outgoing[field] = str(payload[field]).strip()
        return CanonicalMutation(doc=outgoing, evidence_text=f"set employee_id={employee_id} on {target.doc_id}")

    try:
        canonical_doc = apply_canonical_rmw(store, target, job.job_id, transform)
    except _shared.QueueJobError:
        raise
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}"
        ) from exc

    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return

    evidence_text = f"set employee_id={employee_id} on {target.doc_id}"
    _shared.write_mutation_evidence(context, job, canonical_doc, evidence_text)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=set-employee-id status=success error=")
    _shared.structured_log(
        context,
        "set-employee-id",
        computed_job_id=job.job_id,
        job_type=job.job_type,
        entity_type="employee",
        entity_id=target.doc_id,
        employee_id=employee_id,
        target_path="",
        capture_id=_shared.capture_id_for_job(job),
    )
