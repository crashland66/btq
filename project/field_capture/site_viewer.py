from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote

from field_capture.identity import first_name_from_canonical
from field_capture.visual_context import safe_visual_context
from processing_core.artifacts import read_json_object
from processing_core.ids import deterministic_artifact_id
from processing_core.status import STATUS_COMPLETE


EXPECTED_AREAS = [
    "Entryways / Lobby / Doorways",
    "Windows / Glass / Sills / Ledges",
    "Hallways",
    "Common / Open Areas",
    "Restrooms",
    "Offices / Classrooms / Exam Rooms",
    "Break Rooms / Kitchens / Cafes",
    "Supply Levels",
    "Trash",
    "Touch Points",
    "Janitorial Closet",
    "Chemicals / Safety / PPE / SDS",
]


class SiteViewerError(Exception):
    pass


class SiteUploadsNotFound(SiteViewerError):
    pass


class UnsafeMediaPath(SiteViewerError):
    pass


@dataclass(frozen=True)
class SiteImage:
    url: str
    stored_path: str
    visual_context: str = ""


@dataclass(frozen=True)
class SiteAudio:
    url: str
    stored_path: str
    filename: str
    mime_type: str
    size_bytes: int
    duration_seconds: str
    transcript_status: str
    transcript_path: str
    raw_transcript: str
    semantic_status: str
    semantic_path: str
    semantic: dict[str, object] | None


@dataclass(frozen=True)
class SiteUpload:
    upload_id: str
    timestamp: str
    display_time: str
    area: str
    phase: str
    text_note: str
    images: list[SiteImage]
    audio: list[SiteAudio]
    person_id: str = ""
    person_name: str = ""


@dataclass(frozen=True)
class SiteDateGroup:
    date: str
    uploads: list[SiteUpload]


@dataclass(frozen=True)
class DateSummary:
    total_uploads: int
    total_images: int
    total_audio: int
    areas_present: list[str]
    phases_present: list[str]
    missing_areas: list[str]
    latest_upload_timestamp: str
    latest_upload_time: str


def build_site_payload(
    site_id: str,
    captures: list[dict[str, Any]],
    upload_dir: Path,
    transcript_dir: Path | None = None,
    semantic_dir: Path | None = None,
    site_status_path: Path | None = None,
    *,
    known_site_ids: Iterable[str] | None = None,
    site_name: str = "",
) -> dict[str, object]:
    groups = collect_site_uploads(site_id, captures, upload_dir, transcript_dir, semantic_dir)
    if not groups:
        if known_site_ids is None or site_id not in {str(item) for item in known_site_ids}:
            raise SiteUploadsNotFound(f"No uploads found for site {site_id}")
    review_export = load_site_status_export(site_id, site_status_path or default_site_status_path_for_upload_dir(upload_dir, site_id))
    reviewed_by_capture = reviewed_items_by_capture(review_export)
    dates_payload = [
        {
            "date": group.date,
            "summary": summary_as_payload(summarize_date_group(group)),
            "uploads": [
                upload_as_payload(upload, reviewed_by_capture.get(upload.upload_id))
                for upload in group.uploads
            ],
        }
        for group in groups
    ]
    important_items = important_items_for_view(dates_payload, review_export)
    return {
        "site_id": site_id,
        "site_name": site_name,
        "dates": dates_payload,
        "review_export": review_export,
        "important_items": important_items,
        "site_issues": review_export.get("site_issues") if isinstance(review_export.get("site_issues"), dict) else {},
    }


def build_prospect_payload(
    prospect: dict[str, Any],
    captures: list[dict[str, Any]],
    upload_dir: Path,
    transcript_dir: Path | None = None,
    semantic_dir: Path | None = None,
) -> dict[str, object]:
    prospect_id = str(prospect.get("prospect_id") or "").strip()
    groups = collect_prospect_uploads(prospect_id, captures, upload_dir, transcript_dir, semantic_dir)
    dates_payload = [
        {
            "date": group.date,
            "summary": summary_as_payload(summarize_date_group(group)),
            "uploads": [prospect_upload_as_payload(upload, prospect_id) for upload in group.uploads],
        }
        for group in groups
    ]
    return {
        "prospect_id": prospect_id,
        "prospect_name": str(prospect.get("name") or "").strip(),
        "prospect": {
            "prospect_id": prospect_id,
            "name": str(prospect.get("name") or "").strip(),
            "address": str(prospect.get("address") or "").strip(),
            "account": str(prospect.get("account") or "").strip(),
            "status": str(prospect.get("status") or "").strip(),
        },
        "dates": dates_payload,
        "review_export": {},
        "important_items": [],
        "site_issues": {},
    }


def prospect_upload_as_payload(upload: SiteUpload, prospect_id: str) -> dict[str, object]:
    payload = upload_as_payload(upload)
    suffix = f"?prospect_id={quote(prospect_id)}"
    images = payload.get("images")
    if isinstance(images, list):
        rewritten_images = [f"{url}{suffix}" if isinstance(url, str) and url.startswith("/media/") else url for url in images]
        payload["images"] = rewritten_images
        context = payload.get("image_visual_context")
        if isinstance(context, dict):
            payload["image_visual_context"] = {
                f"{url}{suffix}" if isinstance(url, str) and url.startswith("/media/") else url: value
                for url, value in context.items()
            }
    audio_items = payload.get("audio")
    if isinstance(audio_items, list):
        for item in audio_items:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str) and url.startswith("/media/"):
                item["url"] = f"{url}{suffix}"
    return payload


def collect_site_uploads(
    site_id: str,
    captures: list[dict[str, Any]],
    upload_dir: Path,
    transcript_dir: Path | None = None,
    semantic_dir: Path | None = None,
    *,
    visual_context_by_url: dict[str, str] | None = None,
) -> list[SiteDateGroup]:
    transcript_root = transcript_dir
    semantic_root = semantic_dir
    context_by_url = visual_context_by_url or {}
    uploads_by_id: dict[str, SiteUpload] = {}
    for capture in captures:
        if not isinstance(capture, dict):
            continue
        if str(capture.get("site_id") or "").strip() != site_id:
            continue
        job = job_from_capture_doc(capture)
        upload = upload_from_job(job, upload_dir, transcript_root, semantic_root, visual_context_by_url=context_by_url)
        if upload is None:
            continue
        existing = uploads_by_id.get(upload.upload_id)
        if existing is None:
            uploads_by_id[upload.upload_id] = upload
        else:
            latest_upload = max([existing, upload], key=sort_value_for_upload)
            uploads_by_id[upload.upload_id] = SiteUpload(
                upload_id=existing.upload_id,
                timestamp=latest_upload.timestamp,
                display_time=latest_upload.display_time,
                area=existing.area or upload.area,
                phase=existing.phase or upload.phase,
                text_note=existing.text_note or upload.text_note,
                images=existing.images + upload.images,
                audio=existing.audio + upload.audio,
                person_id=latest_upload.person_id or existing.person_id or upload.person_id,
                person_name=latest_upload.person_name or existing.person_name or upload.person_name,
            )

    uploads = sorted(uploads_by_id.values(), key=sort_value_for_upload, reverse=True)
    grouped: dict[str, list[SiteUpload]] = {}
    for upload in uploads:
        grouped.setdefault(date_from_timestamp(upload.timestamp), []).append(upload)
    return [SiteDateGroup(date=date, uploads=items) for date, items in grouped.items()]


