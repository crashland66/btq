from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from btq_vault.couch_store import CANONICAL_EMPLOYEE_FIELDS
from btq_vault.entity_types import current_operator_id
from queue_processor.canonical_rmw import (
    CanonicalEntityState,
    CanonicalMutation,
    CanonicalTarget,
    apply_canonical_rmw,
    resolve_employee_target,
    resolve_site_target,
)
from queue_processor import idempotency_ledger

from . import _shared
from .site_flags_notes import (
    location_content_append_active_visit_key,
    process_employee_content_append_job,
    process_location_content_append_job,
)
from ._shared import (
    QueueJob,
    QueueProcessorError,
    RunContext,
    slugify_issue_component,
)

def completed_idempotency_record(context: _shared.RunContext, job: _shared.QueueJob) -> Optional[dict[str, Any]]:
    if not job.idempotency_key:
        return None
    record = idempotency_ledger.latest_record_for(
        idempotency_ledger.ledger_path_for(context.runtime_root),
        job.idempotency_key,
    )
    if record is None:
        return None
    expected_payload_hash = idempotency_ledger.payload_hash_for(job.job_type, job.payload)
    if record.get("job_type") != job.job_type or record.get("payload_hash") != expected_payload_hash:
        raise _shared.QueueProcessorError(f"Idempotency key conflict for {job.idempotency_key}")
    return record

def append_idempotency_record(
    job_path: Path,
    job: _shared.QueueJob,
    context: _shared.RunContext,
    target_path: Path,
    person_id: Optional[str] = None,
) -> None:
    if context.dry_run or not job.idempotency_key:
        return
    record = idempotency_ledger.build_record(
        idempotency_key=job.idempotency_key,
        job_type=job.job_type,
        payload=job.payload,
        computed_job_id=job.job_id,
        target_path=target_path,
        person_id=person_id,
        source_queue_file=job_path,
        run_id=context.run_id,
    )
    idempotency_ledger.append_record(idempotency_ledger.ledger_path_for(context.runtime_root), record)
    _shared.structured_log(
        context,
        "idempotency_key_completed",
        computed_job_id=job.job_id,
        job_type=job.job_type,
        idempotency_key=job.idempotency_key,
        target_path=str(target_path),
        person_id=person_id,
        capture_id=_shared.capture_id_for_job(job),
    )

CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

def normalize_employee_name(value: str) -> str:
    return " ".join(value.strip().lower().replace(",", " ").split())

def encode_crockford_base32(value: int, length: int) -> str:
    characters: List[str] = []
    for _ in range(length):
        characters.append(CROCKFORD_BASE32[value & 31])
        value >>= 5
    return "".join(reversed(characters))

def generate_person_id() -> str:
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    random_value = int.from_bytes(os.urandom(10), "big")
    return f"per_{encode_crockford_base32(timestamp_ms, 10)}{encode_crockford_base32(random_value, 16)}"

def validate_person_id(value: str) -> bool:
    if not value.startswith("per_") or len(value) != 30:
        return False
    return all(character in CROCKFORD_BASE32 for character in value[4:])

def person_name_key(record: dict) -> tuple[str, str]:
    # Order/middle-initial-insensitive (last, first-token) key: both
    # "Dalton, Eric D." and "Eric Daniel Dalton" yield ("dalton", "eric").
    # Used to derive a readable person_id and to catch format-only duplicates.
    first = str(record.get("first") or "").strip()
    last = str(record.get("last") or "").strip()
    if not (first and last):
        name = " ".join(str(record.get("name") or "").strip().split())
        if "," in name:
            last_part, first_part = [part.strip() for part in name.split(",", 1)]
            last = last or last_part
            first = first or first_part
        else:
            parts = name.split()
            if len(parts) >= 2:
                first = first or " ".join(parts[:-1])
                last = last or parts[-1]
            elif parts:
                last = last or parts[0]
    first_tokens = first.split()
    first_token = first_tokens[0] if first_tokens else ""
    return (last.lower(), first_token.lower())

def derive_person_id_base(payload: dict) -> Optional[str]:
    # Build a readable `lastname_firstname` base; None if no name slug is possible
    # (caller then falls back to an opaque id).
    last, first = person_name_key(payload)
    last_slug = slugify_issue_component(last).replace("-", "_")
    first_slug = slugify_issue_component(first).replace("-", "_")
    if last_slug and first_slug:
        return f"{last_slug}_{first_slug}"
    if last_slug:
        return last_slug
    whole = slugify_issue_component(str(payload.get("name") or "")).replace("-", "_")
    return whole or None

