from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from vault_markdown import frontmatter_list, read_typed_markdown_note


SUPPLY_STATUSES = {"open", "ordered", "delivered", "stocked", "no_action_needed"}
SUPPLY_URGENCIES = {"low", "normal", "high", "critical"}


@dataclass(frozen=True)
class SupplyNeed:
    supply_id: str
    site_id: str
    site_name: str
    account: str
    item_name: str
    quantity_needed: str = ""
    urgency: str = "normal"
    requested_by: str = ""
    observed_at: str = ""
    source: str = ""
    status: str = "open"
    notes: str = ""
    related_capture_ids: tuple[str, ...] = ()
    related_candidate_ids: tuple[str, ...] = ()
    created_at: str = ""
    doc_id: str = ""
    ordered_at: str = ""
    ordered_by: str = ""
    ordered_note: str = ""
    delivered_at: str = ""
    delivered_by: str = ""
    delivered_note: str = ""
    stocked_at: str = ""
    stocked_by: str = ""
    stocked_note: str = ""


def discover_site_supplies(vault_root: Path, *, site_id: str | None = None, status: str | None = None) -> dict[str, object]:
    """Supplies, from CouchDB ``btq_vault`` (canonical) when configured.

    The on-disk vault is iCloud-synced; a background launchd daemon scanning it
    blocks indefinitely. Read ``type: supply_need`` docs from CouchDB in prod;
    keep the Markdown glob only as a dev/CI fallback.
    """
    import os

    if os.environ.get("BTQ_COUCHDB_URL", "").strip():
        return _discover_site_supplies_couchdb(site_id=site_id, status=status)
    return _discover_site_supplies_filesystem(vault_root, site_id=site_id, status=status)


def _supply_from_couch_doc(doc: dict[str, Any]) -> SupplyNeed | None:
    if not isinstance(doc, dict):
        return None
    site_id = clean_string(doc.get("site_id"))
    supply_id = clean_string(doc.get("supply_id"))
    item_name = clean_string(doc.get("item_name"))
    if not site_id or not supply_id or not item_name:
        return None
    status = clean_string(doc.get("status")) or "open"
    if status not in SUPPLY_STATUSES:
        return None
    urgency = clean_string(doc.get("urgency")) or "normal"
    if urgency not in SUPPLY_URGENCIES:
        urgency = "normal"
    return SupplyNeed(
        supply_id=supply_id,
        site_id=site_id,
        site_name=clean_string(doc.get("site_name")),
        account=clean_string(doc.get("account")),
        item_name=item_name,
        quantity_needed=clean_string(doc.get("quantity_needed")),
        urgency=urgency,
        requested_by=clean_string(doc.get("requested_by")),
        observed_at=clean_string(doc.get("observed_at")),
        source=clean_string(doc.get("source")),
        status=status,
        notes=clean_string(doc.get("notes")),
        related_capture_ids=tuple(str(x) for x in (doc.get("related_capture_ids") or doc.get("btq_job_ids") or []) if x),
        related_candidate_ids=tuple(str(x) for x in (doc.get("related_candidate_ids") or []) if x),
        created_at=clean_string(doc.get("created_at")),
        doc_id=clean_string(doc.get("_id")),
        ordered_at=clean_string(doc.get("ordered_at")),
        ordered_by=clean_string(doc.get("ordered_by")),
        ordered_note=clean_string(doc.get("ordered_note")),
        delivered_at=clean_string(doc.get("delivered_at")),
        delivered_by=clean_string(doc.get("delivered_by")),
        delivered_note=clean_string(doc.get("delivered_note")),
        stocked_at=clean_string(doc.get("stocked_at")),
        stocked_by=clean_string(doc.get("stocked_by")),
        stocked_note=clean_string(doc.get("stocked_note")),
    )


def _discover_site_supplies_couchdb(*, site_id: str | None = None, status: str | None = None) -> dict[str, object]:
    import json
    from urllib import parse as urllib_parse, request as urllib_request

    from event_pipeline import couchdb_config

    cfg = couchdb_config.from_env()
    db_name = couchdb_config.vault_database()
    url = f"{cfg.base_url.rstrip('/')}/{urllib_parse.quote(db_name, safe='')}/_find"
    selector: dict[str, object] = {"type": "supply_need"}
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
        return {"supplies": [], "warnings": [{"path": db_name, "reason": f"couchdb_query_failed:{exc}"}], "counts": supply_counts([])}
    status_filter = str(status).strip() if status is not None else None
    supplies: list[SupplyNeed] = []
    for doc in response.get("docs", []):
        supply = _supply_from_couch_doc(doc)
        if supply is None:
            continue
        if site_id is not None and supply.site_id != str(site_id):
            continue
        if status_filter is not None and supply.status != status_filter:
            continue
        supplies.append(supply)
    supplies.sort(key=lambda item: (status_sort(item.status), urgency_sort(item.urgency), item.site_id, item.created_at, item.supply_id))
    return {"supplies": supplies, "warnings": [], "counts": supply_counts(supplies)}