def collect_prospect_uploads(
    prospect_id: str,
    captures: list[dict[str, Any]],
    upload_dir: Path,
    transcript_dir: Path | None = None,
    semantic_dir: Path | None = None,
    *,
    visual_context_by_url: dict[str, str] | None = None,
) -> list[SiteDateGroup]:
    transcript_root = transcript_dir
    semantic_root = semantic_dir
    context_by_url = visual_context_by_url or {}
    uploads_by_id: dict[str, SiteUpload] = {}
    for capture in captures:
        if not isinstance(capture, dict):
            continue
        job = job_from_capture_doc(capture)
        metadata = job.get("metadata")
        payload = job.get("payload")
        if not isinstance(metadata, dict) or not isinstance(payload, dict):
            continue
        if not job_matches_target("prospect", prospect_id, metadata, payload):
            continue
        upload = upload_from_job(job, upload_dir, transcript_root, semantic_root, visual_context_by_url=context_by_url)
        if upload is None:
            continue
        existing = uploads_by_id.get(upload.upload_id)
        if existing is None:
            uploads_by_id[upload.upload_id] = upload
        else:
            latest_upload = max([existing, upload], key=sort_value_for_upload)
            uploads_by_id[upload.upload_id] = SiteUpload(
                upload_id=existing.upload_id,
                timestamp=latest_upload.timestamp,
                display_time=latest_upload.display_time,
                area=existing.area or upload.area,
                phase=existing.phase or upload.phase,
                text_note=existing.text_note or upload.text_note,
                images=existing.images + upload.images,
                audio=existing.audio + upload.audio,
                person_id=latest_upload.person_id or existing.person_id or upload.person_id,
                person_name=latest_upload.person_name or existing.person_name or upload.person_name,
            )

    uploads = sorted(uploads_by_id.values(), key=sort_value_for_upload, reverse=True)
    grouped: dict[str, list[SiteUpload]] = {}
    for upload in uploads:
        grouped.setdefault(date_from_timestamp(upload.timestamp), []).append(upload)
    return [SiteDateGroup(date=date, uploads=items) for date, items in grouped.items()]


def read_json(path: Path) -> dict[str, object] | None:
    return read_json_object(path)


def job_from_capture_doc(doc: dict[str, Any]) -> dict[str, object]:
    return {
        "job_id": str(doc.get("_id") or doc.get("capture_id") or ""),
        "job_type": "photo_capture",
        "metadata": {
            "capture_id": str(doc.get("capture_id") or doc.get("_id") or ""),
            "site_id": str(doc.get("site_id") or ""),
            "person_id": str(doc.get("person_id") or ""),
            "person_name": str(doc.get("person_name") or ""),
            "target_type": str(doc.get("target_type") or ""),
            "target_id": str(doc.get("target_id") or ""),
        },
        "payload": {
            "site": str(doc.get("site") or ""),
            "target_type": str(doc.get("target_type") or ""),
            "target_id": str(doc.get("target_id") or ""),
            "qc_category": str(doc.get("qc_category") or doc.get("area") or ""),
            "phase": str(doc.get("phase") or ""),
            "note": str(doc.get("note") or ""),
            "captured_at": str(doc.get("captured_at") or ""),
            "exported_at": str(doc.get("exported_at") or ""),
            "photos": doc.get("photos") if isinstance(doc.get("photos"), list) else [],
            "audio": doc.get("audio") if isinstance(doc.get("audio"), list) else [],
        },
    }


def job_matches_site(site_id: str, metadata: dict[str, object], payload: dict[str, object]) -> bool:
    return str(metadata.get("site_id", "")).strip() == site_id or str(payload.get("site", "")).strip() == site_id


def job_matches_target(
    target_type: str,
    target_id: str,
    metadata: dict[str, object],
    payload: dict[str, object],
) -> bool:
    payload_target_type = str(payload.get("target_type") or "").strip()
    payload_target_id = str(payload.get("target_id") or "").strip()
    return payload_target_type == target_type and payload_target_id == target_id


def upload_from_job(
    job: dict[str, object],
    upload_dir: Path,
    transcript_dir: Path | None = None,
    semantic_dir: Path | None = None,
    *,
    visual_context_by_url: dict[str, str] | None = None,
) -> SiteUpload | None:
    metadata = job.get("metadata")
    payload = job.get("payload")
    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        return None
    photos = payload.get("photos")
    if not isinstance(photos, list):
        return None

    images: list[SiteImage] = []
    for photo in photos:
        if not isinstance(photo, dict):
            continue
        stored_path = str(photo.get("stored_path", "")).strip()
        if not stored_path:
            continue
        try:
            media_path = resolve_media_path(stored_path, upload_dir)
        except UnsafeMediaPath:
            continue
        if not media_path.exists() or not media_path.is_file():
            continue
        upload_id = upload_id_for_path(media_path, upload_dir)
        media_url = f"/media/{quote(upload_id)}"
        images.append(
            SiteImage(
                url=media_url,
                stored_path=str(media_path),
                visual_context=safe_visual_context((visual_context_by_url or {}).get(media_url)),
            )
        )

    first_asset_path = images[0].stored_path if images else ""
    upload_id = str(metadata.get("capture_id") or (Path(first_asset_path).parent.name if first_asset_path else "")).strip()
    audio_assets = audio_from_payload(payload, upload_dir, transcript_dir, semantic_dir, upload_id)
    if not images and not audio_assets:
        return None
    timestamp = str(payload.get("captured_at") or payload.get("exported_at") or "").strip()
    first_asset_path = images[0].stored_path if images else audio_assets[0].stored_path
    upload_id = upload_id or Path(first_asset_path).parent.name
    area = str(payload.get("area") or payload.get("qc_category") or "").strip()
    phase = normalize_phase(str(payload.get("phase") or metadata.get("phase") or payload.get("status") or payload.get("note") or "").strip())
    text_note = str(payload.get("note") or "").strip()
    return SiteUpload(
        upload_id=upload_id,
        timestamp=timestamp,
        display_time=display_time_from_timestamp(timestamp),
        area=area,
        phase=phase,
        text_note=text_note,
        images=images,
        audio=audio_assets,
        person_id=str(metadata.get("person_id") or "").strip(),
        person_name=str(metadata.get("person_name") or "").strip(),
    )


def audio_from_payload(
    payload: dict[str, object],
    upload_dir: Path,
    transcript_dir: Path | None = None,
    semantic_dir: Path | None = None,
    upload_id: str = "",
) -> list[SiteAudio]:
    audio_records = payload.get("audio")
    if not isinstance(audio_records, list):
        return []
    audio_assets: list[SiteAudio] = []
    for audio in audio_records:
        if not isinstance(audio, dict):
            continue
        stored_path = str(audio.get("stored_path", "")).strip()
        if not stored_path:
            continue
        try:
            media_path = resolve_media_path(stored_path, upload_dir)
        except UnsafeMediaPath:
            continue
        if not media_path.exists() or not media_path.is_file():
            continue
        media_upload_id = upload_id_for_path(media_path, upload_dir)
        asset_id = audio_asset_id(upload_id or media_upload_id, str(audio.get("filename") or media_path.name), str(media_path))
        transcript = transcript_for_audio(transcript_dir, asset_id)
        semantic = semantic_for_audio(semantic_dir, asset_id)
        audio_assets.append(
            SiteAudio(
                url=f"/media/{quote(media_upload_id)}",
                stored_path=str(media_path),
                filename=str(audio.get("filename") or media_path.name),
                mime_type=str(audio.get("mime_type") or ""),
                size_bytes=int(audio.get("size_bytes") or media_path.stat().st_size),
                duration_seconds=str(audio.get("duration_seconds") or ""),
                transcript_status=transcript.get("status", ""),
                transcript_path=transcript.get("path", ""),
                raw_transcript=transcript.get("raw_text", ""),
                semantic_status=str(semantic.get("status", "")),
                semantic_path=str(semantic.get("path", "")),
                semantic=semantic if semantic else None,
            )
        )
    return audio_assets


