from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

from .couchdb import VoiceMemoCouchDBError, put_capture_document, voice_memo_config_from_env


MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_NOTE_CHARS = 2000
AUDIO_CONTENT_TYPES = {
    "audio/aac": ".aac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
}
AUDIO_EXTENSIONS = {".aac", ".m4a", ".mp3", ".ogg", ".wav", ".webm"}


class VoiceMemoError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UploadedVoiceMemo:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class UploadResult:
    record: dict


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_audio(memo: UploadedVoiceMemo) -> str:
    if not memo.content:
        raise VoiceMemoError("missing_audio", "Record a voice memo before saving.")
    if len(memo.content) > MAX_AUDIO_BYTES:
        raise VoiceMemoError("request_too_large", "Keep voice memos under 50 MB.")
    content_type = memo.content_type.lower().split(";", 1)[0].strip()
    suffix = Path(memo.filename.replace("\\", "/")).suffix.lower()
    if content_type not in AUDIO_CONTENT_TYPES and suffix not in AUDIO_EXTENSIONS:
        raise VoiceMemoError("unsupported_audio_type", "Record a WebM, M4A, MP3, WAV, AAC, or OGG audio memo.")
    return hashlib.sha256(memo.content).hexdigest()


def audio_extension(memo: UploadedVoiceMemo) -> str:
    content_type = memo.content_type.lower().split(";", 1)[0].strip()
    suffix = Path(memo.filename.replace("\\", "/")).suffix.lower()
    return AUDIO_CONTENT_TYPES.get(content_type) or (suffix if suffix in AUDIO_EXTENSIONS else ".audio")


def normalize_note(text: object) -> str:
    return " ".join(str(text or "").strip().split())[:MAX_NOTE_CHARS]


