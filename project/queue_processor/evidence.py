from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from io_atomic import atomic_write_text
from epistemic import apply_epistemic_state
from processing_core.time import utc_now
from queue_processor.idempotency import has_job_been_applied, parse_frontmatter_text


CONFIDENCE_STRUCTURALLY_SAFE = "STRUCTURALLY_SAFE"
CONFIDENCE_STRUCTURALLY_UNCERTAIN = "STRUCTURALLY_UNCERTAIN"
CONFIDENCE_SEMANTICALLY_DRIFTED = "SEMANTICALLY_DRIFTED"
CONFIDENCE_SEMANTICALLY_UNKNOWN = "SEMANTICALLY_UNKNOWN"
CONFIDENCE_REPLAY_HIGH_RISK = "REPLAY_HIGH_RISK"


@dataclass(frozen=True)
class DriftAssessment:
    confidence: str
    indicators: list[str]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_capture_id(capture_id: str | None) -> str:
    if capture_id is None or not capture_id.strip():
        return "unknown-capture"
    return capture_id


def evidence_path_for(runtime_root: Path, capture_id: str | None, job_id: str) -> Path:
    return runtime_root / "evidence" / normalize_capture_id(capture_id) / f"{job_id}.json"


def frontmatter_fingerprint(text: str) -> str | None:
    frontmatter, _body, has_frontmatter = parse_frontmatter_text(text)
    if not has_frontmatter:
        return None
    return sha256_text(json.dumps(frontmatter, sort_keys=True, separators=(",", ":")))


def line_hashes(text: str) -> list[str]:
    return [sha256_text(line.strip()) for line in text.splitlines() if line.strip()]


def find_excerpt(text: str, mutation_text: str | None, radius: int = 2) -> tuple[str, dict[str, Any]]:
    if not mutation_text or not mutation_text.strip():
        return "", {"location": "unknown"}
    needle = mutation_text.strip()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if needle in line or line.strip() in needle:
            start = max(0, index - radius)
            end = min(len(lines), index + radius + 1)
            return "\n".join(lines[start:end]), {"line": index + 1, "start_line": start + 1, "end_line": end}
    raw_index = text.find(needle)
    if raw_index >= 0:
        excerpt = text[max(0, raw_index - 240): raw_index + len(needle) + 240]
        return excerpt, {"char_offset": raw_index}
    return "", {"location": "not-found"}


def fingerprint_text(text: str, mutation_text: str | None = None) -> dict[str, Any]:
    excerpt, location = find_excerpt(text, mutation_text)
    return {
        "document_hash": sha256_text(text),
        "frontmatter_hash": frontmatter_fingerprint(text),
        "line_count": len(text.splitlines()),
        "nonempty_line_hashes": line_hashes(text),
        "nearby_excerpt_hash": sha256_text(excerpt) if excerpt else None,
        "nearby_excerpt": excerpt,
        "location": location,
    }


def mutation_intent_summary(job_type: str, payload: dict[str, Any], intent: dict[str, Any] | None) -> str:
    if intent:
        parts = [str(intent.get(key, "")).strip() for key in ("category", "reason", "operator_relevance") if intent.get(key)]
        if parts:
            return " | ".join(parts)
    if job_type == "append_to_note":
        return f"append note to {payload.get('destination', 'journal')}"
    if job_type == "personal_journal_entry":
        return "append personal journal entry"
    if job_type == "flag_access_constraint":
        return "record site access constraint"
    if job_type == "trigger_recruiting":
        return "record recruiting trigger"
    if job_type == "close_recruiting":
        return "record recruiting closure"
    if job_type == "remove_from_schedule":
        return "record schedule removal"
    if job_type == "flag_retention_risk":
        return "record employee retention risk"
    if job_type == "add_person":
        return "create person record"
    if job_type == "visit_create":
        return "record visit evidence"
    return f"process {job_type}"


