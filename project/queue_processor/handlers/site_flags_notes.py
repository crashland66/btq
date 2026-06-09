from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btq_vault.entity_types import OPERATOR_ID_GREG
from queue_processor.canonical_rmw import (
    CanonicalEntityState,
    CanonicalMutation,
    CanonicalTarget,
    SiteContext,
    apply_canonical_rmw,
    resolve_employee_target,
    resolve_site_context,
    resolve_site_target,
)
from vault_errors import NotFoundError

from . import _shared
from ._shared import (
    QueueJob,
    QueueProcessorError,
    RunContext,
)

def site_issue_id(payload: dict) -> str:
    explicit = str(payload.get("issue_id") or "").strip()
    if explicit:
        return explicit
    seed = {
        "site_id": str(payload.get("site_id") or "").strip(),
        "title": str(payload.get("title") or "").strip(),
        "observed_at": str(payload.get("observed_at") or "").strip(),
        "source": str(payload.get("source") or "").strip(),
        "capture_id": next(iter(_shared.parse_string_list_payload(payload.get("related_capture_ids"))), ""),
    }
    digest = hashlib.sha256(json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return f"iss_{digest}"

def issue_status(payload: dict) -> str:
    return str(payload.get("status") or "open").strip() or "open"

def issue_priority(payload: dict) -> str:
    return str(payload.get("priority") or "normal").strip() or "normal"

def issue_category(payload: dict) -> str:
    return str(payload.get("category") or "other").strip() or "other"

def client_notification_field(payload: dict, modern: str, legacy: str) -> str:
    value = payload.get(modern)
    if value in {None, ""}:
        value = payload.get(legacy)
    return str(value or "").strip()

def _build_site_issue_entity_doc(payload: dict, job: QueueJob, site_ctx: SiteContext, issue_id: str, created_at: str) -> dict:
    return {
        "_id": f"site_issue_{issue_id}",
        "type": "site_issue",
        "operator": OPERATOR_ID_GREG,
        "issue_id": issue_id,
        "site_id": site_ctx.site_id,
        "site_name": site_ctx.name,
        "created_at": created_at,
        "btq_job_ids": [job.job_id],
        **{k: v for k, v in payload.items() if k not in {"site_id"}},
    }

def process_log_site_issue_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    site_ctx = resolve_site_context(_shared._vault_store(), str(payload["site_id"]))
    issue_id = site_issue_id(payload)
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    created_at = datetime.now(timezone.utc).isoformat()
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target site_issue_{issue_id}")
    if context.dry_run:
        print(f"Job {job.job_id}: would log site issue")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=log-site-issue status=success error=")
        return

    target = CanonicalTarget(
        doc_id=f"site_issue_{issue_id}",
        doc_type="site_issue",
        allow_create=True,
        require_existing=False,
    )

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        is_new_doc = state.doc is None or set(state.doc) <= {"_id", "type"}
        if is_new_doc:
            doc = _build_site_issue_entity_doc(payload, job, site_ctx, issue_id, created_at)
            doc.pop("btq_job_ids", None)
        else:
            doc = dict(state.doc)
            existing_created_at = doc.get("created_at") or created_at
            existing_job_ids = doc.get("btq_job_ids")
            doc.update(payload)
            doc["created_at"] = existing_created_at
            doc["btq_job_ids"] = existing_job_ids
            doc.setdefault("operator", OPERATOR_ID_GREG)
            doc["issue_id"] = issue_id
            doc["site_id"] = site_ctx.site_id
            doc["site_name"] = site_ctx.name
        return CanonicalMutation(doc=doc, evidence_text=f"site_issue {issue_id}")

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

    _shared.write_mutation_evidence(context, job, canonical_doc, f"site_issue {issue_id}")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=log-site-issue status=success error=")

def process_append_to_note_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    target_path = _shared.ensure_within_root(context.vault_root / str(payload["path"]), context.vault_root, "Note target")
    if target_path.name.endswith("-unknown.md"):
        raise _shared.QueueProcessorError(
            f"append_to_note no longer handles unknown-capture files; use record_unknown_capture: {payload['path']}"
        )
    content = str(payload["content"])
    store = _shared._vault_store()
    event_date = datetime.now(timezone.utc).date().isoformat()
    existing_text = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    visit_key: str | None = None
    if _shared.is_site_about_path(target_path):
        target = CanonicalTarget(
            doc_id=_shared.canonical_location_doc_id_for_projection(target_path, existing_text),
            doc_type="location",
            allow_create=False,
            require_existing=True,
        )
        site_id = target.doc_id.removeprefix("location_")
        try:
            site_ctx = resolve_site_context(store, site_id)
            visit_key = location_content_append_active_visit_key(context, store, site_ctx.name, site_id, event_date)
        except Exception as exc:
            raise _shared.QueueJobError(
                "canonical couchdb write failed "
                f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}"
            ) from exc
        normalized = _shared.append_visit_key_suffix(content.strip(), visit_key)
    else:
        site_id = None
        site_ctx = None
        target = CanonicalTarget(
            doc_id=f"note_journal_{target_path.stem}",
            doc_type="note",
            allow_create=True,
            require_existing=False,
        )
        normalized = content.strip()

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target_path}")

    try:
        current_doc = store.get_optional(target.doc_id)
    except Exception as exc:
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}"
        ) from exc
    if current_doc is None and target.require_existing:
        raise _shared.QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: "
            f"CouchDB required document not found for canonical RMW: {target.doc_id}"
        )
    current_job_ids = [str(item).strip() for item in (current_doc or {}).get("btq_job_ids") or [] if str(item).strip()]
    if job.job_id in current_job_ids:
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        if not context.dry_run:
            moved_path = _shared.move_job_file(job_path, processed_dir)
            print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return
    current_content = (current_doc or {}).get("content", "")
    current_content = current_content if isinstance(current_content, str) else str(current_content)
    if normalized and normalized in current_content:
        print(f"Job {job.job_id}: duplicate append_to_note content skipped")
        if not context.dry_run:
            moved_path = _shared.move_job_file(job_path, processed_dir)
            print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=duplicate-append-to-note-content")
        return
    if context.dry_run:
        print(f"Job {job.job_id}: would apply append-to-note")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=append-to-note status=success error=")
        return

    if site_id is not None and site_ctx is not None and visit_key is None:
        gap_target = CanonicalTarget(
            doc_id=f"visit_gap_{site_id}_{event_date}",
            doc_type="visit_gap",
            allow_create=True,
            require_existing=False,
        )

        def gap_transform(state: CanonicalEntityState) -> CanonicalMutation:
            outgoing = dict(state.doc) if state.doc is not None else {}
            outgoing.setdefault("site", site_ctx.name)
            outgoing.setdefault("site_id", site_id)
            outgoing.setdefault("date", event_date)
            outgoing.setdefault("reason", "event_without_visit")
            outgoing.setdefault("operator", OPERATOR_ID_GREG)
            return CanonicalMutation(doc=outgoing, evidence_text=f"visit_gap {site_id} {event_date}")

        try:
            apply_canonical_rmw(store, gap_target, job.job_id, gap_transform)
        except Exception as exc:
            raise _shared.QueueJobError(
                "canonical couchdb write failed "
                f"job_type={job.job_type} job_id={job.job_id} entity_id={gap_target.doc_id}: {exc}"
            ) from exc

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        content_field = state.doc.get("content", "") if state.doc else ""
        new_content = _shared.append_markdown_block(content_field, normalized)
        outgoing = dict(state.doc) if state.doc else {}
        outgoing["content"] = new_content
        if target.doc_type == "note":
            outgoing.setdefault("operator", OPERATOR_ID_GREG)
            outgoing.setdefault("date", target_path.stem)
        return CanonicalMutation(doc=outgoing, evidence_text=normalized)

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

    _shared.write_mutation_evidence(context, job, canonical_doc, normalized)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=append-to-note status=success error=")

