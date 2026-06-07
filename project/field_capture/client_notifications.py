from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from config import get_config
from field_capture.action_candidates import CandidateReviewError, default_candidate_dir, find_candidate_artifacts
from processing_core.artifacts import read_json_object, resolve_within_root, write_json_object


ARTIFACT_TYPE = "field_capture_client_notification"
METHODS = {"email", "phone", "in_person", "text", "other"}
UNSAFE_TEXT_MARKERS = (
    "bearer ",
    "field_capture_token",
    "fct_",
    " auth",
    "/users/",
    "/srv/",
    "/var/",
    "\\users\\",
)


class ClientNotificationError(RuntimeError):
    pass


def default_runtime_root() -> Path:
    return get_config().runtime_root


def default_notification_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "reviews" / "client_notifications" / "field_capture"


def notification_path(notification_dir: Path, candidate_id: str) -> Path:
    safe_id = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in candidate_id)
    return notification_dir / f"{safe_id}.json"


def safe_text(value: object, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    lowered = text.lower()
    if any(marker in lowered for marker in UNSAFE_TEXT_MARKERS):
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def candidate_identity(candidate: dict[str, object]) -> dict[str, str]:
    metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "capture_id": str(metadata.get("upload_id") or ""),
        "site_id": str(metadata.get("site_id") or ""),
    }


def load_notification(notification_dir: Path, candidate_id: str) -> dict[str, object] | None:
    payload = read_json_object(notification_path(notification_dir, candidate_id))
    if not payload or payload.get("type") != ARTIFACT_TYPE:
        return None
    if str(payload.get("candidate_id") or "") != candidate_id:
        return None
    return payload


def validate_approved_candidate(candidate_dir: Path, candidate_id: str, *, runtime_root: Path | None = None) -> dict[str, object]:
    if not candidate_id.strip():
        raise ClientNotificationError("candidate_id is required")
    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    if runtime_root is not None:
        resolve_within_root(candidate_root, runtime_root.expanduser().resolve(strict=False))
    matches = find_candidate_artifacts(candidate_root, candidate_id)
    if not matches:
        raise ClientNotificationError(f"candidate not found: {candidate_id}")
    if len(matches) > 1:
        raise ClientNotificationError(f"multiple candidate artifacts found for candidate_id: {candidate_id}")
    _path, candidate = matches[0]
    if candidate.get("type") != "action_candidate_review":
        raise ClientNotificationError("candidate artifact type is not action_candidate_review")
    if str(candidate.get("status") or "") != "approved":
        raise ClientNotificationError(f"candidate status is not approved: {candidate.get('status')}")
    return candidate


def mark_client_informed(
    *,
    candidate_id: str,
    method: str,
    informed_by: str,
    note: str = "",
    runtime_root: Path | None = None,
    candidate_dir: Path | None = None,
    notification_dir: Path | None = None,
    informed_at: str | None = None,
) -> dict[str, object]:
    if method not in METHODS:
        raise ClientNotificationError(f"unsupported client informed method: {method}")
    by = safe_text(informed_by, limit=120)
    if not by:
        raise ClientNotificationError("client_informed_by is required")
    safe_note = safe_text(note, limit=500)
    runtime = (runtime_root or default_runtime_root()).expanduser().resolve(strict=False)
    candidate_root = (candidate_dir or default_candidate_dir(runtime)).expanduser().resolve(strict=False)
    notification_root = (notification_dir or default_notification_dir(runtime)).expanduser().resolve(strict=False)
    resolve_within_root(notification_root, runtime)
    candidate = validate_approved_candidate(candidate_root, candidate_id, runtime_root=runtime)
    identity = candidate_identity(candidate)
    timestamp = informed_at or datetime.now(timezone.utc).isoformat()
    existing = load_notification(notification_root, candidate_id)
    history = []
    if existing:
        prior = {
            key: existing.get(key)
            for key in (
                "client_informed_at",
                "client_informed_by",
                "client_informed_method",
                "client_informed_note",
                "updated_at",
            )
            if existing.get(key) not in {None, ""}
        }
        if prior:
            history.append(prior)
        existing_history = existing.get("notification_history")
        if isinstance(existing_history, list):
            history.extend(item for item in existing_history if isinstance(item, dict))
    payload = {
        "type": ARTIFACT_TYPE,
        "channel": "field_capture",
        "candidate_id": identity["candidate_id"],
        "capture_id": identity["capture_id"],
        "site_id": identity["site_id"],
        "client_informed": True,
        "client_informed_at": timestamp,
        "client_informed_by": by,
        "client_informed_method": method,
        "client_informed_note": safe_note,
        "created_at": str(existing.get("created_at") or timestamp) if existing else timestamp,
        "updated_at": timestamp,
        "notification_history": history,
    }
    write_json_object(notification_path(notification_root, candidate_id), payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Mark an approved field-capture candidate as client informed.")
    parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--notification-dir", type=Path)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--method", choices=sorted(METHODS), required=True)
    parser.add_argument("--by", required=True)
    parser.add_argument("--note", default="")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    try:
        payload = mark_client_informed(
            candidate_id=args.candidate_id,
            method=args.method,
            informed_by=args.by,
            note=args.note,
            runtime_root=args.runtime_root,
            candidate_dir=args.candidate_dir,
            notification_dir=args.notification_dir,
        )
    except (ClientNotificationError, CandidateReviewError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "field-capture client informed: "
            f"candidate_id={payload['candidate_id']} method={payload['client_informed_method']} "
            f"by={payload['client_informed_by']}"
        )
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