def employee_doc_id_from_person_file(path: Path) -> str:
    stem = path.stem.strip()
    if "," in stem:
        last, first = [part.strip() for part in stem.split(",", 1)]
        name = f"{last} {first}".strip()
    else:
        name = stem
    slug = slugify_issue_component(name).replace("-", "_")
    if not slug:
        raise QueueProcessorError(f"Employee status update cannot derive employee document id: {path}")
    return f"employee_{slug}"

def _employee_doc_id_taken(store, doc_id: str) -> bool:
    get_optional = getattr(store, "get_optional", None)
    docs = getattr(store, "docs", None)
    if get_optional is not None:
        return get_optional(doc_id) is not None
    return any(doc.get("_id") == doc_id for doc in docs)

def generate_unique_person_id_canonical(store, payload: Optional[dict] = None) -> str:
    get_optional = getattr(store, "get_optional", None)
    docs = getattr(store, "docs", None)
    if get_optional is None and not (store.__class__.__name__ == "RecordingVaultStore" and isinstance(docs, list)):
        raise AttributeError(f"{store.__class__.__name__} object has no attribute 'get_optional'")
    # Prefer a readable `lastname_firstname` id (matches the established convention
    # for the bulk of the roster); disambiguate same-name collisions with a numeric
    # suffix. Fall back to an opaque id only when the name yields no slug.
    base = derive_person_id_base(payload) if payload else None
    if base:
        if not _employee_doc_id_taken(store, f"employee_{base}"):
            return base
        for suffix in range(2, 100):
            candidate = f"{base}_{suffix}"
            if not _employee_doc_id_taken(store, f"employee_{candidate}"):
                return candidate
    for _ in range(10):
        person_id = generate_person_id()
        if not _employee_doc_id_taken(store, f"employee_{person_id}"):
            return person_id
    raise _shared.QueueProcessorError("Could not generate unique person_id")

def canonical_employee_name(doc: dict[str, Any]) -> str:
    name = str(doc.get("name") or "").strip()
    if name:
        return name
    first = str(doc.get("first") or "").strip()
    last = str(doc.get("last") or "").strip()
    return f"{first} {last}".strip()

def ensure_canonical_person_does_not_exist(
    job: QueueJob, name: str, employee_id: Optional[object], payload: Optional[dict] = None
) -> None:
    normalized_name = normalize_employee_name(name)
    normalized_employee_id = None if employee_id is None else str(employee_id).strip()
    # Order/middle-initial-insensitive key so "Dalton, Eric D." is recognized
    # as a duplicate of an existing "Eric Daniel Dalton".
    new_key = person_name_key(payload if payload is not None else {"name": name})
    try:
        docs = _shared._vault_store().find_employee_docs()
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb duplicate check failed "
            f"job_type={job.job_type} job_id={job.job_id}: {exc}"
        ) from exc

    for doc in docs:
        existing_employee_id = str(doc.get("employee_id") or "").strip()
        if normalized_employee_id and existing_employee_id == normalized_employee_id:
            raise _shared.QueueProcessorError(f"Duplicate employee_id for add_person: {normalized_employee_id}")
        existing_name = canonical_employee_name(doc)
        if existing_name and normalize_employee_name(existing_name) == normalized_name:
            raise _shared.QueueProcessorError(f"Duplicate person name for add_person: {name}")
        if new_key[0] and new_key[1] and person_name_key(doc) == new_key:
            raise _shared.QueueProcessorError(f"Duplicate person name for add_person: {name}")

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

def _build_employee_entity_doc(payload: dict, job: QueueJob, person_id: str, created_date: str) -> dict:
    doc = {
        "_id": f"employee_{person_id}",
        "type": "employee",
        "operator": current_operator_id(),
        "person_id": person_id,
        "name": payload["name"],
        "site_ids": payload.get("site_ids") or [],
        "status": payload.get("status") or "active",
        "created_at": created_date,
        "btq_job_ids": [job.job_id],
    }
    doc.update({key: payload[key] for key in CANONICAL_EMPLOYEE_FIELDS if key in payload})
    return doc

