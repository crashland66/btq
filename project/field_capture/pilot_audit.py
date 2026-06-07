from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from config import get_config
from event_pipeline.sites import SITES
from field_capture import photo_vision
from field_capture.action_candidates import default_candidate_dir, list_action_candidates_report
from field_capture.approved_job_drafts import default_draft_dir, list_approved_drafts_report
from field_capture.review_status import review_status_report
from field_capture.site_viewer import UnsafeMediaPath, resolve_media_path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
AUDIO_EXTENSIONS = {".webm", ".m4a", ".mp3", ".wav", ".aac", ".ogg"}
TEXT_NOTE_FIELDS = ("note", "notes", "text_note", "comment", "comments", "description")
SUBMITTER_FIELDS = (
    "submitter",
    "submitter_name",
    "person",
    "person_name",
    "person_id",
    "employee",
    "employee_name",
    "token_label",
    "field_capture_token_label",
    "session_label",
    "user",
    "user_name",
)
ADVISORY_WARNING_PREFIXES = (
    photo_vision.ADVISORY_WARNING,
    "Vision output is advisory interpretation",
)


@dataclass(frozen=True)
class IntakeCapture:
    path: Path
    payload: dict[str, object]
    metadata: dict[str, object]

    @property
    def capture_id(self) -> str:
        return str(self.metadata.get("capture_id") or self.payload.get("capture_id") or "").strip()

    @property
    def site_id(self) -> str:
        return str(self.metadata.get("site_id") or self.payload.get("site_id") or self.payload.get("site") or "").strip()

    @property
    def captured_at(self) -> str:
        return str(self.payload.get("captured_at") or self.payload.get("exported_at") or self.metadata.get("captured_at") or "").strip()

    @property
    def capture_date(self) -> str:
        return photo_vision.date_from_timestamp(self.captured_at)

    @property
    def area(self) -> str:
        return str(self.payload.get("area") or self.payload.get("qc_category") or "").strip()

    @property
    def phase(self) -> str:
        return str(self.payload.get("phase") or self.metadata.get("phase") or self.payload.get("status") or "").strip()

    @property
    def photos(self) -> list[dict[str, object]]:
        photos = self.payload.get("photos")
        return [item for item in photos if isinstance(item, dict)] if isinstance(photos, list) else []

    @property
    def audio(self) -> list[dict[str, object]]:
        audio = self.payload.get("audio")
        return [item for item in audio if isinstance(item, dict)] if isinstance(audio, list) else []

    @property
    def text_note(self) -> str:
        for source in (self.payload, self.metadata):
            for field in TEXT_NOTE_FIELDS:
                value = source.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    @property
    def submitter(self) -> str:
        for source in (self.metadata, self.payload):
            for field in SUBMITTER_FIELDS:
                value = source.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return "Unknown submitter"


def default_runtime_root() -> Path:
    return get_config().runtime_root


def generated_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def site_name_for(site_id: str) -> str:
    for site in SITES:
        if str(site.get("site_id")) == str(site_id):
            return str(site.get("canonical") or "")
    context = photo_vision.site_vision_context_for(site_id)
    return context.site_context_name if context else ""


def read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def iter_intake_captures(intake_dir: Path) -> tuple[list[IntakeCapture], list[dict[str, object]]]:
    captures: list[IntakeCapture] = []
    malformed: list[dict[str, object]] = []
    for path in sorted(intake_dir.expanduser().glob("**/*.json")):
        payload = read_json(path)
        if payload is None:
            malformed.append({"path": str(path), "reason": "unreadable_json"})
            continue
        if payload.get("job_type") != "photo_capture":
            continue
        metadata = payload.get("metadata")
        body = payload.get("payload")
        if not isinstance(metadata, dict) or not isinstance(body, dict):
            malformed.append({"path": str(path), "reason": "missing_metadata_or_payload"})
            continue
        captures.append(IntakeCapture(path=path, payload=body, metadata=metadata))
    return captures, malformed


def filter_captures(captures: Iterable[IntakeCapture], *, site_id: str, date: str, limit: int | None = None) -> list[IntakeCapture]:
    selected = [capture for capture in captures if capture.site_id == site_id and capture.capture_date == date]
    selected.sort(key=lambda capture: (capture.captured_at, capture.capture_id, str(capture.path)))
    return selected[:limit] if limit is not None else selected


def safe_counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def top_counter_items(counter: Counter[str], limit: int = 12) -> list[dict[str, object]]:
    return [{"value": key, "count": count} for key, count in counter.most_common(limit)]


