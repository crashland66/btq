from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

QUALITY_WINDOW = 30  # number of processed photos to examine
QUALITY_THRESHOLD = 3  # minimum occurrences for a coaching tip
QUALITY_MAX_TIPS = 2  # maximum coaching tips to surface

COACHING_TIPS: dict[str, str] = {
    "blurry": "A few came out blurry — tap to focus, then hold still for a second before the shot.",
    "motion_blur": "A few came out blurry from movement — hold the phone steady for a moment after you tap.",
    "too_dark": "A few came out dark — switching on a light or using your camera flash makes them much easier to use.",
    "too_bright": "A few came out washed out — step back from the window or strong light.",
    "glare": "A few had glare — shift your angle slightly to avoid the reflection.",
    "out_of_frame": "A few had the subject cut off — back up a step so the whole area is in the shot.",
    "partly_obscured": "A few had something in the way — make sure nothing is in front of what you're photographing.",
    "low_resolution": "A few were low quality — use the in-app camera rather than a screenshot or forwarded image.",
    "contains_people": "A few had a person in the shot — frame the area so people aren't in the photo.",
    "unanalyzable": "A few couldn't be read — re-take in better light, holding steady.",
}

WORKER_LABELS: dict[str, str] = {
    "blurry": "blurry",
    "motion_blur": "blurry (movement)",
    "too_dark": "too dark",
    "too_bright": "too bright",
    "glare": "glare",
    "out_of_frame": "subject cut off",
    "partly_obscured": "subject blocked",
    "low_resolution": "low quality",
    "contains_people": "person in shot",
    "unanalyzable": "couldn't be read",
}


def is_track_b(capture_doc: dict) -> bool:
    """True if this capture has audio or a text note (actionable track)."""
    has_audio = bool(capture_doc.get("audio"))
    has_text_note = bool(str(capture_doc.get("note") or "").strip())
    return has_audio or has_text_note


def derive_track_b_stage(
    capture_id: str,
    candidates_dir: Path,
) -> tuple[str, str]:
    """Return (stage, outcome_label) for a Track B capture.

    stage is one of: "processing", "reviewed", "acted_on"
    outcome_label is non-empty for "reviewed" and "acted_on" stages.

    "reviewed" means staff looked at it and determined no follow-up action
    was needed (resolution_status == "dismissed").
    "acted_on" means staff took concrete action (notified client, ordered
    supplies, handled equipment, or logged an issue for tracking).
    """
    if not candidates_dir.exists():
        return ("processing", "")

    matching: list[tuple[float, dict]] = []
    for path in candidates_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("capture_id") or "") == capture_id:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            matching.append((mtime, data))

    if not matching:
        return ("processing", "")

    _, candidate = max(matching, key=lambda t: t[0])
    resolution_status = str(candidate.get("resolution_status") or "").strip()

    if not resolution_status or resolution_status == "open":
        return ("processing", "")

    # Dismissed = reviewed by staff, no action required
    if resolution_status == "dismissed":
        return ("reviewed", "No action needed")

    if resolution_status == "client_notified":
        return ("acted_on", "Client notified")

    if resolution_status == "resolved":
        candidate_type = str(
            candidate.get("job_type") or candidate.get("candidate_type") or ""
        ).lower()
        if "supply" in candidate_type:
            return ("acted_on", "Supplies ordered")
        if "equipment" in candidate_type:
            return ("acted_on", "Equipment handled")
        return ("acted_on", "Logged")

    return ("processing", "")


