from __future__ import annotations

import hashlib
import re
from typing import Any

from capture_ingest import (
    build_capture_document_envelope,
    normalize_capture_id,
    utc_now_iso,
)
from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_writer import (
    get_field_capture_document,
    put_field_capture_document,
)
from event_pipeline.sites import resolve_site_id


SOURCE_KINDS = frozenset({"email", "whatsapp", "sms", "manual"})


class CaptureAuthorError(ValueError):
    pass


def author_text_capture(
    *,
    text: str,
    site: str,
    source_kind: str,
    source_ref: str,
    person_id: str = "",
    person_name: str = "",
    captured_at: str | None = None,
    dry_run: bool = False,
    config: couchdb_config.CouchDBConfig | None = None,
) -> dict[str, Any]:
    """
    Build and optionally write a pending text-only field_capture.

    A text capture is pre-canonical intake: it preserves source text in `note`,
    carries source provenance, and then enters the existing review pipeline.
    """
    cleaned_text = _required("text", text)
    cleaned_site = _required("site", site)
    cleaned_source_kind = _normalize_source_kind(source_kind)
    cleaned_source_ref = _required("source_ref", source_ref)

    site_id = resolve_site_id(cleaned_site)
    if site_id is None:
        raise CaptureAuthorError(f"unresolved site: {cleaned_site}")

    capture_id = deterministic_capture_id(
        source_kind=cleaned_source_kind,
        source_ref=cleaned_source_ref,
    )
    now = utc_now_iso()
    captured_at_value = str(captured_at or "").strip() or now
    domain_fields: dict[str, object] = {
        "site": cleaned_site,
        "site_id": site_id,
        "target_type": "location",
        "target_id": site_id,
        "person_id": str(person_id or "").strip(),
        "person_name": str(person_name or "").strip(),
        "source": f"{cleaned_source_kind}_import",
        "source_kind": cleaned_source_kind,
        "source_ref": cleaned_source_ref,
        "qc_category": "",
        "note": cleaned_text,
        "captured_at": captured_at_value,
        "exported_at": now,
    }
    doc = build_capture_document_envelope(
        capture_id=capture_id,
        doc_type="field_capture",
        domain_fields=domain_fields,
        photos=[],
        audio=None,
        created_at=captured_at_value,
    )
    if dry_run:
        return doc

    write_config = config or couchdb_config.from_env()
    database = couchdb_config.field_captures_database()
    existing = get_field_capture_document(write_config, database, capture_id)
    if existing is not None:
        return {"skipped": "duplicate", "capture_id": capture_id}
    return put_field_capture_document(write_config, doc, database=database)


def deterministic_capture_id(*, source_kind: str, source_ref: str) -> str:
    cleaned_source_kind = _normalize_source_kind(source_kind)
    cleaned_source_ref = _required("source_ref", source_ref)
    digest = hashlib.sha256(cleaned_source_ref.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", cleaned_source_ref).strip(".-")
    slug = slug[:72].strip(".-")
    raw_capture_id = (
        f"{cleaned_source_kind}-{slug}-{digest}"
        if slug
        else f"{cleaned_source_kind}-{digest}"
    )
    return normalize_capture_id(raw_capture_id, fallback_prefix=f"{cleaned_source_kind}-")


def _normalize_source_kind(value: str) -> str:
    source_kind = str(value or "").strip().lower()
    if source_kind not in SOURCE_KINDS:
        choices = ", ".join(sorted(SOURCE_KINDS))
        raise CaptureAuthorError(f"source_kind must be one of: {choices}")
    return source_kind


def _required(name: str, value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise CaptureAuthorError(f"{name} is required")
    return cleaned
