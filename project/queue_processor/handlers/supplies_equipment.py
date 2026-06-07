from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from btq_vault.markdown_export import render_equipment_request_markdown, render_supply_need_markdown
from btq_vault.entity_types import OPERATOR_ID_GREG
from io_atomic import atomic_write_text
from queue_processor.canonical_rmw import (
    CanonicalEntityState,
    CanonicalMutation,
    CanonicalTarget,
    SiteContext,
    apply_canonical_rmw,
    resolve_site_context,
)
from queue_processor.idempotency import has_job_been_applied, parse_frontmatter_text, upsert_job_id_frontmatter
from site_equipment import EquipmentRequest, parse_site_equipment
from site_supplies import SupplyNeed, parse_site_supply

from . import _shared
from ._shared import (
    QueueJob,
    QueueProcessorError,
    RunContext,
    slugify_issue_component,
)

def replace_or_insert_subsection(
    existing_text: str,
    parent_heading: str,
    child_heading: str,
    new_body: str,
) -> str:
    """Replace a child subsection body inside a parent heading scope.

    Both headings are full markdown headings, for example
    "## Operational Notes" and "### Supplies / Equipment". The returned
    ``new_body`` does not include the child heading line. Idempotent:
    identical inputs produce identical output.
    """
    had_trailing_newline = existing_text.endswith("\n")
    lines = existing_text.splitlines()

    def heading_level(line: str) -> int | None:
        stripped = line.rstrip()
        if not stripped.startswith("#"):
            return None
        marker = stripped.split(" ", 1)[0]
        if marker and set(marker) == {"#"} and len(marker) < len(stripped):
            return len(marker)
        return None

    parent_index = next((index for index, line in enumerate(lines) if line.rstrip() == parent_heading), None)
    if parent_index is None:
        raise QueueProcessorError(f"parent heading not found: {parent_heading}")

    parent_level = heading_level(parent_heading)
    if parent_level is None:
        raise QueueProcessorError(f"invalid parent heading: {parent_heading}")
    parent_end = len(lines)
    for index in range(parent_index + 1, len(lines)):
        level = heading_level(lines[index])
        if level is not None and level <= parent_level:
            parent_end = index
            break

    child_index = None
    for index in range(parent_index + 1, parent_end):
        if lines[index].rstrip() == child_heading:
            child_index = index
            break

    body_lines = new_body.splitlines()
    if child_index is not None:
        child_level = heading_level(child_heading)
        if child_level is None:
            raise QueueProcessorError(f"invalid child heading: {child_heading}")
        child_end = parent_end
        for index in range(child_index + 1, parent_end):
            level = heading_level(lines[index])
            if level is not None and level <= child_level:
                child_end = index
                break
        updated_lines = lines[: child_index + 1] + body_lines + lines[child_end:]
    else:
        insertion = [child_heading, *body_lines]
        if parent_end > parent_index + 1 and lines[parent_end - 1].strip():
            insertion.insert(0, "")
        updated_lines = lines[:parent_end] + insertion + lines[parent_end:]

    updated_text = "\n".join(updated_lines)
    if had_trailing_newline:
        return f"{updated_text}\n"
    return updated_text