def parse_captured_at(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise VoiceMemoError("invalid_captured_at", "Use a valid capture timestamp.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VoiceMemoError("invalid_captured_at", "Use a valid capture timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_duration_seconds(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return max(1, min(7200, parsed))


def parse_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def normalize_mode(value: object) -> str:
    mode = str(value or "operations").strip().lower()
    if mode not in {"personal", "operations"}:
        raise VoiceMemoError("invalid_mode", "Choose personal or operations mode.")
    return mode


def split_employee_slugs(value: object) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def display_employee_name(employee: dict[str, Any]) -> str:
    preferred = str(employee.get("preferred_name") or "").strip()
    first = str(employee.get("first") or "").strip()
    last = str(employee.get("last") or "").strip()
    name = f"{preferred or first} {last}".strip()
    return " ".join(name.split())


def resolve_context(
    *,
    mode: str,
    site_id: str,
    employee_slugs: str,
    sites: list[dict[str, Any]],
    employees: list[dict[str, Any]],
) -> dict[str, Any]:
    if mode == "personal":
        return {
            "site_id": None,
            "site_account": None,
            "site_location": None,
            "employee_slugs": [],
            "employee_names": [],
            "routing_flag": "personal_journal",
        }
    normalized_site_id = str(site_id or "").strip()
    site_by_id = {str(site.get("id") or "").strip(): site for site in sites}
    employee_by_id = {str(employee.get("id") or "").strip(): employee for employee in employees}
    site = None
    if normalized_site_id:
        site = site_by_id.get(normalized_site_id)
        if site is None:
            raise VoiceMemoError("unknown_site_id", "Site/employee list out of date - refresh the page.")
    slugs = split_employee_slugs(employee_slugs)
    missing = [slug for slug in slugs if slug not in employee_by_id]
    if missing:
        raise VoiceMemoError("unknown_employee_slug", "Site/employee list out of date - refresh the page.")
    names = [display_employee_name(employee_by_id[slug]) for slug in slugs]
    routing_flag = "site_tagged" if site is not None else "employee_tagged" if slugs else "general"
    return {
        "site_id": normalized_site_id or None,
        "site_account": site.get("account") if site else None,
        "site_location": site.get("location") if site else None,
        "employee_slugs": slugs,
        "employee_names": names,
        "routing_flag": routing_flag,
    }


def build_geolocation(
    *,
    lat: object,
    lng: object,
    accuracy_m: object,
    captured_at: object,
    fallback_captured_at: str,
) -> dict[str, Any] | None:
    parsed_lat = parse_float(lat)
    parsed_lng = parse_float(lng)
    if parsed_lat is None or parsed_lng is None:
        return None
    parsed_accuracy = parse_float(accuracy_m)
    geo_captured_at = str(captured_at or "").strip() or fallback_captured_at
    try:
        parsed_geo_captured_at = parse_captured_at(geo_captured_at)
    except VoiceMemoError:
        parsed_geo_captured_at = fallback_captured_at
    return {
        "lat": parsed_lat,
        "lng": parsed_lng,
        "accuracy_m": max(0.0, parsed_accuracy or 0.0),
        "captured_at": parsed_geo_captured_at,
    }


def compute_capture_id(captured_at_utc: str) -> str:
    stamp = re.sub(r"[:.]", "-", captured_at_utc.replace("Z", ""))
    return f"vm-{stamp}-{uuid.uuid4().hex[:6]}"


def normalize_capture_id(value: object, captured_at_utc: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return compute_capture_id(captured_at_utc)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", raw):
        raise VoiceMemoError("invalid_capture_id", "Invalid capture id.")
    return raw


def safe_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp")
    shutil.copy2(source, temp)
    temp.replace(target)


def sanitized_basename(filename: str) -> str:
    return Path(filename.replace("\\", "/")).name or "voice-memo"


def record_upload(
    *,
    data_dir: Path,
    captured_at: str,
    note: str,
    duration_seconds: int | None,
    memo: UploadedVoiceMemo,
    mode: str = "operations",
    site_id: str = "",
    employee_slugs: str = "",
    geolocation_lat: object = None,
    geolocation_lng: object = None,
    geolocation_accuracy_m: object = None,
    geolocation_captured_at: object = None,
    capture_id: object = None,
    sites_lookup: Callable[[], list[dict[str, Any]]] | None = None,
    employees_lookup: Callable[[], list[dict[str, Any]]] | None = None,
    couchdb_put: Callable[[dict], dict] | None = None,
    person_id: str = "",
) -> UploadResult:
    captured_at_utc = parse_captured_at(captured_at)
    normalized_note = normalize_note(note)
    parsed_duration = parse_duration_seconds(duration_seconds)
    normalized_mode = normalize_mode(mode)
    needs_sites = normalized_mode != "personal" and bool(str(site_id or "").strip())
    needs_employees = normalized_mode != "personal" and bool(split_employee_slugs(employee_slugs))
    sites = sites_lookup() if needs_sites and sites_lookup is not None else []
    employees = employees_lookup() if needs_employees and employees_lookup is not None else []
    context = resolve_context(
        mode=normalized_mode,
        site_id=site_id,
        employee_slugs=employee_slugs,
        sites=sites,
        employees=employees,
    )
    geolocation = build_geolocation(
        lat=geolocation_lat,
        lng=geolocation_lng,
        accuracy_m=geolocation_accuracy_m,
        captured_at=geolocation_captured_at,
        fallback_captured_at=captured_at_utc,
    )
    file_hash = validate_audio(memo)
    normalized_capture_id = normalize_capture_id(capture_id, captured_at_utc)
    captured_dt = datetime.fromisoformat(captured_at_utc.replace("Z", "+00:00"))
    ext = audio_extension(memo)
    relative_path = Path(f"{captured_dt:%Y}") / f"{captured_dt:%m}" / f"{normalized_capture_id}{ext}"
    audio_path = data_dir / relative_path
    temp_dir = data_dir / ".tmp" / normalized_capture_id
    temp_audio = temp_dir / f"audio{ext}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        temp_audio.write_bytes(memo.content)
        safe_copy(temp_audio, audio_path)
    except Exception:
        audio_path.unlink(missing_ok=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    now = utc_now_iso()
    record = {
        "_id": normalized_capture_id,
        "capture_id": normalized_capture_id,
        "type": "personal_voice_memo",
        "captured_at": captured_at_utc,
        "uploaded_at": now,
        "note": normalized_note,
        "audio_filename": sanitized_basename(memo.filename),
        "audio_mime_type": memo.content_type.lower().split(";", 1)[0].strip(),
        "audio_size_bytes": len(memo.content),
        "audio_file_hash": file_hash,
        "audio_path": str(relative_path),
        "processing_state": "pending",
        "created_at": now,
        "mode": normalized_mode,
        "routing_flag": context["routing_flag"],
        "site_id": context["site_id"],
        "site_account": context["site_account"],
        "site_location": context["site_location"],
        "employee_slugs": context["employee_slugs"],
        "employee_names": context["employee_names"],
        "geolocation": geolocation,
    }
    normalized_person_id = str(person_id or "").strip()
    if normalized_person_id:
        record["person_id"] = normalized_person_id
    if parsed_duration is not None:
        record["duration_seconds"] = parsed_duration

    try:
        if couchdb_put is None:
            put_capture_document(voice_memo_config_from_env(), record)
        else:
            couchdb_put(record)
    except VoiceMemoCouchDBError as exc:
        audio_path.unlink(missing_ok=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise VoiceMemoError("couchdb_unavailable", "Voice memo storage is unavailable.") from exc
    shutil.rmtree(temp_dir, ignore_errors=True)
    return UploadResult(record=record)
