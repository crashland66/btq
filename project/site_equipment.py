from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


EQUIPMENT_STATUSES = {"open", "approved", "ordered", "provided", "denied", "no_action_needed"}
EQUIPMENT_PRIORITIES = {"low", "normal", "high", "urgent"}


@dataclass(frozen=True)
class EquipmentRequest:
    equipment_id: str
    site_id: str
    site_name: str
    account: str
    equipment_name: str
    reason: str = ""
    priority: str = "normal"
    requested_by: str = ""
    observed_at: str = ""
    source: str = ""
    status: str = "open"
    notes: str = ""
    related_capture_ids: tuple[str, ...] = ()
    related_candidate_ids: tuple[str, ...] = ()
    created_at: str = ""
    doc_id: str = ""
    approved_at: str = ""
    approved_by: str = ""
    approval_note: str = ""
    denied_at: str = ""
    denied_by: str = ""
    denial_note: str = ""
    ordered_at: str = ""
    ordered_by: str = ""
    ordered_note: str = ""
    provided_at: str = ""
    provided_by: str = ""
    provided_note: str = ""
    archived: bool = False
    archived_at: str = ""
    archived_by: str = ""


def discover_site_equipment(
    vault_root: Path,
    *,
    site_id: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
    archived_only: bool = False,
) -> dict[str, object]:
    """Equipment requests, from CouchDB ``btq_vault`` (canonical).

    Reads ``type: equipment_request`` docs from CouchDB. ``vault_root`` is
    accepted for call-site compatibility but no longer used.
    """
    return _discover_site_equipment_couchdb(site_id=site_id, status=status, include_archived=include_archived, archived_only=archived_only)


def _equipment_from_couch_doc(doc: dict[str, Any]) -> EquipmentRequest | None:
    if not isinstance(doc, dict):
        return None
    site_id = clean_string(doc.get("site_id"))
    equipment_id = clean_string(doc.get("equipment_id"))
    equipment_name = clean_string(doc.get("equipment_name"))
    if not site_id or not equipment_id or not equipment_name:
        return None
    status = clean_string(doc.get("status")) or "open"
    if status not in EQUIPMENT_STATUSES:
        return None
    priority = clean_string(doc.get("priority")) or "normal"
    if priority not in EQUIPMENT_PRIORITIES:
        priority = "normal"
    return EquipmentRequest(
        equipment_id=equipment_id,
        site_id=site_id,
        site_name=clean_string(doc.get("site_name")),
        account=clean_string(doc.get("account")),
        equipment_name=equipment_name,
        reason=clean_string(doc.get("reason")),
        priority=priority,
        requested_by=clean_string(doc.get("requested_by")),
        observed_at=clean_string(doc.get("observed_at")),
        source=clean_string(doc.get("source")),
        status=status,
        notes=clean_string(doc.get("notes")),
        related_capture_ids=tuple(str(x) for x in (doc.get("related_capture_ids") or doc.get("btq_job_ids") or []) if x),
        related_candidate_ids=tuple(str(x) for x in (doc.get("related_candidate_ids") or []) if x),
        created_at=clean_string(doc.get("created_at")),
        doc_id=clean_string(doc.get("_id")),
        approved_at=clean_string(doc.get("approved_at")),
        approved_by=clean_string(doc.get("approved_by")),
        approval_note=clean_string(doc.get("approval_note")),
        denied_at=clean_string(doc.get("denied_at")),
        denied_by=clean_string(doc.get("denied_by")),
        denial_note=clean_string(doc.get("denial_note")),
        ordered_at=clean_string(doc.get("ordered_at")),
        ordered_by=clean_string(doc.get("ordered_by")),
        ordered_note=clean_string(doc.get("ordered_note")),
        provided_at=clean_string(doc.get("provided_at")),
        provided_by=clean_string(doc.get("provided_by")),
        provided_note=clean_string(doc.get("provided_note")),
        archived=bool(doc.get("archived")),
        archived_at=clean_string(doc.get("archived_at")),
        archived_by=clean_string(doc.get("archived_by")),
    )


