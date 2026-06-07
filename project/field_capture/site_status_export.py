from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from config import get_config
from field_capture.action_candidates import default_candidate_dir, iter_candidate_artifacts
from field_capture.client_notifications import default_notification_dir, load_notification, safe_text
from processing_core.artifacts import read_json_object, write_json_object
from site_issues import discover_site_issues, issue_as_export


EXPORT_TYPE = "field_capture_site_viewer_status"
REVIEWED_STATUSES = {"approved"}
TOKEN_KEYS = {"token", "bearer", "field_capture_token", "field_capture_token_id", "field_capture_token_label"}


def default_runtime_root() -> Path:
    return get_config().runtime_root


def default_export_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "field_capture" / "site_viewer_exports"


def default_export_path(site_id: str, runtime_root: Path | None = None) -> Path:
    safe_site = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in site_id)
    return default_export_dir(runtime_root) / f"site_{safe_site}.json"


def queue_dir_for(runtime_root: Path) -> Path:
    return runtime_root / "queue"


def upload_dir_for(runtime_root: Path) -> Path:
    return runtime_root / "uploads"


def transcript_dir_for(runtime_root: Path) -> Path:
    return runtime_root / "field_capture" / "audio_transcripts"


def photo_vision_dir_for(runtime_root: Path) -> Path:
    return runtime_root / "field_capture" / "photo_vision"


def intake_dir_for(runtime_root: Path) -> Path:
    return runtime_root / "field_capture" / "intake"


def read_capture_jobs(*, queue_dir: Path, intake_dir: Path | None = None, site_id: str) -> dict[str, dict[str, object]]:
    captures: dict[str, dict[str, object]] = {}
    for root in (queue_dir, intake_dir):
        if root is None or not root.exists():
            continue
        for path in sorted(root.glob("**/*.json")):
            payload = read_json_object(path)
            if not payload or payload.get("job_type") != "photo_capture":
                continue
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            body = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            if str(metadata.get("site_id") or body.get("site_id") or body.get("site") or "").strip() != site_id:
                continue
            capture_id = str(metadata.get("capture_id") or body.get("capture_id") or "").strip()
            if capture_id:
                captures[capture_id] = {"metadata": metadata, "payload": body}
    return captures


def classify_review_item(candidate: dict[str, object]) -> str:
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("summary", "rationale", "source_text", "source_context", "review_rationale")
    ).lower()
    metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    issue_type = str(metadata.get("issue_type") or "").lower()
    if any(word in text for word in ("supply", "supplies", "restock", "towel", "soap", "paper", "order")):
        return "supply_request"
    if any(word in text for word in ("staff", "employee", "team", "request", "asked", "coverage", "schedule")):
        return "staff_request"
    if issue_type in {"water", "maintenance"} or any(
        word in text for word in ("fix", "repair", "maintenance", "broken", "leak", "water", "drain", "hole", "inoperable", "damage")
    ):
        return "maintenance_issue"
    if any(word in text for word in ("site", "area", "location", "reference")):
        return "site_reference"
    return "other"


def priority_score(status: str, category: str, has_audio: bool, has_text_note: bool, has_voice_transcript: bool) -> int:
    score = 0
    if status in REVIEWED_STATUSES:
        score += 100
    if has_voice_transcript:
        score += 25
    if has_audio:
        score += 20
    if has_text_note:
        score += 15
    if category in {"maintenance_issue", "supply_request", "staff_request"}:
        score += 10
    return score


def photo_vision_context_by_media_url(photo_vision_dir: Path) -> dict[str, str]:
    if not photo_vision_dir.exists():
        return {}
    descriptions: dict[str, str] = {}
    for path in sorted(photo_vision_dir.expanduser().glob("*.json")):
        payload = read_json_object(path)
        if not payload or str(payload.get("status") or "") != "completed":
            continue
        media_url = media_url_from_vision_payload(payload)
        description = safe_visual_context(payload.get("description"))
        if media_url and description:
            descriptions[media_url] = description
    return descriptions


def media_url_from_vision_payload(payload: dict[str, object]) -> str:
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    for value in (payload.get("image_media_url"), provenance.get("image_media_url")):
        media_url = str(value or "").strip()
        if media_url.startswith("/media/") and ".." not in media_url:
            return media_url
    return ""


