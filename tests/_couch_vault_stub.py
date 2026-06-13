"""Test stub: back the CouchDB-only ``discover_site_*`` helpers with vault markdown.

The production ``discover_site_supplies`` / ``discover_site_issues`` /
``discover_site_equipment`` helpers read entity docs from CouchDB and ignore the
``vault_root`` argument they still accept. Many ops-dashboard and field-capture
tests seed entity data by writing typed markdown notes into a temporary vault
(via their ``write_vault_*`` helpers) and pass that vault path through the route.

This stub installs markdown-backed reimplementations of the three discover
helpers that honour the ``vault_root`` argument, parsing each note's frontmatter
into the same shape the canonical CouchDB document mappers expect. Install it
with the ``couch_vault_stub`` autouse fixture so existing tests keep passing
without a live CouchDB.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import site_equipment
import site_issues
import site_supplies
from vault_markdown import frontmatter_list, read_typed_markdown_note


def _frontmatter_doc(path: Path, type_name: str) -> dict | None:
    frontmatter, body, warning = read_typed_markdown_note(path, type_name)
    if warning is not None or frontmatter is None:
        return None
    doc: dict = {}
    for key, value in frontmatter.items():
        if key in {"related_capture_ids", "related_candidate_ids"}:
            doc[key] = list(frontmatter_list(value))
        else:
            doc[key] = value
    doc.setdefault("_id", path.stem)
    doc.setdefault("summary", body.strip())
    return doc


def _docs(vault_root: Path | None, subdir: str, type_name: str) -> list[dict]:
    if vault_root is None:
        return []
    root = Path(vault_root).expanduser()
    accounts_root = root / "Accounts"
    if not accounts_root.exists():
        return []
    docs: list[dict] = []
    for path in sorted(accounts_root.glob(f"*/Locations/*/{subdir}/*.md")):
        doc = _frontmatter_doc(path, type_name)
        if doc is not None:
            docs.append(doc)
    return docs


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    def discover_supplies(vault_root, *, site_id=None, status=None, include_archived=False, archived_only=False):
        docs = _docs(vault_root, "Supplies", "supply_need")
        supplies = []
        for doc in docs:
            supply = site_supplies._supply_from_couch_doc(doc)
            if supply is None:
                continue
            if not site_supplies._include_archived_item(supply.archived, include_archived=include_archived, archived_only=archived_only):
                continue
            if site_id is not None and supply.site_id != str(site_id):
                continue
            if status is not None and supply.status != str(status).strip():
                continue
            supplies.append(supply)
        supplies.sort(key=lambda item: (site_supplies.status_sort(item.status), site_supplies.urgency_sort(item.urgency), item.site_id, item.created_at, item.supply_id))
        return {"supplies": supplies, "warnings": [], "counts": site_supplies.supply_counts(supplies, include_archived=include_archived or archived_only)}

    def discover_equipment(vault_root, *, site_id=None, status=None, include_archived=False, archived_only=False):
        docs = _docs(vault_root, "Equipment", "equipment_request")
        equipment = []
        for doc in docs:
            request = site_equipment._equipment_from_couch_doc(doc)
            if request is None:
                continue
            if not site_equipment._include_archived_item(request.archived, include_archived=include_archived, archived_only=archived_only):
                continue
            if site_id is not None and request.site_id != str(site_id):
                continue
            if status is not None and request.status != str(status).strip():
                continue
            equipment.append(request)
        equipment.sort(key=lambda item: (site_equipment.status_sort(item.status), site_equipment.priority_sort(item.priority), item.site_id, item.created_at, item.equipment_id))
        return {"equipment": equipment, "warnings": [], "counts": site_equipment.equipment_counts(equipment, include_archived=include_archived or archived_only)}

    def discover_issues(vault_root, *, site_id=None, include_archived=False, archived_only=False):
        docs = _docs(vault_root, "Issues", "site_issue")
        issues = []
        for doc in docs:
            issue = site_issues._site_issue_from_couch_doc(doc)
            if issue is None:
                continue
            if not site_issues._include_archived_item(issue.archived, include_archived=include_archived, archived_only=archived_only):
                continue
            if site_id is not None and issue.site_id != str(site_id):
                continue
            issues.append(issue)
        issues.sort(key=lambda item: (site_issues.status_sort(item.status), item.site_id, item.priority, item.created_at, item.issue_id))
        return {"issues": issues, "warnings": [], "counts": site_issues.issue_counts(issues, include_archived=include_archived or archived_only)}

    # Patch the canonical modules and every module that imported the helper by name.
    import importlib

    supply_targets = ["site_supplies"]
    equipment_targets = ["site_equipment"]
    issue_targets = ["site_issues"]
    section_modules = [
        ("ops_dashboard.sections.supplies", "discover_site_supplies", discover_supplies),
        ("ops_dashboard.sections.equipment", "discover_site_equipment", discover_equipment),
        ("ops_dashboard.sections.issues", "discover_site_issues", discover_issues),
        ("ops_dashboard.sections.health", "discover_site_issues", discover_issues),
        ("ops_dashboard.sections.inbox", "discover_site_supplies", discover_supplies),
        ("ops_dashboard.sections.inbox", "discover_site_equipment", discover_equipment),
        ("ops_dashboard.sections.inbox", "discover_site_issues", discover_issues),
        ("field_capture.site_status_export", "discover_site_issues", discover_issues),
    ]

    monkeypatch.setattr(site_supplies, "discover_site_supplies", discover_supplies)
    monkeypatch.setattr(site_equipment, "discover_site_equipment", discover_equipment)
    monkeypatch.setattr(site_issues, "discover_site_issues", discover_issues)

    for module_name, attr, fn in section_modules:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            continue
        if hasattr(module, attr):
            monkeypatch.setattr(module, attr, fn)


@pytest.fixture(autouse=True)
def couch_vault_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