def process_add_person_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    name = str(payload["name"]).strip()
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    idempotency_record = completed_idempotency_record(context, job)
    if idempotency_record is not None:
        if context.dry_run:
            print(f"Job {job.job_id}: idempotency key already completed — skipping")
            _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=idempotency-key-already-completed")
            return
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: idempotency key already completed — skipping")
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=idempotency-key-already-completed")
        return

    ensure_canonical_person_does_not_exist(job, name, payload.get("employee_id"), payload)

    created_date = datetime.now(timezone.utc).date().isoformat()
    try:
        person_id = generate_unique_person_id_canonical(_shared._vault_store(), payload)
    except _shared.QueueProcessorError:
        raise
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb person_id uniqueness check failed "
            f"job_type={job.job_type} job_id={job.job_id}: {exc}"
        ) from exc
    target_doc_id = f"employee_{person_id}"
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target_doc_id}")

    if context.dry_run:
        print(f"Job {job.job_id}: would add person")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=add-person status=success error=")
        return

    canonical_doc = _shared._canonical_vault_upsert(job, _build_employee_entity_doc(payload, job, person_id, created_date))
    append_idempotency_record(job_path, job, context, Path(target_doc_id), person_id)
    _shared.write_mutation_evidence(context, job, canonical_doc, f"employee {person_id}")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: created {target_doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=add-person status=success error=")

def process_trigger_recruiting_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    priority = str(payload.get("priority", "normal"))
    open_positions = payload.get("open_positions")
    details = str(payload["details"]).strip()
    count_text = f" | open_positions={open_positions}" if open_positions not in (None, "") else ""
    event_date = _shared.payload_date(payload)
    site_name = str(payload["site"]).strip()
    target = resolve_site_target(site_name)
    site_id = target.doc_id.removeprefix("location_")
    visit_key = location_content_append_active_visit_key(context, _shared._vault_store(), site_name, site_id, event_date)
    note_line = _shared.append_visit_key_suffix(f"{event_date} — priority={priority}{count_text} — {details}", visit_key)
    process_location_content_append_job(
        job_path,
        job,
        context,
        processed_dir,
        site=site_name,
        event_date=event_date,
        visit_key=visit_key,
        subheading="### Recruiting Triggers",
        note_line=note_line,
        action="trigger-recruiting",
        dry_run_message="would append recruiting trigger",
    )

def process_close_recruiting_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    store = _shared._vault_store()
    outcome = str(payload["outcome"])
    filled_by = str(payload.get("filled_by") or "").strip()
    trigger_id = str(payload.get("recruiting_trigger_id") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    event_date = _shared.payload_date(payload)
    site_name = str(payload["site"]).strip()
    try:
        target_site = resolve_site_target(site_name)
    except _shared.QueueProcessorError:
        if " - " not in site_name: raise
        target_site = resolve_site_target(site_name.split(" - ", 1)[0].strip())
    site_id = target_site.doc_id.removeprefix("location_")
    visit_key = location_content_append_active_visit_key(context, store, site_name, site_id, event_date)
    parts = [event_date, f"outcome={outcome}"]
    for item in (f"filled_by={filled_by}" if filled_by else "", f"trigger_id={trigger_id}" if trigger_id else "", notes):
        if item: parts.append(item)
    note_line = _shared.append_visit_key_suffix(" — ".join(parts), visit_key)

    person_note_line = f"{event_date} — placed at {site_name}{f' — {notes}' if notes else ''}" if outcome == "filled" and filled_by else None
    emp_target: CanonicalTarget | None = None
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target_site.doc_id}")
    def canonical_error(entity_id: str, exc: Exception) -> _shared.QueueJobError:
        return _shared.QueueJobError("canonical couchdb write failed " f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}")
    def apply(target: CanonicalTarget, transform) -> dict[str, Any] | None:
        try:
            return apply_canonical_rmw(store, target, job.job_id, transform)
        except Exception as exc:
            raise canonical_error(target.doc_id, exc) from exc
    def append_content_transform(section: str, text: str):
        def transform(state: CanonicalEntityState) -> CanonicalMutation:
            outgoing = dict(state.doc) if state.doc is not None else {}
            outgoing["content"] = _shared.append_to_markdown_section(str(outgoing.get("content") or ""), section, text)
            return CanonicalMutation(doc=outgoing, evidence_text=text)
        return transform
    if context.dry_run:
        try:
            site_doc = store.get_optional(target_site.doc_id)
        except Exception as exc:
            raise canonical_error(target_site.doc_id, exc) from exc
        if site_doc is None:
            raise canonical_error(target_site.doc_id, Exception(f"CouchDB required document not found for canonical RMW: {target_site.doc_id}"))
        if job.job_id in [str(item).strip() for item in site_doc.get("btq_job_ids") or [] if str(item).strip()]:
            print(f"Job {job.job_id}: job_id marker already present — skipping")
            _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
            return
        if person_note_line is not None:
            emp_target = resolve_employee_target(store, filled_by)
            print(f"Job {job.job_id}: also writing {emp_target.doc_id}")
        print(f"Job {job.job_id}: would append recruiting closure")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=close-recruiting status=success error=")
        return

    if visit_key is None:
        gap_target = CanonicalTarget(doc_id=f"visit_gap_{site_id}_{event_date}", doc_type="visit_gap", allow_create=True, require_existing=False)
        def gap_transform(state: CanonicalEntityState) -> CanonicalMutation:
            outgoing = dict(state.doc) if state.doc is not None else {}
            for key, value in (("site", site_name), ("site_id", site_id), ("date", event_date), ("reason", "event_without_visit")): outgoing.setdefault(key, value)
            outgoing.setdefault("operator", current_operator_id())
            return CanonicalMutation(doc=outgoing, evidence_text=f"visit_gap {site_id} {event_date}")
        apply(gap_target, gap_transform)
    employee_doc: dict[str, Any] | None = None
    if person_note_line is not None:
        emp_target = resolve_employee_target(store, filled_by)
        print(f"Job {job.job_id}: also writing {emp_target.doc_id}")
        employee_doc = apply(emp_target, append_content_transform("## Schedule Changes", person_note_line))
    site_doc = apply(target_site, append_content_transform("## Operational Notes", f"### Recruiting Closed\n\n{note_line}"))
    if site_doc is None:
        print(f"Job {job.job_id}: job_id marker already present — skipping"); moved_path = _shared.move_job_file(job_path, processed_dir); print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return
    _shared.write_mutation_evidence(context, job, site_doc, note_line)
    if emp_target is not None and person_note_line is not None:
        canonical_employee_doc = employee_doc if employee_doc is not None else store.get_optional(emp_target.doc_id)
        if canonical_employee_doc is not None:
            _shared.write_mutation_evidence(context, job, canonical_employee_doc, f"placed at {site_name}")

    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target_site.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=close-recruiting status=success error=")

def process_remove_from_schedule_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    note_line = f"{_shared.payload_date(payload)} — removed from schedule for {str(payload['site']).strip()}"
    process_employee_content_append_job(
        job_path,
        job,
        context,
        processed_dir,
        employee=str(payload["employee"]),
        section="## Schedule Changes",
        note_line=note_line,
        action="remove-from-schedule",
        dry_run_message="would append schedule removal",
    )

def process_set_entity_status_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    entity_type = str(payload["entity_type"]).strip()
    entity_id = str(payload["entity_id"]).strip()
    status = str(payload["status"]).strip()
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    store = _shared._vault_store()
    if entity_type == "site":
        target = CanonicalTarget(
            doc_id=f"location_{entity_id}",
            doc_type="location",
            allow_create=False,
            require_existing=True,
        )
    elif entity_type == "employee":
        try:
            target = resolve_employee_target(store, entity_id)
        except _shared.QueueProcessorError:
            raise
        except Exception as exc:
            raise _shared.QueueJobError(
                "canonical couchdb write failed "
                f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}"
            ) from exc
        target = CanonicalTarget(
            doc_id=target.doc_id,
            doc_type=target.doc_type,
            allow_create=False,
            require_existing=True,
        )
    else:
        raise _shared.QueueProcessorError(f"Unsupported entity_type for set_entity_status: {entity_type}")

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
        print(f"Job {job.job_id}: would set {entity_type} {entity_id} status={status}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=set-entity-status status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc or {})
        if entity_type == "site":
            outgoing["active"] = status == "active"
            outgoing.pop("status", None)
        else:
            outgoing["status"] = status
        return CanonicalMutation(doc=outgoing, evidence_text=f"{entity_type} {entity_id} status={status}")

    try:
        canonical_doc = apply_canonical_rmw(store, target, job.job_id, transform)
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

    _shared.write_mutation_evidence(context, job, canonical_doc, f"{entity_type} {entity_id} status={status}")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=set-entity-status status=success error=")
    _shared.structured_log(
        context,
        "set-entity-status",
        computed_job_id=job.job_id,
        job_type=job.job_type,
        entity_type=entity_type,
        entity_id=entity_id,
        status=status,
        target_path="",
        capture_id=_shared.capture_id_for_job(job),
    )