def marker_state(text: str, job_id: str) -> dict[str, Any]:
    return {
        "marker_present": has_job_been_applied(text, job_id),
        "btq_job_ids_mentions": text.count(job_id),
    }


def write_evidence_snapshot(
    runtime_root: Path,
    *,
    capture_id: str | None,
    job_id: str,
    job_type: str,
    payload: dict[str, Any],
    intent: dict[str, Any] | None,
    target_path: Path,
    pre_text: str,
    post_text: str,
    mutation_text: str | None,
) -> Path:
    post_excerpt, location = find_excerpt(post_text, mutation_text)
    epistemic_source = ""
    if intent and isinstance(intent.get("source_context"), str):
        epistemic_source = intent["source_context"]
    elif mutation_text is not None:
        epistemic_source = mutation_text
    epistemic_state = {}
    if intent and isinstance(intent.get("epistemic_state"), dict):
        epistemic_state = intent["epistemic_state"]
    else:
        epistemic_state = apply_epistemic_state({}, epistemic_source or mutation_intent_summary(job_type, payload, intent), source_type="queue_intent")["epistemic_state"]
    snapshot = {
        "observational": True,
        "capture_id": capture_id,
        "job_id": job_id,
        "mutation_timestamp": utc_now(),
        "target_path": str(target_path),
        "handler_type": job_type,
        "mutation_intent_summary": mutation_intent_summary(job_type, payload, intent),
        "intent": intent or {},
        "epistemic_state": epistemic_state,
        "pre_mutation_fingerprint": fingerprint_text(pre_text, mutation_text),
        "post_mutation_fingerprint": fingerprint_text(post_text, mutation_text),
        "nearby_content_excerpt": post_excerpt,
        "mutation_location_hints": location,
        "marker_state_at_mutation_time": {
            "pre": marker_state(pre_text, job_id),
            "post": marker_state(post_text, job_id),
        },
    }
    path = evidence_path_for(runtime_root, capture_id, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    return path


def read_evidence(runtime_root: Path, capture_id: str | None, job_id: str) -> dict[str, Any] | None:
    direct_path = evidence_path_for(runtime_root, capture_id, job_id)
    if direct_path.exists():
        payload = json.loads(direct_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    evidence_root = runtime_root / "evidence"
    if not evidence_root.exists():
        return None
    matches = sorted(evidence_root.glob(f"*/{job_id}.json"))
    if not matches:
        return None
    payload = json.loads(matches[0].read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def assess_drift(evidence: dict[str, Any] | None, current_text: str | None) -> DriftAssessment:
    if evidence is None:
        return DriftAssessment(CONFIDENCE_SEMANTICALLY_UNKNOWN, ["no evidence snapshot available"])
    if current_text is None:
        return DriftAssessment(CONFIDENCE_REPLAY_HIGH_RISK, ["target missing"])
    post = evidence.get("post_mutation_fingerprint")
    if not isinstance(post, dict):
        return DriftAssessment(CONFIDENCE_SEMANTICALLY_UNKNOWN, ["evidence snapshot missing post fingerprint"])
    current = fingerprint_text(current_text, str(evidence.get("nearby_content_excerpt", "")))
    indicators: list[str] = []
    if post.get("frontmatter_hash") != current.get("frontmatter_hash"):
        indicators.append("frontmatter changed")
    if post.get("nearby_excerpt_hash") and post.get("nearby_excerpt_hash") != current.get("nearby_excerpt_hash"):
        indicators.append("nearby mutation context changed")
    if post.get("line_count") != current.get("line_count"):
        indicators.append("target line count changed")
    evidence_excerpt = str(evidence.get("nearby_content_excerpt", "")).strip()
    if evidence_excerpt and evidence_excerpt not in current_text:
        indicators.append("mutation region removed or substantially altered")
    if indicators:
        return DriftAssessment(CONFIDENCE_SEMANTICALLY_DRIFTED, indicators)
    return DriftAssessment(CONFIDENCE_STRUCTURALLY_SAFE, [])