def _discover_site_supplies_filesystem(vault_root: Path, *, site_id: str | None = None, status: str | None = None) -> dict[str, object]:
    supplies: list[SupplyNeed] = []
    warnings: list[dict[str, str]] = []
    root = vault_root.expanduser().resolve(strict=False)
    accounts_root = root / "Accounts"
    if not accounts_root.exists():
        return {"supplies": [], "warnings": [{"path": str(accounts_root), "reason": "accounts_root_missing"}], "counts": supply_counts([])}

    status_filter = str(status).strip() if status is not None else None
    for path in sorted(accounts_root.glob("*/Locations/*/Supplies/*.md")):
        supply, warning = parse_site_supply(path)
        if warning is not None:
            warnings.append(warning)
            continue
        if supply is None:
            continue
        if site_id is not None and supply.site_id != str(site_id):
            continue
        if status_filter is not None and supply.status != status_filter:
            continue
        supplies.append(supply)
    supplies.sort(key=lambda item: (status_sort(item.status), urgency_sort(item.urgency), item.site_id, item.created_at, item.supply_id))
    return {"supplies": supplies, "warnings": warnings, "counts": supply_counts(supplies)}


def parse_site_supply(path: Path) -> tuple[SupplyNeed | None, dict[str, str] | None]:
    frontmatter, _body, warning = read_typed_markdown_note(path, "supply_need")
    if warning is not None or frontmatter is None:
        return None, warning
    site_id = clean_string(frontmatter.get("site_id"))
    supply_id = clean_string(frontmatter.get("supply_id")) or path.stem.split("__", 1)[0]
    item_name = clean_string(frontmatter.get("item_name")) or path.stem
    if not site_id or not supply_id or not item_name:
        return None, {"path": str(path), "reason": "missing_required_site_id_or_supply_id_or_item_name"}
    status = clean_string(frontmatter.get("status")) or "open"
    if status not in SUPPLY_STATUSES:
        return None, {"path": str(path), "reason": f"unknown_status:{status}"}
    urgency = clean_string(frontmatter.get("urgency")) or "normal"
    if urgency not in SUPPLY_URGENCIES:
        return None, {"path": str(path), "reason": f"unknown_urgency:{urgency}"}
    return (
        SupplyNeed(
            supply_id=supply_id,
            site_id=site_id,
            site_name=clean_string(frontmatter.get("site_name")),
            account=clean_string(frontmatter.get("account")),
            item_name=item_name,
            quantity_needed=clean_string(frontmatter.get("quantity_needed")),
            urgency=urgency,
            requested_by=clean_string(frontmatter.get("requested_by")),
            observed_at=clean_string(frontmatter.get("observed_at")),
            source=clean_string(frontmatter.get("source")),
            status=status,
            notes=clean_string(frontmatter.get("notes")),
            related_capture_ids=tuple(frontmatter_list(frontmatter.get("related_capture_ids"))),
            related_candidate_ids=tuple(frontmatter_list(frontmatter.get("related_candidate_ids"))),
            created_at=clean_string(frontmatter.get("created_at")),
            doc_id=supply_id,
            ordered_at=clean_string(frontmatter.get("ordered_at")),
            ordered_by=clean_string(frontmatter.get("ordered_by")),
            ordered_note=clean_string(frontmatter.get("ordered_note")),
            delivered_at=clean_string(frontmatter.get("delivered_at")),
            delivered_by=clean_string(frontmatter.get("delivered_by")),
            delivered_note=clean_string(frontmatter.get("delivered_note")),
            stocked_at=clean_string(frontmatter.get("stocked_at")),
            stocked_by=clean_string(frontmatter.get("stocked_by")),
            stocked_note=clean_string(frontmatter.get("stocked_note")),
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


def supply_counts(supplies: Iterable[SupplyNeed]) -> dict[str, object]:
    items = list(supplies)
    return {
        "total": len(items),
        "by_status": count_by(items, "status"),
        "by_urgency": count_by(items, "urgency"),
    }


def count_by(supplies: Iterable[SupplyNeed], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for supply in supplies:
        value = str(getattr(supply, field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def status_sort(status: str) -> int:
    return {"open": 0, "ordered": 1, "delivered": 2, "stocked": 3, "no_action_needed": 4}.get(status, 9)


def urgency_sort(urgency: str) -> int:
    return {"critical": 0, "high": 1, "normal": 2, "low": 3}.get(urgency, 9)


def supply_as_export(supply: SupplyNeed, *, include_path: bool = False) -> dict[str, object]:
    payload = {
        "supply_id": supply.supply_id,
        "site_id": supply.site_id,
        "site_name": supply.site_name,
        "account": supply.account,
        "item_name": supply.item_name,
        "quantity_needed": supply.quantity_needed,
        "urgency": supply.urgency,
        "requested_by": supply.requested_by,
        "observed_at": supply.observed_at,
        "source": supply.source,
        "status": supply.status,
        "notes": supply.notes,
        "related_capture_ids": list(supply.related_capture_ids),
        "related_candidate_ids": list(supply.related_candidate_ids),
        "created_at": supply.created_at,
        "ordered_at": supply.ordered_at,
        "ordered_by": supply.ordered_by,
        "ordered_note": supply.ordered_note,
        "delivered_at": supply.delivered_at,
        "delivered_by": supply.delivered_by,
        "delivered_note": supply.delivered_note,
        "stocked_at": supply.stocked_at,
        "stocked_by": supply.stocked_by,
        "stocked_note": supply.stocked_note,
    }
    if include_path:
        payload["_id"] = supply.doc_id
    return payload
