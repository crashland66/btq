"""Dependency-free helpers for capture ingestion envelopes.

This module is intentionally standard-library only so it can be vendored into
capture surfaces that do not carry the full BTQ project package.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from pathlib import Path
from typing import Callable, Mapping, TypeAlias, TypeVar


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
DocumentT = TypeVar("DocumentT")


class SubmissionError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UploadedFile:
    field_name: str
    filename: str
    content_type: str
    content: bytes


UploadedPhoto = UploadedFile
MirrorCandidate: TypeAlias = tuple[str, Path]


# Per-submission photo ceiling for the dedicated batch-image import token only.
# Regular capture tokens (the field-capture/unified apps) keep their own server
# max_images; this ceiling keeps the ops-dashboard batch importer explicitly
# aligned with full-QC capture batches. The real upper bound for a batch is the
# server request_max_bytes total-body cap.
IMPORT_MAX_IMAGES = 100


@dataclass(frozen=True)
class IngestLimits:
    max_images: int
    max_upload_bytes: int
    request_max_bytes: int
    photo_mime_extensions: Mapping[str, str]
    audio_mime_extensions: Mapping[str, str]
    audio_allowed_extensions: Mapping[str, set[str]]


@dataclass(frozen=True)
class CaptureMediaPaths:
    upload_root: Path
    photo_records: list[dict[str, str]]
    audio_records: list[dict[str, object]]


def validate_content_length(raw_length: str | None, request_max_bytes: int) -> int:
    try:
        content_length = int(raw_length or "0")
    except ValueError as exc:
        raise SubmissionError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Invalid Content-Length") from exc
    if content_length <= 0:
        raise SubmissionError(HTTPStatus.BAD_REQUEST, "empty_request", "Submission body is required")
    if content_length > request_max_bytes:
        raise SubmissionError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "Submission is too large")
    return content_length


def read_complete_body(read_body: Callable[[int], bytes], content_length: int) -> bytes:
    body = read_body(content_length)
    if len(body) != content_length:
        raise SubmissionError(
            HTTPStatus.BAD_REQUEST,
            "incomplete_request",
            "Submission body was incomplete; retry the upload",
        )
    return body


def read_multipart_submission(
    raw_length: str | None,
    content_type: str,
    read_body: Callable[[int], bytes],
    limits: IngestLimits,
) -> tuple[dict[str, str], list[UploadedFile], list[UploadedFile]]:
    content_length = validate_content_length(raw_length, limits.request_max_bytes)
    if not content_type.startswith("multipart/form-data"):
        raise SubmissionError(HTTPStatus.BAD_REQUEST, "expected_multipart", "Expected multipart form data")
    body = read_complete_body(read_body, content_length)
    fields, uploads = parse_multipart(body, content_type)
    photos = [upload for upload in uploads if upload.field_name == "photos"]
    audio_files = [upload for upload in uploads if upload.field_name == "audio"]
    validate_uploaded_photos(photos, limits)
    validate_uploaded_audio(audio_files, limits)
    return fields, photos, audio_files


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], list[UploadedFile]]:
    headers = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=policy.default).parsebytes(headers + body)
    if not message.is_multipart():
        raise SubmissionError(HTTPStatus.BAD_REQUEST, "expected_multipart", "Expected multipart form data")
    fields: dict[str, str] = {}
    uploads: list[UploadedFile] = []
    for part in message.iter_parts():
        disposition = part.get_content_disposition()
        if disposition != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            uploads.append(
                UploadedFile(
                    field_name=str(name),
                    filename=filename,
                    content_type=part.get_content_type(),
                    content=payload,
                )
            )
        else:
            fields[str(name)] = payload.decode(part.get_content_charset() or "utf-8").strip()
    return fields, uploads


def validate_uploaded_photos(photos: list[UploadedPhoto], limits: IngestLimits) -> None:
    if not photos:
        raise SubmissionError(HTTPStatus.BAD_REQUEST, "missing_photo", "Add at least one photo")
    if len(photos) > limits.max_images:
        raise SubmissionError(HTTPStatus.BAD_REQUEST, "too_many_photos", f"At most {limits.max_images} photos may be submitted")
    for photo in photos:
        if photo.content_type not in limits.photo_mime_extensions:
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "unsupported_photo_type", f"Unsupported photo type: {photo.content_type}")
        if not photo.content:
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "empty_photo", "A photo was empty (0 bytes) — please recapture and resubmit")
        if len(photo.content) > limits.max_upload_bytes:
            raise SubmissionError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "photo_too_large", "A photo is too large")


def validate_uploaded_audio(audio_files: list[UploadedFile], limits: IngestLimits) -> None:
    if len(audio_files) > 1:
        raise SubmissionError(HTTPStatus.BAD_REQUEST, "too_many_audio_files", "At most one voice note may be submitted")
    for audio in audio_files:
        if audio.content_type not in limits.audio_mime_extensions:
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "unsupported_audio_type", f"Unsupported audio type: {audio.content_type}")
        suffix = Path(audio.filename.replace("\\", "/")).suffix.lower()
        if suffix not in limits.audio_allowed_extensions[audio.content_type]:
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "unsupported_audio_extension", f"Unsupported audio extension: {suffix or '(none)'}")
        if not audio.content:
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "empty_audio", "The voice note was empty (0 bytes) — please re-record and resubmit")
        if len(audio.content) > limits.max_upload_bytes:
            raise SubmissionError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "audio_too_large", "The voice note is too large")


def normalize_capture_id(value: str | None, *, fallback_prefix: str) -> str:
    capture_id = str(value or "").strip()
    if capture_id:
        return capture_id
    return f"{fallback_prefix}{uuid.uuid4().hex}"


def lookup_existing_capture(
    capture_id: str,
    get_document: Callable[[str], DocumentT | None],
) -> DocumentT | None:
    if not capture_id:
        return None
    return get_document(capture_id)


def safe_media_filename(name: str, index: int, mime_type: str, extensions: Mapping[str, str], prefix: str) -> str:
    raw_name = Path((name or f"{prefix}-{index:02d}").replace("\\", "/")).name
    fakepath_index = raw_name.lower().rfind("fakepath")
    if fakepath_index != -1:
        stripped = raw_name[fakepath_index + len("fakepath") :].lstrip("/\\:._-")
        raw_name = stripped or raw_name
    stem = SAFE_NAME_RE.sub("-", Path(raw_name).stem).strip(".-")
    if not stem:
        stem = f"{prefix}-{index:02d}"
    return f"{stem[:80]}{extensions[mime_type]}"


def local_date_from_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SubmissionError(HTTPStatus.BAD_REQUEST, "invalid_captured_at", "captured_at must be an ISO datetime") from exc
    return parsed.date().isoformat()


def build_capture_media_records(
    *,
    captured_at: str,
    capture_id: str,
    photos: list[UploadedFile],
    audio_files: list[UploadedFile],
    upload_dir: Path,
    limits: IngestLimits,
    audio_duration_seconds: str = "",
) -> CaptureMediaPaths:
    capture_date = local_date_from_iso(captured_at)
    upload_root = upload_dir / capture_date / capture_id

    photo_payloads: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for index, photo in enumerate(photos, start=1):
        filename = safe_media_filename(photo.filename, index, photo.content_type, limits.photo_mime_extensions, "photo")
        if filename in seen_names:
            filename = f"{Path(filename).stem}-{index:02d}{Path(filename).suffix}"
        seen_names.add(filename)
        stored_path = upload_root / filename
        photo_payloads.append(
            {
                "filename": filename,
                "mime_type": photo.content_type,
                "stored_path": str(stored_path),
                "upload_id": f"{capture_date}/{capture_id}/{filename}",
            }
        )

    audio_payloads: list[dict[str, object]] = []
    for index, audio in enumerate(audio_files, start=1):
        filename = safe_media_filename(audio.filename, index, audio.content_type, limits.audio_mime_extensions, "voice-note")
        stored_path = upload_root / filename
        audio_record: dict[str, object] = {
            "media_type": "audio",
            "filename": filename,
            "mime_type": audio.content_type,
            "stored_path": str(stored_path),
            "upload_id": f"{capture_date}/{capture_id}/{filename}",
            "size_bytes": len(audio.content),
        }
        if audio_duration_seconds:
            audio_record["duration_seconds"] = audio_duration_seconds
        audio_payloads.append(audio_record)
    return CaptureMediaPaths(
        upload_root=upload_root,
        photo_records=photo_payloads,
        audio_records=audio_payloads,
    )


def build_capture_document_envelope(
    *,
    capture_id: str,
    doc_type: str,
    domain_fields: Mapping[str, object],
    photos: list[dict[str, str]],
    audio: list[dict[str, object]] | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": capture_id,
        "type": doc_type,
        "capture_id": capture_id,
    }
    doc.update(domain_fields)
    doc["photos"] = photos
    doc["processing_state"] = "pending"
    doc["created_at"] = created_at or utc_now_iso()
    if audio:
        doc["audio"] = audio
    return doc


def write_capture_media(media_records: list[object], uploads: list[UploadedFile], upload_dir: Path) -> list[MirrorCandidate]:
    from media_store import LocalFilesystemStore

    if len(media_records) != len(uploads):
        raise SubmissionError(HTTPStatus.BAD_REQUEST, "invalid_media", "Invalid media")
    upload_root = upload_dir.expanduser().resolve(strict=False)
    local_store = LocalFilesystemStore(upload_root)
    mirror_candidates: list[MirrorCandidate] = []
    for upload, record in zip(uploads, media_records):
        if not isinstance(record, dict):
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "invalid_photo", "Invalid photo")
        stored_path = Path(str(record["stored_path"])).expanduser().resolve(strict=False)
        try:
            key = stored_path.relative_to(upload_root).as_posix()
        except ValueError as exc:
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "invalid_upload_path", "Invalid upload path") from exc
        local_store.write(key, upload.content)
        mirror_candidates.append((key, stored_path))
    return mirror_candidates


def mirror_capture_media_from_disk(media_candidates: list[MirrorCandidate], upload_dir: Path) -> None:
    from media_store import get_mirror_store

    if not media_candidates:
        return
    upload_root = upload_dir.expanduser().resolve(strict=False)
    try:
        mirror = get_mirror_store(upload_root)
    except Exception as exc:
        logging.warning("btq R2 mirror unavailable: %s", exc)
        return
    if mirror is None:
        return
    for key, stored_path in media_candidates:
        try:
            local_path = Path(stored_path).expanduser().resolve(strict=False)
            local_path.relative_to(upload_root)
            mirror.write(key, local_path.read_bytes())
            logging.info("btq R2 mirror wrote %s", key)
        except Exception as exc:
            logging.warning("btq R2 mirror write failed for %s: %s", key, exc)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
