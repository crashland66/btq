from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb_listener import CouchDBChangesListener, CouchDBListenerError


DEFAULT_DATABASE = "btq_voice_memos"
DEFAULT_STATE_FIELD = "processing_state"
DEFAULT_REMOTE_HOST = os.environ.get("BTQ_VPS_SSH_TARGET", "deploy@vps.example.com")
REMOTE_DATA_ROOT = os.environ.get("BTQ_VOICE_REMOTE_DATA_ROOT", "/srv/btq/voice/data")
PROCESSING_STATE = "claimed"
COMPLETE_STATE = "intake_done"
FAILED_STATE = "intake_failed"
MARK_PROCESSING_BACKOFF_SECONDS = (1.0, 2.0, 4.0)


def default_log_path(runtime_root: Path) -> Path:
    return runtime_root / "logs" / "voice_memo_couchdb_watch.log"


def configure_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("voice_memo.couchdb_watch")
    logger.setLevel(logging.INFO)
    for handler in logger.handlers:
        handler.close()
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


def voice_memo_couchdb_config() -> couchdb_config.CouchDBConfig:
    return couchdb_config.from_env(
        base_url_override=os.environ.get("VOICE_MEMO_COUCHDB_URL") or os.environ.get("BTQ_COUCHDB_URL"),
        username_override=os.environ.get("VOICE_MEMO_COUCHDB_USER") or os.environ.get("BTQ_COUCHDB_USER"),
        password_override=os.environ.get("VOICE_MEMO_COUCHDB_PASSWORD") or os.environ.get("BTQ_COUCHDB_PASSWORD"),
        timeout_default=couchdb_config.DEFAULT_LISTENER_TIMEOUT_SECONDS,
    )


def capture_id_for(doc: dict[str, Any]) -> str:
    return str(doc.get("capture_id") or doc.get("_id") or "").strip()


def destination_audio_path(doc: dict[str, Any], inbox_dir: Path) -> Path:
    capture_id = capture_id_for(doc)
    audio_filename = str(doc.get("audio_filename") or "").strip()
    suffix = Path(audio_filename).suffix.lower()
    if not suffix:
        suffix = ".webm"
    safe_capture_id = "".join(char if char.isalnum() or char in "._-" else "-" for char in capture_id).strip(".-")
    if not safe_capture_id:
        raise ValueError("voice memo doc is missing capture_id")
    return inbox_dir / f"vm-{safe_capture_id.removeprefix('vm-')}{suffix}"


def sidecar_path_for(audio_path: Path) -> Path:
    return audio_path.with_suffix(".metadata.json")


def remote_audio_source(doc: dict[str, Any], remote_host: str) -> str:
    audio_path = str(doc.get("audio_path") or "").strip().lstrip("/")
    if not audio_path:
        raise ValueError("voice memo doc is missing audio_path")
    if ".." in Path(audio_path).parts:
        raise ValueError("voice memo audio_path must not contain '..'")
    return f"{remote_host}:{REMOTE_DATA_ROOT}/{audio_path}"


