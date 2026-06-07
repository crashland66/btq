from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from io_atomic import atomic_write_text
from transcription_pipeline.main import (
    DEFAULT_INITIAL_PROMPT,
    TranscriptionPipelineError,
    build_transcriber,
    configure_logging,
    current_memory_usage_mb,
    ensure_ffmpeg_on_path,
    transcription_log_payload,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one short-lived Whisper transcription worker.")
    parser.add_argument("--audio-path", type=Path, required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--local-root", type=Path, required=True)
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--compare-mode", action="store_true")
    parser.add_argument("--initial-prompt", default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument("--no-initial-prompt", action="store_true")
    return parser.parse_args(argv)


def output_payload(text: str, run_payload: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "text": text,
        "run": run_payload,
    }


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ensure_ffmpeg_on_path()
    log_path = args.log_path or args.local_root / "logs" / "transcription_worker.log"
    logger = configure_logging(log_path.expanduser())
    audio_path = args.audio_path.expanduser()
    output_path = args.output_path.expanduser()
    local_root = args.local_root.expanduser()

    logger.info(
        "worker started audio=%s model=%s compare_mode=%s maxrss_mb=%s",
        audio_path,
        args.model,
        args.compare_mode,
        current_memory_usage_mb(),
    )

    try:
        model_load_started = time.monotonic()
        transcriber = build_transcriber(
            args.model,
            local_root,
            compare_mode=args.compare_mode,
            initial_prompt=None if args.no_initial_prompt else args.initial_prompt,
            logger=logger,
        )
        logger.info(
            "worker model loaded audio=%s model=%s load_seconds=%.3f maxrss_mb=%s",
            audio_path,
            args.model,
            time.monotonic() - model_load_started,
            current_memory_usage_mb(),
        )

        transcription_started = time.monotonic()
        text = transcriber(audio_path)
        run_payload = transcription_log_payload(transcriber)
        logger.info(
            "worker transcription completed audio=%s duration_seconds=%.3f maxrss_mb=%s",
            audio_path,
            time.monotonic() - transcription_started,
            current_memory_usage_mb(),
        )

        atomic_write_text(output_path, json.dumps(output_payload(text, run_payload), indent=2, sort_keys=True) + "\n")
        logger.info("worker exiting audio=%s status=success maxrss_mb=%s", audio_path, current_memory_usage_mb())
        return 0
    except TranscriptionPipelineError as exc:
        logger.error("worker exiting audio=%s status=failed error=%s maxrss_mb=%s", audio_path, exc, current_memory_usage_mb())
        return 1
    except Exception:
        logger.exception("worker exiting audio=%s status=failed maxrss_mb=%s", audio_path, current_memory_usage_mb())
        return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