def default_transcript_dir(queue_dir: Path) -> Path:
    return queue_dir.expanduser().parent / "field_capture" / "audio_transcripts"


def default_semantic_dir(queue_dir: Path) -> Path:
    return queue_dir.expanduser().parent / "field_capture" / "audio_semantics"


def default_photo_vision_dir(queue_dir: Path) -> Path:
    return queue_dir.expanduser().parent / "field_capture" / "photo_vision"


def photo_vision_context_by_media_url(photo_vision_dir: Path) -> dict[str, str]:
    if not photo_vision_dir.exists():
        return {}
    descriptions: dict[str, str] = {}
    for path in sorted(photo_vision_dir.expanduser().glob("*.json")):
        payload = read_json(path)
        if not payload or str(payload.get("status") or "") != "completed":
            continue
        media_url = media_url_from_vision_payload(payload)
        description = safe_visual_context(payload.get("description"))
        if media_url and description:
            descriptions[media_url] = description
    return descriptions


def media_url_from_vision_payload(payload: dict[str, object]) -> str:
    for value in (
        payload.get("image_media_url"),
        (payload.get("provenance") or {}).get("image_media_url") if isinstance(payload.get("provenance"), dict) else "",
    ):
        media_url = str(value or "").strip()
        if media_url.startswith("/media/") and ".." not in media_url:
            return media_url
    return ""


def audio_asset_id(upload_id: str, filename: str, stored_path: str) -> str:
    return deterministic_artifact_id("fca", upload_id, filename, stored_path)


def transcript_for_audio(transcript_dir: Path | None, asset_id: str) -> dict[str, str]:
    if transcript_dir is None:
        return {}
    root = transcript_dir
    path = root / f"{asset_id}.json"
    payload = read_json_object(path)
    if payload is None:
        return {}
    status = str(payload.get("status") or "").strip()
    if not status:
        return {}
    return {
        "status": status,
        "path": str(path),
        "raw_text": str(payload.get("raw_text") or "").strip() if status == STATUS_COMPLETE else "",
    }


def semantic_for_audio(semantic_dir: Path | None, asset_id: str) -> dict[str, object]:
    if semantic_dir is None:
        return {}
    path = semantic_dir / f"{asset_id}.json"
    payload = read_json_object(path)
    if payload is None:
        return {}
    status = str(payload.get("status") or "").strip()
    if not status:
        return {}
    return {
        "status": status,
        "path": str(path),
        "cleaned_internal_note": str(payload.get("cleaned_internal_note") or ""),
        "client_safe_note": str(payload.get("client_safe_note") or ""),
        "operational_summary": str(payload.get("operational_summary") or ""),
        "issue_detected": bool(payload.get("issue_detected")),
        "issue_type": str(payload.get("issue_type") or ""),
        "urgency": str(payload.get("urgency") or ""),
        "suggested_tags": payload.get("suggested_tags") if isinstance(payload.get("suggested_tags"), list) else [],
        "action_candidates": payload.get("action_candidates") if isinstance(payload.get("action_candidates"), list) else [],
    }


def summarize_date_group(group: SiteDateGroup) -> DateSummary:
    areas_present = sorted({upload.area for upload in group.uploads if upload.area})
    phases_present = sorted({upload.phase for upload in group.uploads if upload.phase})
    latest_upload = max(group.uploads, key=sort_value_for_upload)
    return DateSummary(
        total_uploads=len(group.uploads),
        total_images=sum(len(upload.images) for upload in group.uploads),
        total_audio=sum(len(upload.audio) for upload in group.uploads),
        areas_present=areas_present,
        phases_present=phases_present,
        missing_areas=missing_expected_areas(areas_present),
        latest_upload_timestamp=latest_upload.timestamp,
        latest_upload_time=latest_upload.display_time,
    )


def summary_as_payload(summary: DateSummary) -> dict[str, object]:
    return {
        "total_uploads": summary.total_uploads,
        "total_images": summary.total_images,
        "total_audio": summary.total_audio,
        "areas_present": summary.areas_present,
        "phases_present": summary.phases_present,
        "missing_areas": summary.missing_areas,
        "latest_upload_timestamp": summary.latest_upload_timestamp,
        "latest_upload_time": summary.latest_upload_time,
    }


def upload_as_payload(upload: SiteUpload, reviewed_item: dict[str, object] | None = None) -> dict[str, object]:
    has_voice_transcript = any(audio.transcript_status == STATUS_COMPLETE and audio.raw_transcript.strip() for audio in upload.audio)
    has_text_note = bool(upload.text_note.strip())
    has_audio = bool(upload.audio)
    is_reviewed = reviewed_item is not None
    category = str(reviewed_item.get("review_type") or "") if reviewed_item else ""
    reviewed_context_by_url = reviewed_image_context_by_url(reviewed_item)
    image_visual_context = {
        image.url: safe_visual_context(image.visual_context or reviewed_context_by_url.get(image.url))
        for image in upload.images
        if safe_visual_context(image.visual_context or reviewed_context_by_url.get(image.url))
    }
    priority = upload_priority(
        is_reviewed=is_reviewed,
        category=category,
        has_audio=has_audio,
        has_text_note=has_text_note,
        has_voice_transcript=has_voice_transcript,
        timestamp=upload.timestamp,
    )
    return {
        "upload_id": upload.upload_id,
        "timestamp": upload.timestamp,
        "display_time": upload.display_time,
        "area": upload.area,
        "phase": upload.phase,
        "text_note": upload.text_note,
        "person_id": upload.person_id,
        "person_name": upload.person_name,
        "submitter_first_name": first_name_from_canonical(upload.person_name) if upload.person_name else "",
        "images": [image.url for image in upload.images],
        "image_visual_context": image_visual_context,
        "audio": [
            {
                "url": audio.url,
                "filename": audio.filename,
                "mime_type": audio.mime_type,
                "size_bytes": audio.size_bytes,
                "duration_seconds": audio.duration_seconds,
                "transcript": {
                    "status": audio.transcript_status,
                    "path": audio.transcript_path,
                    "raw_text": audio.raw_transcript,
                    "semantic": audio.semantic,
                }
                if audio.transcript_status
                else None,
                "semantic": audio.semantic,
            }
            for audio in upload.audio
        ],
        "image_count": len(upload.images),
        "audio_count": len(upload.audio),
        "is_issue": upload_matches_filter(upload, issue_only=True),
        "is_reviewed": is_reviewed,
        "review": reviewed_item,
        "review_type": category,
        "has_audio": has_audio,
        "has_text_note": has_text_note,
        "has_voice_transcript": has_voice_transcript,
        "has_context": has_audio or has_text_note or has_voice_transcript,
        "priority": priority,
    }


def reviewed_image_context_by_url(reviewed_item: dict[str, object] | None) -> dict[str, str]:
    if not reviewed_item:
        return {}
    media = reviewed_item.get("media")
    if not isinstance(media, list):
        return {}
    contexts: dict[str, str] = {}
    for record in media:
        if not isinstance(record, dict) or record.get("type") != "photo":
            continue
        media_url = str(record.get("media_url") or "").strip()
        context = safe_visual_context(record.get("visual_context") or record.get("vision_description") or record.get("alt_text"))
        if media_url.startswith("/media/") and ".." not in media_url and context:
            contexts[media_url] = context
    return contexts


