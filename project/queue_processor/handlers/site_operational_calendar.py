from __future__ import annotations

from pathlib import Path
from typing import Any

from btq_vault.operational_calendar import (
    normalize_operational_calendar,
    normalize_operational_calendars,
)
from queue_processor.canonical_rmw import (
    CanonicalEntityState,
    CanonicalMutation,
    apply_canonical_rmw,
    resolve_site_target,
)

from . import _shared
from ._shared import QueueJob, RunContext


def _canonical_write_error(job: QueueJob, entity_id: str, exc: Exception) -> _shared.QueueJobError:
    return _shared.QueueJobError(
        "canonical couchdb write failed "
        f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}"
    )


def process_set_site_operational_calendar_job(
    job_path: Path,
    job: QueueJob,
    context: RunContext,
    processed_dir: Path,
) -> None:
    payload = job.payload
    site_id = str(payload["site_id"]).strip()
    action = str(payload["action"]).strip()
    calendar_id = str(payload["calendar_id"]).strip()
    actor = str(payload["actor"]).strip()
    calendar = (
        normalize_operational_calendar(payload["calendar"]) if action == "upsert" else None
    )
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    try:
        target = resolve_site_target(site_id)
    except Exception as exc:
        raise _canonical_write_error(job, site_id, exc) from exc

    evidence_text = (
        f"site_operational_calendar {action} site_id={site_id} "
        f"calendar_id={calendar_id} actor={actor}"
    )

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        print(f"Job {job.job_id}: would {action} operational calendar {calendar_id}")
        _shared.write_log_line(
            context.log_path,
            f"job_id={job.job_id} action={action}-site-operational-calendar status=success error=",
        )
        return

    store = _shared._vault_store()

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc or {})
        had_calendars = "operational_calendars" in outgoing
        raw_calendars: object = outgoing.get("operational_calendars", [])
        normalized_calendars = normalize_operational_calendars(raw_calendars)
        preserved_calendars = _updated_calendars(
            raw_calendars,
            normalized_calendars,
            action=action,
            calendar_id=calendar_id,
            calendar=calendar,
        )
        if action == "upsert" or had_calendars:
            outgoing["operational_calendars"] = preserved_calendars
        return CanonicalMutation(doc=outgoing, evidence_text=evidence_text)

    try:
        canonical_doc = apply_canonical_rmw(store, target, job.job_id, transform)
    except Exception as exc:
        raise _canonical_write_error(job, target.doc_id, exc) from exc

    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present - skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(
            context.log_path,
            f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present",
        )
        return

    _shared.write_mutation_evidence(context, job, canonical_doc, evidence_text)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(
        context.log_path,
        f"job_id={job.job_id} action={action}-site-operational-calendar status=success error=",
    )


def _updated_calendars(
    raw_calendars: object,
    normalized_calendars: list[dict[str, Any]],
    *,
    action: str,
    calendar_id: str,
    calendar: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_calendars, list):
        raise TypeError("operational_calendars must be a list")
    updated: list[dict[str, Any]] = []
    replaced = False
    for raw_calendar, normalized_calendar in zip(raw_calendars, normalized_calendars):
        if normalized_calendar["calendar_id"] != calendar_id:
            updated.append(raw_calendar)
            continue
        if action == "upsert" and calendar is not None:
            updated.append(calendar)
            replaced = True
    if action == "upsert" and not replaced and calendar is not None:
        updated.append(calendar)
    return updated