def supply_need_id(payload: dict) -> str:
    explicit = str(payload.get("supply_id") or "").strip()
    if explicit:
        return explicit
    seed = {
        "site_id": str(payload.get("site_id") or "").strip(),
        "item_name": str(payload.get("item_name") or "").strip(),
        "requested_by": str(payload.get("requested_by") or "").strip(),
        "observed_at": str(payload.get("observed_at") or "").strip(),
    }
    digest = hashlib.sha256(json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return f"sup_{digest}"

def supply_need_path(context: RunContext, site_path: Path, payload: dict) -> Path:
    supply_id = supply_need_id(payload)
    filename = f"{supply_id}__{slugify_issue_component(str(payload.get('item_name') or 'supply need'))}.md"
    supplies_dir = _shared.ensure_within_root(site_path.parent / "Supplies", context.vault_root, "Supplies directory")
    return _shared.ensure_within_root(supplies_dir / filename, context.vault_root, "Supply need target")

def equipment_request_id(payload: dict) -> str:
    explicit = str(payload.get("equipment_id") or "").strip()
    if explicit:
        return explicit
    seed = {
        "site_id": str(payload.get("site_id") or "").strip(),
        "equipment_name": str(payload.get("equipment_name") or "").strip(),
        "requested_by": str(payload.get("requested_by") or "").strip(),
        "observed_at": str(payload.get("observed_at") or "").strip(),
    }
    digest = hashlib.sha256(json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return f"eqr_{digest}"

def equipment_request_path(context: RunContext, site_path: Path, payload: dict) -> Path:
    equipment_id = equipment_request_id(payload)
    filename = f"{equipment_id}__{slugify_issue_component(str(payload.get('equipment_name') or 'equipment request'))}.md"
    equipment_dir = _shared.ensure_within_root(site_path.parent / "Equipment", context.vault_root, "Equipment directory")
    return _shared.ensure_within_root(equipment_dir / filename, context.vault_root, "Equipment request target")

def supply_need_status(payload: dict) -> str:
    return str(payload.get("status") or "open").strip() or "open"

def supply_need_urgency(payload: dict) -> str:
    return str(payload.get("urgency") or "normal").strip() or "normal"

def equipment_request_status(payload: dict) -> str:
    return str(payload.get("status") or "open").strip() or "open"

def equipment_request_priority(payload: dict) -> str:
    return str(payload.get("priority") or "normal").strip() or "normal"

def render_site_equipment_section(
    payload: dict,
    job_id: str,
) -> str:
    """Render the body of the site Supplies / Equipment subsection."""

    def table_value(value: object) -> str:
        return " ".join(str(value).splitlines()).strip().replace("|", r"\|")

    rows = [
        "| Description | Brand | Color | Status | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in payload["equipment"]:
        notes = item.get("notes", "")
        rows.append(
            "| "
            f"{table_value(item['description'])} | "
            f"{table_value(item['brand'])} | "
            f"{table_value(item['color'])} | "
            f"{table_value(item['status'])} | "
            f"{table_value(notes)} |"
        )

    body = "\n".join(rows)
    body += f"\n\nInspection: {payload['inspection_date']} ({payload['inspected_by']})\n\n"
    section_notes = str(payload.get("section_notes") or "").strip()
    if section_notes:
        body += f"{section_notes}\n\n"
    body += f"<!-- btq_job_id: {job_id} -->\n"
    return body

def _build_supply_need_entity_doc(payload: dict, job: QueueJob, site_ctx: SiteContext, supply_id: str, created_at: str) -> dict:
    return {
        "_id": f"supply_need_{supply_id}",
        "type": "supply_need",
        "operator": OPERATOR_ID_GREG,
        "supply_id": supply_id,
        "site_id": site_ctx.site_id,
        "site_name": site_ctx.name,
        "created_at": created_at,
        "btq_job_ids": [job.job_id],
        **{k: v for k, v in payload.items() if k not in {"site_id"}},
    }

def _build_equipment_request_entity_doc(payload: dict, job: QueueJob, site_ctx: SiteContext, equipment_id: str, created_at: str) -> dict:
    return {
        "_id": f"equipment_request_{equipment_id}",
        "type": "equipment_request",
        "operator": OPERATOR_ID_GREG,
        "equipment_id": equipment_id,
        "site_id": site_ctx.site_id,
        "site_name": site_ctx.name,
        "created_at": created_at,
        "btq_job_ids": [job.job_id],
        **{k: v for k, v in payload.items() if k not in {"site_id"}},
    }

def process_log_supply_need_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    site_path = _shared.resolve_site_about_path(context, str(payload["site_id"]))
    site_ctx = resolve_site_context(_shared._vault_store(), str(payload["site_id"]))
    supply_id = supply_need_id(payload)
    target_path = supply_need_path(context, site_path, payload)
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    existing_text = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    created_at = datetime.now(timezone.utc).isoformat()
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target_path}")
    if context.dry_run:
        print(f"Job {job.job_id}: would log supply need")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=log-supply-need status=success error=")
        return

    target = CanonicalTarget(
        doc_id=f"supply_need_{supply_id}",
        doc_type="supply_need",
        projection_path=target_path,
        allow_create=True,
        require_existing=False,
    )

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        is_new_doc = state.doc is None or set(state.doc) <= {"_id", "type"}
        if is_new_doc:
            doc = _build_supply_need_entity_doc(payload, job, site_ctx, supply_id, created_at)
            doc.pop("btq_job_ids", None)
        else:
            doc = dict(state.doc)
            existing_created_at = doc.get("created_at") or created_at
            existing_job_ids = doc.get("btq_job_ids")
            doc.update(payload)
            doc["created_at"] = existing_created_at
            doc["btq_job_ids"] = existing_job_ids
            doc.setdefault("operator", OPERATOR_ID_GREG)
            doc["supply_id"] = supply_id
            doc["site_id"] = site_ctx.site_id
            doc["site_name"] = site_ctx.name
        return CanonicalMutation(doc=doc, evidence_text=f"supply_need {supply_id}")

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

    projection_payload = {
        key: value
        for key, value in canonical_doc.items()
        if key not in {"_id", "_rev", "type", "operator", "btq_job_ids", "created_at"}
    }
    final_text = render_supply_need_markdown(
        payload=projection_payload,
        site_id=str(canonical_doc.get("site_id") or site_ctx.site_id),
        site_name=str(canonical_doc.get("site_name") or site_ctx.name),
        account=site_ctx.account or site_path.parents[2].name,
        supply_id=str(canonical_doc.get("supply_id") or supply_id),
        job_id=job.job_id,
        created_at=str(canonical_doc.get("created_at") or created_at),
        existing_text=existing_text,
    )
    for canonical_job_id in canonical_doc.get("btq_job_ids") or []:
        final_text = _shared.upsert_job_id_frontmatter(final_text, str(canonical_job_id))

    target_path.parent.mkdir(parents=True, exist_ok=True)
    _shared.atomic_write_text(target_path, final_text)
    _shared.write_mutation_evidence(context, job, target_path, existing_text, final_text, f"supply_need {supply_id}")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target_path}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=log-supply-need status=success error=")

def process_log_equipment_request_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    site_path = _shared.resolve_site_about_path(context, str(payload["site_id"]))
    site_ctx = resolve_site_context(_shared._vault_store(), str(payload["site_id"]))
    equipment_id = equipment_request_id(payload)
    target_path = equipment_request_path(context, site_path, payload)
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    existing_text = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    created_at = datetime.now(timezone.utc).isoformat()
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target_path}")
    if context.dry_run:
        print(f"Job {job.job_id}: would log equipment request")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=log-equipment-request status=success error=")
        return

    target = CanonicalTarget(
        doc_id=f"equipment_request_{equipment_id}",
        doc_type="equipment_request",
        projection_path=target_path,
        allow_create=True,
        require_existing=False,
    )

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        is_new_doc = state.doc is None or set(state.doc) <= {"_id", "type"}
        if is_new_doc:
            doc = _build_equipment_request_entity_doc(payload, job, site_ctx, equipment_id, created_at)
            doc.pop("btq_job_ids", None)
        else:
            doc = dict(state.doc)
            existing_created_at = doc.get("created_at") or created_at
            existing_job_ids = doc.get("btq_job_ids")
            doc.update(payload)
            doc["created_at"] = existing_created_at
            doc["btq_job_ids"] = existing_job_ids
            doc.setdefault("operator", OPERATOR_ID_GREG)
            doc["equipment_id"] = equipment_id
            doc["site_id"] = site_ctx.site_id
            doc["site_name"] = site_ctx.name
        return CanonicalMutation(doc=doc, evidence_text=f"equipment_request {equipment_id}")

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

    projection_payload = {
        key: value
        for key, value in canonical_doc.items()
        if key not in {"_id", "_rev", "type", "operator", "btq_job_ids", "created_at"}
    }
    final_text = render_equipment_request_markdown(
        payload=projection_payload,
        site_id=str(canonical_doc.get("site_id") or site_ctx.site_id),
        site_name=str(canonical_doc.get("site_name") or site_ctx.name),
        account=site_ctx.account or site_path.parents[2].name,
        equipment_id=str(canonical_doc.get("equipment_id") or equipment_id),
        job_id=job.job_id,
        created_at=str(canonical_doc.get("created_at") or created_at),
        existing_text=existing_text,
    )
    for canonical_job_id in canonical_doc.get("btq_job_ids") or []:
        final_text = _shared.upsert_job_id_frontmatter(final_text, str(canonical_job_id))

    target_path.parent.mkdir(parents=True, exist_ok=True)
    _shared.atomic_write_text(target_path, final_text)
    _shared.write_mutation_evidence(context, job, target_path, existing_text, final_text, f"equipment_request {equipment_id}")
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target_path}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=log-equipment-request status=success error=")

