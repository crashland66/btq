from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import vision_backends
from config import get_config
from event_pipeline.couchdb_registry import CouchDBRegistryError
from field_capture.site_viewer import UnsafeMediaPath, resolve_media_path, upload_id_for_path
from processing_core.artifacts import read_json_object, resolve_within_root, write_json_object
from processing_core.hashing import file_sha256
from processing_core.ids import deterministic_artifact_id
from processing_core.results import result_counts


ARTIFACT_TYPE = "field_capture_photo_vision"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
QUALITY_FLAGS_ENUM: frozenset[str] = frozenset({
    "blurry",
    "motion_blur",
    "out_of_frame",
    "partly_obscured",
    "too_dark",
    "too_bright",
    "glare",
    "low_resolution",
    "contains_people",
    "unanalyzable",
})
DEFAULT_OLLAMA_URL = vision_backends.DEFAULT_OLLAMA_URL
DEFAULT_BACKEND = vision_backends.DEFAULT_BACKEND
DEFAULT_MLX_MODEL = vision_backends.DEFAULT_MLX_MODEL
DEFAULT_OLLAMA_MODEL = vision_backends.DEFAULT_OLLAMA_MODEL
DEFAULT_MODEL = DEFAULT_MLX_MODEL
DEFAULT_TIMEOUT_SECONDS = vision_backends.DEFAULT_TIMEOUT_SECONDS
DEFAULT_MLX_MAX_TOKENS = vision_backends.DEFAULT_MLX_MAX_TOKENS
LOCAL_OLLAMA_HOSTS = vision_backends.LOCAL_OLLAMA_HOSTS
_MAX_VISION_JSON_ATTEMPTS = vision_backends._MAX_VISION_JSON_ATTEMPTS
_JSON_BLOCK_RE = vision_backends._JSON_BLOCK_RE
VisionModelTimeoutError = vision_backends.VisionModelTimeoutError
VisionModelConnectionError = vision_backends.VisionModelConnectionError
extract_json_from_model_output = vision_backends.extract_json_from_model_output
validate_local_ollama_url = vision_backends.validate_local_ollama_url
_is_local_ollama_host = vision_backends._is_local_ollama_host
ADVISORY_WARNING = (
    "vision_advisory_only; descriptive_visual_context_only; human_review_authoritative; "
    "no_approval_or_rejection; no_photo_selection; no_drafts_or_queue_jobs; no_client_publishing; no_vault_mutation"
)
JUDGMENT_LANGUAGE_REMOVED_WARNING = "possible_judgment_language_removed"
SCREENSHOT_OR_APP_UI_WARNING = "screenshot_or_app_ui"
# Judgment-language scrubbing was intentionally DISABLED 2026-06-08 (internal "candid
# mode"): the operator wants the vision model's unfiltered assessment of condition
# (e.g. "dirty", "needs cleaning") to reach review rather than be rewritten. The
# sanitizer functions below are kept as no-ops so existing call sites and the optional
# reprocess-maintenance path stay wired; nothing is forbidden or rewritten.
FORBIDDEN_JUDGMENT_TERMS: tuple[str, ...] = ()
JUDGMENT_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = ()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldPhotoAsset:
    capture_id: str
    site_id: str
    area: str
    phase: str
    photo_asset_id: str
    photo_id: str
    filename: str
    image_path: Path
    image_media_url: str
    mime_type: str
    size_bytes: int
    intake_json_path: Path
    captured_at: str
    qc_category: str = ""
    note: str = ""


@dataclass(frozen=True)
class VisionDescription:
    description: str
    area_guess: str
    visible_objects: list[str]
    possible_conditions: list[str]
    possible_issues: list[str]
    confidence: float | None
    needs_human_review: bool
    warnings: list[str]
    quality_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SiteVisionContext:
    site_context_id: str
    site_context_name: str
    facility_type: str
    context: str

    @property
    def summary(self) -> str:
        return f"{self.site_context_name}; {self.facility_type}"


def default_runtime_root() -> Path:
    return get_config().runtime_root


def default_intake_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "field_capture" / "intake"


def default_upload_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "uploads"


def default_photo_vision_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "field_capture" / "photo_vision"


