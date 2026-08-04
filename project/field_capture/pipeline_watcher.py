from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from config import get_config
from event_pipeline import couchdb_config
from field_capture import action_candidates
from field_capture import audio_semantics
from field_capture import audio_transcription
from field_capture import job_draft_emission
from field_capture import photo_vision
from field_capture import text_semantics
from processing_core.artifacts import write_json_object
from transcription_pipeline.main import (
    DEFAULT_INITIAL_PROMPT,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    SubprocessWhisperTranscriber,
    ensure_ffmpeg_on_path,
)


DEFAULT_POLL_SECONDS = 60.0
DEFAULT_TRANSCRIBE_LIMIT = 1
DEFAULT_VISION_LIMIT = 1
# Auto-retry knobs for failed-retryable photo vision sidecars.
# Each retry intent triggers one vision call in the next cycle, so cap how many
# we queue per cycle to leave headroom for new-photo processing within the
# poll interval. Cooldown prevents hammering the same photo on rapid cycles;
# max_attempts caps total retries so a permanently flaky photo doesn't loop.
DEFAULT_VISION_AUTO_RETRY_PER_CYCLE = 0
DEFAULT_VISION_AUTO_RETRY_MAX_ATTEMPTS = 5
DEFAULT_VISION_AUTO_RETRY_COOLDOWN_SECONDS = 1800.0
DEFAULT_ISSUE_ROUTING_LIMIT = 25
DEFAULT_CAT_VISION_LIMIT = 1
DEFAULT_CAT_VISION_REPO = ""
DEFAULT_CAT_VISION_TIMEOUT_SECONDS = 120.0
logger = logging.getLogger(__name__)


TranscriberFactory = Callable[[Path, logging.Logger], Callable[[Path], str]]
VisionDescribeFactory = Callable[[str, str, float], Callable[[photo_vision.FieldPhotoAsset], photo_vision.VisionDescription]]
PhotoVisionFunc = Callable[..., dict[str, int]]
SemanticEngineFactory = Callable[[], Callable[[audio_semantics.FieldAudioTranscript], audio_semantics.SemanticResult]]
TextSemanticEngineFactory = Callable[[], object]


def default_runtime_root() -> Path:
    return get_config().runtime_root


