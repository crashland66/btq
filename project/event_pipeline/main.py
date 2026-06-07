from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from event_pipeline.domain_resolver import normalize_text
from event_pipeline.enricher import enrich_events
from event_pipeline.extractor import extract_events
from event_pipeline.validator import validate_events
from epistemic import apply_epistemic_state
from io_atomic import atomic_write_text
from processing_core.artifacts import read_json_object, write_json_object, write_json_value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract, enrich, and validate transcript-derived event JSON files.")
    parser.add_argument("transcript_path", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path.cwd())
    return parser.parse_args(argv)


def normalized_transcript_path_for(transcript_path: Path) -> Path:
    return transcript_path.with_suffix(".normalized.txt")


def corrections_path_for(transcript_path: Path) -> Path:
    return transcript_path.with_suffix(".corrections.json")


def last_site_path_for(output_root: Path) -> Path:
    return output_root / "state" / "last_site.json"


def update_last_site(valid_paths: list[Path], output_root: Path) -> None:
    for path in valid_paths:
        event = read_json_object(path)
        if event is None:
            continue
        site = event.get("site")
        confidence = event.get("confidence")
        if isinstance(site, str) and site != "unknown" and confidence in {"medium", "high"}:
            last_site_path = last_site_path_for(output_root)
            last_site_path.parent.mkdir(parents=True, exist_ok=True)
            write_json_object(
                last_site_path,
                {
                    "site": site,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            return


def attach_capture_id(paths: list[Path], capture_id: str | None) -> None:
    if capture_id is None:
        return
    for path in paths:
        event = read_json_object(path)
        if event is None:
            continue
        event["capture_id"] = capture_id
        write_json_object(path, event)


def attach_epistemic_state(paths: list[Path]) -> None:
    for path in paths:
        event = read_json_object(path)
        if event is None:
            continue
        source_text = str(event.get("source_excerpt") or event.get("details") or "")
        updated = apply_epistemic_state(event, source_text, source_type="transcript")
        write_json_object(path, updated)


def process_transcript(transcript_path: Path, output_root: Path, capture_id: str | None = None) -> tuple[list[Path], list[Path], list[dict[str, str]]]:
    raw_dir = output_root / "events_raw"
    enriched_dir = output_root / "events_enriched"
    valid_dir = output_root / "events_valid"
    failed_dir = output_root / "events_failed"
    original_text = transcript_path.read_text(encoding="utf-8")
    normalized_text, corrections = normalize_text(original_text)
    normalized_path = normalized_transcript_path_for(transcript_path)
    atomic_write_text(normalized_path, normalized_text)
    corrections_path = corrections_path_for(transcript_path)
    write_json_value(corrections_path, corrections)

    raw_paths = extract_events(
        transcript_path,
        raw_dir,
        transcript_text=normalized_text,
        last_site_path=last_site_path_for(output_root),
    )
    attach_capture_id(raw_paths, capture_id)
    attach_epistemic_state(raw_paths)
    enriched_paths = enrich_events(raw_dir, enriched_dir, raw_paths)
    attach_capture_id(enriched_paths, capture_id)
    attach_epistemic_state(enriched_paths)
    valid_paths, failed_paths = validate_events(enriched_dir, valid_dir, failed_dir, enriched_paths)
    attach_capture_id(valid_paths + failed_paths, capture_id)
    update_last_site(valid_paths, output_root)
    return valid_paths, failed_paths, corrections


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    transcript_path = args.transcript_path.resolve()
    output_root = args.output_root.resolve()
    process_transcript(transcript_path, output_root)
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
