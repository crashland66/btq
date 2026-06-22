from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import vision_backends
from field_capture import photo_vision
from field_capture.photo_vision_couchdb import build_photo_vision_document, put_photo_vision_document
from media_store import get_media_store
from processing_core.artifacts import read_json_object, write_json_object
from queue_processor.metrics import _append_json_line


logger = logging.getLogger(__name__)


DEEP_ANALYSIS_PRESETS: list[dict[str, str]] = [
    {
        "id": "condition_detail",
        "label": "Detailed condition assessment",
        "prompt": (
            "You are inspecting a photo from a commercial cleaning / facilities context. "
            "Describe the condition of the space and surfaces in detail — cleanliness, wear, "
            "damage, and anything notable. Be specific and factual; do not speculate beyond "
            "what is visible."
        ),
    },
    {
        "id": "damage_hazard",
        "label": "Damage / safety hazard scan",
        "prompt": (
            "Inspect this photo for damage, safety hazards, or maintenance issues (spills, "
            "wet floors, broken fixtures, blocked exits, electrical problems, trip hazards, "
            "etc.). List each issue with its location in the frame and an estimated severity. "
            "If you see none, say so explicitly."
        ),
    },
    {
        "id": "cleanliness_qc",
        "label": "Cleanliness QC vs standard",
        "prompt": (
            "Assess the cleanliness of what is shown as if performing a commercial cleaning "
            "quality-control inspection. State what meets standard and what falls short "
            "(dust, streaks, trash, restroom/floor/surface issues). End with a brief "
            "pass/fail judgment and the reasons."
        ),
    },
    {
        "id": "text_ocr",
        "label": "Read text (log book / label / meter)",
        "prompt": (
            "Transcribe ALL legible text visible in this image exactly as written — log-book "
            "entries, labels, signage, handwriting, and any meter or gauge readings. Preserve "
            "line/column structure. For a reading, state the value and units. Note anything "
            "illegible."
        ),
    },
    {
        "id": "inventory_count",
        "label": "Count items / supplies",
        "prompt": (
            "Count the items and supplies visible in this image (e.g., bottles, rolls, bags, "
            "equipment). List each item type with a count and your confidence. Note any "
            "partially visible or obscured items."
        ),
    },
    {
        "id": "equipment_id",
        "label": "Identify equipment",
        "prompt": (
            "Identify any equipment or machines visible: type, likely make/model if "
            "discernible, and attachments. Note visible condition and transcribe any labels "
            "or model numbers you can read."
        ),
    },
    {
        "id": "shift_report",
        "label": "Describe in detail for the shift report",
        "prompt": (
            "Describe this photo in rich, factual detail suitable for an area manager's "
            "end-of-day shift report: what it shows, the site/area context, the work or "
            "condition depicted, and anything an area manager should note. Concise but "
            "complete."
        ),
    },
]

_PRESETS_BY_ID = {preset["id"]: preset for preset in DEEP_ANALYSIS_PRESETS}
DEEP_ANALYSIS_METRICS_FILENAME = "deep_analysis_requests.jsonl"


class DeepAnalysisError(ValueError):
    pass


class DeepAnalysisCanonicalWriteError(DeepAnalysisError):
    pass


def resolve_prompt(preset_id: str | None, custom_prompt: str | None) -> tuple[str, str]:
    normalized_id = str(preset_id or "").strip()
    custom_text = str(custom_prompt or "").strip()
    if normalized_id and normalized_id != "custom":
        preset = _PRESETS_BY_ID.get(normalized_id)
        if preset is None:
            raise DeepAnalysisError(f"unknown deep-analysis preset id: {normalized_id}")
        return normalized_id, preset["prompt"]
    if custom_text:
        return "custom", custom_text
    raise DeepAnalysisError("custom deep-analysis prompt must not be empty")


