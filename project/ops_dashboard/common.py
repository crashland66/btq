from __future__ import annotations

import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urllib_request

from urllib.parse import parse_qs, quote, urlencode

from event_pipeline.sites import SITES
from event_pipeline import couchdb_config
from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from field_capture.deep_analysis import DEEP_ANALYSIS_PRESETS
from field_capture import photo_vision as field_photo_vision
from media_store import LocalFilesystemStore, get_media_store, media_key_from_stored_path
from ops_dashboard import audit
from processing_core.slugs import css_status_slug
from voice_memo.couchdb import VoiceMemoCouchDBConfig, VoiceMemoCouchDBError, query_couchdb_find


logger = logging.getLogger(__name__)

DEFAULT_LOG_LINES = 40
PHOTO_VISION_RECENT_LIMIT = 8
UNKNOWN_SUBMITTER = "Unknown submitter"
DEEP_ANALYSIS_LABELS = {str(preset["id"]): str(preset["label"]) for preset in DEEP_ANALYSIS_PRESETS}
KNOWN_JOB_SUMMARY_TYPES = {
    "append_to_note",
    "add_person",
    "assign_employee_site",
    "trigger_recruiting",
    "close_recruiting",
    "remove_from_schedule",
    "flag_access_constraint",
    "flag_retention_risk",
    "reclassify_unknown",
    "record_unknown_capture",
    "visit_create",
    "parse_supply_email",
    "personal_journal_entry",
    "record_shift_report",
    "shift_report_note",
    "record_day_record",
    "photo_capture",
    "deep_analysis",
    "promote_prospect",
    "retarget_capture",
    "log_site_issue",
    "log_supply_need",
    "log_equipment_request",
    "log_personnel_event",
    "log_availability_constraint",
    "set_entity_status",
    "set_employee_id",
    "set_contact",
    "set_site_hours",
    "set_site_url",
    "update_site_equipment",
    "mark_supply_ordered",
    "mark_supply_delivered",
    "mark_supply_stocked",
    "mark_supply_no_action_needed",
    "mark_equipment_approved",
    "mark_equipment_denied",
    "mark_equipment_ordered",
    "mark_equipment_provided",
    "mark_equipment_no_action_needed",
    "mark_issue_monitoring",
    "mark_issue_resolved",
    "mark_issue_open",
    "mark_record_archived",
    "mark_record_unarchived",
    "edit_record_fields",
    "voice_memo_note",
}


def photo_vision_couchdb_config() -> object:
    if not (os.environ.get("BTQ_COUCHDB_URL") or "").strip():
        return None
    return couchdb_config.from_env()


def deep_analysis_prompt_label(entry: dict[str, object]) -> str:
    prompt_id = str(entry.get("prompt_id") or "").strip()
    label = str(entry.get("label") or "").strip()
    if label:
        return label
    if prompt_id == "custom":
        return "Custom"
    if prompt_id in DEEP_ANALYSIS_LABELS:
        return DEEP_ANALYSIS_LABELS[prompt_id]
    return humanize_key(prompt_id) if prompt_id else "Custom"


_DEEP_ANALYSIS_ORDERED_RE = re.compile(r"^\s*\d+\.\s*(.+)$")
_DEEP_ANALYSIS_UNORDERED_RE = re.compile(r"^\s*[-*]\s+(.+)$")
_DEEP_ANALYSIS_STRONG_RE = re.compile(r"\*\*(.+?)\*\*")
_DEEP_ANALYSIS_EM_ASTERISK_RE = re.compile(r"(?<!\*)\*(?![\s*])(.+?)(?<![\s*])\*(?!\*)")
_DEEP_ANALYSIS_EM_UNDERSCORE_RE = re.compile(r"(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])")


def _render_deep_analysis_inline(escaped_text: str) -> str:
    strong_fragments: list[str] = []

    def strong_replacement(match: re.Match[str]) -> str:
        index = len(strong_fragments)
        strong_fragments.append(f"<strong>{match.group(1)}</strong>")
        return f"\x00BTQSTRONG{index}\x00"

    rendered = _DEEP_ANALYSIS_STRONG_RE.sub(strong_replacement, escaped_text)
    rendered = _DEEP_ANALYSIS_EM_ASTERISK_RE.sub(r"<em>\1</em>", rendered)
    rendered = _DEEP_ANALYSIS_EM_UNDERSCORE_RE.sub(r"<em>\1</em>", rendered)
    for index, fragment in enumerate(strong_fragments):
        rendered = rendered.replace(f"\x00BTQSTRONG{index}\x00", fragment)
    return rendered


def render_deep_analysis_markdown(text: object) -> str:
    if isinstance(text, (dict, list)):
        raw = json.dumps(text, indent=2, sort_keys=True)
    elif text is None:
        raw = ""
    else:
        raw = str(text)

    escaped = html.escape(raw, quote=True).replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    paragraph_lines: list[str] = []
    list_kind = ""
    list_items: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        body = "<br>".join(_render_deep_analysis_inline(line.strip()) for line in paragraph_lines)
        parts.append(f"<p>{body}</p>")
        paragraph_lines = []

    def flush_list() -> None:
        nonlocal list_kind, list_items
        if not list_kind or not list_items:
            return
        body = "".join(f"<li>{item}</li>" for item in list_items)
        parts.append(f"<{list_kind}>{body}</{list_kind}>")
        list_kind = ""
        list_items = []

    for line in escaped.split("\n"):
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            continue

        ordered = _DEEP_ANALYSIS_ORDERED_RE.match(line)
        unordered = _DEEP_ANALYSIS_UNORDERED_RE.match(line)
        if ordered or unordered:
            flush_paragraph()
            next_kind = "ol" if ordered else "ul"
            if list_kind and list_kind != next_kind:
                flush_list()
            list_kind = next_kind
            item_text = (ordered or unordered).group(1).strip()
            list_items.append(_render_deep_analysis_inline(item_text))
            continue

        flush_list()
        paragraph_lines.append(line)

    flush_paragraph()
    flush_list()
    return f'<div class="deep-analysis-md">{"".join(parts)}</div>'


class SectionContext:
    def __init__(self, runtime_root: Path, get_config_func) -> None:
        self._runtime_root = runtime_root
        self._get_config = get_config_func
        self.query: dict[str, list[str]] = {}
        self.route_path = ""

    def effective_config(self) -> object:
        return self._get_config()

    @property
    def config(self) -> object:
        return self.effective_config()

    @property
    def runtime_root(self) -> Path:
        return self._runtime_root.expanduser().resolve(strict=False)

    def redirect(self, location: str):
        return 303, "text/html; charset=utf-8", f'<a href="{html.escape(location)}">Return</a>'.encode(), {"Location": location}

    def flash(self, query: dict[str, list[str]] | None = None) -> str:
        query = query if query is not None else self.query
        message = html.escape(first_query_value(query, "message"))
        error_value = html.escape(first_query_value(query, "error"))
        parts = []
        if message:
            parts.append(f'<section class="notice success"><p>{message}</p></section>')
        if error_value:
            parts.append(f'<section class="error"><p>{error_value}</p></section>')
        return "".join(parts)

    def form_payload(self, form: dict[str, list[str]]) -> dict[str, object]:
        return {key: values[0] if values else "" for key, values in form.items()}

    def audit(self, route: str, payload: dict[str, object], result: str) -> None:
        audit.append_audit(self.runtime_root, {"route": route, "actor": "localhost", "payload": payload, "result_summary": result})


DEFAULT_ACTOR_ENV = "BTQ_OPS_DASHBOARD_DEFAULT_ACTOR"
DEFAULT_ACTOR_FALLBACK = "Greg"


def default_actor() -> str:
    """Pre-filled operator name for review/transition forms.

    Override per-operator with BTQ_OPS_DASHBOARD_DEFAULT_ACTOR; defaults to the
    primary operator so the form doesn't pre-fill someone else's name.
    """
    return (os.environ.get(DEFAULT_ACTOR_ENV) or "").strip() or DEFAULT_ACTOR_FALLBACK


def count_files(path: Path, pattern: str = "*", recursive: bool = False) -> int:
    if not path.exists():
        return 0
    iterator = path.rglob(pattern) if recursive else path.glob(pattern)
    return sum(1 for item in iterator if item.is_file())