def safe_visual_context(value: object, *, limit: int = 520) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    lowered = text.lower()
    unsafe_markers = (
        "bearer ",
        "field_capture_token",
        "fct_",
        "auth",
        "/users/",
        "/srv/",
        "/var/",
        "\\users\\",
        "source_image_path",
        "queue",
    )
    if any(marker in lowered for marker in unsafe_markers):
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def media_references(capture: dict[str, object], upload_dir: Path, *, visual_context_by_url: dict[str, str] | None = None) -> list[dict[str, str]]:
    payload = capture.get("payload") if isinstance(capture.get("payload"), dict) else {}
    refs: list[dict[str, str]] = []
    context_by_url = visual_context_by_url or {}
    for media_type, key in (("photo", "photos"), ("audio", "audio")):
        records = payload.get(key)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            stored_path = str(record.get("stored_path") or "").strip()
            if not stored_path:
                continue
            try:
                path = Path(stored_path).expanduser()
                if not path.is_absolute():
                    path = upload_dir / path
                relative = path.resolve(strict=False).relative_to(upload_dir.expanduser().resolve(strict=False)).as_posix()
            except ValueError:
                continue
            media_url = f"/media/{quote(relative)}"
            visual_context = safe_visual_context(context_by_url.get(media_url)) if media_type == "photo" else ""
            refs.append(
                {
                    "type": media_type,
                    "filename": str(record.get("filename") or path.name),
                    "media_url": media_url,
                    **({"visual_context": visual_context, "alt_text": visual_context} if visual_context else {}),
                }
            )
    return refs


def submitter_from_capture(capture: dict[str, object]) -> str:
    metadata = capture.get("metadata") if isinstance(capture.get("metadata"), dict) else {}
    payload = capture.get("payload") if isinstance(capture.get("payload"), dict) else {}
    return str(
        metadata.get("person_name")
        or metadata.get("submitter_name")
        or payload.get("person_name")
        or payload.get("submitter_name")
        or metadata.get("person_id")
        or payload.get("person_id")
        or ""
    ).strip()


def text_note_from_capture(capture: dict[str, object]) -> str:
    payload = capture.get("payload") if isinstance(capture.get("payload"), dict) else {}
    return str(payload.get("note") or payload.get("text_note") or "").strip()


def transcript_excerpt_for_candidate(candidate: dict[str, object], transcript_dir: Path, *, limit: int = 360) -> str:
    provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
    paths = []
    transcript_path = str(provenance.get("source_transcript_path") or "")
    if transcript_path:
        paths.append(Path(transcript_path))
    audio_asset_id = str(provenance.get("audio_asset_id") or "")
    if audio_asset_id:
        paths.append(transcript_dir / f"{audio_asset_id}.json")
    for path in paths:
        payload = read_json_object(path)
        if payload and str(payload.get("status") or "") == "complete":
            return clipped_text(payload.get("raw_text"), limit=limit)
    return ""