def media_path_for(stored_path: object, upload_root: Path) -> Path | None:
    if not isinstance(stored_path, str) or not stored_path.strip():
        return None
    try:
        return resolve_media_path(stored_path, upload_root)
    except UnsafeMediaPath:
        return None


def media_integrity(captures: list[IntakeCapture], upload_dir: Path, assets: list[photo_vision.FieldPhotoAsset]) -> dict[str, object]:
    upload_root = upload_dir.expanduser().resolve(strict=False)
    referenced_paths: list[Path] = []
    missing: list[dict[str, object]] = []
    for capture in captures:
        for media_type, items in (("photo", capture.photos), ("audio", capture.audio)):
            for item in items:
                resolved = media_path_for(item.get("stored_path"), upload_root)
                if resolved is None:
                    missing.append(
                        {
                            "capture_id": capture.capture_id,
                            "media_type": media_type,
                            "filename": str(item.get("filename") or ""),
                            "reason": "unsafe_or_missing_stored_path",
                        }
                    )
                    continue
                referenced_paths.append(resolved)
                if not resolved.exists():
                    missing.append(
                        {
                            "capture_id": capture.capture_id,
                            "media_type": media_type,
                            "filename": str(item.get("filename") or resolved.name),
                            "path": str(resolved),
                            "reason": "referenced_media_missing_locally",
                        }
                    )
    referenced_resolved = {str(path.resolve(strict=False)) for path in referenced_paths}
    unreferenced: list[str] = []
    scan_roots = [upload_root / date for date in sorted({capture.capture_date for capture in captures if capture.capture_date})]
    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        for path in sorted(scan_root.glob("**/*")):
            if not path.is_file() or path.suffix.lower() not in (IMAGE_EXTENSIONS | AUDIO_EXTENSIONS):
                continue
            if str(path.resolve(strict=False)) not in referenced_resolved:
                unreferenced.append(str(path))
    capture_ids = [capture.capture_id for capture in captures if capture.capture_id]
    duplicate_capture_ids = sorted([key for key, count in Counter(capture_ids).items() if count > 1])
    asset_ids = [asset.photo_asset_id for asset in assets]
    duplicate_photo_asset_ids = sorted([key for key, count in Counter(asset_ids).items() if count > 1])
    return {
        "intake_records_missing_media": len(missing),
        "media_files_referenced_but_missing": missing,
        "media_files_present_not_referenced_count": len(unreferenced),
        "media_files_present_not_referenced": unreferenced[:50],
        "duplicate_capture_ids": duplicate_capture_ids,
        "duplicate_photo_asset_ids": duplicate_photo_asset_ids,
    }


def warning_is_advisory_only(warning: str) -> bool:
    return any(warning == prefix or warning.startswith(prefix) for prefix in ADVISORY_WARNING_PREFIXES)