def process_update_site_equipment_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    site_query = str(payload.get("site_id") or payload["site"])
    site_path = _shared.resolve_site_about_path(context, site_query)
    if payload.get("site") and payload.get("site_id"):
        named_site_path = _shared.resolve_site_about_path(context, str(payload["site"]))
        if named_site_path != site_path:
            raise _shared.InvalidSiteIdError(f"Site and site_id resolve to different sites: {payload['site']} / {payload['site_id']}")
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")
    existing_text = site_path.read_text(encoding="utf-8") if site_path.exists() else ""
    job_marker = f"<!-- btq_job_id: {job.job_id} -->"
    if _shared.has_job_been_applied(existing_text, job.job_id) or job_marker in existing_text:
        if context.dry_run:
            print(f"Job {job.job_id}: job_id marker already present — skipping")
            _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
            return
        print(f"Job {job.job_id}: job_id marker already present — skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return
    new_body = render_site_equipment_section(payload=payload, job_id=job.job_id)
    final_text = replace_or_insert_subsection(
        existing_text,
        parent_heading="## Operational Notes",
        child_heading="### Supplies / Equipment",
        new_body=new_body,
    )
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {site_path}")
    if context.dry_run:
        print(f"Job {job.job_id}: would update site equipment inventory")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=update-site-equipment status=success error=")
        return
    _shared.patch_canonical_location_content(site_path, final_text, job)
    _shared.atomic_write_text(site_path, final_text)
    _shared.write_mutation_evidence(context, job, site_path, existing_text, final_text, new_body)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {site_path}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=update-site-equipment status=success error=")
