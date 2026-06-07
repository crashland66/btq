from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from btq_vault.projector import DDOC, query_view
from io_atomic import atomic_write_text
from queue_processor.canonical_rmw import resolve_site_vault_path
from queue_processor.handlers import _shared


logger = logging.getLogger(__name__)

EXPORTABLE_TYPES: tuple[str, ...] = (
    "employee",
    "visit",
    "site_issue",
    "supply_need",
    "equipment_request",
    "personnel_event",
)


@dataclass(frozen=True)
class ExportError:
    doc_id: str
    message: str


@dataclass
class ExportReport:
    seen: int = 0
    rendered: int = 0
    written: int = 0
    would_write: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: list[ExportError] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen,
            "rendered": self.rendered,
            "written": self.written,
            "would_write": self.would_write,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "errors": [error.__dict__ for error in self.errors],
        }


def _write_legacy_markdown(path: Path, text: str, *, create_parent: bool = False) -> None:
    try:
        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)
    except Exception as exc:
        logger.warning("legacy markdown export failed: %s", exc)
        raise


def render_entity_markdown(doc: dict[str, Any]) -> tuple[Path, str] | None:
    doc_type = str(doc.get("type") or "").strip()
    if doc_type == "employee":
        return _employee_projection(doc)
    if doc_type == "visit":
        return _visit_projection(doc)
    if doc_type == "site_issue":
        return _site_issue_projection(doc)
    if doc_type == "supply_need":
        return _supply_need_projection(doc)
    if doc_type == "equipment_request":
        return _equipment_request_projection(doc)
    if doc_type == "personnel_event":
        return _personnel_event_projection(doc)
    return None


def export_entity(doc: dict[str, Any], vault_root: Path) -> Path | None:
    result = _export_entity_result(doc, vault_root, dry_run=False)
    return result.path


def export_all(
    store: Any,
    vault_root: Path,
    *,
    types: Iterable[str] | None = None,
    site: str | None = None,
    dry_run: bool = False,
) -> ExportReport:
    report = ExportReport()
    type_names = tuple(types or EXPORTABLE_TYPES)
    for doc in _iter_docs_by_type(store, type_names):
        report.seen += 1
        if site is not None and str(doc.get("site_id") or doc.get("related_site") or "") != str(site):
            report.skipped += 1
            continue
        try:
            result = _export_entity_result(doc, vault_root, dry_run=dry_run)
        except Exception as exc:
            report.errors.append(ExportError(doc_id=str(doc.get("_id") or ""), message=str(exc)))
            continue
        if result.path is None:
            report.skipped += 1
        else:
            report.rendered += 1
            if result.changed:
                if dry_run:
                    report.would_write += 1
                else:
                    report.written += 1
            else:
                report.unchanged += 1
    return report


@dataclass(frozen=True)
class _ExportEntityResult:
    path: Path | None
    changed: bool = False


def _export_entity_result(doc: dict[str, Any], vault_root: Path, *, dry_run: bool) -> _ExportEntityResult:
    vault_root = vault_root.expanduser().resolve()
    projection_doc = _with_resolved_target(doc, vault_root)
    rendered = render_entity_markdown(projection_doc)
    if rendered is None:
        return _ExportEntityResult(None)
    path, text = rendered
    target = path if path.is_absolute() else vault_root / path
    target = target.expanduser().resolve()
    _shared.ensure_within_root(target, vault_root, "Markdown export target")
    existing = target.read_text(encoding="utf-8") if target.exists() else None
    if existing is not None:
        projection_doc = dict(projection_doc)
        projection_doc["_existing_text"] = existing
        path, text = render_entity_markdown(projection_doc) or (path, text)
        target = path if path.is_absolute() else vault_root / path
        target = target.expanduser().resolve()
        _shared.ensure_within_root(target, vault_root, "Markdown export target")
    changed = existing != text
    if changed and not dry_run:
        _write_legacy_markdown(target, text, create_parent=True)
    return _ExportEntityResult(target, changed)


def _iter_docs_by_type(store: Any, types: tuple[str, ...]) -> Iterable[dict[str, Any]]:
    if hasattr(store, "iter_by_type"):
        for type_name in types:
            yield from store.iter_by_type(type_name)
        return
    if hasattr(store, "docs_for_type"):
        for type_name in types:
            yield from store.docs_for_type(type_name)
        return
    if hasattr(store, "docs"):
        for doc in store.docs:
            if isinstance(doc, dict) and str(doc.get("type") or "") in types:
                yield doc
        return
    for type_name in types:
        rows = query_view(
            store.base_url,
            store.auth_headers,
            store.database,
            DDOC,
            "by_type",
            startkey=[type_name, None],
            endkey=[type_name, {}],
            include_docs=True,
            timeout=getattr(store, "timeout", 10.0),
        )
        for row in rows:
            doc = row.get("doc")
            if isinstance(doc, dict):
                yield doc


