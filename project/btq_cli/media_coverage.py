from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_reader import query_captures_by_site_id
from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from field_capture.photo_vision_couchdb import fetch_all_photo_vision_docs
from instance_config import get_instance_config
from media_store import get_media_store
from site_photo_viewer.read_model import (
    DATE_UNAVAILABLE,
    VISION_UNAVAILABLE,
    CapturePhotoProjection,
    SitePhotoCorpus,
    VisionByCaptureProjection,
)


class ReadOnlyMediaStore(Protocol):
    def exists(self, key: str) -> bool:
        ...


@dataclass(frozen=True)
class MediaCoverageReport:
    total_referenced_photos: int
    valid_media_keys: int
    invalid_media_keys: int
    r2_present: int
    r2_absent: int
    earliest_capture_date_overall: str | None
    latest_capture_date_overall: str | None
    earliest_capture_date_with_media: str | None
    latest_capture_date_with_media: str | None
    missing_by_month: Mapping[str, int]
    missing_by_site_id: Mapping[str, int]
    invalid_reference_photo_ids: tuple[str, ...]
    r2_absent_photo_ids: tuple[str, ...]
    orphan_vision_documents: int
    orphan_vision_document_ids: tuple[str, ...]
    photos_without_vision: int
    photos_without_vision_ids: tuple[str, ...]
    vision_without_photos: int
    vision_without_photo_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "total_referenced_photos": self.total_referenced_photos,
            "valid_media_keys": self.valid_media_keys,
            "invalid_media_keys": self.invalid_media_keys,
            "r2_present": self.r2_present,
            "r2_absent": self.r2_absent,
            "earliest_capture_date_overall": self.earliest_capture_date_overall,
            "latest_capture_date_overall": self.latest_capture_date_overall,
            "earliest_capture_date_with_media": self.earliest_capture_date_with_media,
            "latest_capture_date_with_media": self.latest_capture_date_with_media,
            "missing_by_month": dict(self.missing_by_month),
            "missing_by_site_id": dict(self.missing_by_site_id),
            "invalid_reference_photo_ids": list(self.invalid_reference_photo_ids),
            "r2_absent_photo_ids": list(self.r2_absent_photo_ids),
            "orphan_vision_documents": self.orphan_vision_documents,
            "orphan_vision_document_ids": list(self.orphan_vision_document_ids),
            "photos_without_vision": self.photos_without_vision,
            "photos_without_vision_ids": list(self.photos_without_vision_ids),
            "vision_without_photos": self.vision_without_photos,
            "vision_without_photo_ids": list(self.vision_without_photo_ids),
        }