def default_log_path(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "logs" / "field_capture_pipeline_watch.log"


def _payload_from_intake(path: Path) -> tuple[dict[str, object], dict[str, object]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    body = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    return metadata, body


def process_note_only_text_semantics(
    intake_dir: Path,
    semantic_dir: Path,
    *,
    runtime_root: Path | None = None,
    engine: object | None = None,
) -> dict[str, int]:
    counts = {"discovered": 0, "completed": 0, "failed": 0, "skipped": 0}
    intake_root = intake_dir.expanduser().resolve(strict=False)
    semantic_root = semantic_dir.expanduser().resolve(strict=False)
    if runtime_root is not None:
        runtime_resolved = runtime_root.expanduser().resolve(strict=False)
        from processing_core.artifacts import resolve_within_root

        resolve_within_root(intake_root, runtime_resolved)
        resolve_within_root(semantic_root, runtime_resolved)

    for intake_path in sorted(intake_root.glob("*.json")):
        resolved = intake_path.resolve(strict=False)
        parsed = _payload_from_intake(resolved)
        if parsed is None:
            counts["skipped"] += 1
            continue
        metadata, body = parsed
        note = str(body.get("note") or "").strip()
        audio = body.get("audio") if isinstance(body.get("audio"), list) else []
        # Process the typed note's semantics whenever there is a note and NO audio.
        # Photos are handled separately by the vision stage, so a photo+note capture
        # (the common case) must still have its note extracted; only audio captures
        # route their semantics through the audio-transcript path instead.
        if not note or audio:
            continue
        counts["discovered"] += 1
        capture_id = str(metadata.get("capture_id") or body.get("capture_id") or intake_path.stem).strip() or intake_path.stem
        semantic_path = semantic_root / f"{capture_id}.json"
        if semantic_path.exists():
            counts["skipped"] += 1
            continue
        try:
            artifact = text_semantics.run_text_semantic_pipeline(
                note,
                site_id=str(metadata.get("site_id") or body.get("site_id") or ""),
                upload_id=capture_id,
                area=str(body.get("qc_category") or ""),
                person_id=str(metadata.get("person_id") or ""),
                site_label=str(body.get("site") or ""),
                captured_at=str(body.get("captured_at") or ""),
                engine=engine,
            )
            artifact["source_kind"] = "field_capture_text_note"
            artifact["source_intake_path"] = str(resolved)
            write_json_object(semantic_path, artifact)
            counts["completed"] += 1
        except Exception:  # noqa: BLE001
            counts["failed"] += 1
            raise
    return counts


def configure_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("field_capture.pipeline_watch")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def default_transcriber_factory(
    *,
    model: str,
    initial_prompt: str | None,
    worker_timeout_seconds: float,
) -> TranscriberFactory:
    def factory(runtime_root: Path, logger: logging.Logger) -> Callable[[Path], str]:
        ensure_ffmpeg_on_path()
        return SubprocessWhisperTranscriber(
            model,
            runtime_root / "local",
            compare_mode=False,
            initial_prompt=initial_prompt,
            timeout_seconds=worker_timeout_seconds,
            logger=logger,
        )

    return factory


def default_vision_describe_factory(
    model: str,
    ollama_url: str,
    timeout_seconds: float,
) -> Callable[[photo_vision.FieldPhotoAsset], photo_vision.VisionDescription]:
    return photo_vision.OllamaVisionClient(model=model, ollama_url=ollama_url, timeout_seconds=timeout_seconds)


# MLX client cache: model loads once per process and is reused across cycles.
# Keyed by (model, strategy) so a BTQ_VISION_STRATEGY change never reuses a
# client built for the other strategy.
_MLX_CLIENT_CACHE: dict[tuple[str, str], object] = {}


def mlx_vision_describe_factory(
    model: str,
    ollama_url: str,  # unused — MLX runs locally on-device
    timeout_seconds: float,  # unused — MLX inference is synchronous
) -> object:
    key = (model, photo_vision.vision_strategy())
    if key not in _MLX_CLIENT_CACHE:
        _MLX_CLIENT_CACHE[key] = photo_vision.build_mlx_vision_client(model)
    return _MLX_CLIENT_CACHE[key]


class IsolatedMlxVisionDescribe:
    provider = "mlx"

    def __init__(self, model: str, timeout_seconds: float) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.engine_name = f"mlx:{model}"

    def __call__(self, _asset: photo_vision.FieldPhotoAsset) -> photo_vision.VisionDescription:
        raise RuntimeError("isolated MLX vision placeholder should not be called directly")


def isolated_mlx_vision_describe_factory(
    model: str,
    ollama_url: str,  # unused — MLX runs locally on-device
    timeout_seconds: float,
) -> IsolatedMlxVisionDescribe:
    return IsolatedMlxVisionDescribe(model=model, timeout_seconds=timeout_seconds)


def isolated_mlx_subprocess_timeout_seconds(vision_timeout_seconds: float) -> float:
    raw = os.environ.get("BTQ_FIELD_CAPTURE_VISION_SUBPROCESS_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return max(float(vision_timeout_seconds), 240.0)


def isolated_mlx_enabled() -> bool:
    return os.environ.get("BTQ_FIELD_CAPTURE_VISION_ISOLATED", "").strip().lower() in {"1", "true", "yes", "on"}


def semantic_mlx_isolated() -> bool:
    engine_key = os.environ.get("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_rule").strip().lower()
    provider = os.environ.get("BTQ_SEMANTIC_PROVIDER", "mlx").strip().lower()
    return engine_key == "local_model" and provider == "mlx"


def isolated_semantic_subprocess_timeout_seconds(default: float = 240.0) -> float:
    raw = os.environ.get("BTQ_FIELD_CAPTURE_SEMANTIC_SUBPROCESS_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return default


def process_semantics_isolated(runtime_root: Path, *, timeout_seconds: float) -> dict[str, object]:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    command = [
        sys.executable,
        "-m",
        "field_capture.semantic_child",
        "--runtime-root",
        str(runtime_resolved),
        "--json",
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"semantic subprocess exited {result.returncode}").strip()
        raise RuntimeError(detail)
    try:
        loaded = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"semantic subprocess returned invalid JSON: {(result.stdout or '').strip()}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("semantic subprocess returned non-object JSON")
    return loaded


def process_mlx_photo_assets_isolated(
    intake_dir: Path,
    upload_dir: Path,
    photo_vision_dir: Path,
    describe: Callable[[photo_vision.FieldPhotoAsset], photo_vision.VisionDescription],
    *,
    runtime_root: Path | None = None,
    dry_run: bool = False,
    capture_id: str | None = None,
    site_id: str | None = None,
    date: str | None = None,
    photo_asset_id: str | None = None,
    limit: int | None = None,
    model: str = photo_vision.DEFAULT_MODEL,
    replace_failed: bool = False,
    replace_flagged_judgment_language: bool = False,
) -> dict[str, int]:
    """Process MLX photo vision one image per subprocess.

    A long-lived MLX process is fast once warm, but a single stuck generation
    can block every later photo. The watcher uses this path so each photo has
    an outer wall-clock timeout and failures are recorded as retryable sidecars.
    """
    counts = {"discovered": 0, "would_create": 0, "would_replace": 0, "completed": 0, "failed": 0, "skipped": 0}
    runtime_resolved = (runtime_root or upload_dir.parent).expanduser().resolve(strict=False)
    upload_root = upload_dir.expanduser().resolve(strict=False)
    vision_root = photo_vision_dir.expanduser().resolve(strict=False)
    assets = photo_vision.filter_assets_by_photo_asset_id(
        photo_vision.discover_photo_assets(intake_dir, upload_root, capture_id=capture_id, site_id=site_id, date=date),
        photo_asset_id,
    )
    counts["discovered"] = len(assets)
    timeout_value = float(getattr(describe, "timeout_seconds", photo_vision.DEFAULT_TIMEOUT_SECONDS) or photo_vision.DEFAULT_TIMEOUT_SECONDS)
    subprocess_timeout = isolated_mlx_subprocess_timeout_seconds(timeout_value)
    provider = str(getattr(describe, "provider", "mlx"))
    model_name = photo_vision.vision_engine_name(describe, model)
    cdb_config = photo_vision._photo_vision_couchdb_config()
    processed = 0
    for asset in assets:
        sidecar_path = photo_vision.photo_vision_path_for(vision_root, asset.photo_asset_id)
        existing = photo_vision.read_json(sidecar_path)
        replacement_reason = photo_vision.replacement_reason_for(
            existing,
            replace_failed=replace_failed,
            replace_flagged_judgment_language=replace_flagged_judgment_language,
        )
        if (
            isinstance(existing, dict)
            and existing.get("artifact_type") == photo_vision.ARTIFACT_TYPE
            and existing.get("status") in {photo_vision.STATUS_COMPLETED, photo_vision.STATUS_FAILED}
            and not replacement_reason
        ):
            counts["skipped"] += 1
            continue
        if limit is not None and processed >= limit:
            counts["skipped"] += 1
            continue
        processed += 1
        if dry_run:
            if asset.image_path.exists() and asset.image_path.is_file():
                counts["would_replace" if replacement_reason else "would_create"] += 1
            else:
                counts["failed"] += 1
            continue
        command = [
            sys.executable,
            "-m",
            "field_capture.photo_vision",
            "--runtime-root",
            str(runtime_resolved),
            "--photo-asset-id",
            asset.photo_asset_id,
            "--limit",
            "1",
            "--backend",
            "mlx",
            "--model",
            model,
            "--timeout-seconds",
            str(timeout_value),
            "--json",
        ]
        if replace_failed:
            command.append("--replace-failed")
        if replace_flagged_judgment_language:
            command.append("--replace-flagged-judgment-language")
        try:
            result = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=subprocess_timeout,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or f"photo vision subprocess exited {result.returncode}").strip())
            subprocess_counts = json.loads(result.stdout or "{}")
            counts["completed"] += int(subprocess_counts.get("completed") or 0)
            counts["failed"] += int(subprocess_counts.get("failed") or 0)
            counts["skipped"] += int(subprocess_counts.get("skipped") or 0)
        except subprocess.TimeoutExpired as exc:
            photo_vision_dir.mkdir(parents=True, exist_ok=True)
            metadata = photo_vision.replacement_metadata(existing, replacement_reason) if isinstance(existing, dict) and replacement_reason else None
            source_hash = photo_vision.file_sha256(asset.image_path) if asset.image_path.exists() and asset.image_path.is_file() else ""
            sidecar = photo_vision.failed_payload(
                asset,
                source_image_hash=source_hash,
                model_name=model_name,
                provider=provider,
                error=photo_vision.VisionModelTimeoutError(f"MLX vision subprocess timed out after {subprocess_timeout:g} seconds"),
                timeout_seconds=subprocess_timeout,
                replacement_metadata=metadata,
            )
            write_json_object(sidecar_path, sidecar)
            photo_vision._write_through_couchdb(sidecar, cdb_config=cdb_config)
            counts["failed"] += 1
            logger.warning("MLX photo vision subprocess timed out asset_id=%s timeout_seconds=%s", asset.photo_asset_id, subprocess_timeout)
        except Exception as exc:  # noqa: BLE001
            photo_vision_dir.mkdir(parents=True, exist_ok=True)
            metadata = photo_vision.replacement_metadata(existing, replacement_reason) if isinstance(existing, dict) and replacement_reason else None
            source_hash = photo_vision.file_sha256(asset.image_path) if asset.image_path.exists() and asset.image_path.is_file() else ""
            sidecar = photo_vision.failed_payload(
                asset,
                source_image_hash=source_hash,
                model_name=model_name,
                provider=provider,
                error=exc,
                timeout_seconds=subprocess_timeout,
                replacement_metadata=metadata,
            )
            write_json_object(sidecar_path, sidecar)
            photo_vision._write_through_couchdb(sidecar, cdb_config=cdb_config)
            counts["failed"] += 1
            logger.exception("MLX photo vision subprocess failed asset_id=%s", asset.photo_asset_id)
    return counts


# Semantic engine cache: model-backed engines load once per process and are reused across cycles.
_SEMANTIC_ENGINE_CACHE: dict[tuple[str, str, str], Callable[[audio_semantics.FieldAudioTranscript], audio_semantics.SemanticResult]] = {}


def cached_semantic_engine_factory() -> Callable[[audio_semantics.FieldAudioTranscript], audio_semantics.SemanticResult]:
    engine_key = os.environ.get("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_rule")
    provider = os.environ.get("BTQ_SEMANTIC_PROVIDER", "mlx")
    model = os.environ.get("BTQ_SEMANTIC_MODEL", "")
    cache_key = (engine_key, provider, model)
    if cache_key not in _SEMANTIC_ENGINE_CACHE:
        _SEMANTIC_ENGINE_CACHE[cache_key] = audio_semantics.build_semantic_engine()
    return _SEMANTIC_ENGINE_CACHE[cache_key]


def step_result(name: str, status: str, counts: dict[str, object] | None = None, error: str = "") -> dict[str, object]:
    return {"step": name, "status": status, "counts": counts or {}, "error": error}


def cat_vision_enabled() -> bool:
    return os.environ.get("BTQ_CAT_VISION_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def cat_vision_repo_path() -> Path | None:
    raw = os.environ.get("BTQ_CAT_VISION_REPO", DEFAULT_CAT_VISION_REPO).strip()
    return Path(raw).expanduser() if raw else None


def cat_vision_limit() -> int:
    raw = os.environ.get("BTQ_CAT_VISION_LIMIT", "").strip()
    if not raw:
        return DEFAULT_CAT_VISION_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CAT_VISION_LIMIT
    return value if value > 0 else DEFAULT_CAT_VISION_LIMIT


def cat_vision_timeout_seconds() -> float:
    raw = os.environ.get("BTQ_CAT_VISION_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_CAT_VISION_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CAT_VISION_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_CAT_VISION_TIMEOUT_SECONDS


def cat_vision_isolation_enabled() -> bool:
    return isolated_mlx_enabled()


def prepare_cat_vision_process_environment() -> None:
    # The cat runner performs SSH/backfill subprocess work after MLX/tokenizers
    # are warm. Disable tokenizer worker parallelism before model load so those
    # later forks do not inherit a parallel tokenizer runtime.
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _counts_from_cat_stats(stats: object) -> dict[str, int]:
    keys = ("processed", "identified", "skipped", "errored")
    return {key: int(getattr(stats, key, 0) or 0) for key in keys}


def process_cat_vision_bounded_subprocess(
    *,
    repo_path: Path | None,
    limit: int,
    timeout_seconds: float,
) -> dict[str, int]:
    if repo_path is None:
        raise RuntimeError("BTQ_CAT_VISION_REPO is not set")
    repo_resolved = repo_path.expanduser().resolve(strict=False)
    if not repo_resolved.exists():
        raise RuntimeError(f"BTQ_CAT_VISION_REPO does not exist: {repo_resolved}")

    project_root = Path(__file__).resolve().parents[1]
    pythonpath_parts = [str(project_root), str(project_root.parent)]
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    command = [
        sys.executable,
        "-m",
        "field_capture.cat_vision_child",
        "--repo",
        str(repo_resolved),
        "--limit",
        str(limit),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=project_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"cat vision subprocess timed out after {timeout_seconds:g}s") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"cat vision subprocess exited {result.returncode}").strip()
        raise RuntimeError(detail)
    try:
        loaded = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cat vision subprocess returned invalid JSON: {(result.stdout or '').strip()}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("cat vision subprocess returned non-object JSON")
    return {key: int(loaded.get(key, 0) or 0) for key in ("processed", "identified", "skipped", "errored")}


PHOTO_RETRY_ID_RE = re.compile(r"^fcp_[A-Za-z0-9_-]+$")


def synthesize_photo_vision_auto_retry_intents(
    *,
    runtime_root: Path,
    max_per_cycle: int,
    max_attempts: int,
    cooldown_seconds: float,
    logger: logging.Logger,
) -> dict[str, int]:
    """Queue retry intents for failed-but-retryable photo vision sidecars.

    Scans photo_vision/*.json, picks oldest-failed-with-error.can_retry-true
    sidecars that don't already have an intent file queued and haven't
    exceeded auto_retry_attempts, and writes intent JSONs. The existing
    consume_photo_vision_retry_intents() in the same cycle will process them.
    """
    photo_vision_dir = photo_vision.default_photo_vision_dir(runtime_root)
    retry_dir = runtime_root / "reviews" / "photo_vision_retries"
    retry_dir.mkdir(parents=True, exist_ok=True)
    counts = {
        "queued": 0,
        "skipped_max_attempts": 0,
        "skipped_cooldown": 0,
        "skipped_already_queued": 0,
        "scanned": 0,
    }
    if max_per_cycle <= 0 or not photo_vision_dir.exists():
        return counts
    now = time.time()
    candidates: list[tuple[float, str, int]] = []
    for sidecar_path in photo_vision_dir.glob("*.json"):
        counts["scanned"] += 1
        try:
            mtime = sidecar_path.stat().st_mtime
        except OSError:
            continue
        if now - mtime < cooldown_seconds:
            counts["skipped_cooldown"] += 1
            continue
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("status") != "failed":
            continue
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        if not error.get("can_retry"):
            continue
        asset_id = str(payload.get("photo_asset_id") or "")
        if not PHOTO_RETRY_ID_RE.match(asset_id):
            continue
        if (retry_dir / f"{asset_id}.json").exists():
            counts["skipped_already_queued"] += 1
            continue
        attempts = int(payload.get("auto_retry_attempts") or 0)
        if attempts >= max_attempts:
            counts["skipped_max_attempts"] += 1
            continue
        candidates.append((mtime, asset_id, attempts))
    candidates.sort()  # oldest first
    for _mtime, asset_id, attempts in candidates[:max_per_cycle]:
        intent_path = retry_dir / f"{asset_id}.json"
        intent_payload = {
            "photo_asset_id": asset_id,
            "reason": "auto_retry_failed_timeout",
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "previous_attempts": attempts,
        }
        try:
            intent_path.write_text(json.dumps(intent_payload, sort_keys=True), encoding="utf-8")
            counts["queued"] += 1
            logger.info(
                "photo vision auto-retry queued asset_id=%s previous_attempts=%s",
                asset_id,
                attempts,
            )
        except OSError:
            logger.exception("photo vision auto-retry failed to write intent asset_id=%s", asset_id)
    return counts


def consume_photo_vision_retry_intents(
    *,
    runtime_root: Path,
    vision_model: str,
    ollama_url: str,
    vision_timeout_seconds: float,
    logger: logging.Logger,
    photo_vision_func: PhotoVisionFunc,
    vision_describe_factory: VisionDescribeFactory,
) -> dict[str, int]:
    retry_dir = runtime_root / "reviews" / "photo_vision_retries"
    retry_dir.mkdir(parents=True, exist_ok=True)
    counts = {"completed": 0, "failed": 0, "skipped": 0}
    for intent_path in sorted(retry_dir.glob("*.json")):
        try:
            payload = json.loads(intent_path.read_text(encoding="utf-8"))
            asset_id = str(payload.get("photo_asset_id") or "")
            if not PHOTO_RETRY_ID_RE.match(asset_id):
                logger.warning("photo vision retry intent skipped invalid photo_asset_id=%r path=%s", asset_id, intent_path)
                counts["skipped"] += 1
                continue
            describe = vision_describe_factory(vision_model, ollama_url, vision_timeout_seconds)
            photo_vision_func(
                photo_vision.default_intake_dir(runtime_root),
                photo_vision.default_upload_dir(runtime_root),
                photo_vision.default_photo_vision_dir(runtime_root),
                describe,
                runtime_root=runtime_root,
                photo_asset_id=asset_id,
                replace_failed=True,
                limit=1,
                model=vision_model,
            )
            counts["completed"] += 1
        except Exception:  # noqa: BLE001
            counts["failed"] += 1
            logger.exception("photo vision retry intent failed path=%s", intent_path)
        finally:
            try:
                intent_path.unlink()
            except FileNotFoundError:
                pass
    return counts


def run_cycle(
    *,
    runtime_root: Path,
    transcribe_limit: int,
    vision_limit: int,
    vision_model: str,
    ollama_url: str,
    vision_timeout_seconds: float,
    transcriber_factory: TranscriberFactory,
    logger: logging.Logger,
    # In-process default stays "ollama" so callers/tests aren't forced to have
    # mlx-vlm installed. Production selects the backend explicitly via the CLI
    # --vision-backend flag (env BTQ_FIELD_CAPTURE_VISION_BACKEND=mlx), whose
    # default is photo_vision.DEFAULT_BACKEND ("mlx").
    vision_backend: str = "ollama",
    photo_vision_func: PhotoVisionFunc = photo_vision.process_photo_assets,
    vision_describe_factory: VisionDescribeFactory = default_vision_describe_factory,
    semantic_engine_factory: SemanticEngineFactory = cached_semantic_engine_factory,
    text_semantic_engine_factory: TextSemanticEngineFactory | None = None,
    run_transcribe: bool = True,
    run_semantics: bool = True,
    run_vision: bool = True,
    run_candidates: bool = True,
    run_issue_routing: bool = True,
    issue_routing_limit: int = DEFAULT_ISSUE_ROUTING_LIMIT,
    vision_auto_retry_per_cycle: int = DEFAULT_VISION_AUTO_RETRY_PER_CYCLE,
    vision_auto_retry_max_attempts: int = DEFAULT_VISION_AUTO_RETRY_MAX_ATTEMPTS,
    vision_auto_retry_cooldown_seconds: float = DEFAULT_VISION_AUTO_RETRY_COOLDOWN_SECONDS,
) -> dict[str, object]:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    cycle: dict[str, object] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runtime_root": str(runtime_resolved),
        "steps": [],
        "ok": True,
    }
    steps = cycle["steps"]
    assert isinstance(steps, list)

    use_isolated_mlx = vision_backend == "mlx" and isolated_mlx_enabled() and photo_vision_func is photo_vision.process_photo_assets
    effective_vision_factory: VisionDescribeFactory = (
        isolated_mlx_vision_describe_factory
        if use_isolated_mlx
        else mlx_vision_describe_factory
        if vision_backend == "mlx"
        else vision_describe_factory
    )
    effective_photo_vision_func: PhotoVisionFunc = (
        process_mlx_photo_assets_isolated if use_isolated_mlx else photo_vision_func
    )
    shared_vision_describe: object | None = None

    logger.info("field-capture pipeline cycle started runtime_root=%s", runtime_resolved)
    if cat_vision_enabled():
        prepare_cat_vision_process_environment()

    if run_transcribe:
        try:
            transcription_logger = audio_transcription.configure_logging(audio_transcription.default_log_path(runtime_resolved))
            transcriber = transcriber_factory(runtime_resolved, transcription_logger)
            logger.info("field-capture transcription pass starting limit=%s", transcribe_limit)
            counts = audio_transcription.process_audio_assets(
                audio_transcription.default_intake_dir(runtime_resolved),
                audio_transcription.default_upload_dir(runtime_resolved),
                audio_transcription.default_transcript_dir(runtime_resolved),
                transcriber,
                transcription_logger,
                limit=transcribe_limit,
            )
            logger.info("field-capture transcription pass completed counts=%s", counts)
            steps.append(step_result("transcribe_field_audio", "completed", counts))
        except Exception as exc:  # noqa: BLE001
            cycle["ok"] = False
            steps.append(step_result("transcribe_field_audio", "failed", error=str(exc)))
            logger.exception("field-capture transcription pass failed")
    else:
        steps.append(step_result("transcribe_field_audio", "skipped", error="disabled"))

    if run_semantics:
        if semantic_mlx_isolated():
            timeout_seconds = isolated_semantic_subprocess_timeout_seconds()
            try:
                logger.info("field-capture isolated semantic subprocess starting timeout_seconds=%s", timeout_seconds)
                counts = process_semantics_isolated(runtime_resolved, timeout_seconds=timeout_seconds)
                logger.info("field-capture isolated semantic subprocess completed counts=%s", counts)
                steps.append(step_result("process_semantics_isolated", "completed", counts))
            except subprocess.TimeoutExpired:
                cycle["ok"] = False
                error = f"semantic subprocess timed out after {timeout_seconds:g} seconds"
                steps.append(step_result("process_semantics_isolated", "failed", error=error))
                logger.warning("field-capture isolated semantic subprocess timed out timeout_seconds=%s", timeout_seconds)
            except Exception as exc:  # noqa: BLE001
                cycle["ok"] = False
                steps.append(step_result("process_semantics_isolated", "failed", error=str(exc)))
                logger.exception("field-capture isolated semantic subprocess failed")
        else:
            try:
                semantics_logger = audio_semantics.configure_logger(audio_semantics.default_log_path(runtime_resolved))
                counts = audio_semantics.process_semantics(
                    audio_semantics.default_transcript_dir(runtime_resolved),
                    audio_semantics.default_semantic_dir(runtime_resolved),
                    semantic_engine_factory(),
                    runtime_root=runtime_resolved,
                    logger=semantics_logger,
                )
                steps.append(step_result("process_field_audio_semantics", "completed", counts))
            except Exception as exc:  # noqa: BLE001
                cycle["ok"] = False
                steps.append(step_result("process_field_audio_semantics", "failed", error=str(exc)))
                logger.exception("field-capture audio semantic processing failed")
            try:
                text_counts = process_note_only_text_semantics(
                    audio_transcription.default_intake_dir(runtime_resolved),
                    action_candidates.default_text_semantic_dir(runtime_resolved),
                    runtime_root=runtime_resolved,
                    engine=text_semantic_engine_factory() if text_semantic_engine_factory is not None else None,
                )
                if any(int(text_counts.get(key, 0)) for key in ("discovered", "completed", "failed", "skipped")):
                    steps.append(step_result("process_field_text_semantics", "completed", text_counts))
            except Exception as exc:  # noqa: BLE001
                cycle["ok"] = False
                steps.append(step_result("process_field_text_semantics", "failed", error=str(exc)))
                logger.exception("field-capture text semantic processing failed")
    else:
        steps.append(step_result("process_field_audio_semantics", "skipped", error="disabled"))

    if run_issue_routing:
        try:
            from field_capture import issue_routing

            issue_logger = logger.getChild("issue_routing")
            counts = issue_routing.route_field_reported_issues(
                audio_transcription.default_intake_dir(runtime_resolved),
                runtime_resolved / "queue",
                runtime_root=runtime_resolved,
                logger=issue_logger,
                limit=issue_routing_limit,
            )
            steps.append(step_result("route_field_reported_issues", "completed", counts))
        except Exception as exc:  # noqa: BLE001
            cycle["ok"] = False
            steps.append(step_result("route_field_reported_issues", "failed", error=str(exc)))
            logger.exception("issue routing pass failed")
    else:
        steps.append(step_result("route_field_reported_issues", "skipped", error="disabled"))

    if run_vision and vision_limit > 0:
        try:
            logger.info(
                "field-capture photo vision pass starting limit=%s model=%s backend=%s ollama_url=%s timeout_seconds=%s",
                vision_limit,
                vision_model,
                vision_backend,
                ollama_url,
                vision_timeout_seconds,
            )
            shared_vision_describe = effective_vision_factory(vision_model, ollama_url, vision_timeout_seconds)
            counts = effective_photo_vision_func(
                photo_vision.default_intake_dir(runtime_resolved),
                photo_vision.default_upload_dir(runtime_resolved),
                photo_vision.default_photo_vision_dir(runtime_resolved),
                shared_vision_describe,
                runtime_root=runtime_resolved,
                limit=vision_limit,
                model=vision_model,
            )
            logger.info("field-capture photo vision pass completed counts=%s", counts)
            steps.append(step_result("describe_field_photos", "completed", counts))
        except Exception as exc:  # noqa: BLE001
            cycle["ok"] = False
            steps.append(step_result("describe_field_photos", "failed", error=str(exc)))
            logger.exception("field-capture photo vision pass failed")
    else:
        reason = "disabled" if not run_vision else "limit=0"
        steps.append(step_result("describe_field_photos", "skipped", error=reason))

    if run_vision and vision_auto_retry_per_cycle > 0:
        try:
            auto_counts = synthesize_photo_vision_auto_retry_intents(
                runtime_root=runtime_resolved,
                max_per_cycle=vision_auto_retry_per_cycle,
                max_attempts=vision_auto_retry_max_attempts,
                cooldown_seconds=vision_auto_retry_cooldown_seconds,
                logger=logger,
            )
            steps.append(step_result("photo_vision_auto_retry_synthesis", "completed", auto_counts))
        except Exception as exc:  # noqa: BLE001
            cycle["ok"] = False
            steps.append(step_result("photo_vision_auto_retry_synthesis", "failed", error=str(exc)))
            logger.exception("photo vision auto-retry intent synthesis failed")

    retry_dir = runtime_resolved / "reviews" / "photo_vision_retries"
    has_retry_intents = retry_dir.exists() and any(retry_dir.glob("*.json"))
    if run_vision and has_retry_intents:
        try:
            retry_counts = consume_photo_vision_retry_intents(
                runtime_root=runtime_resolved,
                vision_model=vision_model,
                ollama_url=ollama_url,
                vision_timeout_seconds=vision_timeout_seconds,
                logger=logger,
                photo_vision_func=effective_photo_vision_func,
                vision_describe_factory=effective_vision_factory,
            )
            steps.append(step_result("photo_vision_retry_intents", "completed", retry_counts))
        except Exception as exc:  # noqa: BLE001
            cycle["ok"] = False
            steps.append(step_result("photo_vision_retry_intents", "failed", error=str(exc)))
            logger.exception("field-capture photo vision retry intents failed")
    elif has_retry_intents:
        steps.append(step_result("photo_vision_retry_intents", "skipped", error="vision disabled"))

    if not cat_vision_enabled():
        steps.append(step_result("process_cat_vision", "skipped", error="disabled"))
    elif not run_vision:
        steps.append(step_result("process_cat_vision", "skipped", error="vision disabled"))
    elif vision_backend != "mlx":
        steps.append(step_result("process_cat_vision", "skipped", error="field MLX backend unavailable"))
    elif not cat_vision_isolation_enabled():
        steps.append(step_result("process_cat_vision", "skipped", error="requires BTQ_FIELD_CAPTURE_VISION_ISOLATED=1"))
    elif _MLX_CLIENT_CACHE:
        steps.append(step_result("process_cat_vision", "skipped", error="parent MLX cache already warm; restart watcher with isolated vision"))
    else:
        try:
            limit = cat_vision_limit()
            timeout_seconds = cat_vision_timeout_seconds()
            logger.info("cat vision stage starting limit=%s timeout_seconds=%s repo=%s", limit, timeout_seconds, cat_vision_repo_path())
            counts = process_cat_vision_bounded_subprocess(
                repo_path=cat_vision_repo_path(),
                limit=limit,
                timeout_seconds=timeout_seconds,
            )
            logger.info("cat vision stage completed counts=%s", counts)
            steps.append(step_result("process_cat_vision", "completed", counts))
        except Exception as exc:  # noqa: BLE001
            cycle["ok"] = False
            steps.append(step_result("process_cat_vision", "failed", error=str(exc)))
            logger.exception("cat vision stage failed")

    if run_candidates:
        try:
            counts = job_draft_emission.collect_job_drafts(
                action_candidates.default_semantic_dirs(runtime_resolved),
                runtime_root=runtime_resolved,
            )
            steps.append(step_result("collect_job_drafts", "completed", counts))
        except Exception as exc:  # noqa: BLE001
            cycle["ok"] = False
            steps.append(step_result("collect_job_drafts", "failed", error=str(exc)))
            logger.exception("field-capture job draft collection failed")
    else:
        steps.append(step_result("collect_job_drafts", "skipped", error="disabled"))

    cycle["completed_at"] = datetime.now(timezone.utc).isoformat()
    logger.info("field-capture pipeline cycle completed ok=%s", cycle["ok"])
    return cycle


def format_cycle(cycle: dict[str, object]) -> str:
    lines = [
        "field-capture pipeline cycle",
        f"runtime={cycle['runtime_root']}",
        f"ok={cycle['ok']}",
    ]
    for step in cycle["steps"]:
        counts = step.get("counts") if isinstance(step, dict) else {}
        error = step.get("error") if isinstance(step, dict) else ""
        count_text = " ".join(f"{key}={value}" for key, value in counts.items()) if isinstance(counts, dict) else ""
        suffix = f" {count_text}" if count_text else f" error={error}" if error else ""
        lines.append(f"- {step['step']} {step['status']}{suffix}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Watch field-capture intake and process through review candidate collection.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--transcribe-limit", type=int, default=DEFAULT_TRANSCRIBE_LIMIT)
    parser.add_argument("--vision-limit", type=int, default=DEFAULT_VISION_LIMIT)
    parser.add_argument("--no-transcribe", action="store_true")
    parser.add_argument("--no-semantics", action="store_true")
    parser.add_argument("--no-issue-routing", action="store_true")
    parser.add_argument("--issue-routing-limit", type=int, default=DEFAULT_ISSUE_ROUTING_LIMIT)
    parser.add_argument("--no-vision", action="store_true")
    parser.add_argument("--no-candidates", action="store_true")
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--model", default=config.whisper_model)
    parser.add_argument("--initial-prompt", default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument("--no-initial-prompt", action="store_true")
    parser.add_argument("--worker-timeout-seconds", type=float, default=DEFAULT_WORKER_TIMEOUT_SECONDS)
    parser.add_argument("--vision-model", default=os.environ.get("BTQ_FIELD_CAPTURE_VISION_MODEL", photo_vision.DEFAULT_MODEL))
    parser.add_argument(
        "--vision-backend",
        choices=["ollama", "mlx"],
        default=os.environ.get("BTQ_FIELD_CAPTURE_VISION_BACKEND", photo_vision.DEFAULT_BACKEND),
        help="Vision inference backend. 'mlx' runs a HuggingFace model locally via mlx-vlm (Apple Silicon).",
    )
    parser.add_argument("--ollama-url", default=os.environ.get("BTQ_OLLAMA_URL", photo_vision.DEFAULT_OLLAMA_URL))
    parser.add_argument("--vision-timeout-seconds", type=float, default=photo_vision.default_timeout_seconds())
    parser.add_argument("--vision-auto-retry-per-cycle", type=int, default=DEFAULT_VISION_AUTO_RETRY_PER_CYCLE,
                        help="Max failed-retryable sidecars auto-queued per cycle. 0 disables auto-retry.")
    parser.add_argument("--vision-auto-retry-max-attempts", type=int, default=DEFAULT_VISION_AUTO_RETRY_MAX_ATTEMPTS,
                        help="Stop auto-retrying a sidecar after this many attempts.")
    parser.add_argument("--vision-auto-retry-cooldown-seconds", type=float, default=DEFAULT_VISION_AUTO_RETRY_COOLDOWN_SECONDS,
                        help="Minimum age of a failed sidecar before it's eligible for auto-retry.")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    log_path = (args.log_path or default_log_path(runtime_root)).expanduser()
    logger = configure_logger(log_path)
    try:
        couchdb_config.from_env().assert_can_authenticate()
    except couchdb_config.CouchDBConfigError as exc:
        logger.error("CouchDB startup auth check failed: %s", exc)
        raise SystemExit(2)
    transcribe_limit = max(0, args.transcribe_limit)
    vision_limit = max(0, args.vision_limit)
    factory = default_transcriber_factory(
        model=args.model,
        initial_prompt=None if args.no_initial_prompt else args.initial_prompt,
        worker_timeout_seconds=args.worker_timeout_seconds,
    )
    while True:
        cycle = run_cycle(
            runtime_root=runtime_root,
            transcribe_limit=transcribe_limit,
            vision_limit=vision_limit,
            vision_model=args.vision_model,
            vision_backend=args.vision_backend,
            ollama_url=args.ollama_url,
            vision_timeout_seconds=args.vision_timeout_seconds,
            transcriber_factory=factory,
            logger=logger,
            run_transcribe=not args.no_transcribe,
            run_semantics=not args.no_semantics,
            run_issue_routing=not args.no_issue_routing,
            issue_routing_limit=max(0, args.issue_routing_limit),
            run_vision=not args.no_vision,
            run_candidates=not args.no_candidates,
            vision_auto_retry_per_cycle=max(0, args.vision_auto_retry_per_cycle),
            vision_auto_retry_max_attempts=max(0, args.vision_auto_retry_max_attempts),
            vision_auto_retry_cooldown_seconds=max(0.0, args.vision_auto_retry_cooldown_seconds),
        )
        if args.json:
            print(json.dumps(cycle, indent=2, sort_keys=True), flush=True)
        else:
            print(format_cycle(cycle), flush=True)
        if args.once:
            return 0 if cycle["ok"] else 1
        time.sleep(args.poll_seconds)


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