def upload_priority(
    *,
    is_reviewed: bool,
    category: str,
    has_audio: bool,
    has_text_note: bool,
    has_voice_transcript: bool,
    timestamp: str,
) -> int:
    score = 0
    if is_reviewed:
        score += 100
    if has_voice_transcript:
        score += 25
    if has_audio:
        score += 20
    if has_text_note:
        score += 15
    if category in {"maintenance_issue", "supply_request", "staff_request"}:
        score += 10
    try:
        newest_component = int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        newest_component = 0
    return score * 10_000_000_000 + newest_component


def default_site_status_path(queue_dir: Path, site_id: str) -> Path:
    safe_site = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in site_id)
    return queue_dir.expanduser().parent / "field_capture" / "site_viewer_exports" / f"site_{safe_site}.json"


def default_site_status_path_for_upload_dir(upload_dir: Path, site_id: str) -> Path:
    safe_site = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in site_id)
    return upload_dir.expanduser().parent / "field_capture" / "site_viewer_exports" / f"site_{safe_site}.json"


def load_site_status_export(site_id: str, path: Path) -> dict[str, object]:
    payload = read_json(path)
    if not payload or payload.get("type") != "field_capture_site_viewer_status":
        return {}
    if str(payload.get("site_id") or "") != site_id:
        return {}
    reviewed_items = payload.get("reviewed_items")
    if not isinstance(reviewed_items, list):
        return {}
    safe_items = [sanitize_review_item(item) for item in reviewed_items if isinstance(item, dict)]
    safe_items = [item for item in safe_items if item is not None]
    return {
        "type": "field_capture_site_viewer_status",
        "site_id": site_id,
        "generated_at": str(payload.get("generated_at") or ""),
        "reviewed_items": safe_items,
        "site_issues": sanitize_site_issues(payload.get("site_issues")),
        "counts": payload.get("counts") if isinstance(payload.get("counts"), dict) else {},
    }