def _with_resolved_target(doc: dict[str, Any], vault_root: Path) -> dict[str, Any]:
    if doc.get("_target_path") or doc.get("vault_path"):
        return doc
    doc_type = str(doc.get("type") or "")
    site_id = str(doc.get("site_id") or doc.get("related_site") or "").strip()
    if doc_type not in {"visit", "site_issue", "supply_need", "equipment_request"} or not site_id:
        if doc_type == "personnel_event":
            return _with_resolved_personnel_event_target(doc, vault_root)
        return doc
    enriched = dict(doc)
    try:
        site_path = Path(resolve_site_vault_path(_shared._vault_store(), site_id))
    except Exception as exc:
        logger.warning(
            "markdown export target resolution fallback doc_id=%s type=%s site_id=%s: %s",
            doc.get("_id"),
            doc_type,
            site_id,
            exc,
        )
        return enriched
    if not enriched.get("account"):
        try:
            enriched["account"] = site_path.parents[2].name
        except IndexError:
            pass
    if not enriched.get("site_name") and not enriched.get("site"):
        enriched["site_name"] = site_path.parent.name.split(" - ", 1)[-1]
    if doc_type == "visit":
        enriched["_target_path"] = site_path.parent / "Visits" / f"{str(doc.get('date') or 'visit').strip()}.md"
    elif doc_type == "site_issue":
        issue_id = str(doc.get("issue_id") or _doc_entity_id(doc, "site_issue")).strip()
        title = str(doc.get("title") or "site issue")
        enriched["_target_path"] = site_path.parent / "Issues" / f"{issue_id}__{_shared.slugify_issue_component(title)}.md"
    elif doc_type == "supply_need":
        supply_id = str(doc.get("supply_id") or _doc_entity_id(doc, "supply_need")).strip()
        item_name = str(doc.get("item_name") or "supply need")
        enriched["_target_path"] = site_path.parent / "Supplies" / f"{supply_id}__{_shared.slugify_issue_component(item_name)}.md"
    elif doc_type == "equipment_request":
        equipment_id = str(doc.get("equipment_id") or _doc_entity_id(doc, "equipment_request")).strip()
        equipment_name = str(doc.get("equipment_name") or "equipment request")
        enriched["_target_path"] = site_path.parent / "Equipment" / f"{equipment_id}__{_shared.slugify_issue_component(equipment_name)}.md"
    return enriched


def _with_resolved_personnel_event_target(doc: dict[str, Any], vault_root: Path) -> dict[str, Any]:
    event_id = str(doc.get("event_id") or _doc_entity_id(doc, "personnel_event")).strip()
    employee = str(doc.get("employee") or "Unknown Person")
    event_dir = vault_root / "People" / _shared.person_file_name(employee).removesuffix(".md") / "Events"
    matches = sorted(event_dir.glob(f"{event_id}__*.md")) if event_dir.exists() else []
    if not matches:
        return doc
    enriched = dict(doc)
    enriched["_target_path"] = matches[0]
    return enriched


def _projection_path(doc: dict[str, Any], fallback: Path) -> Path:
    target_path = doc.get("_target_path")
    if target_path:
        return Path(target_path)
    vault_path = str(doc.get("vault_path") or "").strip()
    return Path(vault_path) if vault_path else fallback


def _doc_entity_id(doc: dict[str, Any], prefix: str) -> str:
    doc_id = str(doc.get("_id") or "").strip()
    prefix_text = f"{prefix}_"
    if doc_id.startswith(prefix_text):
        return doc_id[len(prefix_text) :]
    return doc_id or prefix


def _first_job_id(doc: dict[str, Any]) -> str:
    values = doc.get("btq_job_ids")
    if isinstance(values, list):
        for value in values:
            text = str(value).strip()
            if text:
                return text
    return str(doc.get("job_id") or doc.get("_id") or "markdown-export").strip()


def _last_job_id(doc: dict[str, Any]) -> str:
    values = _job_ids(doc)
    return values[-1] if values else _first_job_id(doc)


def _job_ids(doc: dict[str, Any]) -> list[str]:
    values = doc.get("btq_job_ids")
    if isinstance(values, list):
        result = [str(value).strip() for value in values if str(value).strip()]
        if result:
            return result
    return [_first_job_id(doc)]


def _payload_without_doc_fields(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in doc.items()
        if key not in {"_id", "_rev", "type", "operator", "vault_path", "_target_path", "_existing_text", "btq_job_ids"}
    }


def _employee_projection(doc: dict[str, Any]) -> tuple[Path, str]:
    payload = _payload_without_doc_fields(doc)
    if "role" not in payload:
        payload["role"] = ""
    person_id = str(doc.get("person_id") or _doc_entity_id(doc, "employee"))
    created = str(doc.get("created_at") or doc.get("created") or "")
    text = render_person_markdown(payload, created[:10] or created, person_id, _first_job_id(doc))
    return _projection_path(doc, Path("People") / _shared.person_file_name(str(payload.get("name") or person_id))), text


def _visit_projection(doc: dict[str, Any]) -> tuple[Path, str]:
    text = render_visit_markdown(doc)
    site = str(doc.get("site") or doc.get("site_name") or doc.get("site_id") or "site")
    site_id = str(doc.get("site_id") or "").strip()
    account = str(doc.get("account") or "Unknown Account").strip() or "Unknown Account"
    date = str(doc.get("date") or "visit").strip()
    fallback = Path("Accounts") / account / "Locations" / f"{site_id} - {site}" / "Visits" / f"{date}.md"
    return _projection_path(doc, fallback), text


