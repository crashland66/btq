from __future__ import annotations

from pathlib import Path
from typing import Any

from btq_vault.location_urls import LocationUrlError, normalize_location_url_entry
from queue_processor.canonical_rmw import CanonicalEntityState, CanonicalMutation, apply_canonical_rmw, resolve_site_target

from . import _shared
from ._shared import QueueJob, RunContext


def _canonical_write_error(job: QueueJob, entity_id: str, exc: Exception) -> _shared.QueueJobError:
    return _shared.QueueJobError(
        "canonical couchdb write failed "
        f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}"
    )


def _entry_from_payload(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    seed = dict(existing or {})
    if "new_url" in payload:
        seed["url"] = payload.get("new_url")
    elif "url" in payload and not existing:
        seed["url"] = payload.get("url")
    for field in ("label", "kind", "status", "last_verified_at", "last_verified_by", "verification_note"):
        if field in payload:
            seed[field] = payload.get(field)
    return normalize_location_url_entry(seed)


def _normalized_url_entries(raw_urls: object) -> list[dict[str, Any]]:
    if not isinstance(raw_urls, list):
        return []
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_entry in raw_urls:
        if not isinstance(raw_entry, dict):
            continue
        try:
            entry = normalize_location_url_entry(raw_entry)
        except LocationUrlError:
            continue
        if entry["url"] in seen:
            continue
        entries.append(entry)
        seen.add(entry["url"])
    return entries


def process_set_site_url_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    site_id = str(payload["site_id"]).strip()
    action = str(payload["action"]).strip()
    actor = str(payload["actor"]).strip()
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    store = _shared._vault_store()
    try:
        target = resolve_site_target(site_id)
    except Exception as exc:
        raise _canonical_write_error(job, site_id, exc) from exc

    evidence_text = f"site_url {action} site_id={site_id} actor={actor} url={payload['url']}"

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        print(f"Job {job.job_id}: would {action} site url")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action}-site-url status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc or {})
        urls = _normalized_url_entries(outgoing.get("urls"))
        old_url = str(payload["url"]).strip()
        match_index = next((index for index, entry in enumerate(urls) if entry["url"] == old_url), None)

        if action == "remove":
            outgoing["urls"] = [entry for entry in urls if entry["url"] != old_url]
            return CanonicalMutation(doc=outgoing, evidence_text=evidence_text)

        if action == "add":
            entry = _entry_from_payload(payload)
            if match_index is None:
                urls.append(entry)
            else:
                urls[match_index] = entry
            outgoing["urls"] = urls
            return CanonicalMutation(doc=outgoing, evidence_text=evidence_text)

        if match_index is None:
            raise _shared.QueueProcessorError(f"Cannot edit missing site URL: {old_url}")
        entry = _entry_from_payload(payload, urls[match_index])
        urls[match_index] = entry
        outgoing["urls"] = urls
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
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action}-site-url status=success error=")
