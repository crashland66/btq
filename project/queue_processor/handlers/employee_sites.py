from __future__ import annotations

from pathlib import Path

from btq_vault.employee_assignments import bare_site_id
from queue_processor.canonical_rmw import (
    CanonicalEntityState,
    CanonicalMutation,
    CanonicalTarget,
    apply_canonical_rmw,
    resolve_employee_target,
)

from . import _shared
from ._shared import QueueJob, RunContext


def _normalized_site_ids(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _shared.QueueProcessorError("assign_employee_site requires employee site_ids to be a list when present")
    site_ids: list[str] = []
    seen: set[str] = set()
    for item in value:
        site_id = str(item).strip()
        if site_id and site_id not in seen:
            site_ids.append(site_id)
            seen.add(site_id)
    return site_ids


def _employee_site_ids_after_action(current_site_ids: object, site_id: str, action: str) -> list[str]:
    existing = _normalized_site_ids(current_site_ids)
    if action == "assign":
        if site_id in existing:
            return existing
        return [*existing, site_id]
    target_site_id = bare_site_id(site_id)
    return [value for value in existing if bare_site_id(value) != target_site_id]


def _scrub_legacy_site_assignments(doc: dict, site_id: str) -> list[str]:
    target_site_id = bare_site_id(site_id)
    changed_fields: list[str] = []
    for field in ("job", "additional_jobs", "sites"):
        if field not in doc:
            continue
        current = doc[field]
        if isinstance(current, list):
            scrubbed = [value for value in current if bare_site_id(value) != target_site_id]
            if scrubbed != current:
                doc[field] = scrubbed
                changed_fields.append(field)
        elif bare_site_id(current) == target_site_id:
            doc.pop(field)
            changed_fields.append(field)
    return changed_fields


def _canonical_write_error(job: QueueJob, entity_id: str, exc: Exception) -> _shared.QueueJobError:
    return _shared.QueueJobError(
        "canonical couchdb write failed "
        f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}"
    )


def _resolve_employee_site_target(store: object, employee_id: str, job: QueueJob) -> CanonicalTarget:
    try:
        resolved = resolve_employee_target(store, employee_id)
    except _shared.QueueProcessorError:
        raise
    except Exception as exc:
        raise _canonical_write_error(job, employee_id, exc) from exc
    return CanonicalTarget(
        doc_id=resolved.doc_id,
        doc_type="employee",
        allow_create=False,
        require_existing=True,
    )


def _dry_run_assign_employee_site(store: object, target: CanonicalTarget, job: QueueJob, context: RunContext, action: str) -> None:
    try:
        existing_doc = store.get_optional(target.doc_id)
    except Exception as exc:
        raise _canonical_write_error(job, target.doc_id, exc) from exc
    if existing_doc is None:
        raise _canonical_write_error(
            job,
            target.doc_id,
            Exception(f"CouchDB required document not found for canonical RMW: {target.doc_id}"),
        )
    job_ids = [str(item).strip() for item in existing_doc.get("btq_job_ids") or [] if str(item).strip()]
    if job.job_id in job_ids:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return
    print(f"Job {job.job_id}: would {action} employee site")
    if action == "unassign":
        preview = dict(existing_doc)
        scrubbed_fields = _scrub_legacy_site_assignments(preview, str(job.payload["site_id"]).strip())
        if scrubbed_fields:
            print(f"Job {job.job_id}: would scrub legacy assignment fields: {', '.join(scrubbed_fields)}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action}-employee-site status=success error=")


def process_assign_employee_site_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    employee_id = str(payload["employee_id"]).strip()
    site_id = str(payload["site_id"]).strip()
    actor = str(payload["actor"]).strip()
    action = str(payload.get("action") or "assign").strip()
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    store = _shared._vault_store()
    target = _resolve_employee_site_target(store, employee_id, job)
    evidence_text = f"employee {target.doc_id} {action} site_id={site_id} actor={actor}"

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        _dry_run_assign_employee_site(store, target, job, context, action)
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc or {})
        outgoing["site_ids"] = _employee_site_ids_after_action(outgoing.get("site_ids"), site_id, action)
        if action == "unassign":
            _scrub_legacy_site_assignments(outgoing, site_id)
        return CanonicalMutation(doc=outgoing, evidence_text=evidence_text)

    try:
        canonical_doc = apply_canonical_rmw(store, target, job.job_id, transform)
    except Exception as exc:
        raise _canonical_write_error(job, target.doc_id, exc) from exc

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
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action}-employee-site status=success error=")