def _site_issue_projection(doc: dict[str, Any]) -> tuple[Path, str]:
    payload = _payload_without_doc_fields(doc)
    issue_id = str(doc.get("issue_id") or _doc_entity_id(doc, "site_issue"))
    text = render_site_issue_markdown(
        payload=payload,
        site_id=str(doc.get("site_id") or ""),
        site_name=str(doc.get("site_name") or doc.get("site") or ""),
        account=str(doc.get("account") or ""),
        issue_id=issue_id,
        job_id=_last_job_id(doc),
        created_at=str(doc.get("created_at") or ""),
        existing_text=str(doc.get("_existing_text") or ""),
    )
    title = str(doc.get("title") or "site issue")
    fallback = _site_child_fallback(doc, "Issues", f"{issue_id}__{_shared.slugify_issue_component(title)}.md")
    return _projection_path(doc, fallback), text


def _supply_need_projection(doc: dict[str, Any]) -> tuple[Path, str]:
    payload = _payload_without_doc_fields(doc)
    supply_id = str(doc.get("supply_id") or _doc_entity_id(doc, "supply_need"))
    text = render_supply_need_markdown(
        payload=payload,
        site_id=str(doc.get("site_id") or ""),
        site_name=str(doc.get("site_name") or ""),
        account=str(doc.get("account") or ""),
        supply_id=supply_id,
        job_id=_last_job_id(doc),
        created_at=str(doc.get("created_at") or ""),
        existing_text=str(doc.get("_existing_text") or ""),
    )
    item_name = str(doc.get("item_name") or "supply need")
    fallback = _site_child_fallback(doc, "Supplies", f"{supply_id}__{_shared.slugify_issue_component(item_name)}.md")
    return _projection_path(doc, fallback), text


def _equipment_request_projection(doc: dict[str, Any]) -> tuple[Path, str]:
    payload = _payload_without_doc_fields(doc)
    equipment_id = str(doc.get("equipment_id") or _doc_entity_id(doc, "equipment_request"))
    text = render_equipment_request_markdown(
        payload=payload,
        site_id=str(doc.get("site_id") or ""),
        site_name=str(doc.get("site_name") or ""),
        account=str(doc.get("account") or ""),
        equipment_id=equipment_id,
        job_id=_last_job_id(doc),
        created_at=str(doc.get("created_at") or ""),
        existing_text=str(doc.get("_existing_text") or ""),
    )
    equipment_name = str(doc.get("equipment_name") or "equipment request")
    fallback = _site_child_fallback(doc, "Equipment", f"{equipment_id}__{_shared.slugify_issue_component(equipment_name)}.md")
    return _projection_path(doc, fallback), text


def _personnel_event_projection(doc: dict[str, Any]) -> tuple[Path, str]:
    payload = _payload_without_doc_fields(doc)
    event_id = str(doc.get("event_id") or _doc_entity_id(doc, "personnel_event"))
    text = render_personnel_event_markdown(
        payload=payload,
        event_id=event_id,
        job_id=_last_job_id(doc),
        created_at=str(doc.get("created_at") or ""),
        existing_text=str(doc.get("_existing_text") or ""),
    )
    employee = str(doc.get("employee") or "Unknown Person")
    summary = str(doc.get("summary") or doc.get("event_type") or "personnel event")
    filename = f"{event_id}__{_shared.slugify_issue_component(summary)[:80].rstrip('-') or 'personnel-event'}.md"
    fallback = Path("People") / _shared.person_file_name(employee).removesuffix(".md") / "Events" / filename
    return _projection_path(doc, fallback), text


def _site_child_fallback(doc: dict[str, Any], folder: str, filename: str) -> Path:
    account = str(doc.get("account") or "Unknown Account").strip() or "Unknown Account"
    site_id = str(doc.get("site_id") or "").strip()
    site_name = str(doc.get("site_name") or doc.get("site") or site_id or "site").strip()
    return Path("Accounts") / account / "Locations" / f"{site_id} - {site_name}" / folder / filename


def render_visit_markdown(doc: dict[str, Any]) -> str:
    visit_type = str(doc.get("visit_type") or "").strip()
    visited_by = str(doc.get("visited_by") or "").strip()
    visit_key = str(doc.get("visit_key") or "").strip()
    block = (
        "---\n"
        "type: visit\n"
        f"timestamp: {str(doc.get('timestamp') or '').strip()}\n"
        f"site: {str(doc.get('site') or doc.get('site_name') or doc.get('site_id') or '').strip()}\n"
        f"date: {str(doc.get('date') or '').strip()}\n"
        f'visit_key: "{visit_key}"\n'
        f"source: {str(doc.get('source') or '').strip()}\n"
        f"confidence: {str(doc.get('confidence') or '').strip()}\n"
        + (f"visit_type: {visit_type}\n" if visit_type else "")
        + (f"visited_by: {visited_by}\n" if visited_by else "")
        + f"evidence: {str(doc.get('evidence') or '').strip()}\n"
        "---\n"
    )
    for job_id in _job_ids(doc):
        block = _shared.upsert_job_id_frontmatter(block, job_id)
    return block