def process_flag_access_constraint_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    event_date = _shared.payload_date(payload)
    site_name = str(payload["site"]).strip()
    target = resolve_site_target(site_name)
    site_id = target.doc_id.removeprefix("location_")
    visit_key = location_content_append_active_visit_key(context, _shared._vault_store(), site_name, site_id, event_date)
    note_line = _shared.append_visit_key_suffix(f"{event_date} — {str(payload['details']).strip()}", visit_key)
    process_location_content_append_job(
        job_path,
        job,
        context,
        processed_dir,
        site=site_name,
        event_date=event_date,
        visit_key=visit_key,
        subheading="### Access Constraints",
        note_line=note_line,
        action="flag-access-constraint",
        dry_run_message="would append access constraint",
    )

def location_content_append_active_visit_key(
    context: RunContext,
    store: Any,
    site: str,
    site_id: str,
    event_date: str,
) -> str | None:
    try:
        return _shared.get_active_visit(context, site, event_date)
    except NotFoundError:
        if store.find_visit_docs(site_id, event_date):
            return _shared.build_visit_key(site, event_date)
        return None

def process_location_content_append_job(
    job_path: Path,
    job: QueueJob,
    context: RunContext,
    processed_dir: Path,
    *,
    site: str,
    event_date: str,
    visit_key: str | None,
    subheading: str,
    note_line: str,
    action: str,
    dry_run_message: str,
) -> None:
    store = _shared._vault_store()
    target = resolve_site_target(site)
    site_id = target.doc_id.removeprefix("location_")
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        try:
            canonical_doc = store.get_optional(target.doc_id)
        except Exception as exc:
            raise _shared.QueueJobError(
                "canonical couchdb write failed "
                f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}"
            ) from exc
        if canonical_doc is None:
            raise _shared.QueueJobError(
                "canonical couchdb write failed "
                f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: "
                f"CouchDB required document not found for canonical RMW: {target.doc_id}"
            )
        if job.job_id in [str(item).strip() for item in canonical_doc.get("btq_job_ids") or [] if str(item).strip()]:
            print(f"Job {job.job_id}: job_id marker already present — skipping")
            _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
            return
        print(f"Job {job.job_id}: {dry_run_message}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action} status=success error=")
        return

    if visit_key is None:
        gap_target = CanonicalTarget(
            doc_id=f"visit_gap_{site_id}_{event_date}",
            doc_type="visit_gap",
            allow_create=True,
            require_existing=False,
        )

        def gap_transform(state: CanonicalEntityState) -> CanonicalMutation:
            outgoing = dict(state.doc) if state.doc is not None else {}
            outgoing.setdefault("site", site)
            outgoing.setdefault("site_id", site_id)
            outgoing.setdefault("date", event_date)
            outgoing.setdefault("reason", "event_without_visit")
            outgoing.setdefault("operator", OPERATOR_ID_GREG)
            return CanonicalMutation(doc=outgoing, evidence_text=f"visit_gap {site_id} {event_date}")

        try:
            apply_canonical_rmw(store, gap_target, job.job_id, gap_transform)
        except Exception as exc:
            raise _shared.QueueJobError(
                "canonical couchdb write failed "
                f"job_type={job.job_type} job_id={job.job_id} entity_id={gap_target.doc_id}: {exc}"
            ) from exc

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        content = state.doc.get("content", "") if state.doc is not None else ""
        new_content = _shared.append_to_markdown_section(content, "## Operational Notes", f"{subheading}\n\n{note_line}")
        outgoing = dict(state.doc) if state.doc is not None else {}
        outgoing["content"] = new_content
        return CanonicalMutation(doc=outgoing, evidence_text=note_line)

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

    _shared.write_mutation_evidence(context, job, canonical_doc, note_line)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action} status=success error=")

