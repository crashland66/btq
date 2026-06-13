from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from btq_vault.entity_types import OPERATOR_ID_GREG
from processing_core.slugs import lower_dash_slug
from event_pipeline.enricher import enrich_events
from event_pipeline.extractor import extract_events
from event_pipeline.sites import SITES, resolve_site
from event_pipeline.validator import validate_events
from event_to_queue.adapter import event_to_job
from queue_processor.canonical_rmw import CanonicalEntityState, CanonicalMutation, CanonicalTarget, apply_canonical_rmw

from . import _shared
from ._shared import QueueJob, RunContext

def parse_retry_count(value: str | None) -> int:
    if value is None or not value.strip():
        return 0
    try:
        parsed = int(value)
    except ValueError:
        return 0
    return parsed if parsed >= 0 else 0

def parse_last_attempted(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized.lower() == "null":
        return None
    return normalized

def slugify_unknown_id(value: str) -> str:
    return lower_dash_slug(value, fallback="unknown-capture")

def derive_source_unknown_id(path: Path, timestamp: str, audio_file: str) -> str:
    stem = path.stem
    return slugify_unknown_id(f"{stem}-{timestamp}-{audio_file}")

def build_unknown_capture_doc_fields(payload: dict[str, Any]) -> dict[str, Any]:
    source_unknown_id = derive_source_unknown_id(
        Path(str(payload["path"])),
        str(payload["timestamp"]),
        str(payload["audio_file"]),
    )
    doc: dict[str, Any] = {
        "_id": f"unknown_capture_{source_unknown_id}",
        "type": "unknown_capture",
        "operator": OPERATOR_ID_GREG,
        "source_unknown_id": source_unknown_id,
        "timestamp": str(payload["timestamp"]),
        "audio_file": str(payload["audio_file"]),
        "status": "unresolved",
        "retry_count": 0,
        "last_attempted": None,
    }
    for key in (
        "capture_id",
        "original_transcript",
        "normalized_transcript",
        "notes",
        "capture_status",
        "events_created",
        "reason_heading",
    ):
        if key in payload and payload[key] is not None:
            doc[key] = payload[key]
    if "reasons" in payload:
        doc["reasons"] = _shared.parse_string_list_payload(payload.get("reasons"))
    return doc

def process_record_unknown_capture_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    doc_fields = build_unknown_capture_doc_fields(payload)
    target = CanonicalTarget(
        doc_id=str(doc_fields["_id"]),
        doc_type="unknown_capture",
        allow_create=True,
        require_existing=False,
    )
    created_doc = False

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        print(f"Job {job.job_id}: would record unknown capture")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=record-unknown-capture status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        nonlocal created_doc
        existing = dict(state.doc or {})
        if set(existing) <= {"_id", "type"}:
            created_doc = True
            outgoing = dict(doc_fields)
        else:
            created_doc = False
            outgoing = existing
        return CanonicalMutation(doc=outgoing, evidence_text=f"unknown_capture {doc_fields['source_unknown_id']}")

    try:
        canonical_doc = apply_canonical_rmw(_shared._vault_store(), target, job.job_id, transform)
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

    if created_doc:
        _shared.write_mutation_evidence(
            context,
            job,
            canonical_doc,
            str(payload["content"]).strip(),
        )

    moved_path = _shared.move_job_file(job_path, processed_dir)
    if created_doc:
        print(f"Job {job.job_id}: updated {target.doc_id}")
    else:
        print(f"Job {job.job_id}: existing unknown_capture doc left unchanged")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=record-unknown-capture status=success error=")

def unknown_entry_contains_site_signal(body: str) -> bool:
    if "#site:" in body.lower():
        return True

    resolved_site, _confidence = resolve_site(body)
    if resolved_site != "unknown":
        return True

    for site in SITES:
        for alias in site["aliases"]:
            if alias.lower() in body.lower():
                return True
    return False

def _unknown_status(value: dict[str, Any]) -> str:
    return str(value.get("status") or "unresolved")

def _unknown_retry_count(value: dict[str, Any]) -> int:
    retry_count = value.get("retry_count")
    if isinstance(retry_count, int):
        return retry_count if retry_count >= 0 else 0
    return parse_retry_count(None if retry_count is None else str(retry_count))

def _unknown_last_attempted(value: dict[str, Any]) -> str | None:
    return parse_last_attempted(None if value.get("last_attempted") is None else str(value.get("last_attempted")))

def should_retry(entry: dict[str, Any]) -> bool:
    if _unknown_status(entry) != "unresolved":
        return False

    retry_count = _unknown_retry_count(entry)
    last_attempted = _unknown_last_attempted(entry)

    if retry_count >= 3:
        return False
    if not last_attempted:
        return True

    try:
        last_dt = datetime.fromisoformat(last_attempted)
    except Exception:
        return True

    now = datetime.now(last_dt.tzinfo) if last_dt.tzinfo is not None else datetime.now(timezone.utc).replace(tzinfo=None)
    delay_hours = 2 ** retry_count
    return (now - last_dt) > timedelta(hours=delay_hours)

def extract_section(body: str, heading: str) -> str:
    pattern = rf"(?ms)^## {re.escape(heading)}\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, body)
    if match is None:
        return ""
    return match.group(1).strip()

def canonical_unknown_user_added_text(notes: Any) -> str:
    notes_text = "" if notes is None else str(notes)
    stripped_notes = notes_text.strip()
    if stripped_notes in {"#unknown #needs-review", "#partial #needs-review"}:
        return ""
    return notes_text

def canonical_unknown_input_text(doc: dict[str, Any]) -> str:
    normalized_transcript = "" if doc.get("normalized_transcript") is None else str(doc.get("normalized_transcript"))
    user_added_text = canonical_unknown_user_added_text(doc.get("notes"))
    return normalized_transcript if not user_added_text else f"{normalized_transcript}\n{user_added_text}".strip()

def canonical_unknown_contains_site_signal(doc: dict[str, Any]) -> bool:
    body = "\n".join(
        str(value)
        for value in (doc.get("normalized_transcript"), doc.get("notes"))
        if value is not None and str(value).strip()
    )
    return unknown_entry_contains_site_signal(body) if body else False

def canonical_unknown_decision_doc(store: Any, doc: dict[str, Any]) -> dict[str, Any]:
    if doc.get("normalized_transcript") is not None or doc.get("notes") is not None:
        return doc
    doc_id = str(doc.get("_id") or "").strip()
    if not doc_id or not hasattr(store, "get_optional"):
        return doc
    full_doc = store.get_optional(doc_id)
    return dict(full_doc) if isinstance(full_doc, dict) else doc

def queue_job_with_source(job: dict, source_unknown_id: str, capture_id: str | None = None) -> dict:
    payload = dict(job["payload"])
    payload["source_unknown_id"] = source_unknown_id
    updated = {
        "job_id": job["job_id"],
        "job_type": job["job_type"],
        "payload": payload,
    }
    metadata = dict(job.get("metadata")) if isinstance(job.get("metadata"), dict) else {}
    if capture_id is not None:
        metadata["capture_id"] = capture_id
    if metadata:
        updated["metadata"] = metadata
    return updated

def job_already_exists_for_unknown(runtime_root: Path, source_unknown_id: str) -> bool:
    for directory_name in ("queue", "processed", "failed"):
        directory = runtime_root / directory_name
        if not directory.exists():
            continue
        for job_path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(job_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            job_payload = payload.get("payload")
            if isinstance(job_payload, dict) and job_payload.get("source_unknown_id") == source_unknown_id:
                return True
    return False

def best_resolved_site(valid_paths: list[Path]) -> str:
    known_sites: list[str] = []
    for path in valid_paths:
        event = json.loads(path.read_text(encoding="utf-8"))
        site = event.get("site")
        if isinstance(site, str) and site != "unknown" and site not in known_sites:
            known_sites.append(site)
    if not known_sites:
        return "unknown"
    if len(known_sites) == 1:
        return known_sites[0]
    return "multiple"

def mark_unknown_doc_resolved(
    store: Any,
    doc_id: str,
    *,
    resolved_site: str,
) -> dict[str, Any]:
    resolved_at = datetime.now(timezone.utc).isoformat()

    def transform(current: dict[str, Any] | None) -> dict[str, Any]:
        outgoing = dict(current or {})
        outgoing["status"] = "resolved"
        outgoing["resolved_at"] = resolved_at
        outgoing["resolved_site"] = resolved_site
        outgoing["resolution"] = "Reclassified and routed to structured events."
        return outgoing

    return store.update_doc(doc_id, transform, require_existing=True)

def reclassify_unknown(doc: dict[str, Any], context: RunContext) -> int:
    if doc.get("status") != "unresolved":
        return 0

    doc_id = str(doc.get("_id") or "").strip()
    source_unknown_id = str(doc.get("source_unknown_id") or "").strip()
    if not doc_id or not source_unknown_id:
        raise _shared.QueueJobError("canonical unknown_capture doc missing _id or source_unknown_id")

    try:
        store = _shared._vault_store()
    except Exception as exc:
        raise _shared.QueueJobError(f"canonical unknown_capture store unavailable doc_id={doc_id}: {exc}") from exc
    attempted_at = datetime.now(timezone.utc).isoformat()

    def bump_attempt(current: dict[str, Any] | None) -> dict[str, Any] | None:
        outgoing = dict(current or {})
        if _unknown_status(outgoing) != "unresolved":
            return None
        outgoing["last_attempted"] = attempted_at
        outgoing["retry_count"] = _unknown_retry_count(outgoing) + 1
        return outgoing

    try:
        doc = store.update_doc(doc_id, bump_attempt, require_existing=True)
    except Exception as exc:
        raise _shared.QueueJobError(f"canonical unknown_capture attempt update failed doc_id={doc_id}: {exc}") from exc
    if doc.get("status") != "unresolved":
        return 0

    input_text = canonical_unknown_input_text(doc)
    if not input_text.strip():
        return 0

    if job_already_exists_for_unknown(context.runtime_root, source_unknown_id):
        try:
            mark_unknown_doc_resolved(store, doc_id, resolved_site="unknown")
        except Exception as exc:
            raise _shared.QueueJobError(f"canonical unknown_capture resolve update failed doc_id={doc_id}: {exc}") from exc
        return 0

    reclassify_root = context.runtime_root / "unknown_reclassification" / source_unknown_id
    raw_dir = reclassify_root / "events_raw"
    enriched_dir = reclassify_root / "events_enriched"
    valid_dir = reclassify_root / "events_valid"
    failed_dir = reclassify_root / "events_failed"
    transcript_path = reclassify_root / f"{source_unknown_id}.normalized.txt"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    _shared.atomic_write_text(transcript_path, input_text.strip() + "\n")

    raw_paths = extract_events(transcript_path, raw_dir, transcript_text=input_text)
    enriched_paths = enrich_events(raw_dir, enriched_dir, raw_paths)
    valid_paths, _failed_paths = validate_events(enriched_dir, valid_dir, failed_dir, enriched_paths)
    if not valid_paths:
        return 0

    queue_dir = context.runtime_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    created_jobs = 0
    for event_path in sorted(valid_paths):
        event = json.loads(event_path.read_text(encoding="utf-8"))
        job = event_to_job(event)
        if job is None:
            continue
        capture_id = None if doc.get("capture_id") is None else str(doc.get("capture_id"))
        job = queue_job_with_source(job, source_unknown_id, capture_id)
        job_path = queue_dir / f"job_{event['event_id']}.json"
        if job_path.exists():
            continue
        _shared.atomic_write_text(job_path, json.dumps(job, indent=2, sort_keys=True) + "\n")
        created_jobs += 1

    if created_jobs == 0:
        return 0

    try:
        mark_unknown_doc_resolved(store, doc_id, resolved_site=best_resolved_site(valid_paths))
    except Exception as exc:
        raise _shared.QueueJobError(f"canonical unknown_capture resolve update failed doc_id={doc_id}: {exc}") from exc
    return created_jobs

def process_unknowns(context: RunContext) -> int:
    if context.dry_run:
        return 0
    created_jobs = 0
    try:
        store = _shared._vault_store()
        docs = store.find_unknown_capture_docs(status="unresolved")
    except Exception as exc:
        raise _shared.QueueJobError(f"canonical unknown_capture scan failed: {exc}") from exc
    for doc in docs:
        try:
            decision_doc = canonical_unknown_decision_doc(store, doc)
        except Exception as exc:
            raise _shared.QueueJobError(f"canonical unknown_capture read failed doc_id={doc.get('_id')}: {exc}") from exc
        if should_retry(decision_doc) or canonical_unknown_contains_site_signal(decision_doc):
            created_jobs += reclassify_unknown(decision_doc, context)
    return created_jobs

def process_reclassify_unknown_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target unknown_capture scan")

    created_jobs = 0
    if context.dry_run:
        print(f"Job {job.job_id}: would reclassify unknowns")
        _shared.write_log_line(
            context.log_path,
            f"job_id={job.job_id} action=reclassify-unknown status=success error=",
        )
        return

    try:
        store = _shared._vault_store()
        docs = store.find_unknown_capture_docs(status="unresolved")
    except Exception as exc:
        raise _shared.QueueJobError(f"canonical unknown_capture scan failed: {exc}") from exc

    for doc in docs:
        try:
            decision_doc = canonical_unknown_decision_doc(store, doc)
        except Exception as exc:
            raise _shared.QueueJobError(f"canonical unknown_capture read failed doc_id={doc.get('_id')}: {exc}") from exc
        if should_retry(decision_doc) or canonical_unknown_contains_site_signal(decision_doc):
            created_jobs += reclassify_unknown(decision_doc, context)

    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(
        context.log_path,
        f"job_id={job.job_id} action=reclassify-unknown status=success error=",
    )