def sanitize_site_issues(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {"issues": [], "counts": {}, "warnings": []}
    issues = value.get("issues")
    safe_issues = [sanitize_site_issue(item) for item in issues if isinstance(item, dict)] if isinstance(issues, list) else []
    return {
        "issues": [item for item in safe_issues if item is not None],
        "counts": value.get("counts") if isinstance(value.get("counts"), dict) else {},
        "warnings": value.get("warnings") if isinstance(value.get("warnings"), list) else [],
    }


def sanitize_site_issue(item: dict[str, object]) -> dict[str, object] | None:
    issue_id = str(item.get("issue_id") or "").strip()
    site_id = str(item.get("site_id") or "").strip()
    title = str(item.get("title") or "").strip()
    if not issue_id or not site_id or not title:
        return None
    return {
        "issue_id": issue_id,
        "site_id": site_id,
        "site": str(item.get("site") or ""),
        "account": str(item.get("account") or ""),
        "title": title,
        "status": str(item.get("status") or "open"),
        "priority": str(item.get("priority") or "normal"),
        "category": str(item.get("category") or "other"),
        "client_notified": bool(item.get("client_notified")),
        "client_notified_at": str(item.get("client_notified_at") or ""),
        "client_notified_by": safe_visual_context(item.get("client_notified_by"), limit=120),
        "client_notified_method": safe_visual_context(item.get("client_notified_method"), limit=80),
        "reported_by": safe_visual_context(item.get("reported_by"), limit=120),
        "observed_at": str(item.get("observed_at") or ""),
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
        "summary": safe_visual_context(item.get("summary"), limit=520),
        "resolution_trigger": safe_visual_context(item.get("resolution_trigger"), limit=240),
        "related_capture_ids": [str(value) for value in item.get("related_capture_ids", []) if str(value).strip()]
        if isinstance(item.get("related_capture_ids"), list)
        else [],
        "related_candidate_ids": [str(value) for value in item.get("related_candidate_ids", []) if str(value).strip()]
        if isinstance(item.get("related_candidate_ids"), list)
        else [],
    }


def sanitize_review_item(item: dict[str, object]) -> dict[str, object] | None:
    capture_id = str(item.get("capture_id") or "").strip()
    candidate_id = str(item.get("candidate_id") or "").strip()
    if not capture_id or not candidate_id:
        return None
    review_type = str(item.get("review_type") or "other")
    media = item.get("media")
    safe_media = []
    if isinstance(media, list):
        for record in media:
            if not isinstance(record, dict):
                continue
            media_url = str(record.get("media_url") or "")
            if media_url.startswith("/media/") and ".." not in media_url:
                visual_context = safe_visual_context(record.get("visual_context") or record.get("vision_description") or record.get("alt_text"))
                safe_media.append(
                    {
                        "type": str(record.get("type") or ""),
                        "filename": str(record.get("filename") or ""),
                        "media_url": media_url,
                        "visual_context": visual_context,
                        "alt_text": visual_context,
                    }
                )
    return {
        "capture_id": capture_id,
        "candidate_id": candidate_id,
        "status": str(item.get("status") or ""),
        "review_type": review_type,
        "summary": str(item.get("summary") or ""),
        "display_title": str(item.get("display_title") or ""),
        "display_body": str(item.get("display_body") or ""),
        "source_context": str(item.get("source_context") or ""),
        "transcript_excerpt": str(item.get("transcript_excerpt") or ""),
        "text_note": str(item.get("text_note") or ""),
        "review_rationale": str(item.get("review_rationale") or ""),
        "reviewer": str(item.get("reviewer") or ""),
        "reviewed_at": str(item.get("reviewed_at") or ""),
        "submitter": str(item.get("submitter") or ""),
        "has_audio": bool(item.get("has_audio")),
        "has_text_note": bool(item.get("has_text_note")),
        "has_voice_transcript": bool(item.get("has_voice_transcript")),
        "client_informed": bool(item.get("client_informed")),
        "client_informed_at": str(item.get("client_informed_at") or ""),
        "client_informed_method": str(item.get("client_informed_method") or ""),
        "client_informed_by": safe_visual_context(item.get("client_informed_by"), limit=120),
        "client_informed_note": safe_visual_context(item.get("client_informed_note"), limit=500),
        "notification_history_count": int(item.get("notification_history_count") or 0),
        "media": safe_media,
        "priority": int(item.get("priority") or 0),
    }


def reviewed_items_by_capture(review_export: dict[str, object]) -> dict[str, dict[str, object]]:
    items = review_export.get("reviewed_items")
    if not isinstance(items, list):
        return {}
    by_capture: dict[str, dict[str, object]] = {}
    for item in items:
        if isinstance(item, dict):
            capture_id = str(item.get("capture_id") or "")
            existing = by_capture.get(capture_id)
            if capture_id and (existing is None or int(item.get("priority") or 0) > int(existing.get("priority") or 0)):
                by_capture[capture_id] = item
    return by_capture


def important_items_for_view(dates: list[dict[str, object]], review_export: dict[str, object]) -> list[dict[str, object]]:
    uploads_by_id: dict[str, dict[str, object]] = {}
    for date_group in dates:
        uploads = date_group.get("uploads")
        if not isinstance(uploads, list):
            continue
        for upload in uploads:
            if isinstance(upload, dict):
                uploads_by_id[str(upload.get("upload_id") or "")] = upload
    items: list[dict[str, object]] = []
    exported = review_export.get("reviewed_items")
    if isinstance(exported, list):
        for review in exported:
            if not isinstance(review, dict):
                continue
            upload = uploads_by_id.get(str(review.get("capture_id") or ""))
            items.append({"kind": "reviewed", "review": review, "upload": upload, "priority": int(review.get("priority") or 0)})
    items.sort(
        key=lambda item: (
            0 if item.get("kind") == "reviewed" else 1,
            -int(item.get("priority") or 0),
            str((item.get("upload") or {}).get("upload_id") if isinstance(item.get("upload"), dict) else ""),
        )
    )
    return items


def missing_expected_areas(areas_present: list[str]) -> list[str]:
    present = {area.lower() for area in areas_present}
    return [area for area in EXPECTED_AREAS if area.lower() not in present]


def normalize_phase(raw_phase: str) -> str:
    lowered = raw_phase.lower()
    if "issue" in lowered or "problem" in lowered or "needs attention" in lowered:
        return "issue"
    if "after" in lowered or "complete" in lowered or "done" in lowered or "final" in lowered:
        return "after"
    if "before" in lowered or "start" in lowered:
        return "before"
    return raw_phase.strip() or "unspecified"


def upload_matches_filter(upload: SiteUpload, *, area: str = "", phase: str = "", issue_only: bool = False) -> bool:
    if area and upload.area != area:
        return False
    if phase and upload.phase != phase:
        return False
    if issue_only and upload.phase != "issue":
        return False
    return True


def sort_value_for_upload(upload: SiteUpload) -> datetime:
    try:
        return datetime.fromisoformat(upload.timestamp.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


def display_time_from_timestamp(timestamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    hour = parsed.hour % 12 or 12
    minute = f"{parsed.minute:02d}"
    suffix = "AM" if parsed.hour < 12 else "PM"
    return f"{hour}:{minute} {suffix}"


def date_from_timestamp(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return "unknown-date"


def resolve_media_request(media_path: str, upload_dir: Path) -> Path:
    requested = unquote(media_path).lstrip("/")
    return resolve_media_path(str(upload_dir / requested), upload_dir)


def resolve_media_path(path: str, upload_dir: Path) -> Path:
    root = upload_dir.expanduser().resolve(strict=False)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UnsafeMediaPath(f"Media path is outside upload root: {resolved}") from exc
    return resolved


def upload_id_for_path(path: Path, upload_dir: Path) -> str:
    return path.resolve(strict=False).relative_to(upload_dir.expanduser().resolve(strict=False)).as_posix()


def render_site_page(site_id: str, payload: dict[str, object]) -> str:
    return render_target_page(site_id, payload, target_label="Site")


def render_prospect_page(prospect_id: str, payload: dict[str, object]) -> str:
    return render_target_page(prospect_id, payload, target_label="Prospect")


def render_target_page(target_id: str, payload: dict[str, object], *, target_label: str) -> str:
    dates = payload.get("dates")
    if not isinstance(dates, list):
        dates = []
    important_items = payload.get("important_items")
    if not isinstance(important_items, list):
        important_items = []
    areas = filter_values(dates, "area")
    phases = filter_values(dates, "phase")
    if target_label == "Prospect":
        target_name = str(payload.get("prospect_name") or "").strip()
    else:
        target_name = str(payload.get("site_name") or "").strip()
    site_issues = payload.get("site_issues") if isinstance(payload.get("site_issues"), dict) else {}
    heading = f"{target_name} Uploads" if target_name else f"{target_label} {target_id} Uploads"
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="robots" content="noindex,nofollow,noarchive" />
    <title>{html.escape(heading)}</title>
    <style>
      :root {{
        color-scheme: light;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #f6f7f9;
        color: #171a1f;
      }}
      body {{ margin: 0; }}
      main {{ width: min(1120px, calc(100vw - 32px)); margin: 0 auto; padding: 24px 0 48px; }}
      header {{ display: flex; justify-content: space-between; gap: 16px; align-items: end; margin-bottom: 24px; }}
      label {{ display: grid; gap: 5px; font-size: .82rem; color: #4f5b6b; }}
      select {{ min-height: 38px; border: 1px solid #c9d0dc; border-radius: 6px; padding: 0 9px; background: white; }}
      h1, h2, h3, p {{ margin: 0; }}
      h1 {{ font-size: clamp(1.45rem, 4vw, 2rem); }}
      h2 {{ margin: 26px 0 8px; font-size: 1.05rem; color: #3a4656; }}
      .filters {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 18px; align-items: end; }}
      .toggle {{ display: flex; align-items: center; gap: 8px; min-height: 38px; color: #26313f; }}
      .important {{ border: 1px solid #c8d8ee; background: #f8fbff; border-radius: 8px; padding: 14px; margin: 0 0 18px; }}
      .important h2 {{ margin-top: 0; color: #26313f; }}
      .important-empty {{ margin-top: 10px; color: #596578; }}
      .important-list {{ display: grid; gap: 10px; }}
      .important-card {{ border: 1px solid #d5deea; border-left: 4px solid #376fb7; border-radius: 8px; padding: 12px; background: white; }}
      .important-card h3 {{ margin: 0 0 8px; font-size: 1.05rem; }}
      .important-card p {{ margin: 6px 0 0; color: #3a4656; }}
      .important-card[data-kind="context"] {{ border-left-color: #6b7b8f; }}
      .important-media {{ margin: 10px 0 8px; }}
      .important-context {{ margin-top: 10px; padding: 10px; border: 1px solid #d9dee7; border-radius: 6px; background: #f8fafc; }}
      .secondary-meta {{ font-size: .82rem; color: #687386; }}
      .summary {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 10px; color: #4b5565; }}
      .coverage {{ margin: 0 0 12px; font-size: .9rem; color: #755b17; }}
      .raw-stream > h2 {{ margin-top: 0; }}
      .upload {{ padding: 14px 12px 18px; border: 1px solid #d9dee7; border-radius: 8px; margin: 10px 0; background: white; }}
      .empty-state {{ border: 1px solid #d9dee7; border-radius: 8px; background: white; padding: 28px 24px; color: #3a4656; }}
      .empty-state h2 {{ margin: 0 0 8px; color: #26313f; }}
      .upload[data-phase="issue"] {{ border-color: #d44848; box-shadow: inset 3px 0 0 #d44848; }}
      .upload[data-phase="after"] {{ border-color: #48a05d; box-shadow: inset 3px 0 0 #48a05d; }}
      .meta {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; color: #4b5565; }}
      .pill {{ border: 1px solid #cfd6e2; border-radius: 999px; padding: 5px 9px; background: white; font-size: .86rem; }}
      .pill-reviewed {{ border-color: #376fb7; background: #e9f2ff; color: #174a86; }}
      .pill-context {{ border-color: #8ba4c0; background: #f0f5fb; color: #31465f; }}
      .pill.pill-submitter {{ background: #eef4ff; border-color: #93c5fd; color: #1e40af; }}
      .phase-issue {{ background: #ffe8e8; border-color: #d44848; color: #8f2222; }}
      .phase-after {{ background: #e8f6ec; border-color: #48a05d; color: #206d33; }}
      .phase-before {{ background: #eef1f5; }}
      .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }}
      .image-preview {{ display: grid; gap: 6px; }}
      .grid button {{ display: block; width: 100%; padding: 0; border: 0; background: #e7ebf1; border-radius: 6px; overflow: hidden; aspect-ratio: 1; cursor: zoom-in; }}
      .visual-context {{ margin: 0; font-size: .78rem; line-height: 1.3; color: #596578; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
      .back-row {{ margin: 0 0 8px; font-size: .9rem; }}
      .back-link {{ color: #1e40af; text-decoration: none; display: inline-block; padding: 4px 8px; border: 1px solid #c7d2e6; border-radius: 6px; background: #f0f5fb; }}
      .back-link:hover {{ background: #e3edfa; }}
      .audio-player {{ display: grid; gap: 6px; margin-top: 12px; color: #3a4656; font-size: .9rem; }}
      .audio-player span {{ font-weight: 650; }}
      .audio-player audio {{ width: 100%; }}
      .transcript {{ margin: 8px 0 0; padding: 10px; border: 1px solid #d9dee7; border-radius: 6px; background: #f8fafc; }}
      .transcript strong {{ display: block; margin-bottom: 6px; color: #26313f; }}
      .transcript pre {{ margin: 0; white-space: pre-wrap; font: inherit; color: #3a4656; }}
      .semantic {{ margin: 8px 0 0; padding: 10px; border: 1px solid #cdd7e3; border-radius: 6px; background: #fbfcff; }}
      .semantic h4 {{ margin: 0 0 8px; color: #26313f; font-size: .95rem; }}
      .semantic dl {{ display: grid; gap: 6px; margin: 0; }}
      .semantic dt {{ font-weight: 700; color: #3a4656; }}
      .semantic dd {{ margin: 0 0 4px; }}
      .semantic ul {{ margin: 0; padding-left: 18px; }}
      img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
      dialog {{ width: min(96vw, 1100px); border: 0; padding: 0; background: transparent; }}
      dialog::backdrop {{ background: rgba(12, 18, 28, .72); }}
      .modal-frame {{ display: grid; gap: 10px; }}
      .modal-frame img {{ width: 100%; max-height: 86vh; object-fit: contain; background: #10151f; border-radius: 8px; }}
      .modal-frame button {{ justify-self: end; min-height: 36px; border-radius: 6px; border: 1px solid #cfd6e2; background: white; }}
      @media (max-width: 540px) {{
        main {{ width: min(100vw - 20px, 640px); padding-top: 16px; }}
        header {{ display: block; }}
        .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      }}
    </style>
  </head>
  <body>
    <main>
      <header>
        <div>
          <p class="back-row"><a class="back-link" href="/">&larr; Back to capture</a></p>
          <h1>{html.escape(heading)}</h1>
          <p>Submitted photos will appear here during the shift.</p>
        </div>
      </header>
      <section class="filters" aria-label="Upload filters">
        <label>
          Area
          <select id="areaFilter">
            <option value="">All areas</option>
            {render_filter_options(areas)}
          </select>
        </label>
        <label>
          Phase
          <select id="phaseFilter">
            <option value="">All phases</option>
            {render_filter_options(phases)}
          </select>
        </label>
        <label class="toggle">
          <input id="issueFilter" type="checkbox" />
          Issues only
        </label>
        <label>
          Importance
          <select id="importanceFilter">
            <option value="">All</option>
            <option value="reviewed">Reviewed</option>
            <option value="context">With context</option>
            <option value="maintenance">Maintenance / Issues</option>
            <option value="requests">Supplies / Requests</option>
          </select>
        </label>
      </section>
      {render_open_issues(site_issues)}
      {render_important_items(important_items)}
      {render_raw_capture_stream(dates)}
      <dialog id="imageModal">
        <div class="modal-frame">
          <button id="closeModal" type="button">Close</button>
          <img id="modalImage" alt="Full field-capture upload" />
        </div>
      </dialog>
    </main>
    <script>
      // Preserve the token query string so "Back to capture" returns
      // to the capture form authenticated.
      (function () {{
        const backLink = document.querySelector(".back-link");
        if (backLink && window.location.search) {{
          backLink.href = "/" + window.location.search;
        }}
      }})();
      const areaFilter = document.getElementById("areaFilter");
      const phaseFilter = document.getElementById("phaseFilter");
      const issueFilter = document.getElementById("issueFilter");
      const importanceFilter = document.getElementById("importanceFilter");
      const uploads = Array.from(document.querySelectorAll(".upload"));
      const importantCards = Array.from(document.querySelectorAll(".important-card"));
      function importanceVisible(element, value) {{
        if (!value) return true;
        if (value === "reviewed") return element.dataset.reviewed === "true";
        if (value === "context") return element.dataset.context === "true";
        if (value === "maintenance") return element.dataset.reviewType === "maintenance_issue" || element.dataset.reviewType === "maintenance" || element.dataset.phase === "issue";
        if (value === "requests") return element.dataset.reviewType === "supply_request" || element.dataset.reviewType === "staff_request" || element.dataset.reviewType === "supply" || element.dataset.reviewType === "client_request";
        return true;
      }}
      function applyFilters() {{
        const area = areaFilter.value;
        const phase = phaseFilter.value;
        const issuesOnly = issueFilter.checked;
        const importance = importanceFilter.value;
        uploads.forEach((upload) => {{
          const visible = (!area || upload.dataset.area === area)
            && (!phase || upload.dataset.phase === phase)
            && (!issuesOnly || upload.dataset.phase === "issue")
            && importanceVisible(upload, importance);
          upload.hidden = !visible;
        }});
        importantCards.forEach((card) => {{
          card.hidden = !importanceVisible(card, importance);
        }});
      }}
      areaFilter.addEventListener("change", applyFilters);
      phaseFilter.addEventListener("change", applyFilters);
      issueFilter.addEventListener("change", applyFilters);
      importanceFilter.addEventListener("change", applyFilters);

      const modal = document.getElementById("imageModal");
      const modalImage = document.getElementById("modalImage");
      document.querySelectorAll("[data-full-image]").forEach((button) => {{
        button.addEventListener("click", () => {{
          modalImage.src = button.dataset.fullImage;
          modalImage.alt = button.dataset.imageAlt || "Full field-capture upload";
          if (modal.showModal) modal.showModal();
          else window.open(button.dataset.fullImage, "_blank", "noopener");
        }});
      }});
      document.getElementById("closeModal").addEventListener("click", () => modal.close());
      modal.addEventListener("click", (event) => {{
        if (event.target === modal) modal.close();
      }});
    </script>
  </body>
</html>
"""


def render_raw_capture_stream(dates: list[object]) -> str:
    return f"""<section class="raw-stream" aria-label="Raw capture stream">
        <h2>Raw Capture Stream</h2>
        {render_date_groups(dates)}
      </section>"""


def render_open_issues(site_issues: dict[str, object]) -> str:
    issues = site_issues.get("issues") if isinstance(site_issues.get("issues"), list) else []
    current = [issue for issue in issues if isinstance(issue, dict) and str(issue.get("status") or "") in {"open", "monitoring"}]
    if not current:
        return ""
    cards = []
    for issue in current:
        title = html.escape(str(issue.get("title") or "Open site issue"))
        issue_id = html.escape(str(issue.get("issue_id") or ""))
        status = html.escape(str(issue.get("status") or "open"))
        priority = html.escape(str(issue.get("priority") or "normal"))
        category = html.escape(str(issue.get("category") or "other").replace("_", " ").title())
        summary = html.escape(str(issue.get("summary") or ""))
        resolution_trigger = html.escape(str(issue.get("resolution_trigger") or ""))
        reported_by = html.escape(str(issue.get("reported_by") or ""))
        observed_at = html.escape(str(issue.get("observed_at") or ""))
        capture_ids = issue.get("related_capture_ids") if isinstance(issue.get("related_capture_ids"), list) else []
        candidate_ids = issue.get("related_candidate_ids") if isinstance(issue.get("related_candidate_ids"), list) else []
        badges = [
            f'<span class="pill">{status.title()}</span>',
            f'<span class="pill">{priority.title()}</span>',
            f'<span class="pill">{category}</span>',
        ]
        if issue.get("client_notified"):
            method = str(issue.get("client_notified_method") or "").replace("_", " ")
            badges.append('<span class="pill pill-reviewed">Client Informed</span>')
            if method:
                badges.append(f'<span class="pill pill-context">Client informed by {html.escape(method)}</span>')
        else:
            badges.append('<span class="pill pill-context">Client not yet informed</span>')
        related_html = ""
        if capture_ids or candidate_ids:
            related_html = (
                '<p class="secondary-meta">'
                + " | ".join(
                    [
                        *(f"Capture {html.escape(str(value))}" for value in capture_ids),
                        *(f"Candidate {html.escape(str(value))}" for value in candidate_ids),
                    ]
                )
                + "</p>"
            )
        cards.append(
            f"""<article class="important-card" data-kind="issue" data-reviewed="true" data-context="true" data-review-type="{html.escape(str(issue.get("category") or ""))}">
          <h3>{title}</h3>
          <div class="meta">
            <span class="pill">{issue_id}</span>
            {''.join(badges)}
          </div>
          {f'<div class="important-context">{summary}</div>' if summary else ''}
          {f'<p><strong>Resolution trigger:</strong> {resolution_trigger}</p>' if resolution_trigger else ''}
          <p class="secondary-meta">{' | '.join(part for part in [f'Reported by {reported_by}' if reported_by else '', f'Observed {observed_at}' if observed_at else ''] if part)}</p>
          {related_html}
        </article>"""
        )
    return f"""<section class="important" aria-label="Open site issues">
        <h2>Open Site Issues</h2>
        <p>Internal-only structured site issue records from the operational vault.</p>
        <div class="important-list">{''.join(cards)}</div>
      </section>"""


def render_date_groups(dates: list[object]) -> str:
    if not dates:
        return """<div class="empty-state" aria-live="polite">
        <h2>No captures submitted yet.</h2>
        <p>Submitted photos and voice notes will appear here during the shift.</p>
      </div>"""
    sections: list[str] = []
    for date_group in dates:
        if not isinstance(date_group, dict):
            continue
        date = html.escape(str(date_group.get("date", "")))
        uploads = date_group.get("uploads")
        if not isinstance(uploads, list):
            uploads = []
        summary = date_group.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        sections.append(f"<section><h2>{date}</h2>{render_summary(summary)}{render_uploads(uploads)}</section>")
    return "\n      ".join(sections)


def render_important_items(items: list[object]) -> str:
    cards = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "reviewed")
        if kind != "reviewed":
            continue
        review = item.get("review") if isinstance(item.get("review"), dict) else {}
        upload = item.get("upload") if isinstance(item.get("upload"), dict) else {}
        capture_id = str(review.get("capture_id") or upload.get("upload_id") or "")
        summary = str(review.get("display_title") or review.get("review_rationale") or review.get("summary") or "Reviewed item - media not found")
        body = str(review.get("display_body") or review.get("source_context") or review.get("transcript_excerpt") or review.get("text_note") or "")
        review_type = str(review.get("review_type") or upload.get("review_type") or "")
        area = str(upload.get("area") or "")
        display_time = str(upload.get("display_time") or upload.get("timestamp") or "")
        submitter = str(upload.get("submitter_first_name") or "").strip()
        reviewed = bool(review)
        context = bool(review.get("has_audio") or review.get("has_text_note") or review.get("has_voice_transcript") or body)
        images = upload.get("images") if isinstance(upload.get("images"), list) else []
        image_context = upload.get("image_visual_context") if isinstance(upload.get("image_visual_context"), dict) else {}
        media = review.get("media") if isinstance(review.get("media"), list) else []
        image_records = [
            {"url": str(url), "visual_context": safe_visual_context(image_context.get(str(url)))}
            for url in images
        ]
        if not image_records:
            image_records = [
                {
                    "url": str(record.get("media_url")),
                    "visual_context": safe_visual_context(record.get("visual_context") or record.get("vision_description") or record.get("alt_text")),
                }
                for record in media
                if isinstance(record, dict) and record.get("type") == "photo"
            ]
        badges = []
        if reviewed:
            badges.append('<span class="pill pill-reviewed">Reviewed</span>')
        if context:
            badges.append('<span class="pill pill-context">With context</span>')
        if upload.get("has_audio") or review.get("has_audio"):
            badges.append('<span class="pill pill-context">Voice note</span>')
        if upload.get("has_text_note") or review.get("has_text_note"):
            badges.append('<span class="pill pill-context">Text note</span>')
        if upload.get("has_voice_transcript") or review.get("has_voice_transcript"):
            badges.append('<span class="pill pill-context">Transcript</span>')
        if review_type:
            badges.append(f'<span class="pill">{html.escape(review_type.replace("_", " ").title())}</span>')
        if review.get("client_informed"):
            method = str(review.get("client_informed_method") or "").replace("_", " ")
            badges.append('<span class="pill pill-reviewed">Client Informed</span>')
            if method:
                badges.append(f'<span class="pill pill-context">{html.escape(f"Client informed by {method}")}</span>')
        elif review_type in {"maintenance_issue", "supply_request"}:
            badges.append('<span class="pill pill-context">Client not yet informed</span>')
        media_html = ""
        if image_records:
            media_html = (
                '<div class="grid important-media">'
                + "\n".join(render_image_preview(record["url"], record["visual_context"], fallback_image_alt(capture_id)) for record in image_records)
                + "</div>"
            )
        else:
            media_html = f'<p class="important-context"><strong>Reviewed item - media not found</strong><br>{html.escape(capture_id)}</p>'
        body_html = f'<div class="important-context">{html.escape(body)}</div>' if body else ""
        secondary_bits = [f"Candidate {html.escape(str(review.get('candidate_id') or ''))}"]
        if submitter:
            secondary_bits.append(f"Submitter {html.escape(submitter)}")
        if review.get("client_informed_at"):
            secondary_bits.append(f"Client informed at {html.escape(str(review.get('client_informed_at') or ''))}")
        if review.get("client_informed_by"):
            secondary_bits.append(f"Client informed by {html.escape(str(review.get('client_informed_by') or ''))}")
        cards.append(
            f"""<article class="important-card" data-kind="{html.escape(kind)}" data-reviewed="{str(reviewed).lower()}" data-context="{str(context).lower()}" data-review-type="{html.escape(review_type)}" data-phase="{html.escape(str(upload.get("phase") or ""))}">
          <h3>{html.escape(summary)}</h3>
          <div class="meta">
            <span class="pill">{html.escape(capture_id)}</span>
            {f'<span class="pill">{html.escape(area)}</span>' if area else ''}
            {f'<span class="pill">{html.escape(display_time)}</span>' if display_time else ''}
            {''.join(badges)}
          </div>
          {media_html}
          {body_html}
          <p class="secondary-meta">{' | '.join(secondary_bits)}</p>
        </article>"""
        )
    cards_html = (
        '<div class="important-list">' + "".join(cards) + "</div>"
        if cards
        else '<p class="important-empty">No reviewed or important items yet.</p>'
    )
    return f"""<section class="important" aria-label="Reviewed and important field-capture items">
        <h2>Reviewed / Important Items</h2>
        <p>Internal-only reviewed and contextual capture signals from the Mac review workflow.</p>
        {cards_html}
      </section>"""


def render_summary(summary: dict[str, object]) -> str:
    missing = summary.get("missing_areas")
    if not isinstance(missing, list):
        missing = []
    missing_text = ", ".join(html.escape(str(area)) for area in missing[:6])
    if len(missing) > 6:
        missing_text = f"{missing_text}, +{len(missing) - 6} more"
    coverage = f'<p class="coverage">Missing today: {missing_text}</p>' if missing_text else '<p class="coverage">Expected areas covered.</p>'
    return f"""<div class="summary">
        <span class="pill">{html.escape(str(summary.get("total_uploads", 0)))} uploads</span>
        <span class="pill">{html.escape(str(summary.get("total_images", 0)))} images</span>
        <span class="pill">{html.escape(str(summary.get("total_audio", 0)))} audio</span>
        <span class="pill">{len(summary.get("areas_present", [])) if isinstance(summary.get("areas_present"), list) else 0} areas</span>
        <span class="pill">{len(summary.get("phases_present", [])) if isinstance(summary.get("phases_present"), list) else 0} phases</span>
        <span class="pill">Latest {html.escape(str(summary.get("latest_upload_time", "")))}</span>
      </div>
      {coverage}"""


def render_uploads(uploads: list[object]) -> str:
    items: list[str] = []
    for upload in uploads:
        if not isinstance(upload, dict):
            continue
        area = html.escape(str(upload.get("area", "") or "Unspecified area"))
        raw_area = str(upload.get("area", "") or "Unspecified area")
        raw_phase = str(upload.get("phase", "") or "unspecified")
        phase = html.escape(raw_phase)
        timestamp = html.escape(str(upload.get("display_time") or upload.get("timestamp", "")))
        image_count = html.escape(str(upload.get("image_count", 0)))
        audio_count = html.escape(str(upload.get("audio_count", 0)))
        text_note = str(upload.get("text_note") or "").strip()
        is_reviewed = bool(upload.get("is_reviewed"))
        has_text_note = bool(upload.get("has_text_note"))
        has_voice_transcript = bool(upload.get("has_voice_transcript"))
        has_context = bool(upload.get("has_context"))
        review_type = str(upload.get("review_type") or "")
        submitter_first_name = str(upload.get("submitter_first_name") or "").strip()
        submitter_pill = (
            f'<span class="pill pill-submitter">{html.escape(submitter_first_name)}</span>'
            if submitter_first_name
            else ""
        )
        images = upload.get("images")
        if not isinstance(images, list):
            images = []
        image_context = upload.get("image_visual_context") if isinstance(upload.get("image_visual_context"), dict) else {}
        audio_assets = upload.get("audio")
        if not isinstance(audio_assets, list):
            audio_assets = []
        phase_class = f"phase-{safe_css_token(raw_phase)}"
        thumbs = "\n".join(
            render_image_preview(str(url), safe_visual_context(image_context.get(str(url))), fallback_image_alt(str(upload.get("upload_id") or "")))
            for url in images
        )
        audio_players = "\n".join(render_audio_player(audio) for audio in audio_assets if isinstance(audio, dict))
        badges = []
        if is_reviewed:
            badges.append('<span class="pill pill-reviewed">Reviewed</span>')
        if audio_assets:
            badges.append('<span class="pill pill-context">Voice note</span>')
        if has_text_note:
            badges.append('<span class="pill pill-context">Text note</span>')
        if has_voice_transcript:
            badges.append('<span class="pill pill-context">Transcript</span>')
        text_note_html = f'<div class="transcript"><strong>Text note</strong><pre>{html.escape(text_note)}</pre></div>' if text_note else ""
        items.append(
            f"""<article class="upload" data-area="{html.escape(raw_area)}" data-phase="{html.escape(raw_phase)}" data-reviewed="{str(is_reviewed).lower()}" data-context="{str(has_context).lower()}" data-review-type="{html.escape(review_type)}">
          <div class="meta">
            <span class="pill">{area}</span>
            <span class="pill {phase_class}">{phase}</span>
            <span class="pill">{timestamp}</span>
            {submitter_pill}
            <span class="pill">{image_count} images</span>
            <span class="pill">{audio_count} audio</span>
            {''.join(badges)}
          </div>
          <div class="grid">{thumbs}</div>
          {text_note_html}
          {audio_players}
        </article>"""
        )
    return "\n        ".join(items)


def render_image_preview(url: str, visual_context: str, fallback_alt: str) -> str:
    safe_url = html.escape(url)
    context = safe_visual_context(visual_context)
    alt_text = context or fallback_alt
    context_html = f'<p class="visual-context"><strong>Visual context:</strong> {html.escape(clip_visible_context(context))}</p>' if context else ""
    return f"""<div class="image-preview">
              <button type="button" data-full-image="{safe_url}" data-image-alt="{html.escape(alt_text)}">
                <img src="{safe_url}" alt="{html.escape(alt_text)}" loading="lazy" />
              </button>
              {context_html}
            </div>"""


def fallback_image_alt(identifier: str) -> str:
    label = str(identifier or "").strip()
    return f"Field capture image for {label}" if label else "Field capture image"


def clip_visible_context(value: str, *, limit: int = 170) -> str:
    text = safe_visual_context(value, limit=limit)
    return text


def render_audio_player(audio: dict[str, object]) -> str:
    url = html.escape(str(audio.get("url", "")))
    mime_type = html.escape(str(audio.get("mime_type", "")))
    filename = html.escape(str(audio.get("filename", "Voice note")))
    duration = str(audio.get("duration_seconds", "")).strip()
    duration_text = f" ({html.escape(duration)} sec)" if duration else ""
    transcript = audio.get("transcript")
    transcript_html = ""
    if isinstance(transcript, dict) and transcript.get("status") == "complete" and str(transcript.get("raw_text", "")).strip():
        transcript_html = (
            '<div class="transcript">'
            "<strong>Internal transcript - raw/unreviewed</strong>"
            f"<pre>{html.escape(str(transcript.get('raw_text', '')).strip())}</pre>"
            "</div>"
        )
    semantic = audio.get("semantic")
    semantic_html = render_semantic_summary(semantic) if isinstance(semantic, dict) else ""
    return f"""<div class="audio-player">
            <span>Voice note: {filename}{duration_text}</span>
            <audio controls preload="metadata">
              <source src="{url}" type="{mime_type}" />
            </audio>
            {transcript_html}
            {semantic_html}
          </div>"""


def render_semantic_summary(semantic: dict[str, object]) -> str:
    if semantic.get("status") != "complete":
        return ""
    actions = semantic.get("action_candidates")
    if not isinstance(actions, list):
        actions = []
    action_items = "".join(f"<li>{html.escape(str(action))}</li>" for action in actions)
    return f"""<div class="semantic">
              <h4>AI-assisted semantic cleanup - review needed</h4>
              <dl>
                <dt>Internal cleaned note</dt>
                <dd>{html.escape(str(semantic.get("cleaned_internal_note", "")))}</dd>
                <dt>Client-safe prepared note</dt>
                <dd>{html.escape(str(semantic.get("client_safe_note", "")))}</dd>
                <dt>Operational summary</dt>
                <dd>{html.escape(str(semantic.get("operational_summary", "")))}</dd>
                <dt>Suggested actions</dt>
                <dd><ul>{action_items}</ul></dd>
              </dl>
            </div>"""


def filter_values(dates: list[object], key: str) -> list[str]:
    values: set[str] = set()
    for date_group in dates:
        if not isinstance(date_group, dict):
            continue
        uploads = date_group.get("uploads")
        if not isinstance(uploads, list):
            continue
        for upload in uploads:
            if isinstance(upload, dict):
                value = str(upload.get(key, "")).strip()
                if value:
                    values.add(value)
    return sorted(values)


def render_filter_options(values: list[str]) -> str:
    return "\n            ".join(f'<option value="{html.escape(value)}">{html.escape(value)}</option>' for value in values)


def safe_css_token(value: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in value.lower()).strip("-") or "unspecified"