def _include_archived_item(archived: bool, *, include_archived: bool, archived_only: bool) -> bool:
    if archived_only:
        return archived
    return include_archived or not archived


def _discover_site_equipment_couchdb(
    *,
    site_id: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
    archived_only: bool = False,
) -> dict[str, object]:
    import json
    from urllib import parse as urllib_parse, request as urllib_request

    from event_pipeline import couchdb_config

    cfg = couchdb_config.from_env()
    db_name = couchdb_config.vault_database()
    url = f"{cfg.base_url.rstrip('/')}/{urllib_parse.quote(db_name, safe='')}/_find"
    selector: dict[str, object] = {"type": "equipment_request"}
    if archived_only:
        selector["archived"] = True
    if site_id is not None:
        selector["site_id"] = str(site_id)
    payload = {"selector": selector, "limit": 100000}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(cfg.auth_header())
    req = urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=cfg.timeout) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"equipment": [], "warnings": [{"path": db_name, "reason": f"couchdb_query_failed:{exc}"}], "counts": equipment_counts([])}
    status_filter = str(status).strip() if status is not None else None
    equipment: list[EquipmentRequest] = []
    for doc in response.get("docs", []):
        request = _equipment_from_couch_doc(doc)
        if request is None:
            continue
        if not _include_archived_item(request.archived, include_archived=include_archived, archived_only=archived_only):
            continue
        if site_id is not None and request.site_id != str(site_id):
            continue
        if status_filter is not None and request.status != status_filter:
            continue
        equipment.append(request)
    equipment.sort(key=lambda item: (status_sort(item.status), priority_sort(item.priority), item.site_id, item.created_at, item.equipment_id))
    return {"equipment": equipment, "warnings": [], "counts": equipment_counts(equipment, include_archived=include_archived or archived_only)}


def clean_string(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"", '""', "''", "null", "None"}:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text


def equipment_counts(equipment: Iterable[EquipmentRequest], *, include_archived: bool = False) -> dict[str, object]:
    items = [request for request in equipment if include_archived or not request.archived]
    return {
        "total": len(items),
        "by_status": count_by(items, "status"),
        "by_priority": count_by(items, "priority"),
    }


def count_by(equipment: Iterable[EquipmentRequest], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for request in equipment:
        value = str(getattr(request, field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def status_sort(status: str) -> int:
    return {"open": 0, "approved": 1, "ordered": 2, "provided": 3, "denied": 4, "no_action_needed": 5}.get(status, 9)


def priority_sort(priority: str) -> int:
    return {"urgent": 0, "high": 1, "normal": 2, "low": 3}.get(priority, 9)


def equipment_as_export(equipment: EquipmentRequest, *, include_path: bool = False) -> dict[str, object]:
    payload = {
        "equipment_id": equipment.equipment_id,
        "site_id": equipment.site_id,
        "site_name": equipment.site_name,
        "account": equipment.account,
        "equipment_name": equipment.equipment_name,
        "reason": equipment.reason,
        "priority": equipment.priority,
        "requested_by": equipment.requested_by,
        "observed_at": equipment.observed_at,
        "source": equipment.source,
        "status": equipment.status,
        "notes": equipment.notes,
        "related_capture_ids": list(equipment.related_capture_ids),
        "related_candidate_ids": list(equipment.related_candidate_ids),
        "created_at": equipment.created_at,
        "approved_at": equipment.approved_at,
        "approved_by": equipment.approved_by,
        "approval_note": equipment.approval_note,
        "denied_at": equipment.denied_at,
        "denied_by": equipment.denied_by,
        "denial_note": equipment.denial_note,
        "ordered_at": equipment.ordered_at,
        "ordered_by": equipment.ordered_by,
        "ordered_note": equipment.ordered_note,
        "provided_at": equipment.provided_at,
        "provided_by": equipment.provided_by,
        "provided_note": equipment.provided_note,
        "archived": equipment.archived,
        "archived_at": equipment.archived_at,
        "archived_by": equipment.archived_by,
    }
    if include_path:
        payload["_id"] = equipment.doc_id
    return payload