def list_from_doc(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def geolocation_from_doc(doc: dict[str, Any]) -> dict[str, Any] | None:
    geo = doc.get("geolocation")
    if isinstance(geo, dict):
        return {key: value for key, value in geo.items() if value not in {None, ""}}
    keys = {
        "lat": doc.get("geolocation_lat"),
        "lng": doc.get("geolocation_lng"),
        "accuracy_m": doc.get("geolocation_accuracy_m"),
        "captured_at": doc.get("geolocation_captured_at"),
    }
    compact = {key: value for key, value in keys.items() if value not in {None, ""}}
    return compact or None


def sidecar_payload(doc: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": "voice_memo",
        "capture_id": capture_id_for(doc),
    }
    for key in (
        "routing_flag",
        "mode",
        "captured_at",
        "duration_seconds",
        "note",
        "site_id",
        "site_account",
        "site_location",
        "person_id",
    ):
        if doc.get(key) not in {None, ""}:
            payload[key] = doc.get(key)
    employee_slugs = list_from_doc(doc.get("employee_slugs"))
    employee_names = list_from_doc(doc.get("employee_names"))
    if employee_slugs:
        payload["employee_slugs"] = employee_slugs
    if employee_names:
        payload["employee_names"] = employee_names
    geolocation = geolocation_from_doc(doc)
    if geolocation:
        payload["geolocation"] = geolocation
    return payload


def write_sidecar(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


def scp_audio(
    *,
    doc: dict[str, Any],
    destination: Path,
    remote_host: str,
    runner: Callable[..., Any],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = runner(
        ["scp", remote_audio_source(doc, remote_host), str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    if getattr(result, "returncode", 1) != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        raise RuntimeError(f"scp failed: {stderr or 'non-zero exit'}")


def process_one(
    *,
    doc: dict[str, Any],
    inbox_dir: Path,
    remote_host: str,
    runner: Callable[..., Any],
    dry_run: bool,
    logger: logging.Logger,
) -> dict[str, Any]:
    capture_id = capture_id_for(doc)
    audio_destination = destination_audio_path(doc, inbox_dir)
    sidecar_destination = sidecar_path_for(audio_destination)
    payload = sidecar_payload(doc)
    result = {
        "capture_id": capture_id,
        "routing_flag": str(doc.get("routing_flag") or ""),
        "ok": True,
        "audio_path": str(audio_destination),
        "sidecar_path": str(sidecar_destination),
        "scp_skipped": False,
        "sidecar_skipped": False,
        "dry_run": dry_run,
        "error": "",
    }
    if dry_run:
        logger.info(
            "voice-memo dry-run capture_id=%s routing_flag=%s audio=%s sidecar=%s",
            capture_id,
            result["routing_flag"],
            audio_destination,
            sidecar_destination,
        )
        return result
    if audio_destination.exists():
        result["scp_skipped"] = True
        logger.info("voice-memo audio already exists capture_id=%s path=%s", capture_id, audio_destination)
    else:
        scp_audio(doc=doc, destination=audio_destination, remote_host=remote_host, runner=runner)
    if sidecar_destination.exists():
        result["sidecar_skipped"] = True
        logger.info("voice-memo sidecar already exists capture_id=%s path=%s", capture_id, sidecar_destination)
    else:
        write_sidecar(sidecar_destination, payload)
    return result


class _OnceListener:
    def __init__(self, listener: Any) -> None:
        self._listener = listener

    def listen(self) -> Any:
        for doc in self._listener.listen():
            yield doc
            return

    def mark_processing(self, doc: dict[str, Any], state: str = PROCESSING_STATE) -> dict[str, Any]:
        return self._listener.mark_processing(doc, state)

    def mark_complete(self, doc: dict[str, Any], state: str = COMPLETE_STATE) -> None:
        self._listener.mark_complete(doc, state)

    def mark_failed(self, doc: dict[str, Any], reason: str, state: str = FAILED_STATE) -> None:
        self._listener.mark_failed(doc, reason, state)

    def stop(self) -> None:
        self._listener.stop()


class _PendingOnceListener:
    def __init__(self, listener: Any, state_field: str) -> None:
        self._listener = listener
        self._state_field = state_field

    def listen(self) -> Any:
        result = self._listener._request_json(
            "POST",
            "_find",
            {
                "selector": {self._state_field: "pending"},
                "limit": 1,
            },
        )
        docs = result.get("docs") if isinstance(result, dict) else None
        if not isinstance(docs, list):
            return
        for doc in docs[:1]:
            if isinstance(doc, dict):
                yield doc

    def mark_processing(self, doc: dict[str, Any], state: str = PROCESSING_STATE) -> dict[str, Any]:
        return self._listener.mark_processing(doc, state)

    def mark_complete(self, doc: dict[str, Any], state: str = COMPLETE_STATE) -> None:
        self._listener.mark_complete(doc, state)

    def mark_failed(self, doc: dict[str, Any], reason: str, state: str = FAILED_STATE) -> None:
        self._listener.mark_failed(doc, reason, state)

    def stop(self) -> None:
        self._listener.stop()


def run_listener(
    *,
    listener: Any,
    inbox_dir: Path,
    remote_host: str,
    runner: Callable[..., Any],
    dry_run: bool,
    json_output: bool,
    logger: logging.Logger,
) -> int:
    processed_count = 0
    for doc in listener.listen():
        capture_id = capture_id_for(doc)
        processing_doc: dict[str, Any] | None = None
        final_mark_processing_error = ""
        for attempt, delay_seconds in enumerate(MARK_PROCESSING_BACKOFF_SECONDS, start=1):
            try:
                processing_doc = listener.mark_processing(doc, PROCESSING_STATE)
                break
            except CouchDBListenerError as exc:
                final_mark_processing_error = str(exc)
                logger.warning("voice-memo mark_processing failed capture_id=%s attempt=%s error=%s", capture_id, attempt, exc)
                if attempt < len(MARK_PROCESSING_BACKOFF_SECONDS):
                    time.sleep(delay_seconds)
        if processing_doc is None:
            result = {"capture_id": capture_id, "ok": False, "error": f"mark_processing exhausted: {final_mark_processing_error}"}
            if json_output:
                print(json.dumps(result, sort_keys=True), flush=True)
            continue
        try:
            result = process_one(
                doc=processing_doc,
                inbox_dir=inbox_dir,
                remote_host=remote_host,
                runner=runner,
                dry_run=dry_run,
                logger=logger,
            )
            if dry_run:
                logger.info("voice-memo dry-run leaving state claimed capture_id=%s", capture_id)
            else:
                listener.mark_complete(processing_doc, COMPLETE_STATE)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)
            result = {"capture_id": capture_id, "ok": False, "error": reason, "dry_run": dry_run}
            logger.exception("voice-memo intake failed capture_id=%s", capture_id)
            if not dry_run:
                listener.mark_failed(processing_doc, reason=reason, state=FAILED_STATE)
        processed_count += 1
        if json_output:
            print(json.dumps(result, sort_keys=True), flush=True)
    return processed_count


def build_listener(*, database: str, state_field: str) -> CouchDBChangesListener:
    config = voice_memo_couchdb_config()
    return CouchDBChangesListener(
        database=database,
        state_field=state_field,
        base_url=config.base_url,
        username=config.username,
        password=config.password,
        timeout=config.timeout,
        heartbeat_ms=config.heartbeat_ms,
    )


def poll_once(
    *,
    inbox_dir: Path | None = None,
    remote_host: str | None = None,
    database: str | None = None,
    state_field: str = DEFAULT_STATE_FIELD,
    dry_run: bool = False,
    json_output: bool = False,
    log_path: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    config = get_config()
    runtime_root = config.runtime_root.expanduser().resolve(strict=False)
    logger = configure_logger((log_path or default_log_path(runtime_root)).expanduser().resolve(strict=False))
    listener = build_listener(database=database or os.environ.get("VOICE_MEMO_COUCHDB_DB", DEFAULT_DATABASE), state_field=state_field)
    active_listener = _PendingOnceListener(listener, state_field)
    try:
        return run_listener(
            listener=active_listener,
            inbox_dir=(inbox_dir or config.audio_inbox_dir).expanduser(),
            remote_host=remote_host or os.environ.get("VOICE_MEMO_REMOTE_HOST", DEFAULT_REMOTE_HOST),
            runner=runner,
            dry_run=dry_run,
            json_output=json_output,
            logger=logger,
        )
    finally:
        listener.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch CouchDB voice memos and place audio plus metadata sidecars into the local transcription inbox.")
    parser.add_argument("--runtime-root", type=Path, default=get_config().runtime_root)
    parser.add_argument("--remote-host", default=os.environ.get("VOICE_MEMO_REMOTE_HOST", DEFAULT_REMOTE_HOST))
    parser.add_argument("--database", default=os.environ.get("VOICE_MEMO_COUCHDB_DB", DEFAULT_DATABASE))
    parser.add_argument("--state-field", default=DEFAULT_STATE_FIELD)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--inbox-dir", type=Path, default=get_config().audio_inbox_dir)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser().resolve(strict=False)
    logger = configure_logger((args.log_path or default_log_path(runtime_root)).expanduser().resolve(strict=False))
    try:
        listener = build_listener(database=args.database, state_field=args.state_field)
    except (couchdb_config.CouchDBConfigError, CouchDBListenerError) as exc:
        logger.error("voice-memo watcher configuration failed: %s", exc)
        return 1

    def stop_listener(_signum: int, _frame: Any) -> None:
        listener.stop()

    signal.signal(signal.SIGTERM, stop_listener)
    signal.signal(signal.SIGINT, stop_listener)
    active_listener: Any = _OnceListener(listener) if args.once else listener
    try:
        run_listener(
            listener=active_listener,
            inbox_dir=args.inbox_dir.expanduser(),
            remote_host=args.remote_host,
            runner=subprocess.run,
            dry_run=args.dry_run,
            json_output=args.json,
            logger=logger,
        )
    except CouchDBListenerError as exc:
        logger.error("voice-memo watcher stopped with listener error: %s", exc)
        return 1
    finally:
        if args.once:
            listener.stop()
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
