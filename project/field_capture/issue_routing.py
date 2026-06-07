from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive_title(note: str, intake: dict[str, Any]) -> str:
    if note:
        first_line = note.splitlines()[0].strip()
        if len(first_line) > 60:
            return first_line[:59].rstrip() + "…"
        return first_line
    return f"Field-reported issue at {intake.get('site') or intake.get('site_id') or 'unknown site'}"


def _field(intake: dict[str, Any], name: str) -> Any:
    if name in intake:
        return intake.get(name)
    metadata = intake.get("metadata") if isinstance(intake.get("metadata"), dict) else {}
    payload = intake.get("payload") if isinstance(intake.get("payload"), dict) else {}
    if name in metadata:
        return metadata.get(name)
    return payload.get(name)


def _field_view(intake: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    metadata = intake.get("metadata") if isinstance(intake.get("metadata"), dict) else {}
    payload = intake.get("payload") if isinstance(intake.get("payload"), dict) else {}
    merged.update(metadata)
    merged.update(payload)
    merged.update({key: value for key, value in intake.items() if key not in {"metadata", "payload"}})
    return merged


def _related_media(photos: object) -> list[str]:
    if not isinstance(photos, list):
        return []
    related: list[str] = []
    for photo in photos:
        if not isinstance(photo, dict):
            continue
        stored_path = str(photo.get("stored_path") or "")
        if not stored_path:
            continue
        related.append(f"/media/{stored_path.split('runtime/uploads/', 1)[-1]}")
    return related


def route_field_reported_issues(
    intake_dir: Path,
    queue_dir: Path,
    *,
    runtime_root: Path,
    logger: logging.Logger,
    limit: int | None = None,
) -> dict[str, int]:
    """Walk intake captures with qc_category=report_an_issue."""
    counts = {"discovered": 0, "routed": 0, "skipped": 0, "failed": 0}
    intake_dir = intake_dir.expanduser().resolve(strict=False)
    queue_dir = queue_dir.expanduser().resolve(strict=False)
    runtime_root = runtime_root.expanduser().resolve(strict=False)

    routed_this_cycle = 0
    for path in sorted(intake_dir.glob("cap-*.json")):
        if limit is not None and routed_this_cycle >= limit:
            break
        try:
            intake = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(intake, dict):
                counts["skipped"] += 1
                continue
            qc_category = str(_field(intake, "qc_category_canonical") or _field(intake, "qc_category") or "")
            if qc_category != "report_an_issue":
                continue
            counts["discovered"] += 1

            routed_marker = intake_dir / f"{path.stem}.issue_routed.json"
            if routed_marker.exists():
                counts["skipped"] += 1
                continue

            view = _field_view(intake)
            capture_id = str(view.get("capture_id") or "")
            site_id = str(view.get("site_id") or "")
            if not site_id:
                target_type = str(view.get("target_type") or "")
                target_id = str(view.get("target_id") or "")
                if target_type == "location":
                    site_id = target_id
                else:
                    logger.info(
                        "issue_routing: skip prospect-target capture=%s target=%s/%s",
                        capture_id,
                        target_type,
                        target_id,
                    )
                    counts["skipped"] += 1
                    continue

            note = str(view.get("note") or "").strip()
            title = _derive_title(note, view)
            reported_by = str(view.get("person_name") or "").strip() or "Unknown"
            observed_at = str(view.get("captured_at") or view.get("exported_at") or "")
            related_media = _related_media(view.get("photos"))

            payload = {
                "site_id": site_id,
                "title": title,
                "summary": note or title,
                "status": "open",
                "priority": "normal",
                "category": "other",
                "source": "field_capture",
                "reported_by": reported_by,
                "observed_at": observed_at,
                "notes": note,
                "client_notified": False,
                "resolution_trigger": "operator_resolves",
                "related_capture_ids": [capture_id],
                "related_media": related_media,
                "source_artifacts": [f"field_capture/intake/{path.name}"],
            }

            suffix = str(uuid.uuid4())
            job = {
                "job_id": f"log-site-issue-{capture_id}-{suffix}",
                "job_type": "log_site_issue",
                "payload": payload,
            }
            queue_path = queue_dir / f"log-site-issue-{capture_id}-{suffix}.json"
            temp_path = queue_path.with_name(f".{queue_path.name}.tmp")
            queue_dir.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temp_path.replace(queue_path)

            routed_marker.write_text(
                json.dumps(
                    {
                        "routed_at": _utc_now_iso(),
                        "queue_path": str(queue_path),
                        "job_id": job["job_id"],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            routed_this_cycle += 1
            counts["routed"] += 1
        except Exception:  # noqa: BLE001
            counts["failed"] += 1
            logger.exception("issue_routing: failed path=%s", path)
    return counts