def clipped_text(value: object, *, limit: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def is_generic_summary(value: object) -> bool:
    text = " ".join(str(value or "").lower().split())
    return text in {
        "review the field audio note and decide whether follow-up is needed.",
        "review the field audio note and decide whether follow-up is needed",
        "review supply/order follow-up.",
        "review supply/order follow-up",
    }


def display_title_for(candidate: dict[str, object], source_context: str, transcript_excerpt: str, text_note: str) -> str:
    for value in (
        candidate.get("review_rationale"),
        None if is_generic_summary(candidate.get("summary")) else candidate.get("summary"),
        source_context,
        transcript_excerpt,
        text_note,
        candidate.get("summary"),
    ):
        text = clipped_text(value, limit=120)
        if text:
            return text
    return "Reviewed item"


def display_body_for(candidate: dict[str, object], source_context: str, transcript_excerpt: str, text_note: str) -> str:
    for value in (source_context, transcript_excerpt, text_note, candidate.get("review_rationale"), candidate.get("rationale"), candidate.get("summary")):
        text = clipped_text(value, limit=520)
        if text:
            return text
    return ""


def has_transcript_for_candidate(candidate: dict[str, object], transcript_dir: Path) -> bool:
    provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
    transcript_path = str(provenance.get("source_transcript_path") or "")
    if transcript_path and Path(transcript_path).exists():
        return True
    audio_asset_id = str(provenance.get("audio_asset_id") or "")
    return bool(audio_asset_id and (transcript_dir / f"{audio_asset_id}.json").exists())


def reviewed_item_from_candidate(
    candidate_path: Path,
    candidate: dict[str, object],
    *,
    site_id: str,
    captures: dict[str, dict[str, object]],
    upload_dir: Path,
    transcript_dir: Path,
    visual_context_by_url: dict[str, str] | None = None,
    client_notification: dict[str, object] | None = None,
) -> dict[str, object] | None:
    metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    if str(metadata.get("site_id") or "").strip() != site_id:
        return None
    status = str(candidate.get("status") or "")
    if status not in REVIEWED_STATUSES:
        return None
    capture_id = str(metadata.get("upload_id") or "").strip()
    capture = captures.get(capture_id, {})
    payload = capture.get("payload") if isinstance(capture.get("payload"), dict) else {}
    audio_records = payload.get("audio") if isinstance(payload.get("audio"), list) else []
    note = text_note_from_capture(capture)
    source_context = str(candidate.get("source_context") or "").strip()
    transcript_excerpt = transcript_excerpt_for_candidate(candidate, transcript_dir)
    category = classify_review_item(candidate)
    has_audio = bool(audio_records)
    has_text_note = bool(note)
    has_voice_transcript = has_transcript_for_candidate(candidate, transcript_dir)
    priority = priority_score(status, category, has_audio, has_text_note, has_voice_transcript)
    notification = client_notification if isinstance(client_notification, dict) else {}
    client_informed = bool(notification.get("client_informed"))
    return {
        "capture_id": capture_id,
        "candidate_id": str(candidate.get("candidate_id") or candidate_path.stem),
        "status": status,
        "review_type": category,
        "summary": str(candidate.get("summary") or ""),
        "display_title": display_title_for(candidate, source_context, transcript_excerpt, note),
        "display_body": display_body_for(candidate, source_context, transcript_excerpt, note),
        "source_context": clipped_text(source_context, limit=520),
        "transcript_excerpt": transcript_excerpt,
        "text_note": note,
        "review_rationale": str(candidate.get("review_rationale") or ""),
        "reviewer": str(candidate.get("reviewer") or ""),
        "reviewed_at": str(candidate.get("reviewed_at") or ""),
        "submitter": submitter_from_capture(capture),
        "has_audio": has_audio,
        "has_text_note": has_text_note,
        "has_voice_transcript": has_voice_transcript,
        "client_informed": client_informed,
        "client_informed_at": str(notification.get("client_informed_at") or "") if client_informed else "",
        "client_informed_method": str(notification.get("client_informed_method") or "") if client_informed else "",
        "client_informed_by": safe_text(notification.get("client_informed_by"), limit=120) if client_informed else "",
        "client_informed_note": safe_text(notification.get("client_informed_note"), limit=500) if client_informed else "",
        "notification_history_count": len(notification.get("notification_history")) if isinstance(notification.get("notification_history"), list) else 0,
        "media": media_references(capture, upload_dir, visual_context_by_url=visual_context_by_url),
        "priority": priority,
    }


def build_site_status_export(
    *,
    site_id: str,
    runtime_root: Path,
    generated_at: str | None = None,
    candidate_dir: Path | None = None,
    queue_dir: Path | None = None,
    intake_dir: Path | None = None,
    upload_dir: Path | None = None,
    transcript_dir: Path | None = None,
    photo_vision_dir: Path | None = None,
    notification_dir: Path | None = None,
    vault_root: Path | None = None,
    include_issues: bool = False,
) -> dict[str, object]:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    candidate_root = (candidate_dir or default_candidate_dir(runtime_resolved)).expanduser().resolve(strict=False)
    queue_root = (queue_dir or queue_dir_for(runtime_resolved)).expanduser().resolve(strict=False)
    intake_root = (intake_dir or intake_dir_for(runtime_resolved)).expanduser().resolve(strict=False)
    upload_root = (upload_dir or upload_dir_for(runtime_resolved)).expanduser().resolve(strict=False)
    transcript_root = (transcript_dir or transcript_dir_for(runtime_resolved)).expanduser().resolve(strict=False)
    photo_vision_root = (photo_vision_dir or photo_vision_dir_for(runtime_resolved)).expanduser().resolve(strict=False)
    notification_root = (notification_dir or default_notification_dir(runtime_resolved)).expanduser().resolve(strict=False)
    visual_context_by_url = photo_vision_context_by_media_url(photo_vision_root)
    captures = read_capture_jobs(queue_dir=queue_root, intake_dir=intake_root, site_id=site_id)
    reviewed_items = [
        item
        for path, candidate in iter_candidate_artifacts(candidate_root)
        if (
            item := reviewed_item_from_candidate(
                path,
                candidate,
                site_id=site_id,
                captures=captures,
                upload_dir=upload_root,
                transcript_dir=transcript_root,
                visual_context_by_url=visual_context_by_url,
                client_notification=load_notification(notification_root, str(candidate.get("candidate_id") or "")),
            )
        )
        is not None
    ]
    reviewed_items.sort(key=lambda item: (-int(item["priority"]), str(item.get("reviewed_at") or ""), str(item["candidate_id"])))
    counts = {
        "reviewed_items": len(reviewed_items),
        "approved": sum(1 for item in reviewed_items if item.get("status") == "approved"),
        "maintenance_issue": sum(1 for item in reviewed_items if item.get("review_type") == "maintenance_issue"),
        "supply_request": sum(1 for item in reviewed_items if item.get("review_type") == "supply_request"),
        "staff_request": sum(1 for item in reviewed_items if item.get("review_type") == "staff_request"),
        "with_audio": sum(1 for item in reviewed_items if item.get("has_audio")),
        "with_text_note": sum(1 for item in reviewed_items if item.get("has_text_note")),
        "with_voice_transcript": sum(1 for item in reviewed_items if item.get("has_voice_transcript")),
        "client_informed": sum(1 for item in reviewed_items if item.get("client_informed")),
    }
    issue_payload: dict[str, object] = {"issues": [], "warnings": [], "counts": {"total": 0, "current": 0}}
    if include_issues:
        vault_resolved = (vault_root or get_config().vault_dir).expanduser().resolve(strict=False)
        issue_report = discover_site_issues(vault_resolved, site_id=site_id)
        issues = issue_report.get("issues") if isinstance(issue_report.get("issues"), list) else []
        issue_payload = {
            "issues": [issue_as_export(issue) for issue in issues],
            "warnings": issue_report.get("warnings") if isinstance(issue_report.get("warnings"), list) else [],
            "counts": issue_report.get("counts") if isinstance(issue_report.get("counts"), dict) else {},
        }
    return {
        "type": EXPORT_TYPE,
        "site_id": site_id,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "reviewed_items": reviewed_items,
        "site_issues": issue_payload,
        "counts": counts,
    }


def write_site_status_export(output_path: Path, payload: dict[str, object]) -> Path:
    write_json_object(output_path, payload)
    return output_path


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Export reviewed field-capture site viewer status JSON.")
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--queue-dir", type=Path)
    parser.add_argument("--intake-dir", type=Path)
    parser.add_argument("--upload-dir", type=Path)
    parser.add_argument("--transcript-dir", type=Path)
    parser.add_argument("--photo-vision-dir", type=Path)
    parser.add_argument("--notification-dir", type=Path)
    parser.add_argument("--vault-root", type=Path, default=config.vault_dir)
    parser.add_argument("--include-issues", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    payload = build_site_status_export(
        site_id=args.site_id,
        runtime_root=runtime_root,
        candidate_dir=args.candidate_dir,
        queue_dir=args.queue_dir,
        intake_dir=args.intake_dir,
        upload_dir=args.upload_dir,
        transcript_dir=args.transcript_dir,
        photo_vision_dir=args.photo_vision_dir,
        notification_dir=args.notification_dir,
        vault_root=args.vault_root,
        include_issues=args.include_issues,
    )
    output_path = args.output or default_export_path(args.site_id, runtime_root)
    write_site_status_export(output_path.expanduser(), payload)
    if args.json:
        print(json.dumps({"output_path": str(output_path.expanduser()), **payload}, indent=2, sort_keys=True))
    else:
        print(
            "field-capture site status export: "
            f"site_id={args.site_id} reviewed_items={payload['counts']['reviewed_items']} "
            f"output={output_path.expanduser()}"
        )
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
