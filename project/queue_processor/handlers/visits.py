from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from btq_vault.entity_types import current_operator_id
from queue_processor.canonical_rmw import (
    CanonicalEntityState,
    CanonicalMutation,
    CanonicalTarget,
    apply_canonical_rmw,
    resolve_site_context,
)

from . import _shared
from ._shared import QueueJob, QueueProcessorError, RunContext

PHOTO_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

# Site/operator timezone metadata should replace this default when that source
# is available in the canonical model.
DEFAULT_VISIT_DATE_TIMEZONE = "America/New_York"
VISIT_DATE_ZONE = ZoneInfo(DEFAULT_VISIT_DATE_TIMEZONE)


def _build_visit_entity_doc(
    payload: dict,
    job: _shared.QueueJob,
    *,
    site_id: str,
    visit_date: str,
    visit_timestamp: str,
    visit_timestamp_local: str,
    date_timezone: str,
    visit_key: str,
    source: str,
    confidence: str,
    visit_type: str,
    visited_by: str,
    evidence: str,
) -> dict:
    return {
        "_id": f"visit_{site_id}_{visit_date}_{job.job_id[:8]}",
        "type": "visit",
        "operator": current_operator_id(),
        "site": str(payload["site"]).strip(),
        "site_id": site_id,
        "date": visit_date,
        "timestamp": visit_timestamp,
        "timestamp_local": visit_timestamp_local,
        "date_timezone": date_timezone,
        "visit_key": visit_key,
        "source": source,
        "confidence": confidence,
        "visit_type": visit_type or None,
        "visited_by": visited_by or None,
        "evidence": evidence,
        "btq_job_ids": [job.job_id],
    }


def _parse_visit_occurred_at(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise QueueProcessorError("Field occurred_at must be an ISO datetime with timezone")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueueProcessorError("Field occurred_at must be a valid ISO datetime with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QueueProcessorError("Field occurred_at must include a timezone offset")
    return parsed


def _visit_event_times(payload: dict) -> tuple[str, str, str]:
    occurred_at = payload.get("occurred_at")
    if occurred_at is None:
        event_utc = datetime.now(timezone.utc)
    else:
        event_utc = _parse_visit_occurred_at(occurred_at).astimezone(timezone.utc)

    event_local = event_utc.astimezone(VISIT_DATE_ZONE)
    return (
        event_local.date().isoformat(),
        event_utc.isoformat(),
        event_local.isoformat(),
    )


def process_visit_create_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    site_id = resolve_site_context(_shared._vault_store(), str(payload["site"])).site_id
    visit_date, visit_timestamp, visit_timestamp_local = _visit_event_times(payload)
    visit_key = _shared.build_visit_key(str(payload["site"]).strip(), visit_date)
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    evidence = str(payload["evidence"]).strip()
    source = str(payload["source"]).strip()
    confidence = str(payload["confidence"]).strip()
    visit_type = str(payload.get("visit_type") or "").strip()
    visited_by = str(payload.get("visited_by") or "").strip()

    try:
        existing_visit_docs = _shared._vault_store().find_visit_docs(site_id, visit_date)
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb visit dedup check failed "
            f"job_type={job.job_type} job_id={job.job_id} site_id={site_id} date={visit_date}: {exc}"
        ) from exc

    if any(job.job_id in _string_list(doc.get("btq_job_ids")) for doc in existing_visit_docs):
        if context.dry_run:
            print(f"Job {job.job_id}: job_id marker already present — skipping")
            _shared.write_log_line(
                context.log_path,
                f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present",
            )
            return
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(
            context.log_path,
            f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present",
        )
        return

    if any(str(doc.get("evidence") or "").strip() == evidence for doc in existing_visit_docs):
        if context.dry_run:
            print(f"Job {job.job_id}: duplicate visit evidence skipped")
            _shared.write_log_line(
                context.log_path,
                f"job_id={job.job_id} action=skip status=success reason=duplicate-visit-evidence",
            )
            return

        print(f"Job {job.job_id}: duplicate visit evidence skipped")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(
            context.log_path,
            f"job_id={job.job_id} action=skip status=success reason=duplicate-visit-evidence",
        )
        return

    print(f"Job {job.job_id}: validated")

    if context.dry_run:
        print(f"Job {job.job_id}: would create visit entry")
        _shared.write_log_line(
            context.log_path,
            f"job_id={job.job_id} action=visit-create status=success error=",
        )
        return

    canonical_doc = _shared._canonical_vault_upsert(
        job,
        _build_visit_entity_doc(
            payload,
            job,
            site_id=site_id,
            visit_date=visit_date,
            visit_timestamp=visit_timestamp,
            visit_timestamp_local=visit_timestamp_local,
            date_timezone=DEFAULT_VISIT_DATE_TIMEZONE,
            visit_key=visit_key,
            source=source,
            confidence=confidence,
            visit_type=visit_type,
            visited_by=visited_by,
            evidence=evidence,
        ),
        site_id=site_id,
    )
    _shared.write_mutation_evidence(context, job, canonical_doc, f"visit {visit_key}")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(
        context.log_path,
        f"job_id={job.job_id} action=visit-create status=success error=",
    )

def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]