def default_timeout_seconds() -> float:
    raw = os.environ.get("BTQ_FIELD_CAPTURE_VISION_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def photo_vision_path_for(photo_vision_dir: Path, photo_asset_id: str) -> Path:
    return photo_vision_dir / f"{photo_asset_id}.json"


def read_json(path: Path) -> dict[str, object] | None:
    return read_json_object(path)


def discover_photo_assets(
    intake_dir: Path,
    upload_dir: Path,
    *,
    capture_id: str | None = None,
    site_id: str | None = None,
    date: str | None = None,
) -> list[FieldPhotoAsset]:
    assets: dict[str, FieldPhotoAsset] = {}
    upload_root = upload_dir.expanduser().resolve(strict=False)
    for job_path in sorted(intake_dir.expanduser().glob("**/*.json")):
        job = read_json(job_path)
        if not job or job.get("job_type") != "photo_capture":
            continue
        metadata = job.get("metadata")
        payload = job.get("payload")
        if not isinstance(metadata, dict) or not isinstance(payload, dict):
            continue
        job_capture_id = str(metadata.get("capture_id") or "").strip()
        job_site_id = str(metadata.get("site_id") or payload.get("site") or "").strip()
        captured_at = str(payload.get("captured_at") or payload.get("exported_at") or "").strip()
        if capture_id and job_capture_id != capture_id:
            continue
        if site_id and job_site_id != site_id:
            continue
        if date and date_from_timestamp(captured_at) != date:
            continue
        photos = payload.get("photos")
        if not isinstance(photos, list):
            continue
        qc_category = str(payload.get("qc_category") or "").strip()
        area = str(payload.get("area") or qc_category or "").strip()
        phase = str(payload.get("phase") or metadata.get("phase") or payload.get("status") or payload.get("note") or "").strip()
        for photo in photos:
            if not isinstance(photo, dict):
                continue
            stored_path = str(photo.get("stored_path") or "").strip()
            filename = str(photo.get("filename") or "").strip()
            if not stored_path or not filename:
                continue
            try:
                media_path = resolve_media_path(stored_path, upload_root)
            except UnsafeMediaPath:
                continue
            upload_id = str(photo.get("upload_id") or "").strip()
            if not upload_id and media_path.exists() and media_path.is_file():
                upload_id = upload_id_for_path(media_path, upload_root)
            photo_id = upload_id or f"{job_capture_id}/{filename}"
            asset_id = deterministic_artifact_id("fcp", job_capture_id or photo_id, filename, str(media_path))
            assets.setdefault(
                asset_id,
                FieldPhotoAsset(
                    capture_id=job_capture_id,
                    site_id=job_site_id,
                    area=area,
                    qc_category=qc_category,
                    phase=phase,
                    photo_asset_id=asset_id,
                    photo_id=photo_id,
                    filename=filename,
                    image_path=media_path,
                    image_media_url=f"/media/{quote(upload_id)}" if upload_id else "",
                    mime_type=str(photo.get("mime_type") or ""),
                    size_bytes=int(photo.get("size_bytes") or media_path.stat().st_size) if media_path.exists() and media_path.is_file() else 0,
                    intake_json_path=job_path,
                    captured_at=captured_at,
                    note=str(photo.get("note") or "").strip(),
                ),
            )
    return sorted(assets.values(), key=lambda asset: (asset.capture_id, asset.filename, asset.photo_asset_id))


def filter_assets_by_photo_asset_id(assets: list[FieldPhotoAsset], photo_asset_id: str | None) -> list[FieldPhotoAsset]:
    if not photo_asset_id:
        return assets
    matches = [asset for asset in assets if asset.photo_asset_id == photo_asset_id]
    if not matches:
        raise ValueError(f"photo asset not found: {photo_asset_id}")
    return matches


def date_from_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def site_vision_context_for(site_id: str) -> SiteVisionContext | None:
    normalized = str(site_id or "").strip()
    if not normalized:
        return None
    from event_pipeline import sites

    registry = sites._get_registry()
    if registry is None:
        return None
    try:
        context = registry.get_vision_context(normalized)
    except CouchDBRegistryError as exc:
        logger.warning("CouchDB site vision context unavailable; site_id=%s error=%s", normalized, exc)
        return None
    if not isinstance(context, dict):
        return None
    return SiteVisionContext(
        site_context_id=str(context.get("context_id") or normalized),
        site_context_name=str(context.get("label") or ""),
        facility_type=str(context.get("environment") or ""),
        context=str(context.get("summary") or ""),
    )


def generated_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_confidence(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        return 0.0
    if numeric > 1:
        return 1.0
    return numeric


def contains_forbidden_judgment_language(value: str) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", value, flags=re.IGNORECASE) for term in FORBIDDEN_JUDGMENT_TERMS)


def sidecar_contains_flagged_judgment_language(payload: dict[str, object]) -> bool:
    fields = (
        "description",
        "area_guess",
        "visible_objects",
        "possible_conditions",
        "possible_issues",
    )
    for field in fields:
        value = payload.get(field)
        if isinstance(value, list):
            if any(contains_forbidden_judgment_language(str(item)) for item in value):
                return True
        elif contains_forbidden_judgment_language(str(value or "")):
            return True
    for warning in normalize_string_list(payload.get("warnings")):
        if warning == ADVISORY_WARNING or warning.startswith("Vision output is advisory interpretation"):
            continue
        if warning == JUDGMENT_LANGUAGE_REMOVED_WARNING or contains_forbidden_judgment_language(warning):
            return True
    return False


def sanitize_judgment_language(value: str) -> tuple[str, bool]:
    sanitized = value
    changed = False
    for pattern, replacement in JUDGMENT_REPLACEMENTS:
        sanitized, count = pattern.subn(replacement, sanitized)
        changed = changed or count > 0
    sanitized = collapse_spaces(sanitized)
    if contains_forbidden_judgment_language(sanitized):
        raise ValueError("Vision model output retained forbidden judgment language after sanitization")
    return sanitized, changed


def collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def sanitize_string_list(values: list[str]) -> tuple[list[str], bool]:
    sanitized_values: list[str] = []
    changed = False
    for value in values:
        sanitized, item_changed = sanitize_judgment_language(value)
        changed = changed or item_changed
        if sanitized:
            sanitized_values.append(sanitized)
    return sanitized_values, changed


def dedupe_preserving_order(values: list[str]) -> list[str]:
    """Drop case-insensitive duplicate entries, keeping first-seen order and casing."""
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def sanitize_vision_description(description: VisionDescription) -> VisionDescription:
    text_description, changed_description = sanitize_judgment_language(description.description)
    area_guess, changed_area = sanitize_judgment_language(description.area_guess)
    visible_objects, changed_objects = sanitize_string_list(description.visible_objects)
    visible_objects = dedupe_preserving_order(visible_objects)
    possible_conditions, changed_conditions = sanitize_string_list(description.possible_conditions)
    possible_issues, changed_issues = sanitize_string_list(description.possible_issues)
    warnings, changed_warnings = sanitize_string_list(description.warnings)
    changed = any((changed_description, changed_area, changed_objects, changed_conditions, changed_issues, changed_warnings))
    if ADVISORY_WARNING not in warnings:
        warnings.append(ADVISORY_WARNING)
    if changed and JUDGMENT_LANGUAGE_REMOVED_WARNING not in warnings:
        warnings.append(JUDGMENT_LANGUAGE_REMOVED_WARNING)
    return VisionDescription(
        description=text_description,
        area_guess=area_guess,
        visible_objects=visible_objects,
        possible_conditions=possible_conditions,
        possible_issues=possible_issues,
        confidence=description.confidence,
        needs_human_review=description.needs_human_review,
        warnings=warnings,
        quality_flags=description.quality_flags,
    )


def normalize_vision_description(payload: dict[str, object]) -> VisionDescription:
    warnings = normalize_string_list(payload.get("warnings"))
    if ADVISORY_WARNING not in warnings:
        warnings.append(ADVISORY_WARNING)
    raw_quality_flags = payload.get("quality_flags")
    raw_quality_flag_strings = normalize_string_list(raw_quality_flags)
    quality_flags = (
        [f for f in raw_quality_flag_strings if f in QUALITY_FLAGS_ENUM]
        if isinstance(raw_quality_flags, list)
        else []
    )
    area_guess = str(payload.get("area_guess") or "").strip()
    possible_issues = normalize_string_list(payload.get("possible_issues"))
    needs_human_review = bool(payload.get("needs_human_review", True))
    screenshot_or_app_ui = any(
        value.casefold() == SCREENSHOT_OR_APP_UI_WARNING
        for value in [*warnings, *raw_quality_flag_strings]
    )
    if screenshot_or_app_ui:
        warnings = [
            warning
            for warning in warnings
            if warning.casefold() != SCREENSHOT_OR_APP_UI_WARNING
        ]
        warnings.append(SCREENSHOT_OR_APP_UI_WARNING)
        if "unanalyzable" not in quality_flags:
            quality_flags.append("unanalyzable")
        area_guess = "other"
        possible_issues = []
        needs_human_review = True
    return sanitize_vision_description(VisionDescription(
        description=str(payload.get("description") or "").strip(),
        area_guess=area_guess,
        visible_objects=normalize_string_list(payload.get("visible_objects")),
        possible_conditions=normalize_string_list(payload.get("possible_conditions")),
        possible_issues=possible_issues,
        confidence=normalize_confidence(payload.get("confidence")),
        needs_human_review=needs_human_review,
        warnings=warnings,
        quality_flags=quality_flags,
    ))


_ADVISORY_PREFIXES = (
    ADVISORY_WARNING,
    "Vision output is advisory interpretation",
    "Vision description failed closed",
)

_WARNING_FLAG_PATTERNS: list[tuple[str, str]] = [
    ("out of frame", "out_of_frame"),
    ("partly out", "out_of_frame"),
    ("overexposed", "too_bright"),
    ("reflection", "glare"),
    ("motion", "motion_blur"),
    ("obscured", "partly_obscured"),
    ("blurry", "blurry"),
    ("blur", "blurry"),
    ("glare", "glare"),
    ("bright", "too_bright"),
    ("dark", "too_dark"),
    ("people", "contains_people"),
    ("person", "contains_people"),
]


def _derive_flags_from_warnings(warnings: list[str]) -> list[str]:
    flags: set[str] = set()
    for warning in warnings:
        if any(warning == prefix or warning.startswith(prefix) for prefix in _ADVISORY_PREFIXES):
            continue
        low = warning.lower()
        for keyword, flag in _WARNING_FLAG_PATTERNS:
            if keyword in low:
                flags.add(flag)
    return sorted(flags)


def derive_quality(payload: dict[str, object]) -> dict[str, object]:
    """Compute the normalized image-quality block for a sidecar payload.

    Reads model-supplied quality_flags when present (source="model");
    otherwise keyword-maps warnings[] (source="derived"). Never raises.
    """
    status = str(payload.get("status") or "")
    confidence = normalize_confidence(payload.get("confidence"))
    needs_human_review = bool(payload.get("needs_human_review", True))
    description = str(payload.get("description") or "").strip()
    warnings = normalize_string_list(payload.get("warnings"))

    raw_quality_flags = payload.get("quality_flags")
    if isinstance(raw_quality_flags, list):
        flags = [f for f in raw_quality_flags if f in QUALITY_FLAGS_ENUM]
        source = "model"
    else:
        flags = _derive_flags_from_warnings(warnings)
        source = "derived"

    analyzable = "unanalyzable" not in flags and status not in {STATUS_FAILED, "malformed"}

    if status in {STATUS_FAILED, "malformed"} or not analyzable or (
        confidence is not None and confidence < 0.3 and not description
    ):
        severity = "unusable"
    elif flags or needs_human_review or (confidence is not None and confidence < 0.6):
        severity = "degraded"
    else:
        severity = "ok"

    return {
        "analyzable": analyzable,
        "severity": severity,
        "flags": flags,
        "confidence": confidence,
        "needs_human_review": needs_human_review,
        "source": source,
    }


def prompt_for(asset: FieldPhotoAsset) -> str:
    site_context = site_vision_context_for(asset.site_id)
    if site_context:
        site_context_text = (
            "Facility context (background only — describe what is in THIS image. "
            "Do not invent details from the context):\n"
            f"  site_context_id: {site_context.site_context_id}\n"
            f"  site_context_name: {site_context.site_context_name}\n"
            f"  facility_type: {site_context.facility_type}\n"
            f"  context: {site_context.context}\n"
        )
    else:
        site_context_text = (
            "No site-specific background context is available. Do not invent site details.\n"
        )
    return (
        "You are describing a single field-capture photo for an operations review pipeline.\n"
        "Look at the image and report what is visible. Be specific. Name objects by their common noun "
        "(paper towel dispenser, soap dispenser, trash receptacle, urinal, toilet, sink, mirror, hand dryer, "
        "partition, tile floor, doorway, desk, chair, monitor, cabinet, shelf, etc.). Do not hedge into vague "
        "language (\"dark-colored object\") when something is clearly identifiable.\n"
        "\n"
        "Return strict JSON with exactly these keys:\n"
        "  description (string): one or two concrete sentences naming what is visible.\n"
        "  area_guess (string): your independent identification of the room type from the image alone. "
        "Pick one of: restroom, locker_room, kitchen, breakroom, hallway, stairwell, office, classroom, "
        "exam_room, lobby, supply_closet, janitor_closet, exterior, other. Do not echo any operator-provided label.\n"
        "  visible_objects (array of strings): each entry is one common noun naming one identified object. "
        "List each distinct object once; do not repeat the same noun.\n"
        "  possible_conditions (array of strings): observable facts about the state and condition, e.g. "
        "\"paper towel on floor\", \"trash receptacle appears near full\", \"streaks visible on mirror\", "
        "\"floor looks dirty\", \"dust on shelf\", \"area looks like it still needs cleaning\". Be candid.\n"
        "  possible_issues (array of strings): concrete visible exceptions a human reviewer might want to act on, "
        "including anything that looks dirty, dusty, missed, or still needing cleaning. Empty array if none.\n"
        "  confidence (number 0.0-1.0): how confident you are. Lower this when the image is unclear, blurry, "
        "partly obscured, or shows an ambiguous angle. Do not return 0.95 for a vague description.\n"
        "  needs_human_review (boolean): true if the image is ambiguous, contains people, is partly obscured, "
        "or you are uncertain what is shown.\n"
        "  warnings (array of strings): empty, or short flags about the image itself (e.g., \"image is dark\", "
        "\"subject partly out of frame\").\n"
        "  quality_flags (optional, array of strings): zero or more image-quality flags from this exact list: "
        "blurry, motion_blur, out_of_frame, partly_obscured, too_dark, too_bright, glare, low_resolution, "
        "contains_people, unanalyzable. Include only flags that apply to this image. Omit this key or use an "
        "empty array if no flags apply.\n"
        "\n"
        "Hard rules:\n"
        "  - Describe only what is visible. Do not invent objects, scenes, or hidden facts.\n"
        "  - This is an INTERNAL operations review. Assess the condition directly and honestly: if a surface "
        "looks dirty, dusty, streaked, smudged, neglected, or like it still needs cleaning, say so plainly. "
        "Do not soften, hedge, sanitize, or omit observations — report what you actually see.\n"
        "  - Privacy: do not identify any specific person — no names, and no identifying descriptions of "
        "individuals. Flag people only via contains_people / needs_human_review.\n"
        "  - If the image is clearly a phone screenshot, an app screen, a browser/computer UI, or an "
        "error message/dialog rather than a real photo of a physical space, set area_guess to \"other\", "
        "include \"unanalyzable\" in quality_flags, set needs_human_review to true, include "
        f"\"{SCREENSHOT_OR_APP_UI_WARNING}\" in warnings, set possible_issues to [], and state in "
        "description that the image is a screenshot/app UI including any visible error text. Do not "
        "classify thumbnails embedded inside the screenshot as the actual field scene.\n"
        "\n"
        f"{site_context_text}"
        "\n"
        "Example output for a restroom photo:\n"
        "{\n"
        '  "description": "Tiled restroom interior with three white porcelain urinals on a beige tile wall and a stainless steel partition between them. White tile floor visible. A single paper towel sits on the floor near the leftmost urinal.",\n'
        '  "area_guess": "restroom",\n'
        '  "visible_objects": ["urinal", "stainless steel partition", "tile floor", "tile wall", "paper towel", "overhead light"],\n'
        '  "possible_conditions": ["paper towel on floor near urinal"],\n'
        '  "possible_issues": [],\n'
        '  "confidence": 0.9,\n'
        '  "needs_human_review": false,\n'
        '  "warnings": [],\n'
        '  "quality_flags": []\n'
        "}\n"
        "\n"
        "Example output for a phone screenshot or app UI:\n"
        "{\n"
        '  "description": "Phone screenshot of the Field Capture app UI showing a visible error dialog. Embedded photo thumbnails are visible but are not treated as the actual field scene.",\n'
        '  "area_guess": "other",\n'
        '  "visible_objects": ["phone screenshot", "app screen", "error dialog", "embedded thumbnail"],\n'
        '  "possible_conditions": ["visible app UI", "visible error text"],\n'
        '  "possible_issues": [],\n'
        '  "confidence": 0.9,\n'
        '  "needs_human_review": true,\n'
        f'  "warnings": ["{SCREENSHOT_OR_APP_UI_WARNING}"],\n'
        '  "quality_flags": ["unanalyzable"]\n'
        "}"
    )


class OllamaVisionClient:
    provider = "ollama"

    def __init__(self, model: str = DEFAULT_OLLAMA_MODEL, ollama_url: str = DEFAULT_OLLAMA_URL, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._client = vision_backends.OllamaVisionClient(model, ollama_url, timeout_seconds)
        self.model = self._client.model
        self.ollama_url = self._client.ollama_url
        self.timeout_seconds = self._client.timeout_seconds
        self.engine_name = self._client.engine_name

    def __call__(self, asset: FieldPhotoAsset) -> VisionDescription:
        parsed = self._client.describe(asset.image_path, prompt_for(asset))
        return normalize_vision_description(parsed)


class MlxVisionClient:
    # Local inference only: mlx-vlm loads a HuggingFace model onto Apple
    # Silicon and performs inference in-process. No network calls are made
    # to external AI APIs. This is approved as a local-only alternative to
    # the Ollama path while on-premises GPU capacity is being provisioned.
    """Run a HuggingFace vision model locally via mlx-vlm (Apple Silicon only)."""

    provider = "mlx"

    def __init__(self, model: str, max_tokens: int = DEFAULT_MLX_MAX_TOKENS) -> None:
        self._client = vision_backends.MlxVisionClient(model, max_tokens)
        self.model = self._client.model
        self.model_path = self._client.model_path
        self.max_tokens = self._client.max_tokens
        self.engine_name = self._client.engine_name

    def __call__(self, asset: FieldPhotoAsset) -> VisionDescription:
        parsed = self._client.describe(asset.image_path, prompt_for(asset))
        return normalize_vision_description(parsed)


def vision_engine_name(describe: Callable[[FieldPhotoAsset], VisionDescription], model: str) -> str:
    explicit = getattr(describe, "engine_name", None)
    if explicit:
        return str(explicit)
    provider = getattr(describe, "provider", None)
    if provider:
        return f"{provider}:{model}"
    return type(describe).__name__ if hasattr(describe, "__class__") else "vision-model"


def completed_payload(
    asset: FieldPhotoAsset,
    *,
    source_image_hash: str,
    model_name: str,
    provider: str,
    description: VisionDescription,
    replacement_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    safe_description = sanitize_vision_description(description)
    payload: dict[str, object] = base_payload(asset, source_image_hash=source_image_hash, model_name=model_name, provider=provider) | {
        "status": STATUS_COMPLETED,
        "description": safe_description.description,
        "area_guess": safe_description.area_guess,
        "visible_objects": safe_description.visible_objects,
        "possible_conditions": safe_description.possible_conditions,
        "possible_issues": safe_description.possible_issues,
        "confidence": safe_description.confidence,
        "needs_human_review": safe_description.needs_human_review,
        "warnings": safe_description.warnings,
        "quality_flags": safe_description.quality_flags,
    }
    payload["quality"] = derive_quality(payload)
    if replacement_metadata:
        payload.update(replacement_metadata)
    return payload


def failed_payload(
    asset: FieldPhotoAsset,
    *,
    source_image_hash: str,
    model_name: str,
    provider: str,
    error: Exception,
    timeout_seconds: float | None = None,
    replacement_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    if isinstance(error, VisionModelTimeoutError) or "timed out" in str(error).lower():
        error_type = "timeout"
    elif isinstance(error, VisionModelConnectionError):
        error_type = "connection"
    else:
        error_type = type(error).__name__
    # Both timeouts and connection-unreachable are transient — retry. A
    # rebooting Dell or a network blip should auto-heal on the next cycle.
    can_retry = error_type in {"timeout", "connection"}
    payload: dict[str, object] = base_payload(asset, source_image_hash=source_image_hash, model_name=model_name, provider=provider) | {
        "status": STATUS_FAILED,
        "description": "",
        "area_guess": "",
        "visible_objects": [],
        "possible_conditions": [],
        "possible_issues": [],
        "confidence": None,
        "needs_human_review": True,
        "warnings": [ADVISORY_WARNING, f"Vision description failed closed: {type(error).__name__}: {error}"],
        "quality_flags": [],
        "error": {
            "type": error_type,
            "message": str(error),
            "model_name": model_name,
            "timeout_seconds": timeout_seconds,
            "can_retry": can_retry,
        },
    }
    payload["quality"] = derive_quality(payload)
    if replacement_metadata:
        payload.update(replacement_metadata)
    return payload


def replacement_metadata(existing: dict[str, object], reason: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "replaced_existing_sidecar": True,
        "replaced_at": generated_at(),
        "replacement_reason": reason,
        "previous_status": str(existing.get("status") or ""),
        "previous_generated_at": str(existing.get("generated_at") or ""),
        "previous_model_name": str(existing.get("model_name") or ""),
    }
    # Carry forward auto-retry attempts so the pipeline watcher can bound
    # how many times it auto-requeues the same photo. Increment when this
    # replacement is itself a retry of a prior failure; preserve otherwise.
    prior_attempts = int(existing.get("auto_retry_attempts") or 0)
    payload["auto_retry_attempts"] = prior_attempts + 1 if reason == "failed" else prior_attempts
    return payload


def replacement_reason_for(
    existing: dict[str, object] | None,
    *,
    replace_failed: bool,
    replace_flagged_judgment_language: bool,
) -> str:
    if not isinstance(existing, dict) or existing.get("artifact_type") != ARTIFACT_TYPE:
        return ""
    status = existing.get("status")
    if status == STATUS_FAILED and replace_failed:
        return "failed"
    if status == STATUS_COMPLETED and replace_flagged_judgment_language and sidecar_contains_flagged_judgment_language(existing):
        return "flagged_judgment_language"
    return ""


def base_payload(
    asset: FieldPhotoAsset,
    *,
    source_image_hash: str,
    model_name: str,
    provider: str,
) -> dict[str, object]:
    site_context = site_vision_context_for(asset.site_id)
    return {
        "artifact_type": ARTIFACT_TYPE,
        "type": ARTIFACT_TYPE,
        "generated_at": generated_at(),
        "capture_id": asset.capture_id,
        "photo_id": asset.photo_id,
        "photo_asset_id": asset.photo_asset_id,
        "source_image_path": str(asset.image_path),
        "source_image_hash": source_image_hash,
        "site_id": asset.site_id,
        "qc_category": asset.qc_category,
        "submitted_area": asset.area,
        "submitted_phase": asset.phase,
        "site_context_used": site_context is not None,
        "site_context_id": site_context.site_context_id if site_context else "",
        "site_context_name": site_context.site_context_name if site_context else "",
        "site_context_summary": site_context.summary if site_context else "",
        "model_name": model_name,
        "model_provider": provider,
        "provenance": {
            "intake_json_path": str(asset.intake_json_path),
            "capture_id": asset.capture_id,
            "photo_id": asset.photo_id,
            "photo_asset_id": asset.photo_asset_id,
            "source_image_path": str(asset.image_path),
            "source_image_hash": source_image_hash,
            "captured_at": asset.captured_at,
            "image_media_url": asset.image_media_url,
            "image_filename": asset.filename,
            "image_mime_type": asset.mime_type,
            "image_size_bytes": asset.size_bytes,
        },
    }


def _photo_vision_couchdb_config() -> object:
    """Return a CouchDBConfig when BTQ_COUCHDB_URL is explicitly set, else None."""
    if not (os.environ.get("BTQ_COUCHDB_URL") or "").strip():
        return None
    from event_pipeline import couchdb_config as _cdb
    return _cdb.from_env()


def _write_through_couchdb(sidecar: dict[str, object], *, cdb_config: object) -> None:
    """Best-effort write of a sidecar to CouchDB. Never raises; logs on failure."""
    if cdb_config is None:
        return
    try:
        from field_capture.photo_vision_couchdb import build_photo_vision_document, put_photo_vision_document
        doc = build_photo_vision_document(sidecar)
        put_photo_vision_document(cdb_config, doc)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "photo-vision CouchDB write-through failed for %s: %s",
            sidecar.get("photo_asset_id"),
            exc,
        )


def process_photo_assets(
    intake_dir: Path,
    upload_dir: Path,
    photo_vision_dir: Path,
    describe: Callable[[FieldPhotoAsset], VisionDescription],
    *,
    runtime_root: Path | None = None,
    dry_run: bool = False,
    capture_id: str | None = None,
    site_id: str | None = None,
    date: str | None = None,
    photo_asset_id: str | None = None,
    limit: int | None = None,
    model: str = DEFAULT_MODEL,
    replace_failed: bool = False,
    replace_flagged_judgment_language: bool = False,
) -> dict[str, int]:
    counts = result_counts("discovered", "would_create", "would_replace", "completed", "failed", "skipped")
    runtime_resolved = (runtime_root or upload_dir.parent).expanduser().resolve(strict=False)
    upload_root = upload_dir.expanduser().resolve(strict=False)
    vision_root = photo_vision_dir.expanduser().resolve(strict=False)
    resolve_within_root(vision_root, runtime_resolved)
    assets = filter_assets_by_photo_asset_id(
        discover_photo_assets(intake_dir, upload_root, capture_id=capture_id, site_id=site_id, date=date),
        photo_asset_id,
    )
    counts["discovered"] = len(assets)
    provider = str(getattr(describe, "provider", "local"))
    model_name = vision_engine_name(describe, model)
    timeout_seconds = getattr(describe, "timeout_seconds", None)
    timeout_value = float(timeout_seconds) if isinstance(timeout_seconds, (int, float)) else None
    cdb_config = _photo_vision_couchdb_config()
    processed = 0
    for asset in assets:
        sidecar_path = photo_vision_path_for(vision_root, asset.photo_asset_id)
        existing = read_json(sidecar_path)
        replacement_reason = replacement_reason_for(
            existing,
            replace_failed=replace_failed,
            replace_flagged_judgment_language=replace_flagged_judgment_language,
        )
        if isinstance(existing, dict) and existing.get("artifact_type") == ARTIFACT_TYPE and existing.get("status") in {STATUS_COMPLETED, STATUS_FAILED}:
            if not replacement_reason:
                counts["skipped"] += 1
                continue
        if limit is not None and processed >= limit:
            counts["skipped"] += 1
            continue
        processed += 1
        if dry_run:
            if asset.image_path.exists() and asset.image_path.is_file():
                if replacement_reason:
                    counts["would_replace"] += 1
                else:
                    counts["would_create"] += 1
            else:
                counts["failed"] += 1
            continue
        photo_vision_dir.mkdir(parents=True, exist_ok=True)
        metadata = replacement_metadata(existing, replacement_reason) if isinstance(existing, dict) and replacement_reason else None
        try:
            resolve_within_root(asset.image_path, upload_root)
            if not asset.image_path.exists() or not asset.image_path.is_file():
                raise FileNotFoundError(asset.image_path)
            source_hash = file_sha256(asset.image_path)
            description = describe(asset)
            sidecar = completed_payload(
                asset,
                source_image_hash=source_hash,
                model_name=model_name,
                provider=provider,
                description=description,
                replacement_metadata=metadata,
            )
            write_json_object(sidecar_path, sidecar)
            _write_through_couchdb(sidecar, cdb_config=cdb_config)
            counts["completed"] += 1
        except Exception as exc:  # noqa: BLE001
            source_hash = file_sha256(asset.image_path) if asset.image_path.exists() and asset.image_path.is_file() else ""
            sidecar = failed_payload(
                asset,
                source_image_hash=source_hash,
                model_name=model_name,
                provider=provider,
                error=exc,
                timeout_seconds=timeout_value,
                replacement_metadata=metadata,
            )
            write_json_object(sidecar_path, sidecar)
            _write_through_couchdb(sidecar, cdb_config=cdb_config)
            counts["failed"] += 1
    return counts


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Describe field-capture photos with a local vision model and write sidecar artifacts.")
    parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--intake-dir", type=Path, help="Field-capture intake metadata directory.")
    parser.add_argument("--upload-dir", type=Path)
    parser.add_argument("--photo-vision-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--capture-id")
    parser.add_argument("--site-id")
    parser.add_argument("--date")
    parser.add_argument("--photo-asset-id", help="Limit processing to one stable field-capture photo asset id.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--replace-failed", action="store_true", help="Regenerate existing failed photo vision sidecars.")
    parser.add_argument(
        "--replace-flagged-judgment-language",
        action="store_true",
        help="Regenerate completed sidecars only when model fields contain forbidden judgment language.",
    )
    parser.add_argument("--model", default=os.environ.get("BTQ_FIELD_CAPTURE_VISION_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--backend",
        choices=["ollama", "mlx"],
        default=os.environ.get("BTQ_FIELD_CAPTURE_VISION_BACKEND", DEFAULT_BACKEND),
        help="Vision inference backend. 'mlx' runs a HuggingFace model locally via mlx-vlm (Apple Silicon).",
    )
    parser.add_argument("--ollama-url", default=os.environ.get("BTQ_OLLAMA_URL", DEFAULT_OLLAMA_URL))
    parser.add_argument("--timeout-seconds", type=float, default=default_timeout_seconds())
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    runtime_root = args.runtime_root.expanduser()
    intake_dir = (args.intake_dir or default_intake_dir(runtime_root)).expanduser()
    upload_dir = (args.upload_dir or default_upload_dir(runtime_root)).expanduser()
    photo_vision_dir = (args.photo_vision_dir or default_photo_vision_dir(runtime_root)).expanduser()
    if args.backend == "mlx":
        client: OllamaVisionClient | MlxVisionClient = MlxVisionClient(model=args.model)
    else:
        client = OllamaVisionClient(model=args.model, ollama_url=args.ollama_url, timeout_seconds=args.timeout_seconds)
    counts = process_photo_assets(
        intake_dir,
        upload_dir,
        photo_vision_dir,
        client,
        runtime_root=runtime_root,
        dry_run=args.dry_run,
        capture_id=args.capture_id,
        site_id=args.site_id,
        date=args.date,
        photo_asset_id=args.photo_asset_id,
        limit=args.limit,
        model=args.model,
        replace_failed=args.replace_failed,
        replace_flagged_judgment_language=args.replace_flagged_judgment_language,
    )
    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
    else:
        print(
            "field photo vision: "
            f"discovered={counts['discovered']} would_create={counts['would_create']} would_replace={counts['would_replace']} "
            f"completed={counts['completed']} failed={counts['failed']} skipped={counts['skipped']}"
        )
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