def audit_media_coverage(
    *,
    captures_by_site_id: Mapping[str, Sequence[Mapping[str, object]]],
    vision_docs: Sequence[Mapping[str, object]],
    store: ReadOnlyMediaStore,
    upload_root: Path,
) -> MediaCoverageReport:
    capture_photos: list[CapturePhotoProjection] = []
    capture_dates: list[str] = []
    canonical_capture_ids: set[str] = set()
    for site_id in sorted(captures_by_site_id):
        captures = captures_by_site_id[site_id]
        for capture in captures:
            capture_id = str(capture.get("capture_id") or capture.get("_id") or "").strip()
            if capture_id:
                canonical_capture_ids.add(capture_id)
            capture_date = _capture_date(capture)
            if capture_date:
                capture_dates.append(capture_date)
        capture_photos.extend(
            CapturePhotoProjection.from_capture_rows(
                captures,
                site_id=site_id,
                site_label=site_id,
                upload_root=upload_root,
            )
        )

    grouped_vision: dict[str, list[Mapping[str, object]]] = {}
    for doc in vision_docs:
        capture_id = str(doc.get("capture_id") or "").strip()
        grouped_vision.setdefault(capture_id, []).append(doc)
    vision = VisionByCaptureProjection.from_mapping(
        grouped_vision,
        capture_ids=canonical_capture_ids,
    )
    corpus = SitePhotoCorpus.join(capture_photos, vision)

    invalid_ids: list[str] = []
    absent_ids: list[str] = []
    dates_with_media: list[str] = []
    missing_by_month: Counter[str] = Counter()
    missing_by_site: Counter[str] = Counter()
    present = 0
    absent = 0
    existence_by_key: dict[str, bool] = {}
    for photo in capture_photos:
        identity = _photo_identity(photo)
        if photo.media_key is None:
            invalid_ids.append(identity)
            continue
        if photo.media_key not in existence_by_key:
            existence_by_key[photo.media_key] = store.exists(photo.media_key)
        if existence_by_key[photo.media_key]:
            present += 1
            if photo.capture_date:
                dates_with_media.append(photo.capture_date)
            continue
        absent += 1
        absent_ids.append(identity)
        missing_by_month[photo.capture_date[:7] if photo.capture_date else DATE_UNAVAILABLE] += 1
        missing_by_site[photo.site_id] += 1

    photos_without_vision_ids = sorted(
        _record_identity(photo)
        for photo in corpus.photos
        if photo.vision_state == VISION_UNAVAILABLE
    )
    matched_vision_ids = {
        photo.vision_document_id
        for photo in corpus.photos
        if photo.vision_document_id
    }
    orphan_ids: list[str] = []
    vision_without_photo_ids: list[str] = []
    for index, doc in enumerate(vision_docs):
        capture_id = str(doc.get("capture_id") or "").strip()
        identity = _vision_identity(doc, index)
        if capture_id not in canonical_capture_ids:
            orphan_ids.append(identity)
        elif identity not in matched_vision_ids:
            vision_without_photo_ids.append(identity)

    return MediaCoverageReport(
        total_referenced_photos=len(capture_photos),
        valid_media_keys=sum(1 for photo in capture_photos if photo.media_key is not None),
        invalid_media_keys=len(invalid_ids),
        r2_present=present,
        r2_absent=absent,
        earliest_capture_date_overall=min(capture_dates) if capture_dates else None,
        latest_capture_date_overall=max(capture_dates) if capture_dates else None,
        earliest_capture_date_with_media=min(dates_with_media) if dates_with_media else None,
        latest_capture_date_with_media=max(dates_with_media) if dates_with_media else None,
        missing_by_month=dict(sorted(missing_by_month.items())),
        missing_by_site_id=dict(sorted(missing_by_site.items())),
        invalid_reference_photo_ids=tuple(sorted(invalid_ids)),
        r2_absent_photo_ids=tuple(sorted(absent_ids)),
        orphan_vision_documents=len(orphan_ids),
        orphan_vision_document_ids=tuple(sorted(orphan_ids)),
        photos_without_vision=len(photos_without_vision_ids),
        photos_without_vision_ids=tuple(photos_without_vision_ids),
        vision_without_photos=len(vision_without_photo_ids),
        vision_without_photo_ids=tuple(sorted(vision_without_photo_ids)),
    )


def audit_live_media_coverage(*, runtime_root: Path | None = None) -> MediaCoverageReport:
    instance = get_instance_config()
    if instance.media_store != "s3":
        raise ValueError("audit-media-coverage requires media_store 's3'")
    config = couchdb_config.from_env()
    registry = CouchDBSiteRegistry(
        base_url=config.base_url,
        username=config.username,
        password=config.password,
        database=instance.couchdb_vault_db,
        timeout=config.timeout,
    )
    site_ids = sorted(
        {
            str(row.get("site_id") or "").strip()
            for row in registry.list_sites()
            if str(row.get("site_id") or "").strip()
        }
    )
    captures = {
        site_id: query_captures_by_site_id(
            config,
            site_id,
            database=instance.couchdb_field_captures_db,
        )
        for site_id in site_ids
    }
    vision_docs = fetch_all_photo_vision_docs(
        config,
        database=instance.couchdb_photo_vision_db,
    )
    root = (runtime_root or get_config().runtime_root).expanduser().resolve(strict=False)
    upload_root = root / "uploads"
    store = get_media_store(upload_root, instance)
    return audit_media_coverage(
        captures_by_site_id=captures,
        vision_docs=vision_docs,
        store=store,
        upload_root=upload_root,
    )


def handle_audit_media_coverage(args: argparse.Namespace) -> int:
    try:
        report = audit_live_media_coverage(runtime_root=args.runtime_root)
    except Exception as exc:
        raise SystemExit(f"audit-media-coverage: {exc}") from exc
    payload = report.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("BTQ canonical capture media coverage (read-only)")
        for key, value in payload.items():
            print(f"{key}={value}")
    return 0


def _capture_date(capture: Mapping[str, object]) -> str:
    raw = str(
        capture.get("captured_at")
        or capture.get("exported_at")
        or capture.get("created_at")
        or ""
    ).strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _photo_identity(photo: CapturePhotoProjection) -> str:
    return f"{photo.site_id}:{photo.capture_id}:{photo.photo_id}"


def _record_identity(photo: object) -> str:
    return f"{getattr(photo, 'site_id')}:{getattr(photo, 'capture_id')}:{getattr(photo, 'photo_id')}"


def _vision_identity(doc: Mapping[str, object], index: int) -> str:
    return str(
        doc.get("_id")
        or doc.get("photo_asset_id")
        or f"{doc.get('capture_id') or 'unknown'}:{doc.get('photo_id') or index}"
    ).strip()