def photo_vision_summary(assets: list[photo_vision.FieldPhotoAsset], photo_vision_dir: Path) -> dict[str, object]:
    asset_ids = {asset.photo_asset_id for asset in assets}
    sidecars_total = 0
    completed = 0
    failed = 0
    malformed = 0
    sidecars_with_warnings = 0
    warning_counter: Counter[str] = Counter()
    area_guess_counter: Counter[str] = Counter()
    object_counter: Counter[str] = Counter()
    condition_counter: Counter[str] = Counter()
    issue_counter: Counter[str] = Counter()
    failed_sidecars: list[dict[str, object]] = []
    sidecars_by_asset: set[str] = set()

    for path in sorted(photo_vision_dir.expanduser().glob("*.json")):
        photo_asset_id = path.stem
        if photo_asset_id not in asset_ids:
            continue
        payload = read_json(path)
        sidecars_by_asset.add(photo_asset_id)
        sidecars_total += 1
        if payload is None:
            malformed += 1
            continue
        status = str(payload.get("status") or "malformed")
        if status == photo_vision.STATUS_COMPLETED:
            completed += 1
        elif status == photo_vision.STATUS_FAILED:
            failed += 1
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            failed_sidecars.append(
                {
                    "photo_asset_id": photo_asset_id,
                    "capture_id": str(payload.get("capture_id") or ""),
                    "error_type": str(error.get("type") or ""),
                    "can_retry": bool(error.get("can_retry", False)),
                    "message": str(error.get("message") or ""),
                }
            )
        else:
            malformed += 1
        area_guess = str(payload.get("area_guess") or "").strip() or "unknown"
        area_guess_counter[area_guess] += 1
        for field, counter in (
            ("visible_objects", object_counter),
            ("possible_conditions", condition_counter),
            ("possible_issues", issue_counter),
        ):
            values = payload.get(field)
            if isinstance(values, list):
                for value in values:
                    if str(value).strip():
                        counter[str(value).strip()] += 1
        warnings = [str(item).strip() for item in payload.get("warnings", []) if str(item).strip()] if isinstance(payload.get("warnings"), list) else []
        significant = [warning for warning in warnings if not warning_is_advisory_only(warning)]
        if significant:
            sidecars_with_warnings += 1
            warning_counter.update(significant)

    missing_assets = sorted(asset_ids - sidecars_by_asset)
    return {
        "total_image_assets": len(assets),
        "sidecars_total": sidecars_total,
        "completed_sidecars": completed,
        "failed_sidecars": failed,
        "missing_sidecars": len(missing_assets),
        "malformed_sidecars": malformed,
        "sidecars_with_warnings": sidecars_with_warnings,
        "area_guess_distribution": safe_counter_dict(area_guess_counter),
        "common_visible_objects": top_counter_items(object_counter),
        "common_possible_conditions": top_counter_items(condition_counter),
        "common_possible_issues": top_counter_items(issue_counter),
        "failed_sidecar_details": failed_sidecars,
        "warning_summaries": top_counter_items(warning_counter),
        "missing_photo_asset_ids": missing_assets[:50],
    }


def capture_totals(captures: list[IntakeCapture]) -> dict[str, object]:
    image_count = sum(len(capture.photos) for capture in captures)
    audio_count = sum(len(capture.audio) for capture in captures)
    timestamps = sorted(capture.captured_at for capture in captures if capture.captured_at)
    text_note_count = sum(1 for capture in captures if capture.text_note)
    submitters = {capture.submitter for capture in captures if capture.submitter}
    return {
        "total_captures": len(captures),
        "total_media_files": image_count + audio_count,
        "total_images": image_count,
        "total_audio_notes": audio_count,
        "captures_with_audio": sum(1 for capture in captures if capture.audio),
        "captures_without_audio": sum(1 for capture in captures if not capture.audio),
        "captures_with_text_notes": text_note_count,
        "oldest_capture_timestamp": timestamps[0] if timestamps else "",
        "newest_capture_timestamp": timestamps[-1] if timestamps else "",
        "submitter_count": len(submitters),
    }