def build_photo_vision_index(photo_vision_dir: Path) -> dict[str, list[dict]]:
    """Scan photo_vision_dir once and return a dict mapping capture_id → [photo records].

    Each photo record has: severity, flags, description, possible_issues.
    Photo sidecars are named by photo_asset_id (a hash), not capture_id, so we
    must read each file to find its capture_id. This builds the full index in a
    single pass for efficient multi-capture lookups.
    """
    index: dict[str, list[dict]] = {}
    if not photo_vision_dir.exists():
        return index
    for path in sorted(photo_vision_dir.glob("*.json")):
        try:
            sidecar = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(sidecar, dict):
            continue
        capture_id = str(sidecar.get("capture_id") or "").strip()
        if not capture_id:
            continue
        # Quality block may be nested under "quality" key or flat at top level
        quality = sidecar.get("quality")
        if isinstance(quality, dict):
            severity = str(quality.get("severity") or "ok")
            flags_raw = quality.get("flags")
        else:
            severity = str(sidecar.get("severity") or "ok")
            flags_raw = sidecar.get("quality_flags") or sidecar.get("flags")
        flags = [str(f) for f in flags_raw] if isinstance(flags_raw, list) else []
        description = str(sidecar.get("description") or "").strip()
        possible_issues_raw = sidecar.get("possible_issues")
        possible_issues = (
            [str(x) for x in possible_issues_raw if x]
            if isinstance(possible_issues_raw, list)
            else []
        )
        index.setdefault(capture_id, []).append({
            "severity": severity,
            "flags": flags,
            "description": description,
            "possible_issues": possible_issues,
        })
    return index


