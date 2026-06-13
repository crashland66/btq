from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btq_vault.entity_types import OPERATOR_ID_GREG
from field_capture.prospects import TERMINAL_PROSPECT_STATUSES
from queue_processor.canonical_rmw import CanonicalEntityState, CanonicalMutation, CanonicalTarget, apply_canonical_rmw, resolve_employee_target, resolve_site_target
from supply_orders import (
    data_root as supply_data_root,
    normalize_identifier,
    parse_staples_email,
    quarantine_record,
    record_output_path as supply_record_output_path,
    resolve_site as resolve_supply_site,
    site_metadata_by_id,
)

from . import _shared
from ._shared import QueueJob, QueueProcessorError, RunContext
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceMemoNoteTarget:
    target: CanonicalTarget

    def __iter__(self):
        # Backward-compatible target summaries in queue_processor.main.
        yield self.target.doc_id
        yield self.target.allow_create
def render_personal_journal_entry(payload: dict) -> str:
    return (
        f"### {str(payload['timestamp']).strip()}\n\n"
        f"source_audio: {str(payload['audio_file']).strip()}\n"
        f"raw_transcript: {str(payload['raw_transcript_path']).strip()}\n\n"
        f"{str(payload['body']).strip()}\n"
    )
def voice_memo_capture_id(metadata: dict) -> str | None:
    capture_id = str(metadata.get("capture_id") or "").strip()
    return capture_id if capture_id.startswith("vm-") else None
def upsert_voice_memo_capture_id_frontmatter(file_content: str, capture_id: str) -> str:
    frontmatter, body, has_frontmatter = _shared.parse_frontmatter_text(file_content)
    updated = dict(frontmatter) if has_frontmatter else {}
    existing = updated.get("voice_memo_capture_ids")
    if isinstance(existing, list):
        capture_ids = [str(item).strip() for item in existing if str(item).strip()]
    elif existing is None:
        capture_ids = []
    else:
        capture_ids = [str(existing).strip()]
    if capture_id not in capture_ids:
        capture_ids.append(capture_id)
    updated["voice_memo_capture_ids"] = capture_ids
    serialized = _shared.serialize_frontmatter(updated)
    return f"{serialized}\n{body if has_frontmatter else file_content}"
def voice_memo_employee_slug(employee: object) -> str:
    if not isinstance(employee, dict):
        return ""
    return str(employee.get("slug") or "").strip()
def voice_memo_employee_name(employee: object) -> str:
    if not isinstance(employee, dict):
        return ""
    return str(employee.get("name") or employee.get("slug") or "").strip()
def voice_memo_person_link(context: RunContext, employee: object) -> str:
    slug = voice_memo_employee_slug(employee)
    name = voice_memo_employee_name(employee)
    if not slug:
        return name
    try:
        target = resolve_employee_target(_shared._vault_store(), slug)
    except Exception as exc:
        logger.warning("voice memo person link fallback slug=%s name=%s: %s", slug, name, exc)
        return name or slug
    label = name or slug
    return f"[[{target.doc_id}|{label}]]" if label else f"[[{target.doc_id}]]"
def render_voice_memo_note_section(payload: dict, context: RunContext) -> str:
    timestamp = str(payload["timestamp"]).strip()
    lines = [
        f"### {timestamp} — voice memo",
        "",
        f"source_audio: {str(payload['audio_file']).strip()}",
        f"raw_transcript: {str(payload['raw_transcript_path']).strip()}",
    ]
    employees = payload.get("employees")
    if isinstance(employees, list) and employees:
        lines.extend(["", "employees:"])
        for employee in employees:
            link = voice_memo_person_link(context, employee)
            if link:
                lines.append(f"- {link}")
    geolocation = payload.get("geolocation")
    if isinstance(geolocation, dict) and geolocation:
        lat = geolocation.get("lat")
        lng = geolocation.get("lng")
        accuracy = geolocation.get("accuracy_m")
        parts = [part for part in (f"lat={lat}" if lat is not None else "", f"lng={lng}" if lng is not None else "", f"accuracy_m={accuracy}" if accuracy is not None else "") if part]
        if parts:
            lines.extend(["", f"geolocation: {', '.join(parts)}"])
    note = str(payload.get("note") or "").strip()
    if note:
        lines.extend(["", "note:", note])
    transcript = str(payload["transcript_text"]).strip()
    quoted = "\n".join(f"> {line}" if line else ">" for line in transcript.splitlines())
    lines.extend(["", "transcript:", quoted, ""])
    return "\n".join(lines)

