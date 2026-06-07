from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from btq_vault.entity_types import CANONICAL_ENTITY_TYPES, DEPRECATED_TYPE_ALIASES, OPERATOR_ID_GREG
from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb.push_design_doc import push_design_doc
from processing_core.slugs import lower_underscore_slug
from vault_markdown import frontmatter_list, frontmatter_str, read_markdown_note


DASHBOARD_TYPE = "dashboard"
EXCLUDED_DIRS = {".obsidian", ".git"}


@dataclass
class MigrationSummary:
    files_walked: int = 0
    skipped_excluded: int = 0
    skipped_dashboard: int = 0
    skipped_dashboard_owned: int = 0
    parsed_entity: int = 0
    parsed_note: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0

    @property
    def skipped(self) -> int:
        return self.skipped_excluded + self.skipped_dashboard + self.skipped_dashboard_owned

    @property
    def upserted(self) -> int:
        return self.created + self.updated + self.unchanged


def operator_bootstrap_doc() -> dict[str, Any]:
    return {
        "_id": f"operator_{OPERATOR_ID_GREG}",
        "type": "operator",
        "operator_id": OPERATOR_ID_GREG,
        "name": "Jordan",
        "status": "active",
    }


def slug_value(value: str) -> str:
    without_md = re.sub(r"\.md$", "", value, flags=re.IGNORECASE)
    # Legacy CouchDB ids use underscores; keep that shape while sharing the core
    # lowercase/non-alphanumeric normalization primitive.
    return lower_underscore_slug(without_md, fallback="unknown")


def _relative_vault_path(path: Path, vault_root: Path) -> str:
    return path.relative_to(vault_root).as_posix()


def _resolved_type(raw_type: object) -> str | None:
    if not isinstance(raw_type, str) or not raw_type.strip():
        return None
    raw = raw_type.strip()
    return DEPRECATED_TYPE_ALIASES.get(raw, raw)


def _stable_id(entity_type: str, frontmatter: dict[str, Any], relative_path: str) -> str:
    filename_slug = slug_value(Path(relative_path).name)
    if entity_type == "employee":
        person_id = frontmatter_str(frontmatter, "person_id")
        return f"employee_{person_id}" if person_id else f"employee_{filename_slug}"
    if entity_type == "account":
        account = frontmatter_str(frontmatter, "account")
        return f"account_{slug_value(account or Path(relative_path).stem)}"
    if entity_type == "location":
        site_id = frontmatter_str(frontmatter, "site_id") or frontmatter_str(frontmatter, "job")
        return f"location_{site_id}" if site_id else f"location_{filename_slug}"
    if entity_type == "note":
        return f"note_{slug_value(relative_path)}"
    return f"{entity_type}_{slug_value(relative_path)}"


def _string_list(value: object) -> list[str]:
    return list(frontmatter_list(value))


def _employee_site_ids(frontmatter: dict[str, Any]) -> list[str]:
    site_ids: list[str] = []
    # job may be a scalar ("7060") or a YAML list (["7060"] or
    # even ["7060", "7050"]). Use _string_list which normalises
    # both shapes -- same pattern additional_jobs already uses.
    site_ids.extend(_string_list(frontmatter.get("job")))
    site_ids.extend(_string_list(frontmatter.get("additional_jobs")))
    if not site_ids:
        site_ids.extend(_string_list(frontmatter.get("sites")))
    seen: set[str] = set()
    deduped: list[str] = []
    for value in site_ids:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped


def _employee_display_name(frontmatter: dict[str, Any]) -> str:
    """Jordan's convention: (preferred_name or first) + ' ' + last."""
    preferred = frontmatter_str(frontmatter, "preferred_name")
    first = frontmatter_str(frontmatter, "first")
    last = frontmatter_str(frontmatter, "last")
    leading = preferred or first or ""
    if leading and last:
        return f"{leading} {last}"
    return leading or last or ""


# Operational acronyms that should render uppercased when they
# appear as standalone words in a filename slug. Add to this
# set if a new acronym surfaces in opportunity titles.
_TITLE_ACRONYMS = frozenset({
    "PPE",  # personal protective equipment
    "FT",   # full-time
    "PT",   # part-time
    "QC",   # quality control
    "AM",   # area manager
    "HR",   # human resources
    "IT",   # information technology
    "SDS",  # safety data sheet
    "MSDS", # material safety data sheet
    "SOP",  # standard operating procedure
    "HVAC", # heating, ventilation, air conditioning
})


def _render_title_word(word: str) -> str:
    if word.upper() in _TITLE_ACRONYMS:
        return word.upper()
    return word.capitalize()


def _opportunity_display_title(relative_path: str) -> str:
    """Derive a human-readable title from the file's stem."""
    stem = Path(relative_path).stem
    if not stem:
        return ""
    m = re.match(r"^\d{4}[-_]\d{2}[-_]\d{2}[-_](.*)$", stem)
    body = m.group(1) if m else stem
    words = re.split(r"[-_]+", body)
    return " ".join(_render_title_word(word) for word in words if word)


