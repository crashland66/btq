from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import re
import subprocess
import tempfile
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from config import get_config, require_directories
from event_pipeline.main import normalized_transcript_path_for, process_transcript
from event_pipeline.sites import resolve_site, resolve_site_note_path
from event_to_queue.main import convert_event_paths
from io_atomic import atomic_write_text, safe_move
from processing_core.artifacts import write_json_object
from processing_core.sentences import split_sentences
from processing_core.transcripts import transcript_metadata_payload
from queue_processor import metrics
from queue_processor.manifest import create_capture_manifest
from voice_memo.semantics import (
    VoiceMemoSemanticEngine,
    VoiceMemoSemanticPass,
    VoiceMemoTranscriptContext,
    run_semantic_pass,
)


DEFAULT_CONFIG = get_config()
DEFAULT_INBOX_DIR = DEFAULT_CONFIG.audio_inbox_dir
DEFAULT_ARCHIVE_DIR = DEFAULT_CONFIG.audio_archive_dir
DEFAULT_LOCAL_ROOT = DEFAULT_CONFIG.local_root
DEFAULT_LOCAL_RUNTIME_DIR = DEFAULT_CONFIG.local_runtime_dir
DEFAULT_AUDIO_DIR = DEFAULT_LOCAL_ROOT / "audio_processing"
DEFAULT_EVENTS_ROOT = DEFAULT_LOCAL_ROOT
DEFAULT_QUEUE_JOBS_DIR = DEFAULT_LOCAL_ROOT / "queue_jobs"
DEFAULT_LOGS_DIR = DEFAULT_CONFIG.logs_dir
DEFAULT_LOG_PATH = DEFAULT_CONFIG.transcription_log_path
DEFAULT_VAULT_ROOT = DEFAULT_CONFIG.vault_dir
DEFAULT_MODEL = DEFAULT_CONFIG.whisper_model
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_STABLE_SECONDS = 10.0
DEFAULT_FFMPEG_PATH_PREFIX = DEFAULT_CONFIG.ffmpeg_path_prefix
DEFAULT_COMPARE_MODE = False
DEFAULT_WORKER_TIMEOUT_SECONDS = 60 * 60
DEFAULT_TRANSCRIPTION_WORKER_COUNT = 2
# Whisper decoding hint. Operators supply their own site/customer/employee term
# hints via the BTQ_WHISPER_INITIAL_PROMPT env var (set in the whisper-watch
# launchd environment) so real names and customer identities never live in the
# repo. The checked-in fallback is generic janitorial-domain vocabulary only.
_DEFAULT_INITIAL_PROMPT_FALLBACK = (
    "VCT, air movers, slop sink, autoscrubber, day porter, restroom, "
    "dumpster, loading dock, stripping and waxing"
)


def _resolve_initial_prompt() -> str:
    return os.environ.get("BTQ_WHISPER_INITIAL_PROMPT", "").strip() or _DEFAULT_INITIAL_PROMPT_FALLBACK


DEFAULT_INITIAL_PROMPT = _resolve_initial_prompt()
SUPPORTED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".webm"}
LOCAL_WHISPER_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PERSONAL_JOURNAL_TRIGGER_PATTERN = re.compile(
    r"^\s*(?:this\s+is\s+a\s+)?personal\s+(?:journal|note)\b[\s:,.!-]*",
    re.IGNORECASE,
)


class TranscriptionPipelineError(Exception):
    """Raised when the transcription pipeline cannot proceed safely."""


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class TranscriptEvaluation:
    length_difference: int
    sentence_count_difference: int


@dataclass(frozen=True)
class TranscriptionProfile:
    label: str
    model_name: str
    options: dict[str, Any]


@dataclass(frozen=True)
class TranscriptMetrics:
    character_count: int
    word_count: int


