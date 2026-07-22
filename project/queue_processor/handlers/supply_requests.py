from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btq_vault.entity_types import current_operator_id
from queue_processor.canonical_rmw import (
    CanonicalEntityState,
    CanonicalMutation,
    CanonicalTarget,
    SiteContext,
    apply_canonical_rmw,
    resolve_site_context,
)
from queue_spec import _validate_create_supply_request_payload

from . import _shared
from ._shared import QueueJob, RunContext


def normalize_supply_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one complete supply request without dropping lines."""
    if not _validate_create_supply_request_payload(payload):
        raise _shared.QueueJobError(
            "create_supply_request payload requires non-empty site_id, requested_by, observed_at, and items; "
            "each item requires a non-blank item_name"
        )

    normalized: dict[str, Any] = {
        "site_id": str(payload["site_id"]).strip(),
        "requested_by": str(payload["requested_by"]).strip(),
        "items": [],
    }
    for item in payload["items"]:
        normalized_item: dict[str, Any] = {"item_name": str(item["item_name"]).strip()}
        for field in ("quantity", "unit", "note"):
            if field not in item or item[field] is None:
                continue
            value = item[field]
            normalized_item[field] = value if isinstance(value, (int, float)) else str(value).strip()
        normalized["items"].append(normalized_item)

    for field in ("observed_at", "request_id", "notes", "source"):
        if field in payload and payload[field] is not None:
            normalized[field] = str(payload[field]).strip()
    if "related_capture_ids" in payload:
        normalized["related_capture_ids"] = [str(value).strip() for value in payload["related_capture_ids"]]
    return normalized


def supply_request_id(payload: dict[str, Any]) -> str:
    normalized = normalize_supply_request_payload(payload)
    explicit = normalized.get("request_id")
    if explicit:
        return explicit
    seed = {
        "site_id": normalized["site_id"],
        "requested_by": normalized["requested_by"],
        "observed_at": normalized.get("observed_at", ""),
        "items": normalized["items"],
    }
    digest = hashlib.sha256(
        json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"srq_{digest}"


def _build_supply_request_entity_doc(
    payload: dict[str, Any],
    site_ctx: SiteContext,
    request_id: str,
    created_at: str,
) -> dict[str, Any]:
    return {
        "_id": f"supply_request_{request_id}",
        "type": "supply_request",
        "operator": current_operator_id(),
        "supply_request_id": request_id,
        "site_id": site_ctx.site_id,
        "site_name": site_ctx.name,
        "requested_by": payload["requested_by"],
        "observed_at": payload.get("observed_at"),
        "items": [dict(item) for item in payload["items"]],
        "related_capture_ids": list(payload.get("related_capture_ids", [])),
        "status": "open",
        "created_at": created_at,
        "archived": False,
        **{field: payload[field] for field in ("notes", "source") if field in payload},
    }


def process_create_supply_request_job(
    job_path: Path,
    job: QueueJob,
    context: RunContext,
    processed_dir: Path,
) -> None:
    payload = normalize_supply_request_payload(job.payload)
    request_id = supply_request_id(payload)
    target = CanonicalTarget(
        doc_id=f"supply_request_{request_id}",
        doc_type="supply_request",
        allow_create=True,
        require_existing=False,
    )
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    store = _shared._vault_store()
    site_ctx = resolve_site_context(store, payload["site_id"])
    created_at = datetime.now(timezone.utc).isoformat()
    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        print(f"Job {job.job_id}: would create supply request")
        _shared.write_log_line(
            context.log_path,
            f"job_id={job.job_id} action=create-supply-request status=success error=",
        )
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = _build_supply_request_entity_doc(payload, site_ctx, request_id, created_at)
        if state.doc is not None:
            outgoing["created_at"] = state.doc.get("created_at") or created_at
            outgoing["status"] = state.doc.get("status") or "open"
            outgoing["archived"] = bool(state.doc.get("archived", False))
            outgoing["btq_job_ids"] = state.doc.get("btq_job_ids") or []
        return CanonicalMutation(doc=outgoing, evidence_text=f"supply_request {request_id}")

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
        _shared.write_log_line(
            context.log_path,
            f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present",
        )
        return

    evidence_text = f"supply_request {request_id}"
    _shared.write_mutation_evidence(context, job, canonical_doc, evidence_text)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(
        context.log_path,
        f"job_id={job.job_id} action=create-supply-request status=success error=",
    )