def run_deep_analysis(
    *,
    capture_id: str,
    photo_asset_id: str,
    preset_id: str | None = None,
    custom_prompt: str | None = None,
    actor: str,
    runtime_root: Path,
    upload_root: Path,
    vision_client: object | None = None,
    media_store: object | None = None,
    couchdb_put: Callable[[dict[str, object]], object] | None = None,
    now: datetime | Callable[[], datetime] | None = None,
) -> dict[str, object]:
    prompt_id, prompt_text = resolve_prompt(preset_id, custom_prompt)
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    upload_resolved = upload_root.expanduser().resolve(strict=False)
    started = time.perf_counter()
    entry: dict[str, object]
    provider = ""
    model_name = ""

    try:
        client = vision_client or _build_default_vision_client()
        provider = _model_provider(client)
        model_name = _model_name(client)
        store = media_store or get_media_store(upload_resolved)
        asset = _resolve_photo_asset(capture_id, photo_asset_id, runtime_resolved, upload_resolved)
        media_key = _media_key_for_asset(asset, upload_resolved)
        image_bytes = store.read(media_key)
        result = _describe_image_bytes(client, image_bytes, prompt_text, suffix=asset.image_path.suffix)
        entry = _completed_entry(
            prompt_id=prompt_id,
            prompt_text=prompt_text,
            result=result,
            actor=actor,
            generated_at=_iso_utc(now),
            provider=provider,
            model_name=model_name,
        )
        _append_entry_to_sidecar(
            entry,
            asset=asset,
            runtime_root=runtime_resolved,
            couchdb_put=couchdb_put,
            require_canonical_write=True,
        )
    except DeepAnalysisCanonicalWriteError:
        duration_ms = (time.perf_counter() - started) * 1000.0
        _record_metric(
            runtime_resolved,
            {
                "ts": _iso_utc(now),
                "capture_id": str(capture_id),
                "photo_asset_id": str(photo_asset_id),
                "prompt_id": prompt_id,
                "is_custom": prompt_id == "custom",
                "status": "couchdb_write_failed",
                "duration_ms": round(duration_ms, 3),
                "actor": str(actor),
                "model_provider": provider,
                "model_name": model_name,
            },
        )
        raise
    except Exception as exc:  # noqa: BLE001 - operator-triggered worker callable must fail closed.
        entry = _failed_entry(
            prompt_id=prompt_id,
            prompt_text=prompt_text,
            actor=actor,
            generated_at=_iso_utc(now),
            provider=provider,
            model_name=model_name,
            error=exc,
        )
        try:
            asset = _resolve_photo_asset(capture_id, photo_asset_id, runtime_resolved, upload_resolved)
            _append_entry_to_sidecar(
                entry,
                asset=asset,
                runtime_root=runtime_resolved,
                couchdb_put=couchdb_put,
                require_canonical_write=False,
            )
        except Exception:
            pass

    duration_ms = (time.perf_counter() - started) * 1000.0
    _record_metric(
        runtime_resolved,
        {
            "ts": _iso_utc(now),
            "capture_id": str(capture_id),
            "photo_asset_id": str(photo_asset_id),
            "prompt_id": prompt_id,
            "is_custom": prompt_id == "custom",
            "status": str(entry.get("status") or "failed"),
            "duration_ms": round(duration_ms, 3),
            "actor": str(actor),
            "model_provider": provider,
            "model_name": model_name,
        },
    )
    return entry


def _build_default_vision_client() -> object:
    backend = os.environ.get("BTQ_FIELD_CAPTURE_VISION_BACKEND", vision_backends.DEFAULT_BACKEND)
    model = os.environ.get("BTQ_FIELD_CAPTURE_VISION_MODEL") or None
    return vision_backends.build_vision_client(
        backend,
        model=model,
        ollama_url=os.environ.get("BTQ_OLLAMA_URL", vision_backends.DEFAULT_OLLAMA_URL),
        timeout_seconds=photo_vision.default_timeout_seconds(),
    )


def _resolve_photo_asset(
    capture_id: str,
    photo_asset_id: str,
    runtime_root: Path,
    upload_root: Path,
) -> photo_vision.FieldPhotoAsset:
    assets = photo_vision.discover_photo_assets(
        photo_vision.default_intake_dir(runtime_root),
        upload_root,
        capture_id=str(capture_id),
    )
    matches = photo_vision.filter_assets_by_photo_asset_id(assets, str(photo_asset_id))
    if not matches:
        raise DeepAnalysisError(f"photo asset not found: {photo_asset_id}")
    return matches[0]


def _media_key_for_asset(asset: photo_vision.FieldPhotoAsset, upload_root: Path) -> str:
    try:
        return photo_vision.upload_id_for_path(asset.image_path, upload_root)
    except Exception:
        fallback = str(asset.photo_id or "").strip()
        if fallback:
            return fallback
        raise


def _describe_image_bytes(client: object, image_bytes: bytes, prompt_text: str, *, suffix: str) -> str:
    temp_path: Path | None = None
    fd = -1
    try:
        fd, temp_name = tempfile.mkstemp(suffix=suffix or ".jpg")
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(image_bytes)
        result = client.generate_text(temp_path, prompt_text)
        if not isinstance(result, str):
            raise ValueError("deep-analysis vision response was not text")
        return result
    finally:
        if fd != -1:
            os.close(fd)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _completed_entry(
    *,
    prompt_id: str,
    prompt_text: str,
    result: object,
    actor: str,
    generated_at: str,
    provider: str,
    model_name: str,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "prompt_id": prompt_id,
        "prompt_text": prompt_text,
        "result": result,
        "model_name": model_name,
        "model_provider": provider,
        "actor": str(actor),
        "generated_at": generated_at,
        "status": "completed",
    }
    label = _label_for_prompt(prompt_id)
    if label:
        entry["label"] = label
    if isinstance(result, dict) and "confidence" in result:
        entry["confidence"] = result.get("confidence")
    return entry


def _failed_entry(
    *,
    prompt_id: str,
    prompt_text: str,
    actor: str,
    generated_at: str,
    provider: str,
    model_name: str,
    error: Exception,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "prompt_id": prompt_id,
        "prompt_text": prompt_text,
        "result": {},
        "model_name": model_name,
        "model_provider": provider,
        "actor": str(actor),
        "generated_at": generated_at,
        "status": "failed",
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }
    label = _label_for_prompt(prompt_id)
    if label:
        entry["label"] = label
    return entry