def _voice_memo_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []
def _append_voice_memo_capture_id(doc: dict[str, Any], capture_id: str) -> None:
    capture_ids = _voice_memo_string_list(doc.get("voice_memo_capture_ids"))
    if capture_id not in capture_ids:
        capture_ids.append(capture_id)
    doc["voice_memo_capture_ids"] = capture_ids
def _voice_memo_canonical_error(job: _shared.QueueJob, entity_id: str, exc: Exception) -> _shared.QueueJobError:
    return _shared.QueueJobError(
        "canonical couchdb write failed "
        f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}"
    )
def _voice_memo_note_transform(content: str, capture_id: str, *, seed_inbox_scope: bool = False):
    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc) if state.doc is not None else {}
        outgoing["content"] = _shared.append_markdown_block(str(outgoing.get("content") or ""), content)
        _append_voice_memo_capture_id(outgoing, capture_id)
        if seed_inbox_scope:
            outgoing.setdefault("scope", "operational_inbox")
            outgoing.setdefault("operator", OPERATOR_ID_GREG)
        return CanonicalMutation(doc=outgoing, evidence_text=content)
    return transform

def voice_memo_note_targets(payload: dict, context: RunContext, job: QueueJob | None = None) -> list[VoiceMemoNoteTarget]:
    site_id = str(payload.get("site_id") or "").strip()
    if site_id:
        target = resolve_site_target(site_id)
        return [VoiceMemoNoteTarget(target=target)]
    employees = payload.get("employees")
    employee_targets: list[VoiceMemoNoteTarget] = []
    if isinstance(employees, list):
        store = _shared._vault_store()
        for employee in employees:
            slug = voice_memo_employee_slug(employee)
            if not slug:
                continue
            try:
                target = resolve_employee_target(store, slug)
            except _shared.QueueProcessorError:
                raise
            except Exception as exc:
                job_id = job.job_id if job is not None else "unknown"
                raise _shared.QueueJobError(
                    "canonical couchdb write failed "
                    f"job_type=voice_memo_note job_id={job_id} entity_id=employee:{slug}: {exc}"
                ) from exc
            employee_targets.append(VoiceMemoNoteTarget(target=target))
    if employee_targets:
        return employee_targets
    return [
        VoiceMemoNoteTarget(
            target=CanonicalTarget(doc_id="note_voice_memo_general", doc_type="note", allow_create=True, require_existing=False),
        )
    ]

def process_voice_memo_note_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    capture_id = str(payload["capture_id"]).strip()
    content = render_voice_memo_note_section(payload, context)
    targets = voice_memo_note_targets(payload, context, job)
    store = _shared._vault_store()
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: targets {', '.join(target.target.doc_id for target in targets)}")
    if context.dry_run:
        for target in targets:
            try:
                current_doc = store.get_optional(target.target.doc_id)
            except Exception as exc:
                raise _voice_memo_canonical_error(job, target.target.doc_id, exc) from exc
            if current_doc is None and target.target.require_existing:
                raise _voice_memo_canonical_error(
                    job,
                    target.target.doc_id,
                    Exception(f"CouchDB required document not found for canonical RMW: {target.target.doc_id}"),
                )
        print(f"Job {job.job_id}: would append voice memo note")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=voice-memo-note status=success error=")
        return
    written_targets: list[tuple[VoiceMemoNoteTarget, dict[str, Any]]] = []
    for target in targets:
        try:
            canonical_doc = apply_canonical_rmw(
                store,
                target.target,
                job.job_id,
                _voice_memo_note_transform(content, capture_id, seed_inbox_scope=target.target.doc_id == "note_voice_memo_general"),
            )
        except Exception as exc:
            raise _voice_memo_canonical_error(job, target.target.doc_id, exc) from exc
        if canonical_doc is not None:
            written_targets.append((target, canonical_doc))
    if not written_targets:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return
    for target, canonical_doc in written_targets:
        _shared.write_mutation_evidence(context, job, canonical_doc, content)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: appended voice memo note")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=voice-memo-note status=success error=")