def read_json_artifact(path: Path) -> tuple[dict[str, object] | None, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, "JSON artifact is not an object"
    return payload, ""


def safe_submitter(metadata: dict[str, object] | None, payload: dict[str, object] | None = None) -> dict[str, str]:
    metadata = metadata if isinstance(metadata, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    person_name = str(metadata.get("person_name") or metadata.get("submitter_name") or payload.get("person_name") or payload.get("submitter_name") or "").strip()
    person_id = str(metadata.get("person_id") or payload.get("person_id") or "").strip()
    token_label = str(metadata.get("field_capture_token_label") or metadata.get("token_label") or payload.get("field_capture_token_label") or payload.get("token_label") or "").strip()
    display = person_name or person_id or token_label or UNKNOWN_SUBMITTER
    return {
        "submitter_name": display,
        "submitter_id": person_id,
        "token_label": token_label if not person_name and not person_id else "",
    }


_SUBMITTERS_CACHE_TTL_SECONDS = 5.0
# cache[intake_dir] = (cached_at_monotonic, dir_mtime, result)
_SUBMITTERS_CACHE: dict[Path, tuple[float, float, dict[str, dict[str, str]]]] = {}
_SUBMITTERS_CACHE_LOCK = threading.Lock()


def submitters_by_capture(runtime_root: Path) -> dict[str, dict[str, str]]:
    intake_dir = field_photo_vision.default_intake_dir(runtime_root)
    if not intake_dir.exists():
        return {}
    try:
        dir_mtime = intake_dir.stat().st_mtime
    except OSError:
        dir_mtime = 0.0

    def _cached_if_fresh() -> dict[str, dict[str, str]] | None:
        cached = _SUBMITTERS_CACHE.get(intake_dir)
        if cached is None:
            return None
        cached_at, cached_dir_mtime, cached_result = cached
        if cached_dir_mtime != dir_mtime or time.monotonic() - cached_at >= _SUBMITTERS_CACHE_TTL_SECONDS:
            return None
        return cached_result

    fresh = _cached_if_fresh()
    if fresh is not None:
        return fresh

    # Serialize cold loads. Without this, concurrent requests each run the
    # full intake-dir scan in parallel, multiplying transient memory by
    # the worker count. Re-check inside the lock so the second-arriver
    # gets the just-filled cache instead of redoing the scan.
    with _SUBMITTERS_CACHE_LOCK:
        fresh = _cached_if_fresh()
        if fresh is not None:
            return fresh
        submitters: dict[str, dict[str, str]] = {}
        for path in sorted(intake_dir.glob("**/*.json")):
            payload, _error = read_json_artifact(path)
            if not payload or payload.get("job_type") != "photo_capture":
                continue
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            body = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            capture_id = str(metadata.get("capture_id") or body.get("capture_id") or "").strip()
            if capture_id:
                submitters[capture_id] = safe_submitter(metadata, body)
        _SUBMITTERS_CACHE[intake_dir] = (time.monotonic(), dir_mtime, submitters)
        return submitters


def is_audio_file(path: Path) -> bool:
    mime = mimetypes.guess_type(path.name)[0] or ""
    return mime.startswith("audio/") or path.suffix.lower() in {".webm", ".m4a", ".mp3", ".wav", ".aac"}


IMAGE_MIME_PREFIX = "image/"


def is_audio_filename(filename: str, mime_type: str = "") -> bool:
    mime = mime_type or mimetypes.guess_type(filename)[0] or ""
    return mime.startswith("audio/") or Path(filename).suffix.lower() in {".webm", ".m4a", ".mp3", ".wav", ".aac"}


def is_image_filename(filename: str, mime_type: str = "") -> bool:
    mime = mime_type or mimetypes.guess_type(filename)[0] or ""
    return mime.startswith(IMAGE_MIME_PREFIX)


def _field_capture_couchdb_config() -> object | None:
    try:
        return couchdb_config.from_env()
    except couchdb_config.CouchDBConfigError as exc:
        logger.warning("field-capture CouchDB config unavailable for media discovery: %s", exc)
        return None


def _query_field_capture_docs(mango: dict[str, object]) -> list[dict[str, object]]:
    config = _field_capture_couchdb_config()
    if config is None:
        return []
    try:
        response = query_couchdb_find(config, couchdb_config.field_captures_database(), mango)
    except Exception as exc:
        logger.warning("field-capture CouchDB media discovery failed: %s", exc)
        return []
    docs = response.get("docs") if isinstance(response, dict) else None
    return [doc for doc in docs if isinstance(doc, dict)] if isinstance(docs, list) else []


def _query_field_capture_docs_all_result(
    fields: list[str], *, limit: int = 5000
) -> tuple[list[dict[str, object]], bool]:
    config = _field_capture_couchdb_config()
    if config is None:
        return [], False
    docs: list[dict[str, object]] = []
    bookmark: object = None
    for _ in range(400):
        mango: dict[str, object] = {
            "selector": {"type": "field_capture"},
            "fields": fields,
            "limit": limit,
        }
        if bookmark:
            mango["bookmark"] = bookmark
        try:
            response = query_couchdb_find(config, couchdb_config.field_captures_database(), mango)
        except Exception as exc:
            logger.warning("field-capture CouchDB media discovery failed: %s", exc)
            return docs, False
        page = response.get("docs") if isinstance(response, dict) else None
        if not isinstance(page, list):
            return docs, False
        docs.extend(doc for doc in page if isinstance(doc, dict))
        bookmark = response.get("bookmark")
        if len(page) < limit or not bookmark:
            return docs, True
    logger.warning("field-capture CouchDB media discovery hit the pagination safety limit")
    return docs, False


def _query_field_capture_docs_all(fields: list[str], *, limit: int = 5000) -> list[dict[str, object]]:
    docs, _available = _query_field_capture_docs_all_result(fields, limit=limit)
    return docs


_FIELD_CAPTURE_MEDIA_INVENTORY_CACHE_TTL_SECONDS = 300.0
_field_capture_media_inventory_cache: tuple[float, dict[str, object]] | None = None
_field_capture_media_inventory_lock = threading.Lock()


def field_capture_media_inventory() -> dict[str, object]:
    """Count durable capture-media references stored in canonical CouchDB docs.

    The local uploads directory is a bounded working cache and is pruned after
    its retention window.  This inventory is therefore the stable dashboard
    count operators should compare with the local cache size.
    """

    global _field_capture_media_inventory_cache

    def _fresh() -> dict[str, object] | None:
        cached = _field_capture_media_inventory_cache
        if cached is None or time.monotonic() - cached[0] >= _FIELD_CAPTURE_MEDIA_INVENTORY_CACHE_TTL_SECONDS:
            return None
        return cached[1]

    hit = _fresh()
    if hit is not None:
        return hit
    with _field_capture_media_inventory_lock:
        hit = _fresh()
        if hit is not None:
            return hit
        docs, available = _query_field_capture_docs_all_result(
            ["_id", "capture_id", "photos", "audio"]
        )
        image_count = 0
        audio_count = 0
        capture_count = 0
        for doc in docs:
            doc_media_count = 0
            photos = doc.get("photos")
            if isinstance(photos, list):
                image_count += len(photos)
                doc_media_count += len(photos)
            audio = doc.get("audio")
            if isinstance(audio, list):
                audio_count += len(audio)
                doc_media_count += len(audio)
            if doc_media_count:
                capture_count += 1
        result: dict[str, object] = {
            "available": available,
            "total_count": image_count + audio_count,
            "image_count": image_count,
            "audio_count": audio_count,
            "capture_count": capture_count,
        }
        _field_capture_media_inventory_cache = (time.monotonic(), result)
        return result


def reset_field_capture_media_inventory_cache() -> None:
    global _field_capture_media_inventory_cache
    with _field_capture_media_inventory_lock:
        _field_capture_media_inventory_cache = None


def _capture_doc_timestamp(doc: dict[str, object]) -> str:
    return str(doc.get("captured_at") or doc.get("exported_at") or doc.get("created_at") or doc.get("capture_id") or doc.get("_id") or "")


def _media_items(doc: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    items: list[tuple[str, dict[str, object]]] = []
    for media_type, raw in (("photo", doc.get("photos")), ("audio", doc.get("audio"))):
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict):
                items.append((media_type, item))
    return items


def _media_key(record: dict[str, object], upload_root: Path) -> str:
    for value in (record.get("upload_id"), record.get("stored_path")):
        if not value:
            continue
        try:
            return media_key_from_stored_path(value, upload_root)
        except ValueError:
            continue
    return ""


def _media_url_for_key(key: str, upload_root: Path, media_store: object | None = None) -> str:
    if not key:
        return ""
    try:
        local_store = LocalFilesystemStore(upload_root)
        if local_store.exists(key):
            return local_store.url_for(key)
        store = media_store if media_store is not None else get_media_store(upload_root)
        # Do not log the url_for result. S3 implementations return a fresh
        # per-request presigned URL and logs must contain only the key.
        return str(store.url_for(key))
    except Exception:
        logger.warning("media url resolution failed for key=%s", key)
        return ""


def _local_media_store(upload_root: Path, media_store: object | None = None) -> LocalFilesystemStore | None:
    try:
        store = media_store if media_store is not None else get_media_store(upload_root)
    except Exception as exc:
        logger.warning("media store resolution failed for local discovery fallback: %s", exc)
        return None
    return store if isinstance(store, LocalFilesystemStore) else None


def _capture_media_records(
    doc: dict[str, object],
    upload_root: Path,
    *,
    media_store: object | None = None,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    capture_id = str(doc.get("capture_id") or doc.get("_id") or "").strip()
    for media_type, item in _media_items(doc):
        key = _media_key(item, upload_root)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        filename = str(item.get("filename") or Path(key).name).strip()
        mime_type = str(item.get("mime_type") or mimetypes.guess_type(filename)[0] or "")
        records.append(
            {
                "capture_id": capture_id,
                "media_type": media_type,
                "filename": filename,
                "mime_type": mime_type,
                "key": key,
                "url": _media_url_for_key(key, upload_root, media_store),
                "size_bytes": int(item.get("size_bytes") or 0) if str(item.get("size_bytes") or "").isdigit() else 0,
            }
        )
    return records


def _local_capture_media_records(upload_root: Path, capture_id: str, store: LocalFilesystemStore) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for item in sorted(upload_root.glob(f"*/{capture_id}/*"), key=lambda path: path.name):
        if not item.is_file():
            continue
        mime_type = mimetypes.guess_type(item.name)[0] or ""
        if is_image_filename(item.name, mime_type):
            media_type = "photo"
        elif is_audio_filename(item.name, mime_type):
            media_type = "audio"
        else:
            continue
        try:
            key = item.relative_to(upload_root).as_posix()
        except ValueError:
            continue
        try:
            size_bytes = item.stat().st_size
        except OSError:
            size_bytes = 0
        records.append(
            {
                "capture_id": capture_id,
                "media_type": media_type,
                "filename": item.name,
                "mime_type": mime_type,
                "key": key,
                "url": str(store.url_for(key)),
                "size_bytes": size_bytes,
            }
        )
    return records


def field_capture_media_records(
    runtime_root: Path,
    capture_id: str,
    *,
    media_store: object | None = None,
) -> list[dict[str, object]]:
    upload_root = runtime_root.expanduser().resolve(strict=False) / "uploads"
    normalized = str(capture_id or "").strip()
    if not normalized:
        return []
    docs = _query_field_capture_docs(
        {
            "selector": {
                "type": "field_capture",
                "$or": [{"capture_id": normalized}, {"_id": normalized}],
            },
            "fields": ["_id", "capture_id", "photos", "audio"],
            "limit": 10,
        }
    )
    records: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for doc in docs:
        for record in _capture_media_records(doc, upload_root, media_store=media_store):
            key = str(record.get("key") or "")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            records.append(record)
    if not records:
        store = _local_media_store(upload_root, media_store)
        if store is not None:
            records = _local_capture_media_records(upload_root, normalized, store)
    return records


def _latest_uploads_from_local_filesystem(
    upload_root: Path,
    submitters: dict[str, dict[str, str]],
    limit: int,
) -> list[dict[str, object]]:
    if not upload_root.exists():
        return []
    captures: list[tuple[float, Path]] = []
    for path in upload_root.glob("*/*"):
        if path.is_dir():
            try:
                captures.append((path.stat().st_mtime, path))
            except OSError:
                continue
    captures.sort(reverse=True)
    items: list[dict[str, object]] = []
    for _mtime, path in captures[:limit]:
        submitter = submitters.get(path.name, safe_submitter({}))
        items.append(
            {
                "capture_id": path.name,
                "submitter_name": submitter["submitter_name"],
                "submitter_id": submitter["submitter_id"],
                "date": path.parent.name,
                "path": str(path),
                "file_count": count_files(path),
                "audio_count": sum(1 for item in path.glob("*") if item.is_file() and is_audio_file(item)),
                "image_count": sum(1 for item in path.glob("*") if item.is_file() and (mimetypes.guess_type(item.name)[0] or "").startswith("image/")),
            }
        )
    return items


def latest_uploads(
    upload_root: Path,
    submitters: dict[str, dict[str, str]] | None = None,
    limit: int = 5,
    *,
    media_store: object | None = None,
) -> list[dict[str, object]]:
    submitters = submitters or {}
    fields = [
        "_id",
        "capture_id",
        "created_at",
        "captured_at",
        "exported_at",
        "photos",
        "audio",
    ]
    captures = _query_field_capture_docs_all(fields)
    captures.sort(key=_capture_doc_timestamp, reverse=True)
    items: list[dict[str, object]] = []
    seen_capture_ids: set[str] = set()
    for doc in captures:
        if len(items) >= limit:
            break
        capture_id = str(doc.get("capture_id") or doc.get("_id") or "").strip()
        if not capture_id or capture_id in seen_capture_ids:
            continue
        records = _capture_media_records(doc, upload_root, media_store=media_store)
        if not records:
            continue
        seen_capture_ids.add(capture_id)
        first_key = str(records[0].get("key") or "")
        date = first_key.split("/", 1)[0] if "/" in first_key else ""
        submitter = submitters.get(capture_id, safe_submitter(doc))
        items.append(
            {
                "capture_id": capture_id,
                "submitter_name": submitter["submitter_name"],
                "submitter_id": submitter["submitter_id"],
                "date": date,
                "path": first_key.rsplit("/", 1)[0] if "/" in first_key else "",
                "file_count": len(records),
                "audio_count": sum(1 for record in records if is_audio_filename(str(record.get("filename") or ""), str(record.get("mime_type") or ""))),
                "image_count": sum(1 for record in records if is_image_filename(str(record.get("filename") or ""), str(record.get("mime_type") or ""))),
            }
        )
    if not items and _local_media_store(upload_root, media_store) is not None:
        return _latest_uploads_from_local_filesystem(upload_root, submitters, limit)
    return items


def latest_json_artifacts(path: Path, limit: int = 5) -> list[dict[str, object]]:
    if not path.exists():
        return []
    artifacts: list[tuple[float, Path]] = []
    for item in path.glob("*.json"):
        try:
            artifacts.append((item.stat().st_mtime, item))
        except OSError:
            continue
    artifacts.sort(reverse=True)
    return [{"name": item.name, "path": str(item), "modified_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat()} for mtime, item in artifacts[:limit]]


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def is_advisory_warning(value: str) -> bool:
    return value == field_photo_vision.ADVISORY_WARNING or value.startswith("Vision output is advisory interpretation")


def significant_warnings(value: object) -> list[str]:
    return [warning for warning in string_list(value) if not is_advisory_warning(warning)]


def first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0] if values else ""


def candidate_capture_sort_value(candidate: dict[str, object]) -> str:
    return str(candidate.get("captured_at") or candidate.get("capture_id") or candidate.get("candidate_id") or "")


def pending_candidate_counts_by_capture(candidates: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        if str(candidate.get("status") or "") not in ("pending_review", "pending_approval"):
            continue
        capture_id = str(candidate.get("capture_id") or "").strip()
        if capture_id:
            counts[capture_id] = counts.get(capture_id, 0) + 1
    return counts


def apply_pending_candidate_counts(candidates: list[dict[str, object]], counts: dict[str, int]) -> None:
    for candidate in candidates:
        if str(candidate.get("status") or "") not in ("pending_review", "pending_approval"):
            continue
        capture_id = str(candidate.get("capture_id") or "").strip()
        if capture_id:
            candidate["capture_candidate_count"] = counts.get(capture_id, 1)


def render_capture_candidate_signal(value: object) -> str:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 1:
        return ""
    label = f"{count} pending candidates from this capture"
    return f'<span class="pill capture-candidate-count">{html.escape(label)}</span>'


def parse_display_categories(raw: str) -> list[dict[str, str]]:
    if not raw.strip():
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("display_categories must be a JSON list")
    categories: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("display_categories entries must be objects")
        label = str(item.get("label") or "").strip()
        canonical = str(item.get("canonical") or "").strip()
        if not label or not canonical:
            raise ValueError("display_categories entries require label and canonical")
        categories.append({"label": label, "canonical": canonical})
    return categories


def parse_display_categories_rows(form: dict[str, list[str]], field_prefix: str) -> list[dict[str, str]]:
    labels = form.get(f"{field_prefix}_label", [])
    canonicals = form.get(f"{field_prefix}_canonical", [])
    categories: list[dict[str, str]] = []
    for raw_label, raw_canonical in zip(labels, canonicals):
        label = str(raw_label or "").strip()
        canonical = str(raw_canonical or "").strip()
        if not label and not canonical:
            continue
        if not label or not canonical:
            raise ValueError("display_categories entries require label and canonical")
        categories.append({"label": label, "canonical": canonical})
    return categories


def render_display_categories_editor(field_prefix: str, categories: list[dict]) -> str:
    rows = []
    for item in categories if isinstance(categories, list) else []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        canonical = str(item.get("canonical") or "")
        rows.append(
            f"""
            <div class="cat-editor-row">
              <input type="text" name="{html.escape(field_prefix)}_label" value="{html.escape(label)}">
              <input type="text" name="{html.escape(field_prefix)}_canonical" value="{html.escape(canonical)}">
              <button type="button" data-cat-remove>Remove</button>
            </div>
            """
        )
    if not rows:
        rows.append(
            f"""
            <div class="cat-editor-row">
              <input type="text" name="{html.escape(field_prefix)}_label" value="">
              <input type="text" name="{html.escape(field_prefix)}_canonical" value="">
              <button type="button" data-cat-remove>Remove</button>
            </div>
            """
        )
    prefix_json = json.dumps(field_prefix)
    return f"""
    <div class="cat-editor" data-field-prefix="{html.escape(field_prefix)}">
      {''.join(rows)}
      <button type="button" data-cat-add>Add row</button>
    </div>
    <script>
    (() => {{
      const prefix = {prefix_json};
      const scripts = document.getElementsByTagName("script");
      const container = scripts[scripts.length - 1].previousElementSibling;
      const addButton = container.querySelector("[data-cat-add]");
      const wireRemove = (button) => {{
        button.addEventListener("click", () => button.closest(".cat-editor-row").remove());
      }};
      container.querySelectorAll("[data-cat-remove]").forEach(wireRemove);
      addButton.addEventListener("click", () => {{
        const row = document.createElement("div");
        row.className = "cat-editor-row";
        row.innerHTML = `<input type="text" name="${{prefix}}_label" value=""> <input type="text" name="${{prefix}}_canonical" value=""> <button type="button" data-cat-remove>Remove</button>`;
        wireRemove(row.querySelector("[data-cat-remove]"));
        container.insertBefore(row, addButton);
      }});
    }})();
    </script>
    """


def display_categories_json(value: object) -> str:
    if not isinstance(value, list):
        return "[]"
    return json.dumps(value, indent=2, sort_keys=True)


def first_filter_value(query: dict[str, list[str]], key: str) -> str:
    return first_query_value(query, key).strip()


def tail_lines(path: Path, line_count: int = DEFAULT_LOG_LINES) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-line_count:]
    except OSError:
        return []


DEFAULT_LIST_EXCLUDE_KEYS = frozenset({"path", "artifact_path", "source_image_path", "vault_issue_path"})


class HtmlFragment(str):
    """Small marker for trusted HTML returned by dashboard render helpers."""


def humanize_key(key: object) -> str:
    text = str(key or "").strip()
    if not text:
        return ""
    abbreviations = {
        "fc": "FC",
        "id": "ID",
        "ids": "IDs",
        "json": "JSON",
        "url": "URL",
        "api": "API",
        "db": "DB",
    }
    words = re.split(r"[_\s-]+", text)
    return " ".join(abbreviations.get(word.lower(), word.capitalize()) for word in words if word)


def has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    text = str(value).strip()
    if not text:
        return False
    # The YAML loader sometimes surfaces "empty list" as the
    # literal string "[]" or "{}" when frontmatter is malformed.
    if text in ("[]", "{}", "None", "null"):
        return False
    return True


def _strip_wrap_quotes(text: str) -> str:
    """Strip a single pair of matching wrapping quotes (`"..."` or `'...'`)."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1]
    return text


def field_value(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(html.escape(str(item)) for item in value if str(item).strip())
    text = str(value)
    return html.escape(_strip_wrap_quotes(text))


def humanize(key: object) -> str:
    text = str(key or "").replace("_", " ").strip()
    return text.title() if text else str(key or "")


def field_rows(
    doc: dict[str, object],
    keys: tuple[str, ...],
    *,
    value_formatter: Callable[[object], str] = field_value,
) -> str:
    rows = []
    for key in keys:
        value = doc.get(key)
        if has_value(value):
            rows.append(
                f'<div class="field-row"><dt>{html.escape(humanize(key))}</dt>'
                f"<dd>{value_formatter(value)}</dd></div>"
            )
    return "".join(rows)


def record_section(
    title: str,
    doc: dict[str, object],
    keys: tuple[str, ...],
    *,
    actions_html: str = "",
    value_formatter: Callable[[object], str] = field_value,
    dl_class: str = "fields",
) -> str:
    rows = field_rows(doc, keys, value_formatter=value_formatter)
    if not rows:
        return ""
    return f'<section><h3>{html.escape(title)}</h3>{actions_html}<dl class="{html.escape(dl_class, quote=True)}">{rows}</dl></section>'


def other_section(
    doc: dict[str, object],
    ordered_keys: set[str],
    suppressed: set[str],
    *,
    title: str = "Other",
    value_formatter: Callable[[object], str] = field_value,
    dl_class: str = "fields",
) -> str:
    other_keys = tuple(
        sorted(
            key
            for key, value in doc.items()
            if key not in ordered_keys and key not in suppressed and has_value(value)
        )
    )
    return record_section(title, doc, other_keys, value_formatter=value_formatter, dl_class=dl_class)


def render_kv_value(value: object) -> str:
    if isinstance(value, HtmlFragment):
        return str(value)
    if isinstance(value, dict):
        rows = []
        for key, nested_value in value.items():
            rows.append(f"<tr><th>{html.escape(str(key))}</th><td>{render_kv_value(nested_value)}</td></tr>")
        return '<table class="kv-table nested-kv-table">' + "".join(rows) + "</table>"
    if isinstance(value, list):
        if not value:
            return "<ul></ul>"
        items = "".join(f"<li>{render_kv_value(item)}</li>" for item in value)
        return f"<ul>{items}</ul>"
    return html.escape(str(value))


def render_kv(mapping: dict[str, object], *, labels: dict[str, str] | None = None) -> str:
    rows = []
    for key, value in mapping.items():
        label = labels.get(key, humanize_key(key)) if labels is not None else str(key)
        rows.append(f"<tr><th>{html.escape(label)}</th><td>{render_kv_value(value)}</td></tr>")
    return '<table class="kv-table">' + "".join(rows) + "</table>"


def render_fields_value(value: object) -> str:
    if isinstance(value, HtmlFragment):
        return str(value)
    if value is None:
        return ""
    if isinstance(value, dict):
        return render_fields(value)
    if isinstance(value, list):
        if not value:
            return "<ul></ul>"
        items = "".join(f"<li>{render_fields_value(item)}</li>" for item in value)
        return f"<ul>{items}</ul>"
    return field_value(value)


def render_fields(mapping: dict[str, object], *, labels: dict[str, str] | None = None) -> str:
    rows = []
    for key, value in mapping.items():
        label = labels.get(key, humanize(key)) if labels is not None else humanize(key)
        rows.append(
            f'<div class="field-row"><dt>{html.escape(label)}</dt>'
            f"<dd>{render_fields_value(value)}</dd></div>"
        )
    return '<dl class="fields">' + "".join(rows) + "</dl>"


def render_count_badge(value: object, *, kind: str) -> HtmlFragment:
    count = int(value or 0)
    effective_kind = "neutral" if count == 0 else kind
    if effective_kind not in {"danger", "pending", "neutral"}:
        effective_kind = "neutral"
    return HtmlFragment(f'<span class="count-badge count-{effective_kind}">{html.escape(str(count))}</span>')


def _clean_display_part(value: object) -> str:
    return str(value or "").strip()


def render_site_label(
    site_id: object,
    *,
    site_name: object = None,
    account: object = None,
) -> str:
    site_id_text = _clean_display_part(site_id)
    if not site_id_text:
        return ""
    site_name_text = _clean_display_part(site_name)
    account_text = _clean_display_part(account)
    escaped_id = html.escape(site_id_text)
    id_html = f'<span class="site-id">({escaped_id})</span>'
    if account_text and site_name_text and account_text != site_name_text:
        visible = f"{html.escape(account_text)} — {html.escape(site_name_text)} {id_html}"
    elif site_name_text:
        visible = f"{html.escape(site_name_text)} {id_html}"
    else:
        visible = escaped_id
    return f'<span class="site-label">{visible}</span>'


def _location_account(site_id: str) -> str | None:
    cfg = couchdb_config.from_env()
    db_name = couchdb_config.vault_database()
    url = f"{cfg.base_url.rstrip('/')}/{quote(db_name, safe='')}/_find"
    payload = {
        "selector": {"_id": f"location_{site_id}", "type": "location"},
        "fields": ["_id", "account", "location", "site_id", "job", "type"],
        "limit": 1,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(cfg.auth_header())
    req = urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib_request.urlopen(req, timeout=cfg.timeout) as resp:
        response = json.loads(resp.read().decode("utf-8"))
    docs = response.get("docs") if isinstance(response, dict) else None
    if not isinstance(docs, list) or not docs or not isinstance(docs[0], dict):
        raise ValueError(f"missing location document for site {site_id}")
    account = _clean_display_part(docs[0].get("account"))
    return account or None


def resolve_site_label(site_id: object, vault_root: Path) -> str:
    site_id_text = _clean_display_part(site_id)
    if not site_id_text:
        return render_site_label(site_id)
    try:
        rows = CouchDBSiteRegistry().list_sites()
        site_name = next(
            (
                _clean_display_part(row.get("canonical"))
                for row in rows
                if isinstance(row, dict) and _clean_display_part(row.get("site_id")) == site_id_text
            ),
            "",
        )
        if not site_name:
            return render_site_label(site_id)
        name_counts = Counter(
            _clean_display_part(row.get("canonical"))
            for row in rows
            if isinstance(row, dict) and _clean_display_part(row.get("canonical"))
        )
        account = _location_account(site_id_text) if name_counts.get(site_name, 0) > 1 else None
    except Exception as exc:
        logger.warning("site label resolution failed site_id=%s: %s", site_id_text, exc)
        return render_site_label(site_id)
    return render_site_label(
        site_id,
        site_name=site_name,
        account=account,
    )


def slugify_status(value: object) -> str:
    """Sanitize a status value for use in status-* CSS class names."""
    return css_status_slug(value)


def render_status_transition(current: object, target: object) -> str:
    """Render a current -> target status transition as paired pills."""
    current_text = str(current or "").strip()
    target_text = str(target or "").strip()
    if not current_text and not target_text:
        return ""
    current_slug = slugify_status(current_text)
    target_slug = slugify_status(target_text)
    current_class = f" status-{current_slug}" if current_slug else ""
    target_class = f" status-{target_slug}" if target_slug else ""
    current_pill = f'<span class="pill{current_class}">{html.escape(current_text)}</span>' if current_text else ""
    target_pill = f'<span class="pill{target_class}">{html.escape(target_text)}</span>' if target_text else ""
    return f'<div class="status-transition">{current_pill}<span class="arrow" aria-hidden="true">→</span>{target_pill}</div>'


def render_back_link(href: str, label: str) -> str:
    """Render a back-to-list navigation link."""
    return f'<p class="back-link"><a href="{html.escape(href, quote=True)}">← {html.escape(label)}</a></p>'


def render_short_id(
    value: object,
    *,
    prefix_keep: int = 0,
    suffix_keep: int = 22,
) -> str:
    full_value = _clean_display_part(value)
    if not full_value:
        return ""
    escaped_full = html.escape(full_value)
    if len(full_value) <= prefix_keep + suffix_keep + 1:
        return escaped_full
    prefix = full_value[:prefix_keep] if prefix_keep else ""
    suffix = full_value[-suffix_keep:] if suffix_keep else ""
    short_value = f"{prefix}…{suffix}"
    return f'<span class="id-short" title="{escaped_full}">{html.escape(short_value)}</span>'


def _parse_relative_time_value(value: object, now: datetime) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, (int, float)):
        timestamp = datetime.fromtimestamp(now.timestamp() - float(value), timezone.utc)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _month_day(value: datetime, *, include_year: bool) -> str:
    text = f"{value:%b} {value.day}"
    if include_year:
        text = f"{text}, {value.year}"
    return text


def render_relative_time(value: object, *, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    timestamp = _parse_relative_time_value(value, current)
    if timestamp is None:
        return ""
    seconds = max(0, int((current - timestamp).total_seconds()))
    if seconds < 60:
        label = "just now"
    elif seconds < 3600:
        minutes = seconds // 60
        label = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif seconds < 86400:
        hours = seconds // 3600
        label = f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif seconds < 604800:
        days = seconds // 86400
        label = f"{days} day{'s' if days != 1 else ''} ago"
    else:
        label = _month_day(timestamp, include_year=timestamp.year != current.year)
    iso_value = timestamp.isoformat().replace("+00:00", "Z")
    absolute = f"{_month_day(timestamp, include_year=True)} {timestamp:%H:%M} UTC"
    return f'<time datetime="{html.escape(iso_value)}" title="{html.escape(absolute)}">{html.escape(label)}</time>'


def render_short_filename(filename: object) -> str:
    raw = _clean_display_part(filename)
    if not raw:
        return ""
    escaped_raw = html.escape(raw)
    if "T" in raw[:25] and "__" in raw[:25]:
        _prefix, slug_with_ext = raw.split("__", 1)
        if "." in slug_with_ext:
            slug = slug_with_ext.rsplit(".", 1)[0]
            if slug:
                return f'<span class="filename-short" title="{escaped_raw}">{html.escape(slug)}</span>'
    return escaped_raw


def _summary_payload(payload: object) -> dict[str, object]:
    return payload if isinstance(payload, dict) else {}


def _site_summary(payload: dict[str, object]) -> str:
    return render_site_label(
        payload.get("site_id") or payload.get("site"),
        site_name=payload.get("site_name"),
    )


def _join_summary_parts(*parts: str) -> str:
    return " ".join(part for part in parts if part)


def _summary_with_suffix(prefix: str, suffix: str) -> str:
    return f"{prefix}: {suffix}" if suffix else prefix


def _path_summary(value: object) -> str:
    raw = _clean_display_part(value)
    if not raw:
        return ""
    filename = Path(raw).name
    shortened = render_short_filename(filename)
    if shortened != html.escape(filename):
        return shortened
    return html.escape(raw)


def _unknown_job_summary(job_type: str, payload: dict[str, object]) -> str:
    label = html.escape(job_type or "Unknown job")
    fields = []
    for key, value in list(payload.items())[:3]:
        fields.append(f"{html.escape(str(key))}={html.escape(str(value))}")
    return f"{label}: {', '.join(fields)}" if fields else label


def _mark_job_summary(job_type: str, payload: dict[str, object], *, kind: str, id_key: str) -> str:
    status = job_type.removeprefix(f"mark_{kind}_")
    entity_id = render_short_id(payload.get(id_key))
    actor = html.escape(_clean_display_part(payload.get("actor")))
    suffix = _join_summary_parts(entity_id, html.escape(status), f"by {actor}" if actor else "")
    return _summary_with_suffix(f"Mark {kind}", suffix)


def render_job_summary(job_type: object, payload: object) -> str:
    """Render a one-line human summary of a queue job."""
    job_type_text = _clean_display_part(job_type)
    body = _summary_payload(payload)
    if job_type_text not in KNOWN_JOB_SUMMARY_TYPES:
        return _unknown_job_summary(job_type_text, body)

    if job_type_text == "append_to_note":
        suffix = _path_summary(body.get("path"))
        return _summary_with_suffix("Append to", suffix)
    if job_type_text == "add_person":
        suffix = _join_summary_parts(html.escape(_clean_display_part(body.get("name"))), f"({html.escape(_clean_display_part(body.get('role')))})" if _clean_display_part(body.get("role")) else "")
        return _summary_with_suffix("Add", suffix)
    if job_type_text == "assign_employee_site":
        employee = html.escape(_clean_display_part(body.get("employee_id")))
        site = html.escape(_clean_display_part(body.get("site_id")))
        action = _clean_display_part(body.get("action") or "assign")
        verb = "Assign site to" if action != "unassign" else "Unassign site from"
        suffix = _join_summary_parts(employee, f"site {site}" if site else "")
        return _summary_with_suffix(verb, suffix)
    if job_type_text == "set_employee_id":
        person = html.escape(_clean_display_part(body.get("person")))
        emp = html.escape(_clean_display_part(body.get("employee_id")))
        suffix = _join_summary_parts(person, f"(employee_id {emp})" if emp else "")
        return _summary_with_suffix("Set employee ID for", suffix)
    if job_type_text == "trigger_recruiting":
        suffix = _join_summary_parts(_site_summary(body), f"({html.escape(_clean_display_part(body.get('priority')))})" if _clean_display_part(body.get("priority")) else "")
        return _summary_with_suffix("Trigger recruiting at", suffix)
    if job_type_text == "close_recruiting":
        outcome = html.escape(_clean_display_part(body.get("outcome")))
        suffix = _join_summary_parts(_site_summary(body), f"({outcome})" if outcome else "")
        return _summary_with_suffix("Close recruiting at", suffix)
    if job_type_text == "remove_from_schedule":
        employee = html.escape(_clean_display_part(body.get("employee")))
        suffix = _join_summary_parts(employee, f"from {_site_summary(body)}" if _site_summary(body) else "")
        return _summary_with_suffix("Remove", suffix)
    if job_type_text == "flag_access_constraint":
        return _summary_with_suffix("Flag access constraint at", _site_summary(body))
    if job_type_text == "flag_retention_risk":
        employee = html.escape(_clean_display_part(body.get("employee")))
        suffix = _join_summary_parts(employee, f"at {_site_summary(body)}" if _site_summary(body) else "")
        return _summary_with_suffix("Flag retention risk for", suffix)
    if job_type_text == "reclassify_unknown":
        return _summary_with_suffix("Reclassify unknown at", _path_summary(body.get("path")))
    if job_type_text == "record_unknown_capture":
        return _summary_with_suffix("Record unknown capture at", _path_summary(body.get("path")))
    if job_type_text == "visit_create":
        return _summary_with_suffix("Create visit at", _site_summary(body))
    if job_type_text == "parse_supply_email":
        return _summary_with_suffix("Parse supply email", html.escape(_clean_display_part(body.get("subject"))))
    if job_type_text == "personal_journal_entry":
        return _summary_with_suffix("Personal journal for", html.escape(_clean_display_part(body.get("date"))))
    if job_type_text == "record_shift_report":
        date = html.escape(_clean_display_part(body.get("date")))
        prepared_by = html.escape(_clean_display_part(body.get("prepared_by")))
        suffix = _join_summary_parts(date, f"by {prepared_by}" if prepared_by else "")
        return _summary_with_suffix("Record shift report", suffix)
    if job_type_text == "shift_report_note":
        capture = html.escape(_clean_display_part(body.get("capture_id")))
        prompt_label = html.escape(_clean_display_part(body.get("prompt_label") or body.get("prompt_id")))
        suffix = _join_summary_parts(capture, f"({prompt_label})" if prompt_label else "")
        return _summary_with_suffix("Shift report note", suffix)
    if job_type_text == "record_day_record":
        return _summary_with_suffix("Record day record", html.escape(_clean_display_part(body.get("date"))))
    if job_type_text == "photo_capture":
        category = html.escape(_clean_display_part(body.get("qc_category")))
        suffix = _join_summary_parts(_site_summary(body), f"({category})" if category else "")
        return _summary_with_suffix("Photo capture at", suffix)
    if job_type_text == "deep_analysis":
        capture = html.escape(_clean_display_part(body.get("capture_id")))
        preset = html.escape(_clean_display_part(body.get("preset_id")))
        custom_prompt = html.escape(_clean_display_part(body.get("custom_prompt")))
        prompt = preset or ("custom" if custom_prompt else "")
        suffix = _join_summary_parts(f"({prompt})" if prompt else "", f"on capture {capture}" if capture else "")
        return _summary_with_suffix("Deep analysis", suffix)
    if job_type_text == "promote_prospect":
        prospect = html.escape(_clean_display_part(body.get("prospect_id")))
        site = html.escape(_clean_display_part(body.get("site_id")))
        suffix = _join_summary_parts(prospect, f"to site {site}" if site else "")
        return _summary_with_suffix("Promote prospect", suffix)
    if job_type_text == "retarget_capture":
        capture = html.escape(_clean_display_part(body.get("capture_id")))
        target_type = html.escape(_clean_display_part(body.get("new_target_type")))
        target_id = html.escape(_clean_display_part(body.get("new_target_id")))
        suffix = _join_summary_parts(capture, f"to {target_type} {target_id}" if target_type or target_id else "")
        return _summary_with_suffix("Retarget capture", suffix)
    if job_type_text == "log_site_issue":
        title = html.escape(_clean_display_part(body.get("title")))
        site = _site_summary(body)
        return _summary_with_suffix("Log site issue", f"{site}: {title}" if site and title else site or title)
    if job_type_text == "log_supply_need":
        item = html.escape(_clean_display_part(body.get("item_name")))
        site = _site_summary(body)
        return _summary_with_suffix("Log supply need", f"{site}: {item}" if site and item else site or item)
    if job_type_text == "log_equipment_request":
        equipment = html.escape(_clean_display_part(body.get("equipment_name")))
        site = _site_summary(body)
        return _summary_with_suffix("Log equipment request", f"{site}: {equipment}" if site and equipment else site or equipment)
    if job_type_text == "log_personnel_event":
        event_type = html.escape(_clean_display_part(body.get("event_type")))
        employee = html.escape(_clean_display_part(body.get("employee")))
        suffix = _join_summary_parts(f"({event_type})" if event_type else "", employee)
        return _summary_with_suffix("Log personnel event", suffix)
    if job_type_text == "log_availability_constraint":
        employee = html.escape(_clean_display_part(body.get("employee")))
        constraint_type = html.escape(_clean_display_part(body.get("constraint_type")))
        date = html.escape(_clean_display_part(body.get("date")))
        site = html.escape(_clean_display_part(body.get("related_site")))
        suffix = _join_summary_parts(
            f"({constraint_type})" if constraint_type else "",
            employee,
            f"on {date}" if date else "",
            f"at {site}" if site else "",
        )
        return _summary_with_suffix("Log availability constraint", suffix)
    if job_type_text == "set_entity_status":
        entity_type = html.escape(_clean_display_part(body.get("entity_type")))
        entity_id = html.escape(_clean_display_part(body.get("entity_id")))
        status = html.escape(_clean_display_part(body.get("status")))
        suffix = _join_summary_parts(entity_type, entity_id, f"to {status}" if status else "")
        return _summary_with_suffix("Set entity status", suffix)
    if job_type_text == "set_site_url":
        action = html.escape(_clean_display_part(body.get("action") or "update"))
        site = _site_summary(body)
        url = html.escape(_clean_display_part(body.get("new_url") or body.get("url")))
        suffix = _join_summary_parts(site, action, url)
        return _summary_with_suffix("Set site URL", suffix)
    if job_type_text == "set_site_hours":
        action = html.escape(_clean_display_part(body.get("action") or "set"))
        site = _site_summary(body)
        status = ""
        hours = body.get("facility_hours")
        if isinstance(hours, dict):
            status = html.escape(_clean_display_part(hours.get("status")))
        suffix = _join_summary_parts(site, action, status)
        return _summary_with_suffix("Set facility hours", suffix)
    if job_type_text == "set_contact":
        action = html.escape(_clean_display_part(body.get("action") or "upsert"))
        target = body.get("target")
        contact = body.get("contact")
        target_text = ""
        contact_text = ""
        if isinstance(target, dict):
            target_type = html.escape(_clean_display_part(target.get("type")))
            target_id = html.escape(_clean_display_part(target.get("id")))
            target_text = _join_summary_parts(target_type, target_id)
        if isinstance(contact, dict):
            contact_text = html.escape(_clean_display_part(contact.get("name") or contact.get("id")))
        suffix = _join_summary_parts(target_text, action, contact_text)
        return _summary_with_suffix("Set contact", suffix)
    if job_type_text == "update_site_equipment":
        site = _site_summary(body)
        equipment = body.get("equipment")
        count = len(equipment) if isinstance(equipment, list) else 0
        count_text = f"{count} item" if count == 1 else f"{count} items"
        return _summary_with_suffix("Update site equipment", f"{site}: {count_text}" if site else count_text)
    if job_type_text.startswith("mark_supply_"):
        return _mark_job_summary(job_type_text, body, kind="supply", id_key="supply_id")
    if job_type_text.startswith("mark_equipment_"):
        return _mark_job_summary(job_type_text, body, kind="equipment", id_key="equipment_id")
    if job_type_text.startswith("mark_issue_"):
        return _mark_job_summary(job_type_text, body, kind="issue", id_key="issue_id")
    if job_type_text in {"mark_record_archived", "mark_record_unarchived"}:
        action = "Archive record" if job_type_text == "mark_record_archived" else "Restore record"
        record_type = html.escape(_clean_display_part(body.get("record_type")))
        record_id = html.escape(_clean_display_part(body.get("record_id")))
        return _summary_with_suffix(action, _join_summary_parts(record_type, record_id))
    if job_type_text == "edit_record_fields":
        record_type = html.escape(_clean_display_part(body.get("record_type")))
        record_id = html.escape(_clean_display_part(body.get("record_id")))
        fields = body.get("fields")
        count = len(fields) if isinstance(fields, dict) else 0
        field_text = f"{count} field" if count == 1 else f"{count} fields"
        return _summary_with_suffix("Edit record", _join_summary_parts(record_type, record_id, field_text))
    if job_type_text == "voice_memo_note":
        note = _clean_display_part(body.get("note") or body.get("transcript_text"))[:60]
        suffix = f"({html.escape(note)})" if note else ""
        return _summary_with_suffix("Voice memo note", suffix)
    return _unknown_job_summary(job_type_text, body)


def render_collapsible_json(payload: object, *, summary_html: str, summary_label: str = "Raw JSON") -> str:
    """Wrap a JSON-serializable payload in a details disclosure."""
    try:
        raw_json = json.dumps(payload, indent=2, sort_keys=True)
    except TypeError:
        raw_json = json.dumps(str(payload), indent=2, sort_keys=True)
    return f"""
    <p class="job-summary">{summary_html}</p>
    <details class="raw-json">
      <summary>{html.escape(summary_label)}</summary>
      <pre>{html.escape(raw_json)}</pre>
    </details>
    """


def render_table(
    items: list[dict[str, object]],
    columns: list[dict[str, object]],
    *,
    empty_text: str = "Nothing to show.",
    table_class: str = "",
) -> str:
    """Render an HTML table from items and column specs."""
    if not items:
        return f'<p class="zero-state">{html.escape(empty_text)}</p>'

    classes = " ".join(part for part in ("data-table", table_class.strip()) if part)
    header_cells = []
    for column in columns:
        key = str(column.get("key") or "")
        label = str(column.get("label") or key.replace("_", " ").title())
        priority = int(column.get("priority") or 1)
        cell_class = str(column.get("class") or "")
        if column.get("nowrap"):
            cell_class = (cell_class + " td-nowrap").strip()
        class_attr = f' class="{html.escape(cell_class)}"' if cell_class else ""
        priority_attr = f' data-priority="{priority}"' if priority in {2, 3} else ""
        header_cells.append(f"<th{class_attr}{priority_attr}>{html.escape(label)}</th>")

    body_rows = []
    for item in items:
        cells = []
        for column in columns:
            key = str(column.get("key") or "")
            value = item.get(key, "")
            formatter = column.get("format")
            if callable(formatter):
                rendered_value = str(formatter(value, item))
            else:
                rendered_value = html.escape(str(value))
            priority = int(column.get("priority") or 1)
            cell_class = str(column.get("class") or "")
            if column.get("nowrap"):
                cell_class = (cell_class + " td-nowrap").strip()
            class_attr = f' class="{html.escape(cell_class)}"' if cell_class else ""
            priority_attr = f' data-priority="{priority}"' if priority in {2, 3} else ""
            cells.append(f"<td{class_attr}{priority_attr}>{rendered_value}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return f'<table class="{html.escape(classes)}"><thead><tr>{"".join(header_cells)}</tr></thead><tbody>{"".join(body_rows)}</tbody></table>'


def render_list(items: list[dict[str, object]], empty: str, *, exclude_keys: frozenset[str] = DEFAULT_LIST_EXCLUDE_KEYS) -> str:
    keys = sorted({key for item in items for key in item if key not in exclude_keys})
    columns = [{"key": key} for key in keys]
    return render_table(items, columns, empty_text=empty)


def render_issue_list(issues: list[object], empty_text: str) -> str:
    if not issues:
        return f"<p>{html.escape(empty_text)}</p>"
    items = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        badges = " ".join(
            f'<span class="pill">{html.escape(str(value))}</span>'
            for value in (
                issue.get("status"),
                issue.get("priority"),
                issue.get("category"),
                "client_notified" if issue.get("client_notified") else "client_not_notified",
            )
            if value
        )
        issue_id = str(issue.get("issue_id") or "")
        detail_href = f"/field-capture/issues?issue_id={quote(issue_id)}" if issue_id else "/field-capture/issues"
        title_link = f'<a href="{html.escape(detail_href)}">{html.escape(str(issue.get("title") or "Site issue"))}</a>'
        id_link = f'<a href="{html.escape(detail_href)}">{render_short_id(issue_id)}</a>' if issue_id else ""
        items.append(
            f"""<article class="card">
        <h3>{title_link}</h3>
        <p>{badges}</p>
        <p>{render_site_label(issue.get("site_id"), site_name=issue.get("site_name") or issue.get("site"), account=issue.get("account"))} | {id_link}</p>
        <p>{html.escape(str(issue.get("summary") or ""))}</p>
        <p><strong>Resolution trigger:</strong> {html.escape(str(issue.get("resolution_trigger") or ""))}</p>
        <p class="muted">{html.escape(str(issue.get("vault_issue_path") or ""))}</p>
      </article>"""
        )
    return "".join(items)


def write_mark_job(
    runtime_root: Path,
    *,
    job_type: str,
    payload_id_key: str,
    entity_id: str,
    actor: str,
    note: str = "",
    extra_payload: dict[str, object] | None = None,
) -> Path:
    suffix = str(uuid.uuid4())
    job = {
        "job_id": f"mark-{job_type}-{suffix}",
        "job_type": job_type,
        "payload": {
            payload_id_key: entity_id,
            "actor": actor,
        },
    }
    if extra_payload:
        job["payload"].update(extra_payload)
    if note:
        job["payload"]["note"] = note
    queue_dir = runtime_root.expanduser().resolve(strict=False) / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"mark-{job_type}-{suffix}.json"
    temp_path = queue_path.with_name(f".{queue_path.name}.tmp")
    temp_path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(queue_path)
    return queue_path


def write_edit_record_fields_job(
    runtime_root: Path,
    *,
    record_type: str,
    record_id: str,
    fields: dict[str, object],
    actor: str,
) -> Path:
    suffix = str(uuid.uuid4())
    job = {
        "job_id": f"edit-record-fields-{suffix}",
        "job_type": "edit_record_fields",
        "payload": {
            "record_type": record_type,
            "record_id": record_id,
            "fields": fields,
            "actor": actor,
        },
    }
    queue_dir = runtime_root.expanduser().resolve(strict=False) / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"edit-record-fields-{suffix}.json"
    temp_path = queue_path.with_name(f".{queue_path.name}.tmp")
    temp_path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(queue_path)
    return queue_path


def write_deep_analysis_job(
    runtime_root: Path,
    *,
    capture_id: str,
    photo_asset_id: str,
    actor: str,
    preset_id: str | None = None,
    custom_prompt: str | None = None,
) -> Path:
    from queue_spec import JOB_DEEP_ANALYSIS, validate_job

    suffix = str(uuid.uuid4())
    payload: dict[str, object] = {
        "capture_id": capture_id,
        "photo_asset_id": photo_asset_id,
        "actor": actor,
    }
    if preset_id is not None:
        payload["preset_id"] = preset_id
    if custom_prompt is not None:
        payload["custom_prompt"] = custom_prompt
    job = {
        "job_id": f"deep-analysis-{suffix}",
        "job_type": JOB_DEEP_ANALYSIS,
        "payload": payload,
    }
    if not validate_job(job):
        raise ValueError("invalid deep_analysis payload")
    queue_dir = runtime_root.expanduser().resolve(strict=False) / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"deep-analysis-{suffix}.json"
    temp_path = queue_path.with_name(f".{queue_path.name}.tmp")
    temp_path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(queue_path)
    return queue_path


def write_shift_report_note_job(
    queue_dir: Path,
    *,
    date: str,
    content: str,
    actor: str,
    capture_id: str,
    photo_asset_id: str,
    site_id: str = "",
    prompt_id: str = "",
    prompt_label: str = "",
) -> Path:
    from queue_spec import JOB_SHIFT_REPORT_NOTE, validate_job

    suffix = str(uuid.uuid4())
    payload: dict[str, object] = {
        "date": date,
        "content": content,
        "actor": actor,
        "capture_id": capture_id,
        "photo_asset_id": photo_asset_id,
    }
    for key, value in (
        ("site_id", site_id),
        ("prompt_id", prompt_id),
        ("prompt_label", prompt_label),
    ):
        if value:
            payload[key] = value
    idempotency_seed = json.dumps(
        {
            "date": date,
            "content": content,
            "capture_id": capture_id,
            "photo_asset_id": photo_asset_id,
            "prompt_id": prompt_id,
            "prompt_label": prompt_label,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(idempotency_seed.encode("utf-8")).hexdigest()[:32]
    job = {
        "job_id": f"shift-report-note-{suffix}",
        "job_type": JOB_SHIFT_REPORT_NOTE,
        "idempotency_key": f"shift-report-note:{digest}",
        "payload": payload,
    }
    if not validate_job(job):
        raise ValueError("invalid shift_report_note payload")
    queue_dir = queue_dir.expanduser().resolve(strict=False)
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"shift-report-note-{suffix}.json"
    temp_path = queue_path.with_name(f".{queue_path.name}.tmp")
    temp_path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(queue_path)
    return queue_path


def render_site_id_options(current_site_id: str) -> str:
    current = str(current_site_id or "").strip()
    options = ['<option value="">Select site</option>']
    rows = sorted(
        (site for site in SITES if str(site.get("site_id") or "").strip()),
        key=lambda site: (str(site.get("canonical") or site.get("name") or ""), str(site.get("site_id") or "")),
    )
    for site in rows:
        site_id = str(site.get("site_id") or "").strip()
        name = str(site.get("name") or site.get("canonical") or site_id)
        selected = " selected" if site_id == current else ""
        options.append(
            f'<option value="{html.escape(site_id, quote=True)}"{selected}>{html.escape(name)} ({html.escape(site_id)})</option>'
        )
    return "\n".join(options)


def render_record_edit_form(
    record: dict[str, object],
    *,
    record_type: str,
    id_field: str,
    post_route: str,
    fields: tuple[str, ...],
    select_options: dict[str, tuple[str, ...]] | None = None,
) -> str:
    record_id = str(record.get(id_field) or "").strip()
    if not record_id:
        return ""
    select_options = select_options or {}
    controls = [
        f"""
        <label>Site
          <select name="site_id" required>
            {render_site_id_options(str(record.get("site_id") or ""))}
          </select>
        </label>
        """
    ]
    for field in fields:
        if field == "site_id":
            continue
        value = html.escape(str(record.get(field) or ""), quote=True)
        label = html.escape(humanize_key(field))
        if field in select_options:
            options = []
            current = str(record.get(field) or "").strip()
            for option in select_options[field]:
                selected = " selected" if option == current else ""
                options.append(f'<option value="{html.escape(option, quote=True)}"{selected}>{html.escape(humanize_key(option))}</option>')
            controls.append(f"<label>{label}<select name=\"{html.escape(field)}\">{''.join(options)}</select></label>")
        elif field in {"summary", "notes", "reason", "resolution_trigger"}:
            controls.append(f'<label>{label}<textarea name="{html.escape(field)}">{html.escape(str(record.get(field) or ""))}</textarea></label>')
        elif field == "quantity_needed":
            controls.append(f'<label>{label}<input name="{html.escape(field)}" type="number" value="{value}"></label>')
        else:
            controls.append(f'<label>{label}<input name="{html.escape(field)}" value="{value}"></label>')
    return f"""
      <form method="post" action="{html.escape(post_route)}">
        <input type="hidden" name="{html.escape(id_field)}" value="{html.escape(record_id, quote=True)}">
        <input type="hidden" name="record_id" value="{html.escape(record_id, quote=True)}">
        <input type="hidden" name="_id" value="{html.escape(str(record.get('_id') or ''), quote=True)}">
        <input type="hidden" name="_rev" value="{html.escape(str(record.get('_rev') or ''), quote=True)}">
        <input type="hidden" name="record_type" value="{html.escape(record_type, quote=True)}">
        <input type="hidden" name="actor" value="{html.escape(default_actor(), quote=True)}">
        {''.join(controls)}
        <button type="submit">Save edit</button>
      </form>
    """


def _malformed_quality() -> dict[str, object]:
    return {
        "analyzable": False,
        "severity": "unusable",
        "flags": ["unanalyzable"],
        "confidence": None,
        "needs_human_review": True,
        "source": "derived",
    }


def photo_vision_sidecar_summary(path: Path, payload: dict[str, object] | None, error: str = "") -> dict[str, object]:
    if payload is None:
        return {
            "path": str(path),
            "name": path.name,
            "status": "malformed",
            "capture_id": "",
            "photo_asset_id": path.stem,
            "photo_id": "",
            "site_id": "",
            "submitted_area": "",
            "submitted_phase": "",
            "qc_category": "",
            "vision_category": None,
            "category_agreement": "",
            "captured_at": "",
            "source_image_path": "",
            "image_media_url": "",
            "area_guess": "",
            "description": "",
            "visible_objects": [],
            "possible_conditions": [],
            "possible_issues": [],
            "confidence": "",
            "model_name": "",
            "model_provider": "",
            "generated_at": "",
            "warnings": [error or "Malformed photo vision sidecar"],
            "error": {"type": "malformed", "message": error or "Malformed photo vision sidecar", "can_retry": False},
            "quality": _malformed_quality(),
        }
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    error_payload = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else None
    if quality is None:
        quality = field_photo_vision.derive_quality(payload)
    return {
        "path": str(path),
        "name": path.name,
        "status": str(payload.get("status") or ""),
        "capture_id": str(payload.get("capture_id") or provenance.get("capture_id") or ""),
        "photo_asset_id": str(payload.get("photo_asset_id") or provenance.get("photo_asset_id") or path.stem),
        "photo_id": str(payload.get("photo_id") or provenance.get("photo_id") or ""),
        "site_id": str(payload.get("site_id") or ""),
        "submitted_area": str(payload.get("submitted_area") or ""),
        "submitted_phase": str(payload.get("submitted_phase") or ""),
        "qc_category": str(payload.get("qc_category") or ""),
        "vision_category": payload.get("vision_category") if payload.get("vision_category") is not None else None,
        "category_agreement": str(payload.get("category_agreement") or ""),
        "captured_at": str(provenance.get("captured_at") or ""),
        "source_image_path": str(payload.get("source_image_path") or provenance.get("source_image_path") or ""),
        "image_media_url": str(provenance.get("image_media_url") or payload.get("image_media_url") or ""),
        "site_context_used": bool(payload.get("site_context_used", False)),
        "site_context_id": str(payload.get("site_context_id") or ""),
        "site_context_name": str(payload.get("site_context_name") or ""),
        "site_context_summary": str(payload.get("site_context_summary") or ""),
        "area_guess": str(payload.get("area_guess") or ""),
        "description": str(payload.get("description") or ""),
        "visible_objects": string_list(payload.get("visible_objects")),
        "possible_conditions": string_list(payload.get("possible_conditions")),
        "possible_issues": string_list(payload.get("possible_issues")),
        "confidence": "" if payload.get("confidence") is None else str(payload.get("confidence")),
        "model_name": str(payload.get("model_name") or ""),
        "model_provider": str(payload.get("model_provider") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "warnings": string_list(payload.get("warnings")),
        "error": error_payload,
        "quality": quality,
    }


_PHOTO_VISION_CACHE_TTL_SECONDS = 5.0
# cache[dir] = (cached_at_monotonic, dir_mtime, sidecar_summaries)
_PHOTO_VISION_CACHE: dict[Path, tuple[float, float, list[dict[str, object]]]] = {}
_PHOTO_VISION_CACHE_LOCK = threading.Lock()


def load_photo_vision_sidecars(photo_vision_dir: Path) -> list[dict[str, object]]:
    if not photo_vision_dir.exists():
        return []
    try:
        dir_mtime = photo_vision_dir.stat().st_mtime
    except OSError:
        dir_mtime = 0.0

    def _cached_if_fresh() -> list[dict[str, object]] | None:
        cached = _PHOTO_VISION_CACHE.get(photo_vision_dir)
        if cached is None:
            return None
        cached_at, cached_dir_mtime, cached_result = cached
        if cached_dir_mtime != dir_mtime or time.monotonic() - cached_at >= _PHOTO_VISION_CACHE_TTL_SECONDS:
            return None
        return cached_result

    fresh = _cached_if_fresh()
    if fresh is not None:
        return fresh

    # Serialize cold loads. Without this, concurrent /failed or /inbox
    # hits each scan and json-parse all ~2400 sidecar files in parallel,
    # spiking transient memory past the dashboard's RSS watchdog and
    # killing the daemon. The lock makes the first arriver fill the
    # cache while the rest wait, so peak memory is bounded.
    with _PHOTO_VISION_CACHE_LOCK:
        fresh = _cached_if_fresh()
        if fresh is not None:
            return fresh
        sidecars: list[tuple[float, dict[str, object]]] = []
        for path in photo_vision_dir.glob("*.json"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            payload, error = read_json_artifact(path)
            sidecars.append((mtime, photo_vision_sidecar_summary(path, payload, error)))
        sidecars.sort(key=lambda item: (item[0], str(item[1]["name"])), reverse=True)
        result = [summary for _mtime, summary in sidecars]
        _PHOTO_VISION_CACHE[photo_vision_dir] = (time.monotonic(), dir_mtime, result)
        return result


def photo_vision_status(runtime_root: Path) -> dict[str, object]:
    intake_dir = field_photo_vision.default_intake_dir(runtime_root)
    upload_dir = field_photo_vision.default_upload_dir(runtime_root)
    photo_vision_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
    assets = field_photo_vision.discover_photo_assets(intake_dir, upload_dir)
    asset_ids = {asset.photo_asset_id for asset in assets}
    sidecars = load_photo_vision_sidecars(photo_vision_dir)
    status_counts = {"completed": 0, "failed": 0, "skipped": 0, "malformed": 0}
    severity_counts: dict[str, int] = {"ok": 0, "degraded": 0, "unusable": 0}
    terminal_asset_ids: set[str] = set()
    recent_warnings: list[dict[str, object]] = []
    for sidecar in sidecars:
        status = str(sidecar.get("status") or "")
        if status in status_counts:
            status_counts[status] += 1
        else:
            status_counts.setdefault(status or "unknown", 0)
            status_counts[status or "unknown"] += 1
        if status in {"completed", "failed", "skipped"}:
            terminal_asset_ids.add(str(sidecar.get("photo_asset_id") or ""))
        quality = sidecar.get("quality")
        if isinstance(quality, dict):
            sev = str(quality.get("severity") or "")
            if sev in severity_counts:
                severity_counts[sev] += 1
        warnings = significant_warnings(sidecar.get("warnings"))
        if status in {"failed", "malformed"} or warnings:
            recent_warnings.append(
                {
                    "status": status,
                    "capture_id": sidecar.get("capture_id", ""),
                    "photo_asset_id": sidecar.get("photo_asset_id", ""),
                    "warnings": warnings[:3],
                    "path": sidecar.get("path", ""),
                }
            )
    return {
        "sidecar_count": len(sidecars),
        "image_count": len(asset_ids),
        "completed_count": status_counts.get("completed", 0),
        "failed_count": status_counts.get("failed", 0),
        "skipped_count": status_counts.get("skipped", 0),
        "malformed_count": status_counts.get("malformed", 0),
        "backlog_count": len(asset_ids - terminal_asset_ids),
        "warning_count": sum(1 for sidecar in sidecars if significant_warnings(sidecar.get("warnings"))),
        "severity_counts": severity_counts,
        "recent_warnings": recent_warnings[:PHOTO_VISION_RECENT_LIMIT],
    }


VOICE_MEMO_RECENT_LIMIT = 5


def _voice_memo_site_label(target_path: str) -> str:
    if target_path.startswith("location_"):
        return target_path.removeprefix("location_")
    parts = Path(target_path).parts
    if "Locations" in parts:
        index = parts.index("Locations")
        if index + 1 < len(parts):
            return parts[index + 1]
    return ""


def voice_memo_status(runtime_root: Path, audio_inbox_dir: Path | None) -> dict[str, object]:
    """Summarize voice memo processing from Pro-local artifacts.

    A voice memo lands as a vm-*.metadata.json intake sidecar in the audio
    inbox, becomes a voice_memo_note queue job, and finishes as a record in
    processed_index.jsonl once written to the vault.
    """
    intake_count = count_files(audio_inbox_dir, "vm-*.metadata.json") if audio_inbox_dir is not None else 0
    queued_count = count_files(runtime_root / "queue", "job_vm-*.json")
    failed_count = count_files(runtime_root / "failed", "job_vm-*.json")
    note_count = 0
    recent: list[dict[str, object]] = []
    index_path = runtime_root / "processed_index.jsonl"
    if index_path.exists():
        try:
            with open(index_path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "voice_memo_note" not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict) or record.get("job_type") != "voice_memo_note":
                        continue
                    note_count += 1
                    recent.append(
                        {
                            "capture_id": str(record.get("capture_id") or ""),
                            "site": _voice_memo_site_label(str(record.get("target_path") or "")),
                            "processed_at": str(record.get("timestamp") or ""),
                        }
                    )
        except OSError:
            pass
    return {
        "intake_count": intake_count,
        "queued_count": queued_count,
        "failed_count": failed_count,
        "note_count": note_count,
        "recent_notes": list(reversed(recent[-VOICE_MEMO_RECENT_LIMIT:])),
    }


VOICE_MEMO_INTAKE_STATES = ("pending", "claimed", "intake_done", "intake_failed")
_VOICE_MEMO_INTAKE_CACHE_TTL_SECONDS = 60.0
_voice_memo_intake_cache: dict[str, tuple[float, dict[str, object]]] = {}
_voice_memo_intake_lock = threading.Lock()


def _voice_memo_couchdb_config() -> VoiceMemoCouchDBConfig | None:
    url = (os.environ.get("VOICE_MEMO_COUCHDB_URL") or "").strip()
    if not url:
        return None
    return VoiceMemoCouchDBConfig(
        base_url=url.rstrip("/"),
        username=os.environ.get("VOICE_MEMO_COUCHDB_USER") or "",
        password=os.environ.get("VOICE_MEMO_COUCHDB_PASSWORD") or "",
        # Short timeout: a slow CouchDB must not stall the health page.
        timeout=4.0,
        database=os.environ.get("VOICE_MEMO_COUCHDB_DB") or "btq_voice_memos",
    )


def bucket_processing_states(docs: object) -> dict[str, int]:
    """Count voice memo docs by processing_state, with unknown states pooled
    under 'other'."""
    counts: dict[str, int] = {state: 0 for state in VOICE_MEMO_INTAKE_STATES}
    other = 0
    if isinstance(docs, list):
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            state = str(doc.get("processing_state") or "").strip()
            if state in counts:
                counts[state] += 1
            elif state:
                other += 1
    if other:
        counts["other"] = other
    return counts


def voice_memo_intake_state() -> dict[str, object]:
    """Live CouchDB counts of voice memos by intake processing_state.

    Cached briefly so the health page does not query CouchDB on every load.
    Degrades to available=False (never raises) when the voice memo CouchDB is
    not configured or unreachable, so the rest of the health page is safe.
    """
    config = _voice_memo_couchdb_config()
    if config is None:
        return {"available": False, "reason": "not configured", "counts": {}}
    cache_key = f"{config.base_url}/{config.database}"
    now = time.monotonic()
    with _voice_memo_intake_lock:
        cached = _voice_memo_intake_cache.get(cache_key)
        if cached is not None and now - cached[0] < _VOICE_MEMO_INTAKE_CACHE_TTL_SECONDS:
            return cached[1]
    selector = {
        "selector": {"processing_state": {"$exists": True}},
        "fields": ["processing_state"],
        "limit": 100000,
    }
    try:
        response = query_couchdb_find(config, config.database, selector)
        docs = response.get("docs") if isinstance(response, dict) else None
        counts = bucket_processing_states(docs)
        result: dict[str, object] = {"available": True, "counts": counts, "total": sum(counts.values())}
    except VoiceMemoCouchDBError:
        result = {"available": False, "reason": "couchdb unreachable", "counts": {}}
    with _voice_memo_intake_lock:
        _voice_memo_intake_cache[cache_key] = (time.monotonic(), result)
    return result


def reset_voice_memo_intake_cache() -> None:
    """Drop the cached intake state — used by tests."""
    with _voice_memo_intake_lock:
        _voice_memo_intake_cache.clear()


def photo_vision_by_capture(runtime_root: Path) -> dict[str, list[dict[str, object]]]:
    sidecars = load_photo_vision_sidecars(field_photo_vision.default_photo_vision_dir(runtime_root))
    by_capture: dict[str, list[dict[str, object]]] = {}
    for sidecar in sidecars:
        capture_id = str(sidecar.get("capture_id") or "")
        if capture_id:
            by_capture.setdefault(capture_id, []).append(sidecar)
    return by_capture


_HUMAN_SOURCE_CACHE_TTL_SECONDS = 5.0
# cache[dir] = (cached_at_monotonic, dir_mtime, source_details_by_capture)
_HUMAN_SOURCE_CACHE: dict[Path, tuple[float, float, dict[str, dict[str, str]]]] = {}
_HUMAN_SOURCE_CACHE_LOCK = threading.Lock()


def human_source_details_by_capture(runtime_root: Path) -> dict[str, dict[str, str]]:
    semantic_dir = runtime_root / "field_capture" / "semantics"
    if not semantic_dir.exists():
        return {}
    try:
        dir_mtime = semantic_dir.stat().st_mtime
    except OSError:
        dir_mtime = 0.0

    def _cached_if_fresh() -> dict[str, dict[str, str]] | None:
        cached = _HUMAN_SOURCE_CACHE.get(semantic_dir)
        if cached is None:
            return None
        cached_at, cached_dir_mtime, cached_result = cached
        if cached_dir_mtime != dir_mtime or time.monotonic() - cached_at >= _HUMAN_SOURCE_CACHE_TTL_SECONDS:
            return None
        return cached_result

    fresh = _cached_if_fresh()
    if fresh is not None:
        return fresh

    with _HUMAN_SOURCE_CACHE_LOCK:
        fresh = _cached_if_fresh()
        if fresh is not None:
            return fresh
        by_capture: dict[str, dict[str, str]] = {}
        for path in sorted(semantic_dir.glob("*.json")):
            payload, _error = read_json_artifact(path)
            if not payload:
                continue
            capture_id = str(payload.get("capture_id") or payload.get("upload_id") or path.stem).strip()
            source_text = str(payload.get("source_text") or "").strip()
            cleaned_internal_note = str(payload.get("cleaned_internal_note") or "").strip()
            human_text = source_text or cleaned_internal_note
            if not capture_id or not human_text:
                continue
            by_capture[capture_id] = {
                "source_text": human_text,
                "raw_source_text": source_text,
                "cleaned_internal_note": cleaned_internal_note,
                "semantic_artifact_path": str(path),
                "source_transcript_path": str(payload.get("source_transcript_path") or ""),
            }
        _HUMAN_SOURCE_CACHE[semantic_dir] = (time.monotonic(), dir_mtime, by_capture)
        return by_capture


def human_source_by_capture(runtime_root: Path) -> dict[str, str]:
    return {
        capture_id: details["source_text"]
        for capture_id, details in human_source_details_by_capture(runtime_root).items()
        if details.get("source_text")
    }


def safe_media_url(value: object) -> str:
    media_url = str(value or "")
    if media_url.startswith("/media/") and ".." not in media_url:
        return media_url
    return ""


def capture_thumbnails(runtime_root: Path, capture_id: str, *, media_store: object | None = None) -> list[str]:
    """Return store-resolved URLs for every image in a capture.

    Shared by the review surfaces (swipe card + candidates table) so a capture's
    photos can be shown as thumbnails next to the operator's decision.
    """
    urls: list[str] = []
    records = field_capture_media_records(runtime_root, capture_id, media_store=media_store)
    records.sort(key=lambda item: str(item.get("filename") or ""))
    for record in records:
        if not is_image_filename(str(record.get("filename") or ""), str(record.get("mime_type") or "")):
            continue
        url = str(record.get("url") or "")
        if url:
            urls.append(url)
    return urls


def audio_player(media_url: object) -> str:
    """Render an <audio> player for a /media/ URL (shared across sections)."""
    url = safe_media_url(media_url)
    if not url:
        return ""
    escaped = html.escape(url, quote=True)
    return f'<audio controls preload="none" src="{escaped}" style="width:180px;max-width:100%"></audio>'


LIGHTBOX_HTML = (
    '<div id="lb" role="dialog" aria-modal="true" onclick="this.style.display=\'none\'"'
    ' style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;cursor:zoom-out">'
    '<img id="lb-img" onclick="event.stopPropagation()"'
    ' style="max-width:92vw;max-height:92vh;position:absolute;top:50%;left:50%;'
    'transform:translate(-50%,-50%);border-radius:6px;box-shadow:0 4px 32px rgba(0,0,0,.6)" alt="full size photo">'
    "</div>"
    "<script>"
    "function openLb(src){var lb=document.getElementById('lb');document.getElementById('lb-img').src=src;lb.style.display='block'}"
    "document.addEventListener('keydown',function(e){if(e.key==='Escape'){document.getElementById('lb').style.display='none'}})"
    "</script>"
)


def render_thumb(url: str, *, size: int = 60) -> str:
    if not url:
        return ""
    escaped = html.escape(url, quote=True)
    js_arg = html.escape(json.dumps(url), quote=True)
    return (
        f'<a href="#" onclick="openLb({js_arg});return false" title="View full size" style="cursor:zoom-in">'
        f'<img src="{escaped}" width="{size}" height="{size}"'
        f' style="object-fit:cover;border-radius:4px" loading="lazy" alt="photo">'
        f"</a>"
    )


LIST_RETURN_SIGNAL_FIELD = "return"
LIST_RETURN_SIGNAL_VALUE = "list"
LIST_REDIRECT_PARAM_KEYS = ("status", "site_id", "sort", "archived")


def _return_to_list_requested(form: dict[str, list[str]]) -> bool:
    return first_query_value(form, LIST_RETURN_SIGNAL_FIELD).strip() == LIST_RETURN_SIGNAL_VALUE


def _list_redirect_location(redirect_path: str, form: dict[str, list[str]], *, message: str = "", error: str = "") -> str:
    params = [
        (key, value)
        for key in LIST_REDIRECT_PARAM_KEYS
        if (value := first_query_value(form, key).strip())
    ]
    if message:
        params.append(("message", message))
    if error:
        params.append(("error", error))
    query = urlencode(params)
    return f"{redirect_path}?{query}" if query else redirect_path


def handle_mark_transition_post(
    ctx: object,
    body: bytes,
    *,
    job_type: str,
    route: str,
    id_field: str,
    redirect_path: str,
    payload_id_key: str | None = None,
    extra_payload: dict[str, object] | None = None,
) -> tuple:
    """Shared POST handler for entity status-transition forms (supply, equipment, etc.).

    Validates the confirm checkbox, reads the entity id and actor from the form,
    stages a queue job via write_mark_job, and appends an audit record.

    Args:
        ctx: Request context with a ``runtime_root`` attribute.
        body: Raw POST body bytes.
        job_type: Queue job type string (e.g. ``"mark_supply_ordered"``).
        route: Route path for audit logging (e.g. ``"/supplies/mark-ordered"``).
        id_field: Form field name for the entity id (e.g. ``"supply_id"``).
        redirect_path: Base URL path for redirects (e.g. ``"/supplies"``).
    """
    from ops_dashboard import audit as _audit  # lazy to avoid circular import

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    root = Path(getattr(ctx, "runtime_root", Path("."))).expanduser().resolve(strict=False)
    form_payload = {key: values[0] if values else "" for key, values in form.items()}

    def _audit_append(result: str) -> None:
        _audit.append_audit(root, {"route": route, "actor": "localhost", "payload": form_payload, "result_summary": result})

    def _redirect(location: str) -> tuple:
        return 303, "text/html; charset=utf-8", f'<a href="{html.escape(location)}">Return</a>'.encode(), {"Location": location}

    return_to_list = _return_to_list_requested(form)

    if first_query_value(form, "confirm") != "1":
        _audit_append("failed: confirm_required")
        if return_to_list:
            return _redirect(_list_redirect_location(redirect_path, form, error="confirm_required"))
        return _redirect(f"{redirect_path}?error=confirm_required")

    entity_id = first_query_value(form, id_field).strip()
    actor = first_query_value(form, "actor").strip()
    note = first_query_value(form, "note").strip()

    if not entity_id or not actor:
        _audit_append(f"failed: missing {id_field} or actor")
        if return_to_list:
            return _redirect(_list_redirect_location(redirect_path, form, error="missing_field"))
        return _redirect(f"{redirect_path}?error=missing_field")

    try:
        queue_path = write_mark_job(
            root,
            job_type=job_type,
            payload_id_key=payload_id_key or id_field,
            entity_id=entity_id,
            actor=actor,
            note=note,
            extra_payload=extra_payload,
        )
    except Exception as exc:  # noqa: BLE001
        _audit_append(f"failed: {exc}")
        if return_to_list:
            return _redirect(_list_redirect_location(redirect_path, form, error=str(exc)))
        return _redirect(f"{redirect_path}?{id_field}={quote(entity_id)}&error={quote(str(exc))}")

    _audit_append(f"success: staged {id_field}={entity_id} queue_path={queue_path}")
    if return_to_list:
        return _redirect(_list_redirect_location(redirect_path, form, message="staged"))
    return _redirect(f"{redirect_path}?{id_field}={quote(entity_id)}&message=staged")


def handle_edit_record_fields_post(
    ctx: object,
    body: bytes,
    *,
    route: str,
    record_type: str,
    id_field: str,
    redirect_path: str,
    fields: tuple[str, ...],
) -> tuple:
    """Shared POST handler for canonical record edit forms."""
    from ops_dashboard import audit as _audit  # lazy to avoid circular import
    from queue_spec import validate_job

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    root = Path(getattr(ctx, "runtime_root", Path("."))).expanduser().resolve(strict=False)
    form_payload = {key: values[0] if values else "" for key, values in form.items()}

    def _audit_append(result: str) -> None:
        _audit.append_audit(root, {"route": route, "actor": "localhost", "payload": form_payload, "result_summary": result})

    def _redirect(location: str) -> tuple:
        return 303, "text/html; charset=utf-8", f'<a href="{html.escape(location)}">Return</a>'.encode(), {"Location": location}

    record_id = first_query_value(form, id_field).strip() or first_query_value(form, "record_id").strip()
    actor = first_query_value(form, "actor").strip() or default_actor()
    edit_fields = {
        field: first_query_value(form, field).strip()
        for field in fields
        if field in form
    }
    if not record_id or not actor or not edit_fields:
        _audit_append(f"failed: missing {id_field}, actor, or edit fields")
        return _redirect(f"{redirect_path}?{id_field}={quote(record_id)}&error=missing_field")

    job = {
        "job_type": "edit_record_fields",
        "payload": {
            "record_type": record_type,
            "record_id": record_id,
            "fields": edit_fields,
            "actor": actor,
        },
    }
    if not validate_job(job):
        _audit_append(f"failed: invalid edit_record_fields payload {record_id}")
        return _redirect(f"{redirect_path}?{id_field}={quote(record_id)}&error=invalid_payload")

    try:
        queue_path = write_edit_record_fields_job(
            root,
            record_type=record_type,
            record_id=record_id,
            fields=edit_fields,
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001
        _audit_append(f"failed: {exc}")
        return _redirect(f"{redirect_path}?{id_field}={quote(record_id)}&error={quote(str(exc))}")

    _audit_append(f"success: staged {id_field}={record_id} queue_path={queue_path}")
    return _redirect(f"{redirect_path}?{id_field}={quote(record_id)}&message=staged")