def render_person_frontmatter(payload: dict[str, Any], created_date: str, person_id: str, job_id: str) -> str:
    first, last = _person_name_parts(payload)
    lines = [
        "---",
        "type: person",
        f"person_id: {person_id}",
        "",
        f"name: {_shared.yaml_scalar(payload['name'])}",
    ]
    if first is not None:
        lines.append(f"first: {_shared.yaml_scalar(first)}")
    if last is not None:
        lines.append(f"last: {_shared.yaml_scalar(last)}")
    employee_id = payload.get("employee_id")
    if employee_id is not None:
        lines.append(f"employee_id: {_shared.yaml_scalar(employee_id)}")
    lines.append("")
    lines.append(f"role: {_shared.yaml_scalar(payload['role'])}")
    if payload.get("employment_type") is not None:
        lines.append(f"employment_type: {_shared.yaml_scalar(payload['employment_type'])}")
    if payload.get("status") is not None:
        lines.append(f"status: {_shared.yaml_scalar(payload['status'])}")

    primary_job, additional_jobs = _person_jobs_from_payload(payload)
    if primary_job is not None:
        lines.append(f"job: {_shared.yaml_scalar(primary_job)}")
    if additional_jobs:
        lines.extend(["additional_jobs:", *[f"  - {_shared.yaml_scalar(job)}" for job in additional_jobs]])

    assignments = payload.get("assignments")
    if isinstance(assignments, list) and assignments:
        lines.extend(["", "assignments:"])
        for assignment in assignments:
            lines.append(f"  - job: {_shared.yaml_scalar(assignment.get('job'))}")
            for key in ("account", "location", "shift"):
                if assignment.get(key) is not None:
                    lines.append(f"    {key}: {_shared.yaml_scalar(assignment[key])}")

    lines.extend(["", f"created: {created_date}", "source: btq", "", "btq_job_ids:", f"  - {job_id}", "---"])
    return "\n".join(lines)


def render_person_markdown(payload: dict[str, Any], created_date: str, person_id: str, job_id: str) -> str:
    name = " ".join(str(payload["name"]).strip().split())
    return (
        f"{render_person_frontmatter(payload, created_date, person_id, job_id)}\n\n"
        f"# {name}\n\n"
        "## Notes\n\n"
        "## Schedule\n\n"
        "## Training\n\n"
        "## Incidents\n"
    )