def process_personal_journal_entry_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload; date = _shared.validate_date_string(str(payload["date"]))
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    target = CanonicalTarget(doc_id=f"journal_personal_{date}", doc_type="journal", allow_create=True, require_existing=False)
    content = render_personal_journal_entry(payload); capture_id = voice_memo_capture_id(job.metadata)
    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc) if state.doc is not None else {}
        outgoing["content"] = _shared.append_markdown_block(str(outgoing.get("content") or ""), content)
        outgoing.setdefault("date", date); outgoing.setdefault("scope", "personal"); outgoing.setdefault("operator", OPERATOR_ID_GREG)
        if capture_id: _append_voice_memo_capture_id(outgoing, capture_id)
        return CanonicalMutation(doc=outgoing, evidence_text=content)
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        print(f"Job {job.job_id}: would apply personal-journal-entry"); _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=personal-journal-entry status=success error=")
        return
    try:
        canonical_doc = apply_canonical_rmw(_shared._personal_journal_store(), target, job.job_id, transform)
    except Exception as exc:
        raise _shared.QueueJobError("canonical couchdb write failed " f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}") from exc
    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present — skipping"); moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}"); _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}"); print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=personal-journal-entry status=success error=")

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _retarget_intake_jsons(intake_dir: Path, prospect_id: str, site_id: str, *, dry_run: bool) -> int:
    if not intake_dir.is_dir():
        return 0
    updated = 0
    for path in sorted(intake_dir.glob("cap-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("target_type") != "prospect" or str(payload.get("target_id") or "") != prospect_id:
            continue
        payload["target_type"] = "location"
        payload["target_id"] = site_id
        payload["site_id"] = site_id
        payload["promoted_from_prospect_id"] = prospect_id
        if not dry_run:
            _shared.atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        updated += 1
    return updated

def _invalid_retarget_target_id(value: str) -> bool:
    return not value or value.lower() in {"null", "none", "discard"}

def _retarget_intake_json_for_capture(
    intake_dir: Path,
    capture_id: str,
    new_target_type: str,
    new_target_id: str,
    new_site_id: str,
    *,
    dry_run: bool,
) -> bool:
    """Retarget a single capture's intake JSON. Returns True if a
    matching file was found and updated, False otherwise."""
    if not intake_dir.is_dir():
        return False
    for path in sorted(intake_dir.glob("cap-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("capture_id") or "") != capture_id:
            continue
        payload["target_type"] = new_target_type
        payload["target_id"] = new_target_id
        payload["site_id"] = new_site_id
        payload["retargeted_at"] = _utc_now_iso()
        if not dry_run:
            _shared.atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return True
    return False

def handle_promote_prospect(payload: dict[str, Any], context: RunContext) -> dict[str, Any]:
    prospect_id = str(payload["prospect_id"]).strip()
    site_id = str(payload["site_id"]).strip()
    config = _shared._field_capture_config()
    prospect = _shared.load_prospect(config, prospect_id)
    if prospect is None:
        raise _shared.QueueProcessorError(f"prospect not found: {prospect_id}")
    if prospect.get("status") == "won" and str(prospect.get("promoted_to_site_id") or "") == site_id:
        return {"status": "already_promoted", "capture_count": 0, "intake_count": 0}
    if prospect.get("promoted_to_site_id"):
        raise _shared.QueueProcessorError(f"prospect already promoted: {prospect_id}")
    if str(prospect.get("status") or "") in TERMINAL_PROSPECT_STATUSES:
        raise _shared.QueueProcessorError(f"prospect is terminal: {prospect_id}")
    if not _shared._site_id_registered(site_id):
        raise _shared.QueueProcessorError(f"site not registered: {site_id}")

    captures = _shared._find_prospect_captures(config, prospect_id, database=_shared._field_capture_database())
    for capture in captures:
        capture["target_type"] = "location"
        capture["target_id"] = site_id
        capture["site_id"] = site_id
        capture["promoted_from_prospect_id"] = prospect_id
        if not context.dry_run:
            _shared.put_field_capture_document(config, capture, database=_shared._field_capture_database())

    intake_count = _retarget_intake_jsons(
        context.runtime_root / "field_capture" / "intake",
        prospect_id,
        site_id,
        dry_run=context.dry_run,
    )
    prospect["status"] = "won"
    prospect["promoted_to_site_id"] = site_id
    prospect["promoted_at"] = _utc_now_iso()
    if not context.dry_run:
        _shared.write_prospect(config, prospect=prospect)
    return {"status": "promoted", "capture_count": len(captures), "intake_count": intake_count}

def handle_retarget_capture(payload: dict[str, Any], context: RunContext) -> dict[str, Any]:
    capture_id = str(payload["capture_id"]).strip()
    new_target_type = str(payload["new_target_type"]).strip()
    new_target_id = str(payload["new_target_id"]).strip()
    actor = str(payload.get("actor") or "").strip()

    if new_target_type not in ("location", "prospect"):
        raise _shared.QueueProcessorError(f"invalid target_type: {new_target_type}")
    if _invalid_retarget_target_id(new_target_id):
        raise _shared.QueueProcessorError("new_target_id required")
    if not capture_id:
        raise _shared.QueueProcessorError("capture_id required")

    config = _shared._field_capture_config()
    capture = _shared.get_field_capture_document(config, _shared._field_capture_database(), capture_id)
    if capture is None:
        raise _shared.QueueProcessorError(f"capture not found: {capture_id}")

    if new_target_type == "location":
        if not _shared._site_id_registered(new_target_id):
            raise _shared.QueueProcessorError(f"site not registered: {new_target_id}")
        new_site_id = new_target_id
    else:
        prospect = _shared.load_prospect(config, new_target_id)
        if prospect is None:
            raise _shared.QueueProcessorError(f"prospect not found: {new_target_id}")
        if str(prospect.get("status") or "") in TERMINAL_PROSPECT_STATUSES:
            raise _shared.QueueProcessorError(f"prospect is terminal: {new_target_id}")
        new_site_id = ""

    from_target_type = str(capture.get("target_type") or "location")
    from_target_id = str(capture.get("target_id") or "")
    if from_target_type == new_target_type and from_target_id == new_target_id:
        return {"status": "already_at_target", "capture_id": capture_id, "intake_updated": False}

    capture["target_type"] = new_target_type
    capture["target_id"] = new_target_id
    capture["site_id"] = new_site_id
    history = capture.setdefault("retarget_history", [])
    if not isinstance(history, list):
        history = []
        capture["retarget_history"] = history
    history.append({
        "at": _utc_now_iso(),
        "actor": actor,
        "from_target_type": from_target_type,
        "from_target_id": from_target_id,
        "to_target_type": new_target_type,
        "to_target_id": new_target_id,
    })

    if not context.dry_run:
        _shared.put_field_capture_document(config, capture, database=_shared._field_capture_database())

    intake_updated = _retarget_intake_json_for_capture(
        context.runtime_root / "field_capture" / "intake",
        capture_id,
        new_target_type,
        new_target_id,
        new_site_id,
        dry_run=context.dry_run,
    )
    return {
        "status": "retargeted",
        "capture_id": capture_id,
        "intake_updated": intake_updated,
    }

def process_promote_prospect_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    result = handle_promote_prospect(job.payload, context)
    print(f"Job {job.job_id}: promote_prospect {result['status']}")
    if context.dry_run:
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=promote-prospect status=success error=")
        return
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=promote-prospect status=success error=")

def process_retarget_capture_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    result = handle_retarget_capture(job.payload, context)
    print(f"Job {job.job_id}: retarget_capture {result['status']}")
    if context.dry_run:
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=retarget-capture status=success error=")
        return
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=retarget-capture status=success error=")

def resolve_supply_email_path(context: RunContext, path_value: str) -> Path:
    candidate = Path(path_value).expanduser()
    if candidate.is_absolute():
        repo_root = context.project_root.parent
        return _shared.ensure_within_root(candidate, repo_root, "Supply email source")
    return _shared.ensure_within_root(context.project_root.parent / candidate, context.project_root.parent, "Supply email source")

def parse_source_email_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _shared.QueueProcessorError("Field source_email_date must be a valid ISO datetime") from exc
    return parsed

def _supply_order_canonical_error(job: _shared.QueueJob, entity_id: str, exc: Exception) -> _shared.QueueJobError:
    return _shared.QueueJobError("canonical couchdb write failed " f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}")

def _supply_order_transform(resolved_order, created_at: str):
    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        existing = state.doc or {}
        outgoing = dict(resolved_order.to_record())
        outgoing["operator"] = str(existing.get("operator") or OPERATOR_ID_GREG)
        outgoing["created_at"] = str(existing.get("created_at") or created_at)
        return CanonicalMutation(doc=outgoing, evidence_text=f"supply_order {resolved_order.order_number}")

    return transform

def process_parse_supply_email_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    html_source_path = resolve_supply_email_path(context, str(payload["html_path"]))
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {html_source_path}")

    if not html_source_path.exists():
        quarantine_path = quarantine_record(
            {
                "job_id": job.job_id,
                "job_type": job.job_type,
                "error": "html source missing",
                "html_path": str(payload["html_path"]),
                "subject": str(payload["subject"]),
                "source_email_date": str(payload["source_email_date"]),
            },
            context.project_root,
            job.job_id,
            datetime.now(timezone.utc).date().isoformat(),
        )
        raise _shared.QueueProcessorError(f"Supply email source does not exist: {html_source_path}; quarantined at {quarantine_path}")

    if context.dry_run:
        print(f"Job {job.job_id}: would parse supply email")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=parse-supply-email status=success error=")
        return

    source_email_datetime = parse_source_email_datetime(str(payload["source_email_date"]))
    html = html_source_path.read_text(encoding="utf-8")
    order = parse_staples_email(html, str(payload["subject"]), source_email_datetime)
    target = CanonicalTarget(doc_id=f"supply_order_{normalize_identifier(order.order_number)}", doc_type="supply_order", allow_create=True, require_existing=False)
    store = _shared._vault_store()

    try:
        existing_doc = store.get_optional(target.doc_id)
    except Exception as exc:
        raise _supply_order_canonical_error(job, target.doc_id, exc) from exc

    if job.job_id in [str(value).strip() for value in (existing_doc or {}).get("btq_job_ids") or []]:
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return

    site_id, confidence = resolve_supply_site(order.site_name_raw, order._address_block, store)
    unresolved_reason = order.unresolved_reason
    account = None
    if site_id is None:
        unresolved_reason = ",".join(filter(None, [unresolved_reason, "unresolved_site"])) or "unresolved_site"
    else:
        metadata = site_metadata_by_id(site_id, store)
        account = None if metadata is None else metadata.account
    resolved_order = order.with_resolution(site_id=site_id, account=account, unresolved_reason=unresolved_reason)

    try:
        apply_canonical_rmw(
            store,
            target,
            job.job_id,
            _supply_order_transform(resolved_order, datetime.now(timezone.utc).isoformat()),
        )
    except _shared.QueueProcessorError:
        raise
    except Exception as exc:
        raise _supply_order_canonical_error(job, target.doc_id, exc) from exc

    json_path = supply_record_output_path(supply_data_root(context.project_root), resolved_order)
    try:
        _shared.atomic_write_text(json_path, json.dumps(resolved_order.to_record(), indent=2, sort_keys=True) + "\n")
    except Exception as exc:
        print(f"Job {job.job_id}: skipped supply order record for {target.doc_id}: {exc}")

    if resolved_order.unresolved_reason is not None or confidence < 0.7:
        quarantine_path: Path | str = "skipped"
        try:
            quarantine_path = quarantine_record(
                {
                    **resolved_order.to_record(),
                    "address_block": order._address_block,
                    "resolution_confidence": confidence,
                    "source_html_path": str(html_source_path),
                },
                context.project_root,
                resolved_order.order_number,
                resolved_order.order_date,
            )
        except Exception as exc:
            print(f"Job {job.job_id}: skipped supply order quarantine for {target.doc_id}: {exc}")
        _shared.write_log_line(
            context.log_path,
            f"job_id={job.job_id} action=parse-supply-email status=success unresolved={resolved_order.unresolved_reason or 'low-confidence'} quarantine={quarantine_path}",
        )
    else:
        _shared.write_log_line(
            context.log_path,
            f"job_id={job.job_id} action=parse-supply-email status=success order_number={resolved_order.order_number} json={json_path}",
        )

    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {json_path}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
