from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from btq_vault.couch_store import CouchDBEntityStore
from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_writer import get_field_capture_document, put_field_capture_document
from event_pipeline.couchdb_registry import CouchDBRegistryError, CouchDBSiteRegistry
from event_pipeline.sites import SITES, resolve_site_id
from field_capture.prospects import load_prospect, write_prospect
from io_atomic import atomic_write_text, safe_move
from processing_core.slugs import lower_dash_slug
from queue_processor import idempotency_ledger
from queue_processor.evidence import canonical_evidence_text, write_evidence_snapshot
from queue_processor.idempotency import (
    compute_job_id,
    has_job_been_applied,
    parse_frontmatter_text,
    serialize_frontmatter,
    upsert_job_id_frontmatter,
)
from queue_processor.manifest import record_processed_mutation
from queue_processor.processed_index import (
    append_record,
    build_record,
    processed_job_id_exists as indexed_processed_job_id_exists,
)
from queue_processor.structured_log import write_event as write_structured_event
from queue_spec import (
    JOB_ADD_PERSON,
    JOB_APPEND_TO_NOTE,
    JOB_CLOSE_RECRUITING,
    JOB_FLAG_ACCESS_CONSTRAINT,
    JOB_FLAG_RETENTION_RISK,
    JOB_PARSE_SUPPLY_EMAIL,
    JOB_PERSONAL_JOURNAL_ENTRY,
    JOB_PHOTO_CAPTURE,
    JOB_PROMOTE_PROSPECT,
    JOB_RECLASSIFY_UNKNOWN,
    JOB_REMOVE_FROM_SCHEDULE,
    JOB_RETARGET_CAPTURE,
    JOB_TRIGGER_RECRUITING,
    JOB_VISIT_CREATE,
    JOB_VOICE_MEMO_NOTE,
    VAULT_RELATIVE_PATH_JOB_TYPES,
    normalize_vault_relative_path,
    validate_job,
)
from vault_errors import NotFoundError

DEFAULT_CONFIG = get_config()

DEFAULT_PROJECT_ROOT = DEFAULT_CONFIG.project_dir

DEFAULT_VAULT_ROOT = DEFAULT_CONFIG.vault_dir

DEFAULT_PERSONAL_VAULT_ROOT = DEFAULT_CONFIG.personal_vault_dir

DEFAULT_RUNTIME_ROOT = DEFAULT_CONFIG.project_runtime_root

DEFAULT_LOGS_ROOT = DEFAULT_CONFIG.queue_processor_logs_dir

QUEUE_PROCESSOR_LOG_MAX_BYTES = 20 * 1024 * 1024

_VAULT_STORE: CouchDBEntityStore | None = None
_PERSONAL_JOURNAL_STORE: CouchDBEntityStore | None = None
class QueueProcessorError(Exception):
    """Raised when a queue job cannot be processed safely."""
class QueueJobError(QueueProcessorError):
    """Raised when a queue job must fail and remain retryable."""
class InvalidSiteIdError(QueueProcessorError):
    """Raised when an employee references a site_id that does not exist."""
def _vault_store() -> CouchDBEntityStore:
    global _VAULT_STORE
    if _VAULT_STORE is None:
        _VAULT_STORE = CouchDBEntityStore.from_env()
    return _VAULT_STORE
def _personal_journal_store() -> CouchDBEntityStore:
    global _PERSONAL_JOURNAL_STORE
    if _PERSONAL_JOURNAL_STORE is None: _PERSONAL_JOURNAL_STORE = CouchDBEntityStore.for_database_from_env(couchdb_config.personal_journal_database())
    return _PERSONAL_JOURNAL_STORE
def _reset_vault_store() -> None:
    global _VAULT_STORE; _VAULT_STORE = None
def slugify_issue_component(value: str) -> str:
    return lower_dash_slug(value, fallback="site-issue")


def extract_section(body: str, heading: str) -> str:
    pattern = rf"(?ms)^## {re.escape(heading)}\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, body)
    if match is None:
        return ""
    return match.group(1).strip()


def bool_yaml(value: object) -> str:
    return "true" if bool(value) else "false"