def _person_name_parts(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    explicit_first = payload.get("first")
    explicit_last = payload.get("last")
    first = str(explicit_first).strip() if explicit_first is not None else ""
    last = str(explicit_last).strip() if explicit_last is not None else ""
    if first and last:
        return first, last

    cleaned = " ".join(str(payload.get("name", "")).strip().split())
    if "," in cleaned:
        last_part, first_part = [part.strip() for part in cleaned.split(",", 1)]
        return (first or first_part or None, last or last_part or None)
    parts = cleaned.split()
    if len(parts) >= 2:
        return (first or " ".join(parts[:-1]), last or parts[-1])
    return (first or None, last or None)


def _person_jobs_from_payload(payload: dict[str, Any]) -> tuple[str | None, list[str]]:
    assignment_jobs: list[str] = []
    assignments = payload.get("assignments")
    if isinstance(assignments, list):
        for assignment in assignments:
            if isinstance(assignment, dict) and assignment.get("job") is not None:
                job = str(assignment["job"]).strip()
                if job:
                    assignment_jobs.append(job)
    if payload.get("job") is not None:
        primary_job = str(payload["job"]).strip()
    else:
        primary_job = assignment_jobs[0] if assignment_jobs else None
    additional_jobs = _shared.parse_string_list_payload(payload.get("additional_jobs")) + assignment_jobs[1:]
    return (primary_job or None, [job for job in additional_jobs if job != primary_job])


def render_site_issue_markdown(
    *,
    payload: dict[str, Any],
    site_id: str,
    site_name: str,
    account: str,
    issue_id: str,
    job_id: str,
    created_at: str,
    existing_text: str = "",
) -> str:
    related_capture_ids = _shared.parse_string_list_payload(payload.get("related_capture_ids"))
    related_candidate_ids = _shared.parse_string_list_payload(payload.get("related_candidate_ids"))
    related_media = _shared.parse_string_list_payload(payload.get("related_media"))
    source_artifacts = _shared.parse_string_list_payload(payload.get("source_artifacts"))
    notes = str(payload.get("notes") or "").strip()
    notified = _shared.client_notified(payload)
    resolved_at = str(payload.get("resolved_at") or "").strip()
    resolution_summary = str(payload.get("resolution_summary") or "").strip()
    existing_job_ids: list[str] = []
    if existing_text:
        existing_frontmatter, _existing_body, existing_has_frontmatter = _shared.parse_frontmatter_text(existing_text)
        existing_ids = existing_frontmatter.get("btq_job_ids") if existing_has_frontmatter else None
        if isinstance(existing_ids, list):
            existing_job_ids = [str(existing_id).strip() for existing_id in existing_ids if str(existing_id).strip()]
    job_ids = [*existing_job_ids]
    if job_id not in job_ids:
        job_ids.append(job_id)
    fields = [
        "---",
        "type: site_issue",
        f"issue_id: {issue_id}",
        f'site_id: "{site_id}"',
        f"site: {_shared.yaml_scalar(site_name)}",
        f"account: {_shared.yaml_scalar(account)}",
        f"title: {_shared.yaml_scalar(payload['title'])}",
        f"status: {_issue_status(payload)}",
        f"priority: {_issue_priority(payload)}",
        f"category: {_issue_category(payload)}",
        f"source: {_shared.yaml_scalar(payload.get('source') or 'queue')}",
        f"notes: {_shared.yaml_scalar(notes)}",
        f"reported_by: {_shared.yaml_scalar(payload['reported_by'])}",
        f"observed_at: {_shared.yaml_optional(payload.get('observed_at'))}",
        f"created_at: {created_at}",
        f"client_notified: {_shared.bool_yaml(notified)}",
        f"client_informed: {_shared.bool_yaml(notified)}",
        f"client_informed_at: {_client_notification_field(payload, 'client_notified_at', 'client_informed_at')}",
        f"client_informed_by: {_client_notification_field(payload, 'client_notified_by', 'client_informed_by')}",
        f"client_informed_method: {_client_notification_field(payload, 'client_notified_method', 'client_informed_method')}",
        f"client_informed_note: {_shared.yaml_scalar(_client_notification_field(payload, 'client_notified_note', 'client_informed_note'))}",
        f"client_notified_at: {_client_notification_field(payload, 'client_notified_at', 'client_informed_at')}",
        f"client_notified_by: {_client_notification_field(payload, 'client_notified_by', 'client_informed_by')}",
        f"client_notified_method: {_client_notification_field(payload, 'client_notified_method', 'client_informed_method')}",
        f"client_notified_note: {_shared.yaml_scalar(_client_notification_field(payload, 'client_notified_note', 'client_informed_note'))}",
        f"resolved_at: {resolved_at}",
        f"resolution_trigger: {_shared.yaml_scalar(payload['resolution_trigger'])}",
        f"resolution_summary: {_shared.yaml_scalar(resolution_summary)}",
        *_shared.yaml_list_lines("related_capture_ids", related_capture_ids),
        *_shared.yaml_list_lines("related_candidate_ids", related_candidate_ids),
        *_shared.yaml_list_lines("related_media", related_media),
        *_shared.yaml_list_lines("source_artifacts", source_artifacts),
        "btq_job_ids:",
        *[f"  - {existing_job_id}" for existing_job_id in job_ids],
        "---",
    ]
    observations = _shared.parse_string_list_payload(payload.get("observations"))
    observations_text = "\n".join(f"- {item}" for item in observations) or str(payload.get("summary") or "").strip()
    evidence_items = related_capture_ids + related_candidate_ids + related_media + source_artifacts
    evidence_text = "\n".join(f"- {item}" for item in evidence_items) or "- No structured evidence references supplied."
    client_note = _client_notification_field(payload, "client_notified_note", "client_informed_note")
    client_text = (
        f"- Client notified: {_shared.bool_yaml(notified)}\n"
        f"- Method: {_client_notification_field(payload, 'client_notified_method', 'client_informed_method') or 'not recorded'}\n"
        f"- By: {_client_notification_field(payload, 'client_notified_by', 'client_informed_by') or 'not recorded'}\n"
        f"- At: {_client_notification_field(payload, 'client_notified_at', 'client_informed_at') or 'not recorded'}\n"
        f"- Note: {client_note or 'not recorded'}"
    )
    followup_text = (
        f"- Status: {_issue_status(payload)}\n"
        f"- Resolution trigger: {payload['resolution_trigger']}\n"
        f"- Resolved at: {resolved_at or 'not resolved'}\n"
        f"- Resolution summary: {resolution_summary or 'not resolved'}"
    )
    existing_history = ""
    if existing_text:
        _frontmatter, existing_body, _has_frontmatter = _shared.parse_frontmatter_text(existing_text)
        existing_history = _shared.extract_section(existing_body, "History")
    history_lines = [line for line in existing_history.splitlines() if line.strip()]
    history_entry = f"- {created_at}: queue job {job_id} logged/updated this issue."
    if history_entry not in history_lines:
        history_lines.append(history_entry)
    body = (
        f"# {payload['title']}\n\n"
        "## Summary\n"
        f"{str(payload.get('summary') or '').strip()}\n\n"
        "## Observations\n"
        f"{observations_text}\n\n"
        "## Notes\n"
        f"{notes or 'No notes recorded.'}\n\n"
        "## Evidence\n"
        f"{evidence_text}\n\n"
        "## Client Communication\n"
        f"{client_text}\n\n"
        "## Follow-up / Resolution Notes\n"
        f"{followup_text}\n\n"
        "## History\n"
        + "\n".join(history_lines)
        + "\n"
    )
    return "\n".join(fields) + "\n" + body


def render_supply_need_markdown(
    *,
    payload: dict[str, Any],
    site_id: str,
    site_name: str,
    account: str,
    supply_id: str,
    job_id: str,
    created_at: str,
    existing_text: str = "",
) -> str:
    related_capture_ids = _shared.parse_string_list_payload(payload.get("related_capture_ids"))
    related_candidate_ids = _shared.parse_string_list_payload(payload.get("related_candidate_ids"))
    related_media = _shared.parse_string_list_payload(payload.get("related_media"))
    source_artifacts = _shared.parse_string_list_payload(payload.get("source_artifacts"))
    existing_job_ids: list[str] = []
    existing_created_at = ""
    existing_supply_id = ""
    if existing_text:
        existing_frontmatter, _existing_body, existing_has_frontmatter = _shared.parse_frontmatter_text(existing_text)
        if existing_has_frontmatter:
            existing_ids = existing_frontmatter.get("btq_job_ids")
            if isinstance(existing_ids, list):
                existing_job_ids = [str(existing_id).strip() for existing_id in existing_ids if str(existing_id).strip()]
            existing_created_at = str(existing_frontmatter.get("created_at") or "").strip()
            existing_supply_id = str(existing_frontmatter.get("supply_id") or "").strip()
    final_supply_id = existing_supply_id or supply_id
    final_created_at = existing_created_at or created_at
    job_ids = [*existing_job_ids]
    if job_id not in job_ids:
        job_ids.append(job_id)

    fields = [
        "---",
        "type: supply_need",
        f"supply_id: {final_supply_id}",
        f'site_id: "{site_id}"',
        f"site_name: {_shared.yaml_scalar(site_name)}",
        f"account: {_shared.yaml_scalar(account)}",
        f"item_name: {_shared.yaml_scalar(payload['item_name'])}",
        f"quantity_needed: {_shared.yaml_scalar(payload.get('quantity_needed') or '')}",
        f"urgency: {_supply_need_urgency(payload)}",
        f"requested_by: {_shared.yaml_scalar(payload['requested_by'])}",
        f"observed_at: {_shared.yaml_optional(payload.get('observed_at'))}",
        f"source: {_shared.yaml_scalar(payload.get('source') or 'queue')}",
        f"status: {_supply_need_status(payload)}",
        f"created_at: {final_created_at}",
    ]
    notes = str(payload.get("notes") or "").strip()
    if notes:
        fields.append(f"notes: {_shared.yaml_scalar(notes)}")
    fields.extend(_shared.yaml_list_lines("related_capture_ids", related_capture_ids))
    fields.extend(_shared.yaml_list_lines("related_candidate_ids", related_candidate_ids))
    for field_name in (
        "ordered_at",
        "ordered_by",
        "ordered_note",
        "delivered_at",
        "delivered_by",
        "delivered_note",
        "stocked_at",
        "stocked_by",
        "stocked_note",
    ):
        value = str(payload.get(field_name) or "").strip()
        if value:
            fields.append(f"{field_name}: {_shared.yaml_scalar(value)}")
    fields.extend(_shared.yaml_list_lines("related_media", related_media))
    fields.extend(_shared.yaml_list_lines("source_artifacts", source_artifacts))
    fields.extend(["btq_job_ids:", *[f"  - {existing_job_id}" for existing_job_id in job_ids], "---"])

    related_items = related_capture_ids + related_candidate_ids
    related_text = "\n".join(f"- {item}" for item in related_items) or "- No related captures or candidates supplied."
    evidence_items = related_media + source_artifacts
    evidence_text = "\n".join(f"- {item}" for item in evidence_items) if evidence_items else ""
    existing_history = ""
    if existing_text:
        _frontmatter, existing_body, _has_frontmatter = _shared.parse_frontmatter_text(existing_text)
        existing_history = _shared.extract_section(existing_body, "History")
    history_lines = [line for line in existing_history.splitlines() if line.strip()]
    history_entry = f"- {created_at}: queue job {job_id} logged/updated this supply need."
    if history_entry not in history_lines:
        history_lines.append(history_entry)
    body = (
        f"# Supply need: {payload['item_name']}\n\n"
        "## Notes\n"
        f"{notes or 'No notes recorded.'}\n\n"
        "## Related captures/candidates\n"
        f"{related_text}\n\n"
    )
    if evidence_text:
        body += f"## Source artifacts/media\n{evidence_text}\n\n"
    body += "## History\n" + "\n".join(history_lines) + "\n"
    return "\n".join(fields) + "\n" + body


def render_equipment_request_markdown(
    *,
    payload: dict[str, Any],
    site_id: str,
    site_name: str,
    account: str,
    equipment_id: str,
    job_id: str,
    created_at: str,
    existing_text: str = "",
) -> str:
    related_capture_ids = _shared.parse_string_list_payload(payload.get("related_capture_ids"))
    related_candidate_ids = _shared.parse_string_list_payload(payload.get("related_candidate_ids"))
    related_media = _shared.parse_string_list_payload(payload.get("related_media"))
    source_artifacts = _shared.parse_string_list_payload(payload.get("source_artifacts"))
    existing_job_ids: list[str] = []
    existing_created_at = ""
    existing_equipment_id = ""
    if existing_text:
        existing_frontmatter, _existing_body, existing_has_frontmatter = _shared.parse_frontmatter_text(existing_text)
        if existing_has_frontmatter:
            existing_ids = existing_frontmatter.get("btq_job_ids")
            if isinstance(existing_ids, list):
                existing_job_ids = [str(existing_id).strip() for existing_id in existing_ids if str(existing_id).strip()]
            existing_created_at = str(existing_frontmatter.get("created_at") or "").strip()
            existing_equipment_id = str(existing_frontmatter.get("equipment_id") or "").strip()
    final_equipment_id = existing_equipment_id or equipment_id
    final_created_at = existing_created_at or created_at
    job_ids = [*existing_job_ids]
    if job_id not in job_ids:
        job_ids.append(job_id)

    reason = str(payload.get("reason") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    fields = [
        "---",
        "type: equipment_request",
        f"equipment_id: {final_equipment_id}",
        f'site_id: "{site_id}"',
        f"site_name: {_shared.yaml_scalar(site_name)}",
        f"account: {_shared.yaml_scalar(account)}",
        f"equipment_name: {_shared.yaml_scalar(payload['equipment_name'])}",
        f"reason: {_shared.yaml_scalar(reason)}",
        f"priority: {_equipment_request_priority(payload)}",
        f"requested_by: {_shared.yaml_scalar(payload['requested_by'])}",
        f"observed_at: {_shared.yaml_optional(payload.get('observed_at'))}",
        f"source: {_shared.yaml_scalar(payload.get('source') or 'queue')}",
        f"status: {_equipment_request_status(payload)}",
        f"created_at: {final_created_at}",
    ]
    if notes:
        fields.append(f"notes: {_shared.yaml_scalar(notes)}")
    fields.extend(_shared.yaml_list_lines("related_capture_ids", related_capture_ids))
    fields.extend(_shared.yaml_list_lines("related_candidate_ids", related_candidate_ids))
    for field_name in (
        "approved_at",
        "approved_by",
        "approval_note",
        "denied_at",
        "denied_by",
        "denial_note",
        "ordered_at",
        "ordered_by",
        "ordered_note",
        "provided_at",
        "provided_by",
        "provided_note",
    ):
        value = str(payload.get(field_name) or "").strip()
        if value:
            fields.append(f"{field_name}: {_shared.yaml_scalar(value)}")
    fields.extend(_shared.yaml_list_lines("related_media", related_media))
    fields.extend(_shared.yaml_list_lines("source_artifacts", source_artifacts))
    fields.extend(["btq_job_ids:", *[f"  - {existing_job_id}" for existing_job_id in job_ids], "---"])

    related_items = related_capture_ids + related_candidate_ids
    related_text = "\n".join(f"- {item}" for item in related_items) or "- No related captures or candidates supplied."
    evidence_items = related_media + source_artifacts
    evidence_text = "\n".join(f"- {item}" for item in evidence_items) if evidence_items else ""
    existing_history = ""
    if existing_text:
        _frontmatter, existing_body, _has_frontmatter = _shared.parse_frontmatter_text(existing_text)
        existing_history = _shared.extract_section(existing_body, "History")
    history_lines = [line for line in existing_history.splitlines() if line.strip()]
    history_entry = f"- {created_at}: queue job {job_id} logged/updated this equipment request."
    if history_entry not in history_lines:
        history_lines.append(history_entry)
    body = (
        f"# Equipment request: {payload['equipment_name']}\n\n"
        "## Notes\n"
        f"{notes or 'No notes recorded.'}\n\n"
        "## Reason\n"
        f"{reason or 'No reason recorded.'}\n\n"
        "## Related captures/candidates\n"
        f"{related_text}\n\n"
    )
    if evidence_text:
        body += f"## Source artifacts/media\n{evidence_text}\n\n"
    body += "## History\n" + "\n".join(history_lines) + "\n"
    return "\n".join(fields) + "\n" + body


def render_personnel_event_markdown(
    *,
    payload: dict[str, Any],
    event_id: str,
    job_id: str,
    created_at: str,
    existing_text: str = "",
) -> str:
    related_capture_ids = _shared.parse_string_list_payload(payload.get("related_capture_ids"))
    related_candidate_ids = _shared.parse_string_list_payload(payload.get("related_candidate_ids"))
    related_media = _shared.parse_string_list_payload(payload.get("related_media"))
    source_artifacts = _shared.parse_string_list_payload(payload.get("source_artifacts"))
    notified = _shared.client_notified(payload)
    severity = str(payload.get("severity") or "").strip()
    status = str(payload.get("status") or "open").strip() or "open"
    resolution_trigger = str(payload.get("resolution_trigger") or "").strip()
    resolved_at = str(payload.get("resolved_at") or "").strip()
    resolution_summary = str(payload.get("resolution_summary") or "").strip()

    existing_job_ids: list[str] = []
    existing_created_at = ""
    existing_event_id = ""
    if existing_text:
        existing_frontmatter, _existing_body, existing_has_frontmatter = _shared.parse_frontmatter_text(existing_text)
        if existing_has_frontmatter:
            existing_ids = existing_frontmatter.get("btq_job_ids")
            if isinstance(existing_ids, list):
                existing_job_ids = [str(existing_id).strip() for existing_id in existing_ids if str(existing_id).strip()]
            existing_created_at = str(existing_frontmatter.get("created_at") or "").strip()
            existing_event_id = str(existing_frontmatter.get("event_id") or "").strip()
    final_event_id = existing_event_id or event_id
    final_created_at = existing_created_at or created_at
    job_ids = [*existing_job_ids]
    if job_id not in job_ids:
        job_ids.append(job_id)

    fields = [
        "---",
        "type: personnel_event",
        f"event_id: {final_event_id}",
        f"employee: {_shared.yaml_scalar(payload['employee'])}",
        f"event_type: {payload['event_type']}",
        f"severity: {_shared.yaml_scalar(severity)}",
        f"status: {status}",
        f"reported_by: {_shared.yaml_scalar(payload['reported_by'])}",
        f"occurred_at: {_shared.yaml_optional(payload.get('occurred_at'))}",
        f"created_at: {final_created_at}",
        f"source: {_shared.yaml_scalar(payload.get('source') or 'queue')}",
        f"related_site: {_shared.yaml_scalar(str(payload.get('related_site') or ''))}",
        f"client_notified: {_shared.bool_yaml(notified)}",
        f"client_notified_at: {_shared.yaml_scalar(str(payload.get('client_notified_at') or ''))}",
        f"client_notified_by: {_shared.yaml_scalar(str(payload.get('client_notified_by') or ''))}",
        f"client_notified_method: {_shared.yaml_scalar(str(payload.get('client_notified_method') or ''))}",
        f"client_notified_note: {_shared.yaml_scalar(str(payload.get('client_notified_note') or ''))}",
        f"resolution_trigger: {_shared.yaml_scalar(resolution_trigger)}",
        f"resolved_at: {resolved_at}",
        f"resolution_summary: {_shared.yaml_scalar(resolution_summary)}",
        *_shared.yaml_list_lines("related_capture_ids", related_capture_ids),
        *_shared.yaml_list_lines("related_candidate_ids", related_candidate_ids),
        *_shared.yaml_list_lines("related_media", related_media),
        *_shared.yaml_list_lines("source_artifacts", source_artifacts),
        "btq_job_ids:",
        *[f"  - {existing_job_id}" for existing_job_id in job_ids],
        "---",
    ]
    notes = str(payload.get("notes") or "").strip()
    evidence_items = related_capture_ids + related_candidate_ids + related_media + source_artifacts
    evidence_text = "\n".join(f"- {item}" for item in evidence_items) or "- No structured evidence references supplied."
    client_text = (
        f"- Client notified: {_shared.bool_yaml(notified)}\n"
        f"- Method: {str(payload.get('client_notified_method') or '').strip() or 'not recorded'}\n"
        f"- By: {str(payload.get('client_notified_by') or '').strip() or 'not recorded'}\n"
        f"- At: {str(payload.get('client_notified_at') or '').strip() or 'not recorded'}\n"
        f"- Note: {str(payload.get('client_notified_note') or '').strip() or 'not recorded'}"
    )
    followup_text = (
        f"- Status: {status}\n"
        f"- Resolution trigger: {resolution_trigger or 'not recorded'}\n"
        f"- Resolved at: {resolved_at or 'not resolved'}\n"
        f"- Resolution summary: {resolution_summary or 'not resolved'}"
    )
    existing_history = ""
    if existing_text:
        _frontmatter, existing_body, _has_frontmatter = _shared.parse_frontmatter_text(existing_text)
        existing_history = _shared.extract_section(existing_body, "History")
    history_lines = [line for line in existing_history.splitlines() if line.strip()]
    history_entry = f"- {created_at}: queue job {job_id} logged/updated this personnel event."
    if history_entry not in history_lines:
        history_lines.append(history_entry)
    body = (
        f"# {payload['event_type'].title()} event: {payload['employee']}\n\n"
        "## Summary\n"
        f"{str(payload.get('summary') or '').strip()}\n\n"
        "## Notes\n"
        f"{notes or 'No additional notes.'}\n\n"
        "## Evidence\n"
        f"{evidence_text}\n\n"
        "## Client Communication\n"
        f"{client_text}\n\n"
        "## Follow-up / Resolution Notes\n"
        f"{followup_text}\n\n"
        "## History\n"
        + "\n".join(history_lines)
        + "\n"
    )
    return "\n".join(fields) + "\n" + body


def _issue_status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or "open").strip() or "open"


def _issue_priority(payload: dict[str, Any]) -> str:
    return str(payload.get("priority") or "normal").strip() or "normal"


def _issue_category(payload: dict[str, Any]) -> str:
    return str(payload.get("category") or "other").strip() or "other"


def _client_notification_field(payload: dict[str, Any], modern: str, legacy: str) -> str:
    value = payload.get(modern)
    if value in {None, ""}:
        value = payload.get(legacy)
    return str(value or "").strip()


def _supply_need_status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or "open").strip() or "open"


def _supply_need_urgency(payload: dict[str, Any]) -> str:
    return str(payload.get("urgency") or "normal").strip() or "normal"


def _equipment_request_status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or "open").strip() or "open"


def _equipment_request_priority(payload: dict[str, Any]) -> str:
    return str(payload.get("priority") or "normal").strip() or "normal"