def parse_vault_file(path: Path, vault_root: Path) -> dict[str, Any] | None:
    resolved_root = vault_root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    relative_path = _relative_vault_path(resolved_path, resolved_root)
    frontmatter, body, has_frontmatter = read_markdown_note(resolved_path)

    if not has_frontmatter:
        content = resolved_path.read_text(encoding="utf-8")
        return {
            "_id": _stable_id("note", {}, relative_path),
            "type": "note",
            "operator": OPERATOR_ID_GREG,
            "vault_path": relative_path,
            "content": content,
        }

    entity_type = _resolved_type(frontmatter.get("type"))
    if entity_type == DASHBOARD_TYPE:
        return None
    if entity_type is None:
        content = resolved_path.read_text(encoding="utf-8")
        return {
            "_id": _stable_id("note", frontmatter, relative_path),
            "type": "note",
            "operator": OPERATOR_ID_GREG,
            "vault_path": relative_path,
            "content": content,
        }
    if entity_type not in CANONICAL_ENTITY_TYPES:
        raise ValueError(f"{relative_path}: unsupported vault entity type {entity_type!r}")

    doc: dict[str, Any] = {
        "_id": _stable_id(entity_type, frontmatter, relative_path),
        "type": entity_type,
        "operator": OPERATOR_ID_GREG,
        "vault_path": relative_path,
        "content": body.strip(),
    }
    for key, value in frontmatter.items():
        if key == "type":
            continue
        doc[key] = value
    if entity_type == "employee":
        doc["site_ids"] = _employee_site_ids(frontmatter)
        doc["name"] = _employee_display_name(frontmatter)
    elif entity_type == "opportunity":
        doc["title"] = _opportunity_display_title(relative_path)
    return doc


def is_excluded_markdown_path(path: Path, vault_root: Path) -> bool:
    relative = path.relative_to(vault_root)
    if relative.parts == ("README.md",):
        return True
    return any(part in EXCLUDED_DIRS for part in relative.parts)


def iter_markdown_files(vault_root: Path) -> list[Path]:
    return sorted(path for path in vault_root.rglob("*.md") if path.is_file())


def request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    config = couchdb_config.from_env()
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    data = None
    if payload is not None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(f"{config.base_url}/{path.lstrip('/')}", data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return 404, None
        raise
    if not raw:
        return status, None
    return status, json.loads(raw.decode("utf-8"))


def get_existing_docs(database: str) -> dict[str, dict[str, Any]]:
    quoted_db = parse.quote(database, safe="")
    _status, payload = request_json("GET", f"{quoted_db}/_all_docs?include_docs=true")
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    existing: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("doc"), dict):
            doc = row["doc"]
            doc_id = doc.get("_id")
            if isinstance(doc_id, str):
                existing[doc_id] = doc
    return existing


def without_rev(doc: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in doc.items() if key != "_rev"}


def bulk_write_docs(database: str, docs: list[dict[str, Any]]) -> None:
    if not docs:
        return
    quoted_db = parse.quote(database, safe="")
    request_json("POST", f"{quoted_db}/_bulk_docs", {"docs": docs})


def prepare_upserts(
    desired_docs: list[dict[str, Any]],
    existing_docs: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    writes: list[dict[str, Any]] = []
    created = 0
    updated = 0
    unchanged = 0
    skipped_dashboard_owned = 0
    for desired in desired_docs:
        existing = existing_docs.get(str(desired["_id"]))
        if existing is None:
            writes.append(desired)
            created += 1
            continue
        if existing is not None and desired.get("type") in {"location", "employee"}:
            skipped_dashboard_owned += 1
            continue
        if without_rev(existing) == desired:
            unchanged += 1
            continue
        updated_doc = dict(desired)
        updated_doc["_rev"] = existing["_rev"]
        writes.append(updated_doc)
        updated += 1
    return writes, created, updated, unchanged, skipped_dashboard_owned


def collect_desired_docs(vault_root: Path, summary: MigrationSummary) -> list[dict[str, Any]]:
    desired = [operator_bootstrap_doc()]
    for path in iter_markdown_files(vault_root):
        summary.files_walked += 1
        if is_excluded_markdown_path(path, vault_root):
            summary.skipped_excluded += 1
            continue
        try:
            doc = parse_vault_file(path, vault_root)
        except Exception as exc:
            summary.errors += 1
            print(f"error: {path}: {exc}")
            continue
        if doc is None:
            summary.skipped_dashboard += 1
            continue
        if doc["type"] == "note":
            summary.parsed_note += 1
        else:
            summary.parsed_entity += 1
        desired.append(doc)
    return desired


def print_summary(summary: MigrationSummary) -> None:
    print(f"total files walked: {summary.files_walked}")
    print(
        "skipped: "
        f"{summary.skipped} "
        f"(dashboard={summary.skipped_dashboard}, "
        f"dashboard_owned={summary.skipped_dashboard_owned}, "
        f"excluded={summary.skipped_excluded})"
    )
    print(f"parsed as entity: {summary.parsed_entity}")
    print(f"parsed as note: {summary.parsed_note}")
    print(
        "upserted: "
        f"{summary.upserted} "
        f"(created={summary.created}, updated={summary.updated}, unchanged={summary.unchanged})"
    )
    print(f"errors: {summary.errors}")


def main() -> int:
    vault_root = get_config().vault_dir.expanduser().resolve(strict=False)
    summary = MigrationSummary()
    push_design_doc("vault")
    database = couchdb_config.vault_database()

    desired_docs = collect_desired_docs(vault_root, summary)
    existing_docs = get_existing_docs(database)
    (
        writes,
        summary.created,
        summary.updated,
        summary.unchanged,
        summary.skipped_dashboard_owned,
    ) = prepare_upserts(desired_docs, existing_docs)
    bulk_write_docs(database, writes)
    print_summary(summary)
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
