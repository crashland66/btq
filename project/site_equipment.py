from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from vault_markdown import frontmatter_list, read_typed_markdown_note


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
    vault_path: str = ""
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


def discover_site_equipment(vault_root: Path, *, site_id: str | None = None, status: str | None = None) -> dict[str, object]:
    equipment: list[EquipmentRequest] = []
    warnings: list[dict[str, str]] = []
    root = vault_root.expanduser().resolve(strict=False)
    accounts_root = root / "Accounts"
    if not accounts_root.exists():
        return {"equipment": [], "warnings": [{"path": str(accounts_root), "reason": "accounts_root_missing"}], "counts": equipment_counts([])}

    status_filter = str(status).strip() if status is not None else None
    for path in sorted(accounts_root.glob("*/Locations/*/Equipment/*.md")):
        request, warning = parse_site_equipment(path)
        if warning is not None:
            warnings.append(warning)
            continue
        if request is None:
            continue
        if site_id is not None and request.site_id != str(site_id):
            continue
        if status_filter is not None and request.status != status_filter:
            continue
        equipment.append(request)
    equipment.sort(key=lambda item: (status_sort(item.status), priority_sort(item.priority), item.site_id, item.created_at, item.equipment_id))
    return {"equipment": equipment, "warnings": warnings, "counts": equipment_counts(equipment)}


def parse_site_equipment(path: Path) -> tuple[EquipmentRequest | None, dict[str, str] | None]:
    frontmatter, _body, warning = read_typed_markdown_note(path, "equipment_request")
    if warning is not None or frontmatter is None:
        return None, warning
    site_id = clean_string(frontmatter.get("site_id"))
    equipment_id = clean_string(frontmatter.get("equipment_id")) or path.stem.split("__", 1)[0]
    equipment_name = clean_string(frontmatter.get("equipment_name")) or path.stem
    if not site_id or not equipment_id or not equipment_name:
        return None, {"path": str(path), "reason": "missing_required_site_id_or_equipment_id_or_equipment_name"}
    status = clean_string(frontmatter.get("status")) or "open"
    if status not in EQUIPMENT_STATUSES:
        return None, {"path": str(path), "reason": f"unknown_status:{status}"}
    priority = clean_string(frontmatter.get("priority")) or "normal"
    if priority not in EQUIPMENT_PRIORITIES:
        return None, {"path": str(path), "reason": f"unknown_priority:{priority}"}
    return (
        EquipmentRequest(
            equipment_id=equipment_id,
            site_id=site_id,
            site_name=clean_string(frontmatter.get("site_name")),
            account=clean_string(frontmatter.get("account")),
            equipment_name=equipment_name,
            reason=clean_string(frontmatter.get("reason")),
            priority=priority,
            requested_by=clean_string(frontmatter.get("requested_by")),
            observed_at=clean_string(frontmatter.get("observed_at")),
            source=clean_string(frontmatter.get("source")),
            status=status,
            notes=clean_string(frontmatter.get("notes")),
            related_capture_ids=tuple(frontmatter_list(frontmatter.get("related_capture_ids"))),
            related_candidate_ids=tuple(frontmatter_list(frontmatter.get("related_candidate_ids"))),
            created_at=clean_string(frontmatter.get("created_at")),
            vault_path=relative_vault_path(path),
            approved_at=clean_string(frontmatter.get("approved_at")),
            approved_by=clean_string(frontmatter.get("approved_by")),
            approval_note=clean_string(frontmatter.get("approval_note")),
            denied_at=clean_string(frontmatter.get("denied_at")),
            denied_by=clean_string(frontmatter.get("denied_by")),
            denial_note=clean_string(frontmatter.get("denial_note")),
            ordered_at=clean_string(frontmatter.get("ordered_at")),
            ordered_by=clean_string(frontmatter.get("ordered_by")),
            ordered_note=clean_string(frontmatter.get("ordered_note")),
            provided_at=clean_string(frontmatter.get("provided_at")),
            provided_by=clean_string(frontmatter.get("provided_by")),
            provided_note=clean_string(frontmatter.get("provided_note")),
        ),
        None,
    )


def clean_string(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"", '""', "''", "null", "None"}:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text


def relative_vault_path(path: Path) -> str:
    parts = path.expanduser().resolve(strict=False).parts
    if "Accounts" in parts:
        return str(Path(*parts[parts.index("Accounts") :]))
    return path.name


def equipment_counts(equipment: Iterable[EquipmentRequest]) -> dict[str, object]:
    items = list(equipment)
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
    }
    if include_path:
        payload["vault_equipment_path"] = equipment.vault_path
    return payload