def quality_for_capture(
    capture_id: str,
    photo_vision_dir: Path,
    index: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """Return per-photo quality+description records for a capture.

    Returns [] when no vision sidecars exist for this capture (i.e. vision
    processing has not yet completed).

    Pass a pre-built index (from build_photo_vision_index) when calling for
    multiple captures to avoid redundant filesystem scans.
    """
    if index is not None:
        return list(index.get(capture_id, []))
    # Single-capture path: build a targeted index for just this capture
    if not photo_vision_dir.exists():
        return []
    result: list[dict] = []
    for path in sorted(photo_vision_dir.glob("*.json")):
        try:
            sidecar = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(sidecar, dict):
            continue
        if str(sidecar.get("capture_id") or "").strip() != capture_id:
            continue
        quality = sidecar.get("quality")
        if isinstance(quality, dict):
            severity = str(quality.get("severity") or "ok")
            flags_raw = quality.get("flags")
        else:
            severity = str(sidecar.get("severity") or "ok")
            flags_raw = sidecar.get("quality_flags") or sidecar.get("flags")
        flags = [str(f) for f in flags_raw] if isinstance(flags_raw, list) else []
        description = str(sidecar.get("description") or "").strip()
        possible_issues_raw = sidecar.get("possible_issues")
        possible_issues = (
            [str(x) for x in possible_issues_raw if x]
            if isinstance(possible_issues_raw, list)
            else []
        )
        result.append({
            "severity": severity,
            "flags": flags,
            "description": description,
            "possible_issues": possible_issues,
        })
    return result


def rolling_quality_summary(
    person_id: str,
    capture_docs: list[dict],
    photo_vision_dir: Path,
    window: int = QUALITY_WINDOW,
) -> dict:
    """Aggregate quality flag counts over the last `window` processed photos.

    Only counts photos from captures that have a photo_vision sidecar
    (processing complete). Processes captures newest-first until `window`
    photos are counted. Returns:
        {
            "total_processed": int,
            "clear": int,
            "flag_counts": {"flag_name": count, ...}  # only non-zero flags
        }
    """
    sorted_docs = sorted(
        [doc for doc in capture_docs if isinstance(doc, dict)],
        key=lambda d: str(d.get("captured_at") or ""),
        reverse=True,
    )

    # Build the index once for all captures
    vision_index = build_photo_vision_index(photo_vision_dir)

    total_processed = 0
    clear = 0
    flag_counts: dict[str, int] = {}

    for doc in sorted_docs:
        if total_processed >= window:
            break
        capture_id = str(doc.get("capture_id") or doc.get("_id") or "")
        if not capture_id:
            continue
        per_photo = quality_for_capture(capture_id, photo_vision_dir, index=vision_index)
        if not per_photo:
            continue
        for photo in per_photo:
            if total_processed >= window:
                break
            total_processed += 1
            flags = photo.get("flags") or []
            severity = str(photo.get("severity") or "ok")
            if severity == "ok" and not flags:
                clear += 1
            for flag in flags:
                flag_str = str(flag)
                flag_counts[flag_str] = flag_counts.get(flag_str, 0) + 1

    return {
        "total_processed": total_processed,
        "clear": clear,
        "flag_counts": {k: v for k, v in flag_counts.items() if v > 0},
    }


def collect_my_submissions(
    person_id: str,
    capture_docs: list[dict],
    *,
    upload_dir: Path,
    photo_vision_dir: Path,
    candidates_dir: Path,
    can_retarget: bool = False,
    window_days: int = 14,
    window_count: int = 30,
) -> list[dict]:
    """Return submissions for the feed, newest first.

    Filters to captures with matching person_id, within the last
    `window_days` days or last `window_count` submissions — whichever
    is larger. Cap at 50 total.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    person_docs = [
        doc for doc in capture_docs
        if isinstance(doc, dict) and str(doc.get("person_id") or "") == person_id
    ]
    person_docs.sort(key=lambda d: str(d.get("captured_at") or ""), reverse=True)

    # Build photo_vision index once for all captures
    vision_index = build_photo_vision_index(photo_vision_dir)

    result: list[dict] = []
    for i, doc in enumerate(person_docs):
        if len(result) >= 50:
            break
        in_count_window = i < window_count
        in_time_window = False
        captured_at_str = str(doc.get("captured_at") or "")
        if captured_at_str:
            try:
                captured_at_dt = datetime.fromisoformat(captured_at_str.replace("Z", "+00:00"))
                in_time_window = captured_at_dt >= cutoff
            except ValueError:
                pass
        if not in_count_window and not in_time_window:
            continue

        capture_id = str(doc.get("capture_id") or doc.get("_id") or "")
        site_id = str(doc.get("site_id") or "")
        site_name = str(doc.get("site_name") or doc.get("site") or site_id or "")
        target_type = str(doc.get("target_type") or "location")
        target_id = str(doc.get("target_id") or "")
        # Back-compat: pre-prompt-133 docs may lack target_*
        # but always have site_id. Treat them as location.
        if not target_id and target_type == "location":
            target_id = site_id

        photos_raw = doc.get("photos")
        photos = list(photos_raw) if isinstance(photos_raw, list) else []
        photo_count = len(photos)
        has_audio = bool(doc.get("audio"))
        note_text = str(doc.get("note") or "").strip()
        has_text_note = bool(note_text)

        photo_urls: list[str] = []
        for p in photos:
            if isinstance(p, dict):
                upload_id = str(p.get("upload_id") or "")
                if upload_id:
                    photo_urls.append(f"/media/{upload_id}")

        per_photo_quality = quality_for_capture(capture_id, photo_vision_dir, index=vision_index)

        if is_track_b(doc):
            track = "B"
            stage, outcome_label = derive_track_b_stage(capture_id, candidates_dir)
        else:
            track = "A"
            # Stage reflects whether photo vision processing has completed
            stage = "processed" if per_photo_quality else "processing"
            outcome_label = ""

        result.append({
            "capture_id": capture_id,
            "site_id": site_id,
            "site_name": site_name,
            "target_type": target_type,
            "target_id": target_id,
            "captured_at": captured_at_str,
            "photo_count": photo_count,
            "has_audio": has_audio,
            "has_text_note": has_text_note,
            "note_text": note_text,
            "photo_urls": photo_urls,
            "track": track,
            "stage": stage,
            "retargetable": bool(can_retarget and stage != "acted_on"),
            "outcome_label": outcome_label,
            "per_photo_quality": per_photo_quality,
        })

    return result