def safe_photo_filename(value: str, index: int, mime_type: str) -> str:
    raw_name = Path(value.strip()).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(raw_name).stem).strip(".-")
    if not stem:
        stem = f"photo-{index:02d}"
    expected_suffix = PHOTO_MIME_EXTENSIONS[mime_type]
    return f"{stem}{expected_suffix}"

def decode_photo_data_url(data_url: str, mime_type: str) -> bytes:
    prefix = f"data:{mime_type};base64,"
    if not data_url.startswith(prefix):
        raise _shared.QueueProcessorError("Photo data_url must be a base64 data URL matching mime_type")
    encoded = data_url[len(prefix) :]
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise _shared.QueueProcessorError("Photo data_url is not valid base64") from exc

def read_photo_bytes(photo: dict, mime_type: str, context: RunContext) -> bytes:
    data_url = photo.get("data_url")
    if isinstance(data_url, str) and data_url.strip():
        return decode_photo_data_url(data_url, mime_type)
    stored_path = str(photo.get("stored_path", "")).strip()
    if not stored_path:
        raise _shared.QueueProcessorError("Photo must include data_url or stored_path")
    upload_root = context.resolve_runtime_path("uploads", label="Photo upload root")
    source_path = _shared.ensure_within_root(Path(stored_path), upload_root, "Photo upload source")
    if not source_path.exists() or not source_path.is_file():
        raise _shared.QueueProcessorError(f"Photo upload source is missing: {source_path}")
    return source_path.read_bytes()

def photo_capture_date(payload: dict) -> str:
    value = str(payload.get("captured_at", "")).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _shared.QueueProcessorError("Field captured_at must be a valid ISO datetime") from exc
    return parsed.date().isoformat()

def render_photo_capture_entry(payload: dict, attachment_links: list[str]) -> str:
    qc_category = str(payload["qc_category"]).strip()
    note = str(payload["note"]).strip()
    site = str(payload["site"]).strip() or "Unknown site"
    captured_at = str(payload["captured_at"]).strip()
    exported_at = str(payload["exported_at"]).strip()
    lines = [
        f"### Photo Capture - {captured_at}",
        "",
        f"- Site: {site}",
        f"- Area / QC Category: {qc_category}",
        f"- Exported At: {exported_at}",
        "",
        "#### Photos",
        "",
    ]
    for link in attachment_links:
        lines.append(f"- ![[{link}]]")
    if note:
        lines.extend(["", "#### Note", "", note])
    return "\n".join(lines)

def _append_photo_capture_entry(existing_text: str, capture_entry: str) -> str:
    append_text = capture_entry if capture_entry.endswith("\n") else f"{capture_entry}\n"
    return f"{existing_text}\n{append_text}" if existing_text and not existing_text.endswith("\n") else f"{existing_text}{append_text}"

def process_photo_capture_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    date = photo_capture_date(payload)
    target = CanonicalTarget(
        doc_id=f"journal_operational_{date}",
        doc_type="journal",
        allow_create=True,
        require_existing=False,
    )
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    try:
        store = _shared._vault_store()
        canonical_doc = store.get_optional(target.doc_id)
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb journal dedup check failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}"
        ) from exc

    if job.job_id in _string_list((canonical_doc or {}).get("btq_job_ids")):
        if context.dry_run:
            print(f"Job {job.job_id}: job_id marker already present — skipping")
            _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
            return
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return

    attachment_links: list[str] = []
    seen_names: set[str] = set()
    for index, photo in enumerate(payload["photos"], start=1):
        mime_type = str(photo["mime_type"]).strip()
        if mime_type not in PHOTO_MIME_EXTENSIONS:
            raise _shared.QueueProcessorError(f"Unsupported photo MIME type: {mime_type}")
        filename = safe_photo_filename(str(photo["filename"]), index, mime_type)
        if filename in seen_names:
            filename = f"{Path(filename).stem}-{index:02d}{Path(filename).suffix}"
        seen_names.add(filename)
        attachment_links.append(f"Attachments/{date}/{filename}")

    capture_entry = render_photo_capture_entry(payload, attachment_links)

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")

    if context.dry_run:
        print(f"Job {job.job_id}: would apply photo capture")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=photo-capture status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc or {})
        content = ""
        if state.doc is not None:
            content = str(state.doc.get("content", "") or "")
        outgoing["content"] = _append_photo_capture_entry(content, capture_entry)
        is_create = state.doc is None or set(state.doc) <= {"_id", "type"}
        if is_create:
            outgoing["date"] = date
            outgoing["scope"] = "operational"
            outgoing["operator"] = current_operator_id()
        return CanonicalMutation(doc=outgoing, evidence_text=capture_entry)

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

    _shared.write_mutation_evidence(context, job, canonical_doc, capture_entry)

    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=photo-capture status=success error=")
