from __future__ import annotations

from pathlib import Path

from btq_vault.facility_hours import normalize_facility_hours
from queue_processor.canonical_rmw import CanonicalEntityState, CanonicalMutation, apply_canonical_rmw, resolve_site_target

from . import _shared
from ._shared import QueueJob, RunContext


def _canonical_write_error(job: QueueJob, entity_id: str, exc: Exception) -> _shared.QueueJobError:
    return _shared.QueueJobError(
        "canonical couchdb write failed "
        f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}"
    )


def process_set_site_hours_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    site_id = str(payload["site_id"]).strip()
    action = str(payload.get("action") or "set").strip()
    actor = str(payload["actor"]).strip()
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    store = _shared._vault_store()
    try:
        target = resolve_site_target(site_id)
    except Exception as exc:
        raise _canonical_write_error(job, site_id, exc) from exc

    evidence_text = f"site_hours {action} site_id={site_id} actor={actor}"

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        print(f"Job {job.job_id}: would {action} facility hours")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action}-site-hours status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc or {})
        if action == "clear":
            outgoing.pop("facility_hours", None)
        else:
            outgoing["facility_hours"] = normalize_facility_hours(payload["facility_hours"])
        return CanonicalMutation(doc=outgoing, evidence_text=evidence_text)

    try:
        canonical_doc = apply_canonical_rmw(store, target, job.job_id, transform)
    except Exception as exc:
        raise _canonical_write_error(job, target.doc_id, exc) from exc

    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present - skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return

    _shared.write_mutation_evidence(context, job, canonical_doc, evidence_text)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action}-site-hours status=success error=")