def submitter_breakdown(captures: list[IntakeCapture]) -> list[dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for capture in captures:
        key = capture.submitter
        row = rows.setdefault(key, {"submitter": key, "captures": 0, "photos": 0, "audio_notes": 0, "text_notes": 0})
        row["captures"] = int(row["captures"]) + 1
        row["photos"] = int(row["photos"]) + len(capture.photos)
        row["audio_notes"] = int(row["audio_notes"]) + len(capture.audio)
        row["text_notes"] = int(row["text_notes"]) + (1 if capture.text_note else 0)
    return sorted(rows.values(), key=lambda row: (-int(row["captures"]), str(row["submitter"])))


def area_phase_breakdown(captures: list[IntakeCapture]) -> tuple[dict[str, object], dict[str, object]]:
    area_captures: Counter[str] = Counter()
    area_images: Counter[str] = Counter()
    phase_captures: Counter[str] = Counter()
    for capture in captures:
        area = capture.area or "missing/Other"
        phase = capture.phase or "missing/blank"
        area_captures[area] += 1
        area_images[area] += len(capture.photos)
        phase_captures[phase] += 1
    return (
        {
            "captures_by_area": safe_counter_dict(area_captures),
            "images_by_area": safe_counter_dict(area_images),
            "captures_with_area_missing_or_other": sum(
                count for area, count in area_captures.items() if not area.strip() or area.lower() in {"other", "missing/other"}
            ),
        },
        {
            "captures_by_phase": safe_counter_dict(phase_captures),
            "captures_with_phase_missing_or_blank": phase_captures.get("missing/blank", 0),
        },
    )


def behavior_signals(captures: list[IntakeCapture]) -> dict[str, object]:
    photos_per_capture = Counter(str(len(capture.photos)) for capture in captures)
    large_batches = [capture.capture_id for capture in captures if len(capture.photos) > 4]
    single_photo = [capture.capture_id for capture in captures if len(capture.photos) == 1]
    photo_only = [capture.capture_id for capture in captures if capture.photos and not capture.audio and not capture.text_note]
    audio_tags = [capture.capture_id for capture in captures if capture.audio]
    text_notes = [capture.capture_id for capture in captures if capture.text_note]
    intentional = [capture.capture_id for capture in captures if 1 <= len(capture.photos) <= 3 and (capture.audio or capture.text_note or capture.area)]
    return {
        "photos_per_capture_distribution": safe_counter_dict(photos_per_capture),
        "captures_with_more_than_4_photos": len(large_batches),
        "capture_ids_with_more_than_4_photos": large_batches,
        "captures_with_exactly_1_photo": len(single_photo),
        "capture_ids_with_exactly_1_photo": single_photo,
        "likely_photo_only_captures": len(photo_only),
        "photo_only_capture_ids": photo_only,
        "captures_with_audio_tags": len(audio_tags),
        "captures_with_text_notes": len(text_notes),
        "large_batches_possible_dumping": len(large_batches),
        "small_intentional_captures": len(intentional),
        "small_intentional_capture_ids": intentional,
    }


def review_state(runtime_root: Path) -> dict[str, object]:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    candidate_report = list_action_candidates_report(default_candidate_dir(runtime_resolved), runtime_root=runtime_resolved)
    draft_report = list_approved_drafts_report(default_draft_dir(runtime_resolved), runtime_root=runtime_resolved)
    status_report = review_status_report(runtime_root=runtime_resolved)
    counts = status_report.get("counts") if isinstance(status_report.get("counts"), dict) else {}
    return {
        "action_candidate_counts": candidate_report.get("counts", {}),
        "approved_draft_counts": draft_report.get("counts", {}),
        "staging_counts": {
            "runtime_queue": int(counts.get("staged_queue_jobs_runtime_queue", 0)),
            "processed": int(counts.get("staged_jobs_processed", 0)),
            "failed": int(counts.get("staged_jobs_failed", 0)),
        },
        "runtime_queue_count": len(sorted((runtime_resolved / "queue").glob("*.json"))),
        "processed_field_capture_evidence_count": int(counts.get("staged_jobs_processed", 0)),
        "failed_field_capture_evidence_count": int(counts.get("staged_jobs_failed", 0)),
    }


def recommendations(report: dict[str, object]) -> list[str]:
    recs = [f"Review photo-only captures in /captures?site={report['site_id']}&date_from={report['date']}&date_to={report['date']}&has_photo=1."]
    vision = report["photo_vision"] if isinstance(report.get("photo_vision"), dict) else {}
    totals = report["capture_totals"] if isinstance(report.get("capture_totals"), dict) else {}
    behavior = report["behavior_signals"] if isinstance(report.get("behavior_signals"), dict) else {}
    areas = report["areas"] if isinstance(report.get("areas"), dict) else {}
    if int(vision.get("failed_sidecars", 0)):
        recs.append(f"Retry failed local vision sidecars with ./scripts/btq describe-field-photos --channel field_capture --site-id {report['site_id']} --date {report['date']} --replace-failed.")
    if int(vision.get("sidecars_with_warnings", 0)):
        recs.append(f"Review captures with photo-vision sidecars in /captures?site={report['site_id']}&date_from={report['date']}&date_to={report['date']}&has_vision_sidecar=1 — open each capture to see per-photo warnings.")
    if int(totals.get("captures_with_audio", 0)) == 0 or int(totals.get("captures_with_audio", 0)) * 4 < max(1, int(totals.get("total_captures", 0))):
        recs.append("Reinforce short optional voice tags for locations or context that are not obvious.")
    if int(areas.get("captures_with_area_missing_or_other", 0)):
        recs.append("Adjust the field-capture area choices if many submissions are missing area or marked Other.")
    if int(behavior.get("large_batches_possible_dumping", 0)):
        recs.append("Reinforce fewer intentional photos of logical areas rather than large photo batches.")
    recs.append("Keep client-facing views paused until a moderated, client-safe layer exists.")
    return recs


def build_report(
    *,
    runtime_root: Path,
    site_id: str,
    date: str,
    include_paths: bool = False,
    limit: int | None = None,
) -> dict[str, object]:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    intake_dir = photo_vision.default_intake_dir(runtime_resolved)
    upload_dir = photo_vision.default_upload_dir(runtime_resolved)
    vision_dir = photo_vision.default_photo_vision_dir(runtime_resolved)
    captures_all, malformed = iter_intake_captures(intake_dir)
    captures = filter_captures(captures_all, site_id=site_id, date=date, limit=limit)
    assets = photo_vision.discover_photo_assets(intake_dir, upload_dir, site_id=site_id, date=date)
    if limit is not None:
        selected_capture_ids = {capture.capture_id for capture in captures}
        assets = [asset for asset in assets if asset.capture_id in selected_capture_ids]
    areas, phases = area_phase_breakdown(captures)
    integrity = media_integrity(captures, upload_dir, assets)
    integrity["malformed_intake_records"] = malformed if include_paths else [{"reason": item["reason"]} for item in malformed]
    integrity["malformed_intake_record_count"] = len(malformed)
    if not include_paths:
        integrity["media_files_present_not_referenced"] = []
        for item in integrity["media_files_referenced_but_missing"]:
            if isinstance(item, dict):
                item.pop("path", None)
    report: dict[str, object] = {
        "site_id": str(site_id),
        "site_name": site_name_for(site_id),
        "date": date,
        "runtime_root": str(runtime_resolved),
        "generated_at": generated_at(),
        "capture_totals": capture_totals(captures),
        "submitters": submitter_breakdown(captures),
        "areas": areas,
        "phases": phases,
        "behavior_signals": behavior_signals(captures),
        "photo_vision": photo_vision_summary(assets, vision_dir),
        "metadata_integrity": integrity,
        "review_state": review_state(runtime_resolved),
        "recommendations": [],
    }
    report["recommendations"] = recommendations(report)
    return report


def markdown_table(headers: list[str], rows: Iterable[Iterable[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def format_human(report: dict[str, object]) -> str:
    totals = report["capture_totals"]
    vision = report["photo_vision"]
    behavior = report["behavior_signals"]
    review = report["review_state"]
    lines = [
        f"Field-capture pilot audit: site_id={report['site_id']} site={report['site_name'] or 'unknown'} date={report['date']}",
        f"runtime={report['runtime_root']} generated_at={report['generated_at']}",
        "",
        "Capture/media totals:",
        (
            f"captures={totals['total_captures']} media={totals['total_media_files']} images={totals['total_images']} "
            f"audio={totals['total_audio_notes']} with_audio={totals['captures_with_audio']} without_audio={totals['captures_without_audio']} "
            f"text_notes={totals['captures_with_text_notes']}"
        ),
        f"oldest={totals['oldest_capture_timestamp'] or 'n/a'} newest={totals['newest_capture_timestamp'] or 'n/a'} submitters={totals['submitter_count']}",
        "",
        "Behavior signals:",
        (
            f"photos_per_capture={behavior['photos_per_capture_distribution']} more_than_4={behavior['captures_with_more_than_4_photos']} "
            f"one_photo={behavior['captures_with_exactly_1_photo']} photo_only={behavior['likely_photo_only_captures']} "
            f"small_intentional={behavior['small_intentional_captures']}"
        ),
        "",
        "Photo vision:",
        (
            f"assets={vision['total_image_assets']} sidecars={vision['sidecars_total']} completed={vision['completed_sidecars']} "
            f"failed={vision['failed_sidecars']} missing={vision['missing_sidecars']} malformed={vision['malformed_sidecars']} "
            f"warnings={vision['sidecars_with_warnings']}"
        ),
        f"area_guesses={vision['area_guess_distribution']}",
        "",
        "Review state:",
        (
            f"candidates={review['action_candidate_counts']} drafts={review['approved_draft_counts']} "
            f"runtime_queue={review['runtime_queue_count']}"
        ),
        "",
        "Recommendations:",
    ]
    lines.extend(f"- {item}" for item in report["recommendations"])
    return "\n".join(lines)


def format_markdown(report: dict[str, object]) -> str:
    totals = report["capture_totals"]
    vision = report["photo_vision"]
    behavior = report["behavior_signals"]
    integrity = report["metadata_integrity"]
    lines = [
        f"# {report['site_name'] or report['site_id']} Field Capture Pilot Audit - {report['date']}",
        "",
        "## Scope",
        "",
        f"- Site ID: {report['site_id']}",
        f"- Site name: {report['site_name'] or 'unknown'}",
        f"- Runtime root: {report['runtime_root']}",
        f"- Generated at: {report['generated_at']}",
        "",
        "## Capture And Media Totals",
        "",
    ]
    lines.extend(
        markdown_table(
            ["Metric", "Count"],
            [
                ["captures", totals["total_captures"]],
                ["media files", totals["total_media_files"]],
                ["images", totals["total_images"]],
                ["audio notes", totals["total_audio_notes"]],
                ["captures with audio", totals["captures_with_audio"]],
                ["captures without audio", totals["captures_without_audio"]],
                ["captures with text notes", totals["captures_with_text_notes"]],
                ["submitters", totals["submitter_count"]],
            ],
        )
    )
    lines.extend(["", "## Submitters", ""])
    lines.extend(markdown_table(["Submitter", "Captures", "Photos", "Audio", "Text notes"], [[row["submitter"], row["captures"], row["photos"], row["audio_notes"], row["text_notes"]] for row in report["submitters"]]))
    lines.extend(["", "## Areas And Phases", ""])
    lines.append("### Areas")
    lines.extend(markdown_table(["Area", "Captures"], report["areas"]["captures_by_area"].items()))
    lines.append("")
    lines.append("### Phases")
    lines.extend(markdown_table(["Phase", "Captures"], report["phases"]["captures_by_phase"].items()))
    lines.extend(["", "## Behavior Signals", ""])
    lines.extend(
        markdown_table(
            ["Signal", "Value"],
            [
                ["photos per capture", behavior["photos_per_capture_distribution"]],
                ["captures with more than 4 photos", behavior["captures_with_more_than_4_photos"]],
                ["captures with exactly 1 photo", behavior["captures_with_exactly_1_photo"]],
                ["likely photo-only captures", behavior["likely_photo_only_captures"]],
                ["captures with audio tags", behavior["captures_with_audio_tags"]],
                ["captures with text notes", behavior["captures_with_text_notes"]],
                ["small intentional captures", behavior["small_intentional_captures"]],
            ],
        )
    )
    lines.extend(["", "## Photo Vision", ""])
    lines.extend(
        markdown_table(
            ["Metric", "Count"],
            [
                ["image assets", vision["total_image_assets"]],
                ["sidecars", vision["sidecars_total"]],
                ["completed", vision["completed_sidecars"]],
                ["failed", vision["failed_sidecars"]],
                ["missing/backlog", vision["missing_sidecars"]],
                ["malformed", vision["malformed_sidecars"]],
                ["with warnings", vision["sidecars_with_warnings"]],
            ],
        )
    )
    lines.extend(["", "### Area Guess Distribution"])
    lines.extend(markdown_table(["Area guess", "Count"], vision["area_guess_distribution"].items()))
    lines.extend(["", "## Metadata Integrity", ""])
    lines.extend(
        markdown_table(
            ["Signal", "Value"],
            [
                ["missing referenced media", integrity["intake_records_missing_media"]],
                ["unreferenced local media", integrity["media_files_present_not_referenced_count"]],
                ["duplicate capture ids", ", ".join(integrity["duplicate_capture_ids"]) or "none"],
                ["duplicate photo asset ids", ", ".join(integrity["duplicate_photo_asset_ids"]) or "none"],
                ["malformed intake records", integrity["malformed_intake_record_count"]],
            ],
        )
    )
    lines.extend(["", "## Review State", ""])
    lines.append("```json")
    lines.append(json.dumps(report["review_state"], indent=2, sort_keys=True))
    lines.append("```")
    lines.extend(
        [
            "",
            "## Operator Observations",
            "",
            "- [ ] What worked?",
            "- [ ] What confused employees?",
            "- [ ] Were the photos intentional?",
            "- [ ] Were voice notes used?",
            "- [ ] Which areas need a better area list?",
            "- [ ] What should change before Continental?",
            "- [ ] What should be client-visible later?",
            "",
            "## Recommended Next Review Actions",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in report["recommendations"])
    return "\n".join(lines) + "\n"


def write_text(path: Path, text: str) -> None:
    path.expanduser().parent.mkdir(parents=True, exist_ok=True)
    path.expanduser().write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only audit of local field-capture pilot intake, media, vision, and review state.")
    parser.add_argument("--runtime-root", type=Path, default=default_runtime_root())
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--include-paths", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(
        runtime_root=args.runtime_root,
        site_id=args.site_id,
        date=args.date,
        include_paths=args.include_paths,
        limit=args.limit,
    )
    if args.output_md is not None:
        write_text(args.output_md, format_markdown(report))
    if args.output_json is not None:
        write_text(args.output_json, json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_human(report))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
