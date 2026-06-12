from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import get_config
from field_capture import action_candidates
from field_capture import audio_semantics
from field_capture import audio_transcription
from field_capture.pipeline_watcher import process_note_only_text_semantics
from processing_core import capture_semantics


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Run field-capture semantic extraction in a load-exit child process.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log-path", type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser().resolve(strict=False)
    log_path = (args.log_path or audio_semantics.default_log_path(runtime_root)).expanduser()
    semantics_logger = audio_semantics.configure_logger(log_path)

    engine = capture_semantics.build_semantic_engine()
    audio_counts = audio_semantics.process_semantics(
        audio_semantics.default_transcript_dir(runtime_root),
        audio_semantics.default_semantic_dir(runtime_root),
        engine,
        runtime_root=runtime_root,
        logger=semantics_logger,
    )
    text_counts = process_note_only_text_semantics(
        audio_transcription.default_intake_dir(runtime_root),
        action_candidates.default_text_semantic_dir(runtime_root),
        runtime_root=runtime_root,
        engine=engine,
    )
    counts = {"audio": audio_counts, "text": text_counts}
    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
    else:
        print(
            "field semantics: "
            f"audio_completed={audio_counts['completed']} audio_failed={audio_counts['failed']} "
            f"text_completed={text_counts['completed']} text_failed={text_counts['failed']}"
        )
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