def _append_entry_to_sidecar(
    entry: dict[str, object],
    *,
    asset: photo_vision.FieldPhotoAsset,
    runtime_root: Path,
    couchdb_put: Callable[[dict[str, object]], object] | None,
    require_canonical_write: bool,
) -> None:
    sidecar_path = photo_vision.photo_vision_path_for(photo_vision.default_photo_vision_dir(runtime_root), asset.photo_asset_id)
    existing = read_json_object(sidecar_path)
    sidecar = dict(existing) if isinstance(existing, dict) else _minimal_sidecar(asset)
    history = sidecar.get("deep_analysis")
    entries = list(history) if isinstance(history, list) else []
    entries.append(entry)
    sidecar["deep_analysis"] = entries
    if not require_canonical_write:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_object(sidecar_path, sidecar)
        try:
            doc = build_photo_vision_document(sidecar)
            _put_couchdb_doc(doc, couchdb_put=couchdb_put)
        except Exception as exc:  # noqa: BLE001 - failed-analysis sidecar recording stays best-effort.
            logger.warning("deep-analysis CouchDB write-through failed for %s: %s", asset.photo_asset_id, exc)
        return

    doc = build_photo_vision_document(sidecar)
    _put_couchdb_doc(doc, couchdb_put=couchdb_put)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_object(sidecar_path, sidecar)


def _put_couchdb_doc(
    doc: dict[str, object],
    *,
    couchdb_put: Callable[[dict[str, object]], object] | None,
) -> None:
    if couchdb_put is not None:
        try:
            couchdb_put(doc)
        except DeepAnalysisCanonicalWriteError:
            raise
        except Exception as exc:  # noqa: BLE001 - injected CouchDB writer represents canonical publish.
            raise DeepAnalysisCanonicalWriteError(
                f"deep-analysis CouchDB write-through failed for {doc.get('_id')}: {exc}"
            ) from exc
    else:
        _default_couchdb_put(doc)


def _minimal_sidecar(asset: photo_vision.FieldPhotoAsset) -> dict[str, object]:
    return {
        "artifact_type": photo_vision.ARTIFACT_TYPE,
        "status": "",
        "generated_at": "",
        "site_id": asset.site_id,
        "capture_id": asset.capture_id,
        "photo_asset_id": asset.photo_asset_id,
        "photo_id": asset.photo_id,
        "submitted_area": asset.area,
        "submitted_phase": asset.phase,
        "description": "",
        "area_guess": "",
        "visible_objects": [],
        "possible_conditions": [],
        "possible_issues": [],
        "confidence": None,
        "needs_human_review": True,
        "warnings": [],
        "quality": None,
        "model_name": "",
        "model_provider": "",
        "source_image_hash": "",
        "provenance": {
            "capture_id": asset.capture_id,
            "photo_id": asset.photo_id,
            "photo_asset_id": asset.photo_asset_id,
            "source_image_path": str(asset.image_path),
            "image_media_url": asset.image_media_url,
            "image_filename": asset.filename,
            "image_mime_type": asset.mime_type,
            "image_size_bytes": asset.size_bytes,
        },
    }


def _default_couchdb_put(doc: dict[str, object]) -> None:
    try:
        cdb_config = photo_vision._photo_vision_couchdb_config()  # noqa: SLF001 - match existing photo vision write-through gate.
    except Exception as exc:  # noqa: BLE001 - enabled CouchDB with invalid config is a canonical publish failure.
        raise DeepAnalysisCanonicalWriteError(
            f"deep-analysis CouchDB configuration failed for {doc.get('_id')}: {exc}"
        ) from exc
    if cdb_config is None:
        return
    try:
        put_photo_vision_document(cdb_config, doc)
    except Exception as exc:  # noqa: BLE001 - canonical deep-analysis publish must fail visibly.
        raise DeepAnalysisCanonicalWriteError(
            f"deep-analysis CouchDB write-through failed for {doc.get('_id')}: {exc}"
        ) from exc


def _record_metric(runtime_root: Path, payload: dict[str, object]) -> None:
    try:
        _append_json_line(runtime_root / "metrics" / DEEP_ANALYSIS_METRICS_FILENAME, payload)
    except Exception as exc:  # noqa: BLE001 - do not crash the operator-triggered worker callable.
        logger.warning("deep-analysis metric write failed for %s: %s", payload.get("photo_asset_id"), exc)


def _label_for_prompt(prompt_id: str) -> str:
    preset = _PRESETS_BY_ID.get(prompt_id)
    return str(preset.get("label") or "") if preset else ""


def _model_provider(client: object) -> str:
    return str(getattr(client, "provider", None) or getattr(client, "model_provider", None) or "local")


def _model_name(client: object) -> str:
    return str(
        getattr(client, "model", None)
        or getattr(client, "model_name", None)
        or getattr(client, "engine_name", None)
        or type(client).__name__
    )


def _iso_utc(now: datetime | Callable[[], datetime] | None = None) -> str:
    value = now() if callable(now) else now
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