def yaml_optional(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def yaml_list_lines(key: str, values: list[str]) -> list[str]:
    if not values:
        return [f"{key}: []"]
    return [f"{key}:", *[f"  - {yaml_scalar(value)}" for value in values]]


def client_notified(payload: dict) -> bool:
    value = payload.get("client_notified")
    if isinstance(value, bool):
        return value
    legacy = payload.get("client_informed")
    return bool(legacy) if isinstance(legacy, bool) else False


@dataclass(frozen=True)
class QueueJob:
    job_id: str
    job_type: str
    payload: dict
    metadata: dict[str, Any]
    intent: dict[str, Any]
    idempotency_key: Optional[str] = None
def _canonical_vault_upsert(job: QueueJob, doc: dict, *, site_id: object | None = None) -> dict:
    entity_id = str(doc.get("_id") or "").strip() or "unknown"
    site_text = "" if site_id is None else f" site_id={site_id}"
    try:
        store = _vault_store()
        store.upsert(doc)
        return dict(doc)
    except Exception as exc:
        logging.getLogger("queue_processor.main").error("btq_vault write failed: %s", exc)
        raise QueueJobError(
            "canonical couchdb write failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}{site_text}: {exc}"
        ) from exc

def canonical_content_body(markdown_text: str) -> str:
    _frontmatter, body, has_frontmatter = parse_frontmatter_text(markdown_text)
    return body if has_frontmatter else markdown_text

def canonical_location_doc_id_for_projection(path: Path, markdown_text: str) -> str:
    try:
        fields, _body = parse_frontmatter(markdown_text)
    except QueueProcessorError:
        fields = []
    site_id = get_frontmatter_value(fields, "site_id") or get_frontmatter_value(fields, "job")
    if site_id is None or not str(site_id).strip():
        folder_site_id = path.parent.name.split(" - ", 1)[0].strip()
        if folder_site_id:
            site_id = folder_site_id
    if site_id is None or not str(site_id).strip():
        raise QueueProcessorError(f"Could not resolve canonical location document for site path: {path}")
    return f"location_{str(site_id).strip()}"

def canonical_employee_doc_id_for_projection(path: Path, markdown_text: str) -> str:
    try:
        fields, _body = parse_frontmatter(markdown_text)
    except QueueProcessorError:
        fields = []
    person_id = get_frontmatter_value(fields, "person_id")
    if person_id is not None and str(person_id).strip():
        return f"employee_{str(person_id).strip()}"
    stem = path.stem.strip()
    if "," in stem:
        last, first = [part.strip() for part in stem.split(",", 1)]
        name = f"{last} {first}".strip()
    else:
        name = stem
    slug = slugify_issue_component(name).replace("-", "_")
    if not slug:
        raise QueueProcessorError(f"Could not resolve canonical employee document for employee path: {path}")
    return f"employee_{slug}"

def patch_canonical_content(doc_id: str, final_text: str, job: QueueJob) -> dict:
    try:
        store = _vault_store()
        content = canonical_content_body(final_text)
        update_doc = getattr(store, "update_doc", None)
        if callable(update_doc):
            def transform(current: dict[str, Any] | None) -> dict[str, Any] | None:
                if current is None:
                    return None
                outgoing = dict(current)
                outgoing["content"] = content
                job_ids = [str(value).strip() for value in outgoing.get("btq_job_ids") or [] if str(value).strip()]
                if job.job_id not in job_ids:
                    job_ids.append(job.job_id)
                outgoing["btq_job_ids"] = job_ids
                return outgoing
            return dict(update_doc(doc_id, transform, require_existing=True))
        store.patch_fields(
            doc_id,
            {"content": content, "btq_job_ids": [job.job_id]},
            require_existing=True,
        )
        get_optional = getattr(store, "get_optional", None)
        if callable(get_optional):
            stored = get_optional(doc_id)
            if stored is None:
                raise QueueJobError(
                    "canonical couchdb content patch failed "
                    f"job_type={job.job_type} job_id={job.job_id} entity_id={doc_id}: patched document missing"
                )
            return dict(stored)
        return {"_id": doc_id, "content": content, "btq_job_ids": [job.job_id]}
    except QueueJobError:
        raise
    except Exception as exc:
        raise QueueJobError(
            "canonical couchdb content patch failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={doc_id}: {exc}"
        ) from exc

def patch_canonical_location_content(path: Path, final_text: str, job: QueueJob) -> dict:
    doc_id = "unknown"
    try:
        doc_id = canonical_location_doc_id_for_projection(path, final_text)
        return patch_canonical_content(doc_id, final_text, job)
    except QueueJobError:
        raise
    except Exception as exc:
        raise QueueJobError(
            "canonical couchdb content patch failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={doc_id}: {exc}"
        ) from exc

def patch_canonical_employee_content(path: Path, final_text: str, job: QueueJob) -> dict:
    doc_id = "unknown"
    try:
        doc_id = canonical_employee_doc_id_for_projection(path, final_text)
        return patch_canonical_content(doc_id, final_text, job)
    except QueueJobError:
        raise
    except Exception as exc:
        raise QueueJobError(
            "canonical couchdb content patch failed "
            f"job_type={job.job_type} job_id={job.job_id} entity_id={doc_id}: {exc}"
        ) from exc
@dataclass(frozen=True)
class RunContext:
    project_root: Path
    vault_root: Path
    personal_vault_root: Path
    runtime_root: Path
    log_path: Path
    dry_run: bool
    valid_site_ids: set[str] = field(default_factory=set)
    site_id_to_opportunities_dir: dict[str, Path] = field(default_factory=dict)
    run_id: str = ""
    structured_log_path: Path | None = None
    def __post_init__(self) -> None:
        runtime_root = self.runtime_root.expanduser().resolve()
        object.__setattr__(self, "runtime_root", runtime_root)
        object.__setattr__(self, "log_path", self.assert_runtime_write_path(self.log_path, "queue processor log path"))
        if self.structured_log_path is not None:
            object.__setattr__(
                self,
                "structured_log_path",
                self.assert_runtime_write_path(self.structured_log_path, "queue processor structured log path"),
            )
    def resolve_runtime_path(self, *parts: str, label: str = "runtime path") -> Path:
        return ensure_within_root(self.runtime_root.joinpath(*parts), self.runtime_root, label)
    def assert_runtime_write_path(self, path: Path, label: str) -> Path:
        return ensure_within_root(path.expanduser(), self.runtime_root, label)
def ensure_within_root(path: Path, root: Path, label: str) -> Path:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        common = Path(os.path.commonpath([str(resolved_path), str(resolved_root)]))
    except ValueError as exc:
        raise QueueProcessorError(f"{label} is outside allowed root: {resolved_path}") from exc
    if common != resolved_root:
        raise QueueProcessorError(f"{label} is outside allowed root: {resolved_path}")
    return resolved_path
def ensure_within_any_root(path: Path, roots: list[Path], label: str) -> Path:
    last_error: QueueProcessorError | None = None
    for root in roots:
        try:
            return ensure_within_root(path, root, label)
        except QueueProcessorError as exc:
            last_error = exc
    resolved_path = path.resolve()
    allowed_roots = ", ".join(str(root.resolve()) for root in roots)
    raise QueueProcessorError(f"{label} is outside allowed roots: {resolved_path}; allowed roots: {allowed_roots}") from last_error
def path_is_within(path: Path, root: Path) -> bool:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        common = Path(os.path.commonpath([str(resolved_path), str(resolved_root)]))
    except ValueError:
        return False
    return common == resolved_root
def looks_like_icloud_path(path: Path) -> bool:
    return "Mobile Documents" in str(path.expanduser())
def validate_date_string(date_value: str) -> str:
    try:
        parsed_date = datetime.strptime(date_value, "%Y-%m-%d")
    except ValueError as exc:
        raise QueueProcessorError("Field date must be a valid YYYY-MM-DD value") from exc
    normalized_date = parsed_date.strftime("%Y-%m-%d")
    if normalized_date != date_value:
        raise QueueProcessorError("Field date must match YYYY-MM-DD exactly")
    return date_value
def validate_payload_keys(raw_payload: dict, required_keys: set[str], optional_keys: set[str] | None = None) -> None:
    allowed_optional_keys = optional_keys or set()
    payload_keys = set(raw_payload.keys())
    missing_keys = sorted(required_keys - payload_keys)
    extra_keys = sorted(payload_keys - required_keys - allowed_optional_keys)
    if missing_keys:
        raise QueueProcessorError(f"Missing required field(s): {', '.join(missing_keys)}")
    if extra_keys:
        raise QueueProcessorError(f"Unexpected field(s): {', '.join(extra_keys)}")
def parse_frontmatter(text: str) -> tuple[list[tuple[str, Any]], str]:
    parsed, body, has_frontmatter = parse_frontmatter_text(text)
    if not has_frontmatter:
        raise QueueProcessorError("File must start with frontmatter")
    return list(parsed.items()), body
def get_frontmatter_value(fields: list[tuple[str, Any]], key: str) -> str | None:
    for existing_key, value in fields:
        if existing_key == key and isinstance(value, str):
            return value
    return None
def set_frontmatter_value(fields: list[tuple[str, Any]], key: str, value: Any) -> list[tuple[str, Any]]:
    updated_fields: list[tuple[str, Any]] = []
    found = False
    for existing_key, existing_value in fields:
        if existing_key == key:
            updated_fields.append((key, value))
            found = True
        else:
            updated_fields.append((existing_key, existing_value))
    if not found:
        updated_fields.append((key, value))
    return updated_fields

def remove_frontmatter_value(fields: list[tuple[str, Any]], key: str) -> list[tuple[str, Any]]:
    return [(existing_key, value) for existing_key, value in fields if existing_key != key]

def frontmatter_to_text(fields: list[tuple[str, Any]], body: str) -> str:
    frontmatter_lines: list[str] = []
    for key, value in fields:
        if isinstance(value, list):
            frontmatter_lines.append(f"{key}:")
            for item in value:
                frontmatter_lines.append(f"  - {item}")
            continue
        if isinstance(value, bool):
            frontmatter_lines.append(f"{key}: {bool_yaml(value)}")
            continue
        frontmatter_lines.append(f"{key}: {value}")
    normalized_body = body if body.endswith("\n") else f"{body}\n"
    return f"---\n" + "\n".join(frontmatter_lines) + f"\n---\n{normalized_body}"
def parse_string_list_payload(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result
def yaml_scalar(value: object) -> str:
    if value is None:
        return "null"
    text = str(value).strip()
    if text == "":
        return '""'
    return text
def canonical_employee_doc_id_from_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    if "," in cleaned:
        last, first = [part.strip() for part in cleaned.split(",", 1)]
        cleaned = f"{last} {first}".strip()
    else:
        parts = cleaned.split()
        if len(parts) >= 2:
            cleaned = f"{parts[-1]} {' '.join(parts[:-1])}".strip()
    slug = slugify_issue_component(cleaned).replace("-", "_")
    return f"employee_{slug}"
def person_file_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    if not cleaned:
        raise QueueProcessorError("Person name must not be empty")
    if any(separator in cleaned for separator in ("/", "\\")):
        raise QueueProcessorError("Person name must not contain path separators")
    if cleaned in {".", ".."} or cleaned.endswith(".md"):
        raise QueueProcessorError("Person name must not look like a path")
    if "," not in cleaned:
        parts = cleaned.split()
        if len(parts) >= 2:
            return f"{parts[-1]}, {' '.join(parts[:-1])}.md"
    return f"{cleaned}.md"
def append_to_markdown_section(existing_text: str, section_heading: str, content: str) -> str:
    section_block = f"{section_heading}\n"
    content_block = content if content.endswith("\n") else f"{content}\n"
    if section_block in existing_text:
        section_index = existing_text.index(section_block)
        insert_at = len(existing_text)
        next_section_index = existing_text.find("\n## ", section_index + len(section_block))
        if next_section_index != -1:
            insert_at = next_section_index + 1
        section_content = existing_text[section_index + len(section_block):insert_at]
        if section_content and not section_content.endswith("\n"):
            return f"{existing_text[:insert_at]}\n{content_block}{existing_text[insert_at:]}"
        return f"{existing_text[:insert_at]}{content_block}{existing_text[insert_at:]}"
    if not existing_text:
        return f"{section_heading}\n{content_block}"
    if existing_text.endswith("\n"):
        return f"{existing_text}\n{section_heading}\n{content_block}"
    return f"{existing_text}\n\n{section_heading}\n{content_block}"
def write_log_line(log_path: Path, line: str) -> None:
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{line}\n")
def rotate_log_if_large(log_path: Path, max_bytes: int = QUEUE_PROCESSOR_LOG_MAX_BYTES) -> None:
    """Bound the shared queue-processor log with a single rotated backup.
    The processor runs every few seconds; a file per run accumulated hundreds
    of thousands of tiny logs. Runs now append to one shared log, rotated here
    so it never exceeds ~2x max_bytes.
    """
    try:
        if log_path.exists() and log_path.stat().st_size >= max_bytes:
            os.replace(log_path, log_path.with_name(f"{log_path.name}.1"))
    except OSError:
        pass
def structured_log(context: RunContext, event: str, **fields: Any) -> None:
    if context.structured_log_path is None:
        return
    write_structured_event(
        context.structured_log_path,
        event,
        run_id=context.run_id,
        **fields,
    )
def capture_id_for_job(job: QueueJob) -> str | None:
    metadata_capture_id = job.metadata.get("capture_id")
    if isinstance(metadata_capture_id, str) and metadata_capture_id.strip():
        return metadata_capture_id
    payload_capture_id = job.payload.get("capture_id")
    if isinstance(payload_capture_id, str) and payload_capture_id.strip():
        return payload_capture_id
    related_capture_ids = job.payload.get("related_capture_ids")
    if isinstance(related_capture_ids, list):
        for capture_id in related_capture_ids:
            if isinstance(capture_id, str) and capture_id.strip():
                return capture_id.strip()
    return None
def write_mutation_evidence(
    context: RunContext,
    job: QueueJob,
    canonical_doc: dict,
    mutation_text: str | None,
    *,
    pre_doc: dict | None = None,
) -> None:
    if context.dry_run:
        return
    target_doc_id = str(canonical_doc["_id"])
    pre_text = canonical_evidence_text(pre_doc) if pre_doc is not None else ""
    post_text = canonical_evidence_text(canonical_doc)
    path = write_evidence_snapshot(
        context.runtime_root,
        capture_id=capture_id_for_job(job),
        job_id=job.job_id,
        job_type=job.job_type,
        payload=job.payload,
        intent=job.intent,
        target_doc_id=target_doc_id,
        pre_doc=pre_doc,
        post_doc=canonical_doc,
        pre_text=pre_text,
        post_text=post_text,
        mutation_text=mutation_text,
    )
    structured_log(
        context,
        "mutation_evidence_snapshot_created",
        computed_job_id=job.job_id,
        job_type=job.job_type,
        capture_id=capture_id_for_job(job),
        evidence_path=str(path),
        target_path=target_doc_id,
        target_doc_id=target_doc_id,
    )
def processed_job_id_exists(runtime_root: Path, processed_dir: Path, job_id: str) -> tuple[bool, str]:
    return indexed_processed_job_id_exists(runtime_root, processed_dir, job_id)
def move_job_file(job_path: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_path = destination_dir / job_path.name
    if destination_path.exists():
        raise QueueProcessorError(f"Destination already exists: {destination_path}")
    safe_move(job_path, destination_path)
    return destination_path
def payload_date(payload: dict) -> str:
    date_value = payload.get("date")
    if isinstance(date_value, str):
        match = date_value[:10]
        try:
            return validate_date_string(match)
        except QueueProcessorError:
            pass
    return datetime.now(timezone.utc).date().isoformat()
def build_visit_key(site: str, date: str) -> str:
    return f"{site}:{date}"
def get_active_visit(context: RunContext, site: str, date: str) -> str | None:
    site_id = resolve_site_id(site) or (resolve_site_id(site.split(" - ", 1)[0].strip()) if " - " in site else None)
    if site_id is None: raise NotFoundError(f"Unknown site: {site}")
    if _vault_store().find_visit_docs(site_id, date): return build_visit_key(site, date)
    return None
def site_name_for_about_path(path: Path) -> str | None:
    if path.name != "about.md":
        return None
    try:
        fields, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
    except QueueProcessorError:
        return None
    if get_frontmatter_value(fields, "type") != "location":
        return None
    location = get_frontmatter_value(fields, "location")
    if isinstance(location, str) and location.strip():
        return location.strip()
    return None
def is_site_about_path(path: Path) -> bool:
    return path.name == "about.md" and path.parent.parent.name == "Locations"
def append_visit_key_suffix(text: str, visit_key: str | None) -> str:
    normalized = text.strip()
    if not normalized or visit_key is None:
        return normalized
    token = f'visit_key: "{visit_key}"'
    if token in normalized:
        return normalized
    return f'{normalized}\n{token}'
def build_visit_gap_block(site: str, date: str) -> str:
    return (
        "---\n"
        "type: visit_gap\n"
        f"site: {site}\n"
        f"date: {date}\n"
        'reason: "event_without_visit"\n'
        "---\n"
    )
def append_visit_gap_if_missing(existing_text: str, site: str | None, date: str, visit_key: str | None) -> str:
    if site is None or visit_key is not None:
        return existing_text
    gap_block = build_visit_gap_block(site, date)
    if gap_block in existing_text:
        return existing_text
    if not existing_text:
        return gap_block
    if existing_text.endswith("\n"):
        return f"{existing_text}\n{gap_block}"
    return f"{existing_text}\n\n{gap_block}"
def append_markdown_block(existing_text: str, append_text: str) -> str:
    normalized_append = append_text if append_text.endswith("\n") else f"{append_text}\n"
    if not existing_text:
        return normalized_append
    if existing_text.endswith("\n\n"):
        return f"{existing_text}{normalized_append}"
    if existing_text.endswith("\n"):
        return f"{existing_text}\n{normalized_append}"
    return f"{existing_text}\n\n{normalized_append}"
def _field_capture_config() -> couchdb_config.CouchDBConfig:
    return couchdb_config.from_env()
def _field_capture_database() -> str:
    return couchdb_config.field_captures_database()
def _site_id_registered(site_id: str) -> bool:
    normalized = site_id.strip()
    if not normalized:
        return False
    if os.environ.get("BTQ_COUCHDB_URL"):
        try:
            return CouchDBSiteRegistry().resolve_canonical(normalized) is not None
        except CouchDBRegistryError as exc:
            raise QueueProcessorError(f"site registry unavailable: {exc}") from exc
    return normalized in {str(site["site_id"]) for site in SITES}
def _find_prospect_captures(
    config: couchdb_config.CouchDBConfig,
    prospect_id: str,
    *,
    database: str,
) -> list[dict[str, Any]]:
    payload = {
        "selector": {
            "type": "field_capture",
            "target_type": "prospect",
            "target_id": prospect_id,
        }
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())
    url = f"{config.base_url}/{url_parse.quote(database, safe='')}/_find"
    req = url_request.Request(url, data=json.dumps(payload, sort_keys=True).encode("utf-8"), headers=headers, method="POST")
    try:
        with url_request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except url_error.HTTPError as exc:
        raise QueueProcessorError(f"CouchDB capture query failed: HTTP {exc.code}") from exc
    except (url_error.URLError, OSError) as exc:
        raise QueueProcessorError("CouchDB capture query failed") from exc
    if not 200 <= status < 300:
        raise QueueProcessorError(f"CouchDB capture query failed: HTTP {status}")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QueueProcessorError("CouchDB capture query returned invalid JSON") from exc
    docs = parsed.get("docs") if isinstance(parsed, dict) else None
    if not isinstance(docs, list):
        return []
    return [doc for doc in docs if isinstance(doc, dict)]