@dataclass(frozen=True)
class TranscriptionRun:
    baseline_profile: TranscriptionProfile | None
    enhanced_profile: TranscriptionProfile
    baseline_metrics: TranscriptMetrics | None
    enhanced_metrics: TranscriptMetrics
    evaluation: TranscriptEvaluation | None
    compare_dir: str | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch iCloud voice inbox and run transcription -> events -> queue jobs.")
    parser.add_argument("--inbox-dir", type=Path, default=DEFAULT_INBOX_DIR)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL_ROOT)
    parser.add_argument("--local-runtime-dir", type=Path, default=DEFAULT_LOCAL_RUNTIME_DIR)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--compare-mode", action="store_true", default=DEFAULT_COMPARE_MODE)
    parser.add_argument("--initial-prompt", default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument("--no-initial-prompt", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--stable-seconds", type=float, default=DEFAULT_STABLE_SECONDS)
    parser.add_argument("--worker-timeout-seconds", type=float, default=DEFAULT_WORKER_TIMEOUT_SECONDS)
    parser.add_argument("--worker-count", type=int, default=DEFAULT_TRANSCRIPTION_WORKER_COUNT)
    parser.add_argument("--whisper-url", default=os.environ.get("BTQ_WHISPER_URL"))
    parser.add_argument("--no-voice-memo-intake", action="store_true", help="Skip the optional voice-memo CouchDB intake poll before scanning.")
    parser.add_argument("--once", action="store_true", help="Process one scan and exit.")
    parser.add_argument("--watch", action="store_true", help="Continuously watch the iCloud inbox.")
    args = parser.parse_args(argv)
    if args.worker_count < 1:
        logging.getLogger("transcription_pipeline").warning(
            "worker-count below one; degrading to sequential worker_count=%s",
            args.worker_count,
        )
        args.worker_count = 1
    return args


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("transcription_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def ensure_ffmpeg_on_path() -> None:
    current_path = os.environ.get("PATH", "")
    path_parts = [part for part in current_path.split(":") if part]
    for candidate in reversed(DEFAULT_FFMPEG_PATH_PREFIX.split(":")):
        if candidate and candidate not in path_parts:
            path_parts.insert(0, candidate)
    os.environ["PATH"] = ":".join(path_parts)


def iter_audio_files(inbox_dir: Path) -> list[Path]:
    inbox_dir.mkdir(parents=True, exist_ok=True)
    return sorted(path for path in inbox_dir.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS)


def build_fingerprint(path: Path) -> FileFingerprint:
    stat = path.stat()
    return FileFingerprint(path=str(path.resolve()), size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def is_stable_file(path: Path, stable_seconds: float, now: float | None = None) -> bool:
    current_time = time.time() if now is None else now
    stat = path.stat()
    return current_time - stat.st_mtime >= stable_seconds


def transcript_path_for(audio_path: Path) -> Path:
    return audio_path.with_suffix(f"{audio_path.suffix}.whisper.txt")


def processed_marker_path(audio_path: Path) -> Path:
    return audio_path.with_name(f"{audio_path.name}.processed")


def marker_exists(audio_path: Path) -> bool:
    return processed_marker_path(audio_path).exists()


def archive_processed_marker(audio_path: Path, archive_dir: Path) -> Path | None:
    marker_path = processed_marker_path(audio_path)
    if not marker_path.exists():
        return None
    destination = unique_destination_path(archive_dir, marker_path.name)
    safe_move(marker_path, destination)
    return destination


def write_processed_marker(audio_path: Path, processed_at: datetime | None = None) -> Path:
    marker_path = processed_marker_path(audio_path)
    timestamp = datetime.now(timezone.utc) if processed_at is None else processed_at
    atomic_write_text(marker_path, timestamp.isoformat() + "\n")
    return marker_path


def transcript_metrics(text: str) -> TranscriptMetrics:
    stripped = text.strip()
    return TranscriptMetrics(
        character_count=len(stripped),
        word_count=len(stripped.split()) if stripped else 0,
    )


def evaluate_transcript_quality(original: str, enhanced: str) -> dict[str, int]:
    original_sentences = split_sentences(original)
    enhanced_sentences = split_sentences(enhanced)
    evaluation = TranscriptEvaluation(
        length_difference=len(enhanced) - len(original),
        sentence_count_difference=len(enhanced_sentences) - len(original_sentences),
    )
    return asdict(evaluation)


def build_simple_diff(original: str, enhanced: str) -> str:
    original_lines = original.splitlines()
    enhanced_lines = enhanced.splitlines()
    line_count = max(len(original_lines), len(enhanced_lines))
    diff_lines: list[str] = []
    for index in range(line_count):
        original_line = original_lines[index] if index < len(original_lines) else ""
        enhanced_line = enhanced_lines[index] if index < len(enhanced_lines) else ""
        if original_line == enhanced_line:
            diff_lines.append(f"  {original_line}")
            continue
        if original_line:
            diff_lines.append(f"- {original_line}")
        if enhanced_line:
            diff_lines.append(f"+ {enhanced_line}")
    return "\n".join(diff_lines).rstrip() + "\n"


def compare_output_dir(compare_root: Path, audio_path: Path, now: datetime | None = None) -> Path:
    recorded_at = datetime.now(timezone.utc) if now is None else now
    return compare_root / recorded_at.date().isoformat() / audio_path.stem


def transcription_log_payload(transcribe: Callable[[Path], str]) -> dict[str, Any] | None:
    run = getattr(transcribe, "last_run", None)
    if run is None:
        return None
    payload = asdict(run)
    payload["compare_mode"] = run.compare_dir is not None
    return payload


def current_memory_usage_mb() -> float | None:
    try:
        import resource
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return usage / (1024 * 1024)
    return usage / 1024


class WorkerTranscriptionError(TranscriptionPipelineError):
    """Raised when a short-lived transcription worker fails."""


class SubprocessWhisperTranscriber:
    """Runs Whisper in a child process so model memory dies with the process.

    Whisper and PyTorch can retain large allocations even after Python objects
    are deleted. On Apple Silicon this can leave a long-running watcher sitting
    on several GB of idle RAM. The reliable cleanup boundary is process teardown:
    the parent stays small, the child loads the model only for active work, and
    macOS reclaims the model memory when that child exits.
    """

    def __init__(
        self,
        model_name: str,
        local_root: Path,
        compare_mode: bool = False,
        initial_prompt: str | None = DEFAULT_INITIAL_PROMPT,
        timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._model_name = model_name
        self._local_root = local_root
        self._compare_mode = compare_mode
        self._initial_prompt = initial_prompt
        self._timeout_seconds = timeout_seconds
        self._logger = logger
        self.last_run: TranscriptionRun | None = None

    def __call__(self, audio_path: Path) -> str:
        output_dir = self._local_root / "worker_outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{audio_path.name}.{uuid.uuid4().hex}.worker.json"
        command = [
            sys.executable,
            "-m",
            "transcription_pipeline.worker",
            "--audio-path",
            str(audio_path),
            "--output-path",
            str(output_path),
            "--local-root",
            str(self._local_root),
            "--model",
            self._model_name,
        ]
        if self._compare_mode:
            command.append("--compare-mode")
        if self._initial_prompt is None:
            command.append("--no-initial-prompt")
        else:
            command.extend(["--initial-prompt", self._initial_prompt])

        if self._logger is not None:
            self._logger.info(
                "worker start audio=%s model=%s compare_mode=%s parent_maxrss_mb=%s",
                audio_path,
                self._model_name,
                self._compare_mode,
                current_memory_usage_mb(),
            )
        started_at = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkerTranscriptionError(
                f"transcription worker timed out after {self._timeout_seconds:.1f}s for {audio_path}"
            ) from exc

        elapsed = time.monotonic() - started_at
        if self._logger is not None:
            self._logger.info(
                "worker exit audio=%s returncode=%s duration_seconds=%.3f parent_maxrss_mb=%s",
                audio_path,
                completed.returncode,
                elapsed,
                current_memory_usage_mb(),
            )
            if completed.stdout.strip():
                self._logger.info("worker stdout audio=%s output=%s", audio_path, completed.stdout.strip())
            if completed.stderr.strip():
                self._logger.warning("worker stderr audio=%s output=%s", audio_path, completed.stderr.strip())

        if completed.returncode != 0:
            raise WorkerTranscriptionError(
                f"transcription worker failed for {audio_path} with exit code {completed.returncode}"
            )
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise WorkerTranscriptionError(f"transcription worker did not write valid output for {audio_path}") from exc
        finally:
            try:
                output_path.unlink()
            except OSError:
                pass

        self.last_run = transcription_run_from_payload(payload.get("run"))
        return str(payload.get("text", "")).strip() + "\n"


class RemoteWhisperTranscriber:
    """Calls an OpenAI-compatible Whisper endpoint across the local network."""

    def __init__(
        self,
        whisper_url: str,
        model_name: str,
        timeout_seconds: float,
        logger: logging.Logger | None = None,
    ) -> None:
        self._whisper_url = whisper_url.rstrip("/")
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds
        self._logger = logger
        self.last_run: TranscriptionRun | None = None

    def __call__(self, audio_path: Path) -> str:
        endpoint = f"{self._whisper_url}/v1/audio/transcriptions"
        body, content_type = _multipart_whisper_body(audio_path, self._model_name)
        req = request.Request(
            endpoint,
            data=body,
            headers={"Accept": "application/json", "Content-Type": content_type},
            method="POST",
        )
        if self._logger is not None:
            self._logger.info(
                "remote whisper start audio=%s model=%s url=%s",
                audio_path,
                self._model_name,
                endpoint,
            )
        started_at = time.monotonic()
        try:
            with request.urlopen(req, timeout=self._timeout_seconds) as response:
                response_body = response.read()
                if response.status < 200 or response.status >= 300:
                    raise WorkerTranscriptionError(
                        f"remote whisper transcription failed for {audio_path}: HTTP {response.status}"
                    )
        except HTTPError as exc:
            raise WorkerTranscriptionError(
                f"remote whisper transcription failed for {audio_path}: HTTP {exc.code}"
            ) from exc
        except TimeoutError as exc:
            raise WorkerTranscriptionError(
                f"remote whisper transcription timed out after {self._timeout_seconds:.1f}s for {audio_path}"
            ) from exc
        except (URLError, OSError) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError) or "timed out" in str(exc).lower():
                raise WorkerTranscriptionError(
                    f"remote whisper transcription timed out after {self._timeout_seconds:.1f}s for {audio_path}"
                ) from exc
            raise WorkerTranscriptionError(f"remote whisper transcription failed for {audio_path}: {exc}") from exc

        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerTranscriptionError(f"remote whisper returned invalid JSON for {audio_path}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise WorkerTranscriptionError(f"remote whisper returned no transcript text for {audio_path}")

        text = payload["text"].strip() + "\n"
        self.last_run = TranscriptionRun(
            baseline_profile=None,
            enhanced_profile=TranscriptionProfile(label="remote", model_name=self._model_name, options={}),
            baseline_metrics=None,
            enhanced_metrics=transcript_metrics(text),
            evaluation=None,
            compare_dir=None,
        )
        if self._logger is not None:
            elapsed = time.monotonic() - started_at
            self._logger.info(
                "remote whisper finished audio=%s duration_seconds=%.3f chars=%s words=%s",
                audio_path,
                elapsed,
                self.last_run.enhanced_metrics.character_count,
                self.last_run.enhanced_metrics.word_count,
            )
        return text


def _multipart_whisper_body(audio_path: Path, model_name: str) -> tuple[bytes, str]:
    boundary = f"btq-whisper-{uuid.uuid4().hex}"
    chunks = [
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="model"\r\n\r\n',
        model_name.encode("utf-8"),
        b"\r\n",
        f"--{boundary}\r\n".encode("ascii"),
        f'Content-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\n'.encode("utf-8"),
        b"Content-Type: application/octet-stream\r\n\r\n",
        audio_path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def validate_local_whisper_url(whisper_url: str) -> None:
    parsed = urlsplit(whisper_url)
    if parsed.scheme not in {"http", "https"}:
        raise TranscriptionPipelineError("BTQ transcription requires a local Whisper http(s) URL")
    if not _is_local_whisper_host(parsed.hostname):
        raise TranscriptionPipelineError(
            "BTQ transcription only supports local Whisper endpoints; cloud transcription APIs are not allowed"
        )


_TAILSCALE_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_local_whisper_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname in LOCAL_WHISPER_HOSTS:
        return True
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr in _TAILSCALE_CGNAT_NETWORK


def transcription_run_from_payload(payload: Any) -> TranscriptionRun | None:
    if not isinstance(payload, dict):
        return None

    def profile_from(value: Any) -> TranscriptionProfile | None:
        if not isinstance(value, dict):
            return None
        return TranscriptionProfile(
            label=str(value.get("label", "")),
            model_name=str(value.get("model_name", "")),
            options=dict(value.get("options") or {}),
        )

    def metrics_from(value: Any) -> TranscriptMetrics | None:
        if not isinstance(value, dict):
            return None
        return TranscriptMetrics(
            character_count=int(value.get("character_count", 0)),
            word_count=int(value.get("word_count", 0)),
        )

    def evaluation_from(value: Any) -> TranscriptEvaluation | None:
        if not isinstance(value, dict):
            return None
        return TranscriptEvaluation(
            length_difference=int(value.get("length_difference", 0)),
            sentence_count_difference=int(value.get("sentence_count_difference", 0)),
        )

    enhanced_profile = profile_from(payload.get("enhanced_profile"))
    enhanced_metrics = metrics_from(payload.get("enhanced_metrics"))
    if enhanced_profile is None or enhanced_metrics is None:
        return None
    return TranscriptionRun(
        baseline_profile=profile_from(payload.get("baseline_profile")),
        enhanced_profile=enhanced_profile,
        baseline_metrics=metrics_from(payload.get("baseline_metrics")),
        enhanced_metrics=enhanced_metrics,
        evaluation=evaluation_from(payload.get("evaluation")),
        compare_dir=payload.get("compare_dir"),
    )


def personal_journal_body(transcript_text: str) -> str | None:
    match = PERSONAL_JOURNAL_TRIGGER_PATTERN.match(transcript_text)
    if match is None:
        return None
    body = transcript_text[match.end():].strip()
    return body or None


def sidecar_path_for_audio(audio_path: Path) -> Path:
    return audio_path.with_suffix(".metadata.json")


def load_voice_memo_sidecar(audio_path: Path, logger: logging.Logger) -> dict[str, Any] | None:
    sidecar_path = sidecar_path_for_audio(audio_path)
    if not sidecar_path.exists():
        return None
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("voice-memo sidecar ignored path=%s error=%s", sidecar_path, exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("voice-memo sidecar ignored path=%s error=non-object-json", sidecar_path)
        return None
    if payload.get("source") != "voice_memo":
        return None
    return payload


VOICE_MEMO_DISPATCH_FLAGS = {"site_tagged", "employee_tagged", "general"}


def sidecar_employee_structures(sidecar: dict[str, Any]) -> list[dict[str, str]]:
    slugs = sidecar.get("employee_slugs") if isinstance(sidecar.get("employee_slugs"), list) else []
    names = sidecar.get("employee_names") if isinstance(sidecar.get("employee_names"), list) else []
    employees: list[dict[str, str]] = []
    for index, slug in enumerate(slugs):
        slug_text = str(slug or "").strip()
        if not slug_text:
            continue
        name_text = str(names[index] or "").strip() if index < len(names) else ""
        employee = {"slug": slug_text, "source": "voice_memo_picker"}
        if name_text:
            employee["name"] = name_text
        employees.append(employee)
    return employees


def sidecar_site_label(sidecar: dict[str, Any]) -> str:
    account = str(sidecar.get("site_account") or "").strip()
    location = str(sidecar.get("site_location") or "").strip()
    if account and location:
        return f"{account} - {location}"
    if location:
        return location
    return ""


def voice_memo_semantic_context(
    *,
    capture_id: str,
    routing_flag: str,
    transcript_text: str,
    transcript_path: Path,
    audio_file_name: str,
    sidecar: dict[str, Any],
    recorded_at: datetime,
) -> VoiceMemoTranscriptContext:
    return VoiceMemoTranscriptContext(
        capture_id=capture_id,
        routing_flag=routing_flag,
        raw_text=transcript_text.strip(),
        raw_transcript_path=transcript_path,
        audio_file=audio_file_name,
        site_id=str(sidecar.get("site_id") or "").strip(),
        site=sidecar_site_label(sidecar),
        employees=sidecar_employee_structures(sidecar),
        note=str(sidecar.get("note") or "").strip(),
        captured_at=str(sidecar.get("captured_at") or recorded_at.isoformat()),
    )


def voice_memo_semantic_event_metadata(semantic_pass: VoiceMemoSemanticPass | None) -> dict[str, Any]:
    if semantic_pass is None:
        return {}
    if semantic_pass.result is None and not semantic_pass.error:
        return {}
    metadata: dict[str, Any] = {
        "voice_memo_semantic_artifact_path": str(semantic_pass.artifact_path),
    }
    if semantic_pass.candidate_paths:
        candidate_paths = [str(path) for path in semantic_pass.candidate_paths]
        metadata["voice_memo_action_candidate_paths"] = candidate_paths
        metadata["voice_memo_action_candidate_count"] = len(candidate_paths)
        metadata["voice_memo_action_candidate_path"] = candidate_paths[0]
    if semantic_pass.error:
        metadata["voice_memo_semantic_status"] = "failed"
        metadata["voice_memo_semantic_error"] = semantic_pass.error
        return metadata
    if semantic_pass.result is not None:
        result = semantic_pass.result
        actions = list(result.extracted_actions or [])
        primary = actions[0] if actions else None
        primary_intent = "unknown"
        primary_action = ""
        primary_target_type = "general"
        primary_target_id = ""
        primary_confidence: Any = "normal"
        if primary is not None:
            primary_intent = primary.action_key
            primary_action = primary.action_key
            primary_target_type = primary.target_type
            primary_target_id = primary.target_id
            primary_confidence = primary.confidence
            if primary.job_type == "set_entity_status":
                status = ""
                if isinstance(primary.payload_fields, dict):
                    status = str(primary.payload_fields.get("status") or "")
                primary_action = "set_inactive" if status == "inactive" else "status_change"
                primary_intent = "site_status_change" if primary.target_type == "site" else "employee_status_change"
        metadata.update(
            {
                "voice_memo_semantic_status": "complete",
                "voice_memo_semantic_action_count": len(actions),
                "voice_memo_semantic_actions": [
                    {
                        "action_key": action.action_key,
                        "candidate_type": action.candidate_type,
                        "job_type": action.job_type,
                        "target_type": action.target_type,
                        "target_id": action.target_id,
                        "target_label": action.target_label,
                        "summary": action.summary,
                        "confidence": action.confidence,
                    }
                    for action in actions
                ],
                "voice_memo_semantic_primary_intent": primary_intent,
                "voice_memo_semantic_primary_action": primary_action,
                "voice_memo_semantic_primary_target_type": primary_target_type,
                "voice_memo_semantic_primary_target_id": primary_target_id,
                "voice_memo_semantic_primary_confidence": primary_confidence,
                "voice_memo_semantic_review_required": bool(actions),
                "voice_memo_semantic_intent": primary_intent,
                "voice_memo_semantic_action": primary_action,
                "voice_memo_semantic_target_type": primary_target_type,
                "voice_memo_semantic_target_id": primary_target_id,
                "voice_memo_semantic_confidence": primary_confidence,
            }
        )
    return metadata


def voice_memo_sidecar_has_context(sidecar: dict[str, Any]) -> bool:
    return bool(sidecar.get("site_id") or sidecar.get("employee_slugs") or sidecar.get("geolocation"))


def augment_event_with_voice_memo_sidecar(
    event: dict[str, Any],
    sidecar: dict[str, Any],
    *,
    transcript_text: str,
    transcript_path: Path,
    audio_file_name: str,
    semantic_pass: VoiceMemoSemanticPass | None = None,
) -> dict[str, Any]:
    augmented = dict(event)
    site_id = str(sidecar.get("site_id") or "").strip()
    if site_id:
        augmented["site_id"] = site_id
        site_label = sidecar_site_label(sidecar)
        if site_label:
            augmented["site"] = site_label
        augmented["site_source"] = "voice_memo_picker"
    employees = sidecar_employee_structures(sidecar)
    if employees:
        augmented["employees"] = employees
    if isinstance(sidecar.get("geolocation"), dict):
        augmented["geolocation"] = sidecar["geolocation"]
    capture_id = str(sidecar.get("capture_id") or "").strip()
    routing_flag = str(sidecar.get("routing_flag") or "").strip()
    if capture_id:
        augmented["capture_id"] = capture_id
        augmented["voice_memo_capture_id"] = capture_id
    if routing_flag:
        augmented["voice_memo_routing_flag"] = routing_flag
    note = str(sidecar.get("note") or "").strip()
    if note:
        augmented["note"] = note
    person_id = str(sidecar.get("person_id") or "").strip()
    if person_id:
        augmented["visited_by"] = person_id
    augmented["transcript_text"] = transcript_text.strip()
    augmented["raw_transcript_path"] = str(transcript_path)
    augmented["audio_file"] = audio_file_name
    augmented.update(voice_memo_semantic_event_metadata(semantic_pass))
    return augmented


def build_voice_memo_observation_event(
    *,
    transcript_text: str,
    sidecar: dict[str, Any],
    transcript_path: Path,
    audio_file_name: str,
    recorded_at: datetime,
    semantic_pass: VoiceMemoSemanticPass | None = None,
) -> dict[str, Any]:
    capture_id = str(sidecar.get("capture_id") or "").strip()
    excerpt = " ".join(transcript_text.strip().split())[:280]
    site_label = sidecar_site_label(sidecar)
    event = {
        "event_id": f"{capture_id}-voice-memo-observation",
        "type": "voice_memo_observation",
        "site": site_label,
        "site_id": str(sidecar.get("site_id") or "").strip(),
        "source_excerpt": excerpt,
        "transcript_text": transcript_text.strip(),
        "note": str(sidecar.get("note") or "").strip(),
        "employees": sidecar_employee_structures(sidecar),
        "capture_id": capture_id,
        "voice_memo_capture_id": capture_id,
        "voice_memo_routing_flag": str(sidecar.get("routing_flag") or "").strip(),
        "raw_transcript_path": str(transcript_path),
        "audio_file": audio_file_name,
        "timestamp": str(sidecar.get("captured_at") or recorded_at.isoformat()),
    }
    if isinstance(sidecar.get("geolocation"), dict):
        event["geolocation"] = sidecar["geolocation"]
    event.update(voice_memo_semantic_event_metadata(semantic_pass))
    return event


def augment_voice_memo_event_paths(
    valid_paths: list[Path],
    sidecar: dict[str, Any],
    *,
    transcript_text: str,
    transcript_path: Path,
    audio_file_name: str,
    semantic_pass: VoiceMemoSemanticPass | None = None,
) -> list[Path]:
    updated_paths: list[Path] = []
    for path in valid_paths:
        event = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(event, dict):
            continue
        augmented = augment_event_with_voice_memo_sidecar(
            event,
            sidecar,
            transcript_text=transcript_text,
            transcript_path=transcript_path,
            audio_file_name=audio_file_name,
            semantic_pass=semantic_pass,
        )
        atomic_write_text(path, json.dumps(augmented, indent=2, sort_keys=True) + "\n")
        updated_paths.append(path)
    return updated_paths


def is_significant_sentence(sentence: str) -> bool:
    return len(sentence.split()) >= 6


def find_unmapped_sentences(original_text: str, event_paths: list[Path]) -> list[str]:
    sentences = [sentence for sentence in split_sentences(original_text) if is_significant_sentence(sentence)]
    if not sentences:
        return []

    excerpts: list[str] = []
    for path in event_paths:
        event = json.loads(path.read_text(encoding="utf-8"))
        excerpt = str(event.get("source_excerpt", "")).strip()
        if excerpt:
            excerpts.append(excerpt.lower())

    unmatched: list[str] = []
    for sentence in sentences:
        lowered_sentence = sentence.lower()
        if not any(lowered_sentence in excerpt or excerpt in lowered_sentence for excerpt in excerpts):
            unmatched.append(sentence)
    return unmatched


def missed_job_path_for(queue_jobs_dir: Path, audio_file_name: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", audio_file_name).strip("-")
    return queue_jobs_dir / f"job_missed_{safe_name}.json"


def personal_journal_job_path_for(queue_jobs_dir: Path, audio_file_name: str, entry_id: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", audio_file_name).strip("-")
    digest = entry_id.replace("personal-", "", 1)[:16]
    return queue_jobs_dir / f"job_personal_{safe_name}_{digest}.json"


def personal_entry_id(audio_file_name: str, body: str, recorded_at: datetime) -> str:
    digest = hashlib.sha256(f"{audio_file_name}\n{recorded_at.isoformat()}\n{body.strip()}".encode("utf-8")).hexdigest()
    return f"personal-{digest}"


def emit_personal_journal_job(
    queue_jobs_dir: Path,
    audio_file_name: str,
    body: str,
    raw_transcript_path: Path,
    recorded_at: datetime,
    capture_id: str | None = None,
    source_note: str | None = None,
) -> Path | None:
    queue_jobs_dir.mkdir(parents=True, exist_ok=True)
    entry_id = personal_entry_id(audio_file_name, body, recorded_at)
    job_path = personal_journal_job_path_for(queue_jobs_dir, audio_file_name, entry_id)
    if job_path.exists():
        return None
    payload = {
        "job_id": entry_id,
        "job_type": "personal_journal_entry",
        "metadata": {"capture_id": capture_id} if capture_id is not None else {},
        "intent": {
            "category": "personal_journal",
            "reason": "personal journal trigger",
            "source_context": audio_file_name,
            "operator_relevance": "personal",
            "confidence": "operator_triggered",
        },
        "payload": {
            "date": recorded_at.date().isoformat(),
            "timestamp": recorded_at.isoformat(),
            "audio_file": audio_file_name,
            "body": body.strip(),
            "raw_transcript_path": str(raw_transcript_path),
        },
    }
    if source_note:
        payload["payload"]["source_note"] = source_note.strip()
    atomic_write_text(job_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return job_path


def build_missed_entry(
    timestamp: str,
    audio_file_name: str,
    original_text: str,
    normalized_text: str,
    events_created: int,
    status: str,
    reason_heading: str,
    reasons: list[str],
    capture_id: str | None = None,
) -> str:
    capture_line = f"capture_id: {capture_id}\n" if capture_id is not None else ""
    return (
        f"---\n"
        f"type: unknown_capture\n"
        f"{capture_line}"
        f"timestamp: {timestamp}\n"
        f"audio_file: {audio_file_name}\n"
        f"status: unresolved\n"
        f"retry_count: 0\n"
        f"last_attempted: null\n"
        f"events_created: {events_created}\n"
        f"capture_status: {status}\n"
        f"reason_heading: {reason_heading}\n"
        f"reasons: {' | '.join(reasons)}\n"
        f"---\n\n"
        f"## Original Transcript\n"
        f"{original_text.strip()}\n\n"
        f"## Normalized Transcript\n"
        f"{normalized_text.strip()}\n\n"
        f"## Notes\n"
        f"{status}\n"
    )


def build_site_audio_memo_entry(
    timestamp: str,
    audio_file_name: str,
    site: str,
    original_text: str,
    normalized_text: str,
    events_created: int,
    status: str,
    reason_heading: str,
    reasons: list[str],
    capture_id: str | None = None,
) -> str:
    capture_line = f"capture_id: {capture_id}\n" if capture_id is not None else ""
    return (
        f"---\n"
        f"type: site_audio_memo\n"
        f"{capture_line}"
        f"timestamp: {timestamp}\n"
        f"audio_file: {audio_file_name}\n"
        f"site: {site}\n"
        f"status: needs_review\n"
        f"events_created: {events_created}\n"
        f"capture_status: {status}\n"
        f"reason_heading: {reason_heading}\n"
        f"reasons: {' | '.join(reasons)}\n"
        f"---\n\n"
        f"## Original Transcript\n"
        f"{original_text.strip()}\n\n"
        f"## Normalized Transcript\n"
        f"{normalized_text.strip()}\n\n"
        f"## Notes\n"
        f"{status}\n"
    )


def site_from_event_paths(event_paths: list[Path]) -> str | None:
    for path in event_paths:
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        site = str(event.get("site") or "").strip()
        if site and site.lower() != "unknown":
            return site
    return None


def emit_missed_capture_job(
    queue_jobs_dir: Path,
    audio_file_name: str,
    original_text: str,
    normalized_text: str,
    event_paths: list[Path],
    processed_at: datetime,
    capture_id: str | None = None,
) -> Path | None:
    unmatched_sentences = find_unmapped_sentences(original_text, event_paths)
    reasons: list[str] = []
    if not event_paths:
        status = "#unknown #needs-review"
        reason_heading = "Why Unknown"
        reasons.append("No strong event pattern matched")
    elif unmatched_sentences:
        status = "#partial #needs-review"
        reason_heading = "Why Review"
        reasons.append("Partial extraction")
    else:
        return None

    site = site_from_event_paths(event_paths)
    site_confidence = ""
    if site is None:
        resolved_site, site_confidence = resolve_site(f"{original_text}\n{normalized_text}")
        if resolved_site != "unknown":
            site = resolved_site

    if site is None:
        reasons.insert(0, "No site detected")

    queue_jobs_dir.mkdir(parents=True, exist_ok=True)
    job_path = missed_job_path_for(queue_jobs_dir, audio_file_name)
    if job_path.exists():
        return None

    note_path = resolve_site_note_path(site) if site is not None else None
    if note_path is not None:
        reasons.insert(0, f"Site detected: {site}")
        entry = build_site_audio_memo_entry(
            processed_at.isoformat(),
            audio_file_name,
            site,
            original_text,
            normalized_text,
            len(event_paths),
            status,
            "Why Site Note",
            reasons,
            capture_id,
        )
        payload = {
            "job_type": "append_to_note",
            "metadata": {"capture_id": capture_id} if capture_id is not None else {},
            "intent": {
                "category": "site_audio_memo",
                "reason": "site detected but no complete deterministic event route",
                "source_context": normalized_text[:500],
                "operator_relevance": "needs-review",
                "confidence": site_confidence or ("partial" if event_paths else "medium"),
            },
            "payload": {
                "path": note_path,
                "content": entry,
                "destination": "site_note",
            },
        }
        atomic_write_text(job_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return job_path

    daily_path = f"Journal/{processed_at.date().isoformat()}-unknown.md"
    entry = build_missed_entry(
        processed_at.isoformat(),
        audio_file_name,
        original_text,
        normalized_text,
        len(event_paths),
        status,
        reason_heading,
        reasons,
        capture_id,
    )
    payload = {
        "job_type": "record_unknown_capture",
        "metadata": {"capture_id": capture_id} if capture_id is not None else {},
        "intent": {
            "category": "unknown_capture",
            "reason": "no complete deterministic event route",
            "source_context": normalized_text[:500],
            "operator_relevance": "needs-review",
            "confidence": "low" if not event_paths else "partial",
        },
        "payload": {
            "path": daily_path,
            "content": entry,
            "timestamp": processed_at.isoformat(),
            "audio_file": audio_file_name,
            "original_transcript": original_text,
            "normalized_transcript": normalized_text,
            "capture_status": status,
            "events_created": len(event_paths),
            "reason_heading": reason_heading,
            "reasons": reasons,
            **({"capture_id": capture_id} if capture_id is not None else {}),
        },
    }
    atomic_write_text(job_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return job_path


def stage_queue_jobs(runtime_root: Path, job_paths: list[Path]) -> list[Path]:
    queue_dir = runtime_root / "queue"
    temp_dir = runtime_root / "temp" / "queue-stage"
    queue_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("transcription_pipeline")
    staged_paths: list[Path] = []
    for job_path in job_paths:
        destination = queue_dir / job_path.name
        if destination.exists():
            logger.info("queue job already staged; skipping source=%s destination=%s", job_path, destination)
            continue
        lock_path = temp_dir / f"{job_path.name}.lock"
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            logger.info("queue job staging already in progress; skipping source=%s destination=%s", job_path, destination)
            continue
        os.close(lock_fd)
        temp_path: Path | None = None
        try:
            fd, temp_path_str = tempfile.mkstemp(prefix=f".{job_path.name}.", suffix=".tmp", dir=temp_dir)
            temp_path = Path(temp_path_str)
            with os.fdopen(fd, "wb") as temp_file, job_path.open("rb") as source_file:
                while chunk := source_file.read(1024 * 1024):
                    temp_file.write(chunk)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            if destination.exists():
                logger.info("queue job already staged; skipping source=%s destination=%s", job_path, destination)
                temp_path.unlink()
                continue
            os.replace(temp_path, destination)
        except Exception:
            try:
                if temp_path is not None and temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            raise
        finally:
            try:
                if lock_path.exists():
                    lock_path.unlink()
            except OSError:
                pass
        staged_paths.append(destination)
    return staged_paths


def process_btq_queue_jobs(local_root: Path, job_paths: list[Path]) -> int:
    if not job_paths:
        return 0
    config = get_config()
    runtime_root = config.local_runtime_dir if local_root == config.local_root else local_root / "runtime"
    staged_paths = stage_queue_jobs(runtime_root, job_paths)
    return len(staged_paths)


class WhisperTranscriber:
    def __init__(
        self,
        baseline_model: Any,
        enhanced_model: Any,
        baseline_profile: TranscriptionProfile,
        enhanced_profile: TranscriptionProfile,
        compare_mode: bool,
        compare_root: Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self._baseline_model = baseline_model
        self._enhanced_model = enhanced_model
        self._baseline_profile = baseline_profile
        self._enhanced_profile = enhanced_profile
        self._compare_mode = compare_mode
        self._compare_root = compare_root
        self._logger = logger
        self.last_run: TranscriptionRun | None = None

    def _transcribe(self, model: Any, audio_path: Path, profile: TranscriptionProfile) -> str:
        result = model.transcribe(str(audio_path), **profile.options)
        text = str(result.get("text", ""))
        return text.strip() + "\n"

    def _write_compare_outputs(self, audio_path: Path, original: str, enhanced: str) -> str:
        output_dir = compare_output_dir(self._compare_root, audio_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output_dir / "original.txt", original)
        atomic_write_text(output_dir / "enhanced.txt", enhanced)
        atomic_write_text(output_dir / "diff.txt", build_simple_diff(original, enhanced))
        return str(output_dir)

    def __call__(self, audio_path: Path) -> str:
        if self._logger is not None:
            self._logger.info(
                "starting transcription audio=%s enhanced_model=%s compare_mode=%s",
                audio_path,
                self._enhanced_profile.model_name,
                self._compare_mode,
            )
        baseline_text: str | None = None
        baseline_metrics: TranscriptMetrics | None = None
        compare_dir: str | None = None
        evaluation: dict[str, int] | None = None

        if self._compare_mode:
            baseline_text = self._transcribe(self._baseline_model, audio_path, self._baseline_profile)
            baseline_metrics = transcript_metrics(baseline_text)

        enhanced_text = self._transcribe(self._enhanced_model, audio_path, self._enhanced_profile)
        enhanced_metrics = transcript_metrics(enhanced_text)

        if baseline_text is not None:
            compare_dir = self._write_compare_outputs(audio_path, baseline_text, enhanced_text)
            evaluation = evaluate_transcript_quality(baseline_text, enhanced_text)

        self.last_run = TranscriptionRun(
            baseline_profile=self._baseline_profile if self._compare_mode else None,
            enhanced_profile=self._enhanced_profile,
            baseline_metrics=baseline_metrics,
            enhanced_metrics=enhanced_metrics,
            evaluation=TranscriptEvaluation(**evaluation) if evaluation is not None else None,
            compare_dir=compare_dir,
        )
        if self._logger is not None:
            self._logger.info(
                "finished transcription audio=%s chars=%s words=%s compare_dir=%s",
                audio_path,
                enhanced_metrics.character_count,
                enhanced_metrics.word_count,
                compare_dir,
            )
        return enhanced_text


def build_transcriber(
    model_name: str,
    local_root: Path,
    compare_mode: bool = False,
    initial_prompt: str | None = DEFAULT_INITIAL_PROMPT,
    logger: logging.Logger | None = None,
) -> Callable[[Path], str]:
    try:
        import torch
        import whisper
    except ImportError as exc:
        raise TranscriptionPipelineError(
            "The 'whisper' package is not installed in project/.venv. Install it before running the watcher."
        ) from exc

    use_fp16 = bool(torch.cuda.is_available())
    baseline_model = whisper.load_model(model_name) if compare_mode else None
    enhanced_model = whisper.load_model("large-v3")

    baseline_profile = TranscriptionProfile(
        label="baseline",
        model_name=model_name,
        options={"fp16": use_fp16},
    )
    enhanced_options: dict[str, Any] = {
        "fp16": use_fp16,
        "beam_size": 5,
        "best_of": 5,
        "temperature": 0.0,
        "language": "en",
        "condition_on_previous_text": True,
        "patience": 2.0,
        "word_timestamps": True,
    }
    if initial_prompt is not None and initial_prompt.strip():
        enhanced_options["initial_prompt"] = initial_prompt.strip()
    enhanced_profile = TranscriptionProfile(
        label="enhanced",
        model_name="large-v3",
        options=enhanced_options,
    )

    return WhisperTranscriber(
        baseline_model=baseline_model,
        enhanced_model=enhanced_model,
        baseline_profile=baseline_profile,
        enhanced_profile=enhanced_profile,
        compare_mode=compare_mode,
        compare_root=local_root / "transcripts",
        logger=logger,
    )


def unique_destination_path(destination_dir: Path, filename: str) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    candidate = destination_dir / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while True:
        candidate = destination_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def move_to_local_processing(source_path: Path, audio_processing_dir: Path) -> Path:
    destination = unique_destination_path(audio_processing_dir, source_path.name)
    safe_move(source_path, destination)
    return destination


def claim_audio_file(source_path: Path, local_runtime_dir: Path, logger: logging.Logger) -> Path:
    claimed_dir = local_runtime_dir / "claimed" / "audio"
    claimed_dir.mkdir(parents=True, exist_ok=True)
    claim_path = source_path.with_name(f"{source_path.name}.claiming")
    local_claim_path = unique_destination_path(claimed_dir, source_path.name)
    logger.info("claim started type=audio path=%s claim_path=%s", source_path, claim_path)
    safe_move(source_path, claim_path)
    try:
        safe_move(claim_path, local_claim_path)
    except Exception:
        if claim_path.exists() and not source_path.exists():
            try:
                safe_move(claim_path, source_path)
            except Exception:
                logger.exception("claim rollback failed type=audio claim_path=%s source=%s", claim_path, source_path)
        raise
    logger.info("local move success type=audio source=%s local_path=%s", source_path, local_claim_path)
    return local_claim_path


def archive_processed_audio(local_audio_path: Path, archive_dir: Path) -> Path:
    destination = unique_destination_path(archive_dir, local_audio_path.name)
    safe_move(local_audio_path, destination)
    return destination


def write_process_log(
    logs_dir: Path,
    audio_file: Path,
    transcript_file: Path | None,
    events_created: int,
    jobs_created: int,
    status: str,
    domain_corrections: list[dict[str, str]] | None = None,
    transcription: dict[str, Any] | None = None,
    capture_id: str | None = None,
) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    processed_at = datetime.now(timezone.utc)
    timestamp = processed_at.strftime("%Y%m%dT%H%M%S%fZ")
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", audio_file.stem).strip("-") or "audio"
    log_path = logs_dir / f"{safe_stem}.{timestamp}.{uuid.uuid4().hex[:8]}.json"
    payload = {
        "audio_file": str(audio_file),
        "processed_at": processed_at.isoformat(),
        "transcript_file": str(transcript_file) if transcript_file is not None else None,
        "events_created": events_created,
        "jobs_created": jobs_created,
        "status": status,
        "capture_id": capture_id,
        "domain_corrections": domain_corrections or [],
        "transcription": transcription,
    }
    write_json_object(log_path, payload)
    return log_path


def process_audio_file(
    source_fingerprint: FileFingerprint,
    local_root: Path,
    logger: logging.Logger,
    transcribe: Callable[[Path], str],
    process_generated_jobs: Callable[[Path, list[Path]], int] | None = None,
    archive_dir: Path | None = None,
    local_runtime_dir: Path | None = None,
    sidecar_metadata: dict[str, Any] | None = None,
    voice_memo_semantic_engine: VoiceMemoSemanticEngine | None = None,
) -> tuple[str, str | None, int, int]:
    source_path = Path(source_fingerprint.path)
    audio_processing_dir = local_root / "audio_processing"
    events_root = local_root
    queue_jobs_dir = local_root / "queue_jobs"
    logs_dir = local_root / "logs"
    audio_archive_dir = DEFAULT_CONFIG.audio_archive_dir if archive_dir is None else archive_dir
    runtime_root = DEFAULT_CONFIG.local_runtime_dir if local_runtime_dir is None else local_runtime_dir

    local_audio_path = audio_processing_dir / source_path.name
    transcript_path = transcript_path_for(local_audio_path)
    sidecar = sidecar_metadata if sidecar_metadata is not None else load_voice_memo_sidecar(source_path, logger)
    sidecar_capture_id = str(sidecar.get("capture_id") or "").strip() if sidecar else ""
    capture_id = sidecar_capture_id or f"cap-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}"

    try:
        if source_path.parent != audio_processing_dir:
            local_audio_path = move_to_local_processing(source_path, audio_processing_dir)
            transcript_path = transcript_path_for(local_audio_path)
        if sidecar is None:
            sidecar = load_voice_memo_sidecar(local_audio_path, logger)
            sidecar_capture_id = str(sidecar.get("capture_id") or "").strip() if sidecar else ""
            if sidecar_capture_id:
                capture_id = sidecar_capture_id
        logger.info("processing started type=audio local_path=%s", local_audio_path)
        logger.info("capture lineage started capture_id=%s audio=%s", capture_id, local_audio_path)
        transcript_text = transcribe(local_audio_path)
        transcription_payload = transcription_log_payload(transcribe)
        atomic_write_text(transcript_path, transcript_text)
        write_json_object(
            transcript_path.with_suffix(f"{transcript_path.suffix}.metadata.json"),
            transcript_metadata_payload(
                capture_id=capture_id,
                audio_file=str(local_audio_path),
                source_fingerprint=asdict(source_fingerprint),
                transcript_file=str(transcript_path),
            ),
        )
        processed_at = datetime.now(timezone.utc)
        recorded_at = datetime.fromtimestamp(source_fingerprint.mtime_ns / 1_000_000_000, timezone.utc)
        sidecar_routing_flag = str(sidecar.get("routing_flag") or "").strip() if sidecar else ""
        personal_body = transcript_text.strip() if sidecar_routing_flag == "personal_journal" else personal_journal_body(transcript_text)
        if personal_body is not None:
            job_path = emit_personal_journal_job(
                queue_jobs_dir,
                local_audio_path.name,
                personal_body,
                transcript_path,
                recorded_at,
                capture_id,
                source_note=str(sidecar.get("note") or "").strip() if sidecar else None,
            )
            generated_job_paths = [] if job_path is None else [job_path]
            if process_generated_jobs is not None:
                process_generated_jobs(local_root, generated_job_paths)
            process_log_path = write_process_log(
                logs_dir,
                source_path,
                transcript_path,
                0,
                len(generated_job_paths),
                "success",
                domain_corrections=[],
                transcription=transcription_payload,
                capture_id=capture_id,
            )
            create_capture_manifest(
                runtime_root,
                capture_id,
                audio_file=local_audio_path,
                source_fingerprint=asdict(source_fingerprint),
                transcript_file=transcript_path,
                normalized_transcript_file=None,
                event_paths=[],
                queue_job_paths=generated_job_paths,
                process_log=process_log_path,
            )
            archived_audio_path = archive_processed_audio(local_audio_path, audio_archive_dir)
            logger.info(
                "processing completed type=personal-journal capture_id=%s local_path=%s completed_path=%s transcript=%s jobs=%s",
                capture_id,
                local_audio_path,
                archived_audio_path,
                transcript_path,
                len(generated_job_paths),
            )
            return "success", str(transcript_path), 0, len(generated_job_paths)
        voice_memo_semantic_pass: VoiceMemoSemanticPass | None = None
        if sidecar is not None and sidecar_routing_flag in VOICE_MEMO_DISPATCH_FLAGS:
            voice_memo_semantic_pass = run_semantic_pass(
                voice_memo_semantic_context(
                    capture_id=capture_id,
                    routing_flag=sidecar_routing_flag,
                    transcript_text=transcript_text,
                    transcript_path=transcript_path,
                    audio_file_name=local_audio_path.name,
                    sidecar=sidecar,
                    recorded_at=recorded_at,
                ),
                runtime_root,
                engine=voice_memo_semantic_engine,
            )
            if voice_memo_semantic_pass.error:
                logger.warning(
                    "voice memo semantic pass failed capture_id=%s artifact=%s error=%s",
                    capture_id,
                    voice_memo_semantic_pass.artifact_path,
                    voice_memo_semantic_pass.error,
                )
        valid_paths, _failed_paths, domain_corrections = process_transcript(transcript_path, events_root, capture_id=capture_id)
        if sidecar is not None and sidecar_routing_flag in VOICE_MEMO_DISPATCH_FLAGS and voice_memo_sidecar_has_context(sidecar):
            valid_paths = augment_voice_memo_event_paths(
                valid_paths,
                sidecar,
                transcript_text=transcript_text,
                transcript_path=transcript_path,
                audio_file_name=local_audio_path.name,
                semantic_pass=voice_memo_semantic_pass,
            )
        if (
            sidecar is not None
            and sidecar_routing_flag in VOICE_MEMO_DISPATCH_FLAGS
            and not valid_paths
        ):
            fallback_event = build_voice_memo_observation_event(
                transcript_text=transcript_text,
                sidecar=sidecar,
                transcript_path=transcript_path,
                audio_file_name=local_audio_path.name,
                recorded_at=recorded_at,
                semantic_pass=voice_memo_semantic_pass,
            )
            fallback_dir = events_root / "events_valid"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            fallback_path = fallback_dir / f"{capture_id}-voice-memo.json"
            atomic_write_text(fallback_path, json.dumps(fallback_event, indent=2, sort_keys=True) + "\n")
            valid_paths = [fallback_path]
        job_paths = convert_event_paths(valid_paths, queue_jobs_dir)
        normalized_path = normalized_transcript_path_for(transcript_path)
        normalized_text = normalized_path.read_text(encoding="utf-8") if normalized_path.exists() else transcript_text
        missed_job_path = None
        if not (sidecar is not None and sidecar_routing_flag in VOICE_MEMO_DISPATCH_FLAGS and valid_paths):
            missed_job_path = emit_missed_capture_job(
                queue_jobs_dir,
                local_audio_path.name,
                transcript_text,
                normalized_text,
                valid_paths,
                processed_at,
                capture_id,
            )
        generated_job_paths = list(job_paths)
        if missed_job_path is not None:
            generated_job_paths.append(missed_job_path)
        total_job_count = len(generated_job_paths)
        if process_generated_jobs is not None:
            process_generated_jobs(local_root, generated_job_paths)
        process_log_path = write_process_log(
            logs_dir,
            source_path,
            transcript_path,
            len(valid_paths),
            total_job_count,
            "success",
            domain_corrections=domain_corrections,
            transcription=transcription_payload,
            capture_id=capture_id,
        )
        create_capture_manifest(
            runtime_root,
            capture_id,
            audio_file=local_audio_path,
            source_fingerprint=asdict(source_fingerprint),
            transcript_file=transcript_path,
            normalized_transcript_file=normalized_transcript_path_for(transcript_path),
            event_paths=valid_paths,
            queue_job_paths=generated_job_paths,
            process_log=process_log_path,
        )
        archived_audio_path = archive_processed_audio(local_audio_path, audio_archive_dir)
        logger.info(
            "processing completed type=audio capture_id=%s local_path=%s completed_path=%s transcript=%s events=%s jobs=%s",
            capture_id,
            local_audio_path,
            archived_audio_path,
            transcript_path,
            len(valid_paths),
            total_job_count,
        )
        if transcription_payload is not None:
            logger.info(
                "transcription enhanced_model=%s compare_mode=%s enhanced_chars=%s enhanced_words=%s",
                transcription_payload["enhanced_profile"]["model_name"],
                transcription_payload["compare_mode"],
                transcription_payload["enhanced_metrics"]["character_count"],
                transcription_payload["enhanced_metrics"]["word_count"],
            )
            if transcription_payload.get("baseline_profile") is not None:
                logger.info(
                    "transcription baseline_model=%s baseline_chars=%s baseline_words=%s compare_dir=%s",
                    transcription_payload["baseline_profile"]["model_name"],
                    transcription_payload["baseline_metrics"]["character_count"],
                    transcription_payload["baseline_metrics"]["word_count"],
                    transcription_payload["compare_dir"],
                )
        return "success", str(transcript_path), len(valid_paths), total_job_count
    except Exception:
        write_process_log(
            logs_dir,
            source_path,
            transcript_path if transcript_path.exists() else None,
            0,
            0,
            "failed",
            domain_corrections=[],
            transcription=transcription_log_payload(transcribe),
            capture_id=capture_id,
        )
        failed_audio_dir = runtime_root / "failed" / "audio"
        failed_source = local_audio_path if local_audio_path.exists() else source_path
        if failed_source.exists():
            try:
                failed_destination = unique_destination_path(failed_audio_dir, failed_source.name)
                safe_move(failed_source, failed_destination)
                logger.info("local move failure type=audio local_path=%s failed_path=%s", failed_source, failed_destination)
            except Exception:
                logger.exception("failed to move failed audio to local failed directory audio=%s", failed_source)
        logger.exception("failed to process audio=%s", source_path)
        raise


def _handle_ready_file(
    source_path: Path,
    archive_dir: Path,
    local_root: Path,
    local_runtime_root: Path,
    logger: logging.Logger,
    transcribe: Callable[[Path], str],
    process_generated_jobs: Callable[[Path, list[Path]], int] | None,
) -> int:
    logger.info("detected file type=audio path=%s", source_path)
    if marker_exists(source_path):
        try:
            claimed_path = claim_audio_file(source_path, local_runtime_root, logger)
            marker_destination = archive_processed_marker(source_path, archive_dir)
            archive_destination = archive_processed_audio(claimed_path, archive_dir)
            logger.info(
                "archived previously processed audio=%s archive_destination=%s marker_destination=%s",
                source_path,
                archive_destination,
                marker_destination,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("previously processed audio remains eligible for retry path=%s error=%s", source_path, exc)
        return 1
    fingerprint = build_fingerprint(source_path)
    logger.info(
        "claiming stable audio path=%s size=%s mtime_ns=%s",
        source_path,
        fingerprint.size,
        fingerprint.mtime_ns,
    )
    try:
        sidecar_metadata = load_voice_memo_sidecar(source_path, logger)
        latency_started = time.monotonic()
        claimed_path = claim_audio_file(source_path, local_runtime_root, logger)
        claimed_fingerprint = build_fingerprint(claimed_path)
        process_audio_file(
            claimed_fingerprint,
            local_root,
            logger,
            transcribe,
            process_generated_jobs,
            archive_dir,
            local_runtime_root,
            sidecar_metadata,
        )
        metrics.record_latency(local_runtime_root, "transcription", (time.monotonic() - latency_started) * 1000)
    except Exception as exc:  # noqa: BLE001
        logger.error("audio remains eligible for retry path=%s error=%s", source_path, exc)
    return 1


def scan_once(
    inbox_dir: Path,
    archive_dir: Path,
    local_root: Path,
    stable_seconds: float,
    logger: logging.Logger,
    transcribe: Callable[[Path], str],
    process_generated_jobs: Callable[[Path, list[Path]], int] | None = None,
    local_runtime_dir: Path | None = None,
    now: float | None = None,
    worker_count: int = 1,
    transcribe_factory: Callable[[], Callable[[Path], str]] | None = None,
) -> int:
    local_root.mkdir(parents=True, exist_ok=True)
    local_runtime_root = local_root / "runtime" if local_runtime_dir is None else local_runtime_dir
    local_runtime_root.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    ready_files = [
        path for path in iter_audio_files(inbox_dir) if is_stable_file(path, stable_seconds=stable_seconds, now=now)
    ]
    handled_count = 0
    if ready_files:
        logger.info(
            "found stable audio files count=%s files=%s",
            len(ready_files),
            [path.name for path in ready_files],
        )

    if worker_count <= 1:
        for source_path in ready_files:
            handled_count += _handle_ready_file(
                source_path,
                archive_dir,
                local_root,
                local_runtime_root,
                logger,
                transcribe,
                process_generated_jobs,
            )
        return handled_count

    if transcribe_factory is None:
        raise TranscriptionPipelineError("worker_count above one requires transcribe_factory")

    def handle_with_fresh_transcriber(source_path: Path) -> int:
        try:
            fresh_transcriber = transcribe_factory()
        except Exception as exc:  # noqa: BLE001
            if marker_exists(source_path):
                logger.error("previously processed audio remains eligible for retry path=%s error=%s", source_path, exc)
            else:
                logger.error("audio remains eligible for retry path=%s error=%s", source_path, exc)
            return 1
        return _handle_ready_file(
            source_path,
            archive_dir,
            local_root,
            local_runtime_root,
            logger,
            fresh_transcriber,
            process_generated_jobs,
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(handle_with_fresh_transcriber, source_path) for source_path in ready_files]
        for future in as_completed(futures):
            handled_count += future.result()

    return handled_count


def voice_memo_intake_enabled(args: argparse.Namespace) -> bool:
    if args.no_voice_memo_intake:
        return False
    raw = os.environ.get("VOICE_MEMO_CONSUMER_ENABLED", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"} or bool(os.environ.get("VOICE_MEMO_COUCHDB_URL"))


def poll_voice_memos_once(inbox_dir: Path, logger: logging.Logger) -> None:
    try:
        from voice_memo import couchdb_watcher as voice_memo_watcher
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice-memo intake unavailable import_error=%s", exc)
        return
    try:
        count = voice_memo_watcher.poll_once(inbox_dir=inbox_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice-memo intake poll failed error=%s", exc)
        return
    if count:
        logger.info("voice-memo intake poll completed count=%s", count)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ensure_ffmpeg_on_path()
    logger = configure_logging(args.log_path.expanduser())
    inbox_dir = args.inbox_dir.expanduser()
    archive_dir = args.archive_dir.expanduser()
    local_root = args.local_root.expanduser()
    local_runtime_dir = args.local_runtime_dir.expanduser()
    archive_dir.mkdir(parents=True, exist_ok=True)
    local_runtime_dir.mkdir(parents=True, exist_ok=True)
    require_directories(
        {
            "audio_inbox_dir": inbox_dir,
            "vault_dir": DEFAULT_VAULT_ROOT,
        }
    )

    def transcriber_factory() -> Callable[[Path], str]:
        if args.whisper_url:
            validate_local_whisper_url(args.whisper_url)
            return RemoteWhisperTranscriber(
                args.whisper_url,
                args.model,
                timeout_seconds=args.worker_timeout_seconds,
                logger=logger,
            )
        return SubprocessWhisperTranscriber(
            args.model,
            local_root,
            compare_mode=args.compare_mode,
            initial_prompt=None if args.no_initial_prompt else args.initial_prompt,
            timeout_seconds=args.worker_timeout_seconds,
            logger=logger,
        )

    try:
        transcriber = transcriber_factory()
    except TranscriptionPipelineError as exc:
        logger.error("%s", exc)
        return 1

    if args.whisper_url:
        logger.info("transcription mode=remote url=%s", args.whisper_url)
    else:
        logger.info("transcription mode=local-subprocess")

    logger.info(
        "watching inbox=%s archive=%s local_root=%s local_runtime_dir=%s model=%s compare_mode=%s interval=%.1fs stable=%.1fs",
        inbox_dir,
        archive_dir,
        local_root,
        local_runtime_dir,
        args.model,
        args.compare_mode,
        args.poll_seconds,
        args.stable_seconds,
    )

    if args.once or not args.watch:
        if voice_memo_intake_enabled(args):
            poll_voice_memos_once(inbox_dir, logger)
        scan_once(
            inbox_dir,
            archive_dir,
            local_root,
            args.stable_seconds,
            logger,
            transcriber,
            process_btq_queue_jobs,
            local_runtime_dir,
            worker_count=args.worker_count,
            transcribe_factory=transcriber_factory,
        )
        return 0

    while True:
        if voice_memo_intake_enabled(args):
            poll_voice_memos_once(inbox_dir, logger)
        scan_once(
            inbox_dir,
            archive_dir,
            local_root,
            args.stable_seconds,
            logger,
            transcriber,
            process_btq_queue_jobs,
            local_runtime_dir,
            worker_count=args.worker_count,
            transcribe_factory=transcriber_factory,
        )
        time.sleep(args.poll_seconds)


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