def process_employee_content_append_job(
    job_path: Path,
    job: QueueJob,
    context: RunContext,
    processed_dir: Path,
    *,
    employee: str,
    section: str,
    note_line: str,
    action: str,
    dry_run_message: str,
) -> None:
    store = _shared._vault_store()
    resolved_target = resolve_employee_target(store, employee)
    target = CanonicalTarget(
        doc_id=resolved_target.doc_id,
        doc_type=resolved_target.doc_type,
        allow_create=False,
        require_existing=True,
    )
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        try:
            canonical_doc = store.get_optional(target.doc_id)
        except Exception as exc:
            raise _shared.QueueJobError(
                "canonical couchdb write failed "
                f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: {exc}"
            ) from exc
        if canonical_doc is None:
            raise _shared.QueueJobError(
                "canonical couchdb write failed "
                f"job_type={job.job_type} job_id={job.job_id} entity_id={target.doc_id}: "
                f"CouchDB required document not found for canonical RMW: {target.doc_id}"
            )
        if job.job_id in [str(item).strip() for item in canonical_doc.get("btq_job_ids") or [] if str(item).strip()]:
            print(f"Job {job.job_id}: job_id marker already present — skipping")
            _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
            return
        print(f"Job {job.job_id}: {dry_run_message}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action} status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        content = state.doc.get("content", "") if state.doc is not None else ""
        new_content = _shared.append_to_markdown_section(content, section, note_line)
        outgoing = dict(state.doc) if state.doc is not None else {}
        outgoing["content"] = new_content
        return CanonicalMutation(doc=outgoing, evidence_text=note_line)

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

    _shared.write_mutation_evidence(context, job, canonical_doc, note_line)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action} status=success error=")

def process_flag_retention_risk_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    note_line = f"{_shared.payload_date(payload)} — {str(payload['site']).strip()} — {str(payload['details']).strip()}"
    process_employee_content_append_job(
        job_path,
        job,
        context,
        processed_dir,
        employee=str(payload["employee"]),
        section="## Retention Risks",
        note_line=note_line,
        action="flag-retention-risk",
        dry_run_message="would append retention risk",
    )
