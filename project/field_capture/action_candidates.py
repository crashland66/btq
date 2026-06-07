from __future__ import annotations
from event_pipeline.site_registry_data import load_brand_keywords

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from config import get_config
from processing_core.action_candidates import (
    REVIEW_STATUSES,
    action_candidate_payload,
    action_candidate_review_path,
    write_action_candidate_review,
)
from processing_core.artifacts import read_json_object, resolve_within_root, write_json_object
from processing_core.extracted_actions import (
    EXTRACTED_ACTION_SCHEMA_VERSION,
    EXTRACTED_ACTIONS_KEY,
    ExtractedAction,
    validated_extracted_actions,
)
from processing_core.results import result_counts
from processing_core.status import STATUS_COMPLETE
from queue_spec import normalize_vault_relative_path as _normalize_vault_relative_path
from queue_spec import validate_job


FIELD_CAPTURE_CANDIDATE_TYPE = "field_capture_follow_up"
AFTER_PHOTO_ACTION = "Capture after photo once corrected."
GENERIC_REVIEW_ACTION = "Review the field audio note and decide whether follow-up is needed."
SUPPLY_REVIEW_ACTION = "Review supply/order follow-up."
REVIEWABLE_STATUSES = {"approved", "rejected"}
NON_FIELD_OPERATION_ACTION_TERMS = (
    "add journal",
    "journal entry",
    "personal journal",
    "vault",
    "transcription process",
    "semantic layer",
    "model",
    "pipeline",
    "processing",
)
ACCEPTED_SEMANTIC_TYPES = frozenset({
    "field_audio_semantic_summary",
    "field_text_semantic_summary",
    "voice_memo_semantic_summary",
})
LISTABLE_STATUSES = sorted(REVIEW_STATUSES)
CANDIDATE_PLAN_WOULD_CREATE = "would_create"
CANDIDATE_PLAN_SKIPPED = "skipped"
CANDIDATE_PLAN_FAILED = "failed"


class CandidateReviewError(RuntimeError):
    """Raised when a candidate review update is unsafe."""


def default_runtime_root() -> Path:
    return get_config().runtime_root


def default_semantic_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "field_capture" / "audio_semantics"


def default_text_semantic_dir(runtime_root: Path | None = None) -> Path:
    root = (runtime_root or Path.cwd()).expanduser().resolve(strict=False)
    return root / "field_capture" / "semantics"


def default_voice_semantic_dir(runtime_root: Path | None = None) -> Path:
    root = (runtime_root or Path.cwd()).expanduser().resolve(strict=False)
    return root / "voice_memo" / "semantics"


def default_semantic_dirs(runtime_root: Path | None = None) -> tuple[Path, ...]:
    """All semantic-artifact directories the candidate pipeline walks.

    Order matters for stable iteration in tests: audio first
    (legacy single-dir consumers), then text, then voice.
    """
    return (
        default_semantic_dir(runtime_root),
        default_text_semantic_dir(runtime_root),
        default_voice_semantic_dir(runtime_root),
    )


def default_candidate_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "reviews" / "action_candidates" / "field_capture"


def iter_semantic_artifacts(semantic_dir: Path) -> Iterable[tuple[Path, dict[str, object]]]:
    root = semantic_dir.expanduser().resolve(strict=False)
    for path in sorted(root.glob("*.json")):
        resolved = path.resolve(strict=False)
        try:
            resolve_within_root(resolved, root)
        except ValueError:
            continue
        payload = read_json_object(resolved)
        if payload is not None:
            yield resolved, payload


def semantic_provenance(path: Path, payload: dict[str, object]) -> dict[str, object]:
    provenance: dict[str, object] = {
        "semantic_artifact_path": str(path),
        "source_transcript_path": str(payload.get("source_transcript_path") or ""),
        "audio_asset_id": str(payload.get("audio_asset_id") or ""),
        "raw_text_hash": str(payload.get("raw_text_hash") or ""),
    }
    return provenance


normalize_vault_relative_path = _normalize_vault_relative_path


def semantic_channel_metadata(payload: dict[str, object]) -> dict[str, object]:
    suggested_tags = payload.get("suggested_tags")
    source_kind = str(payload.get("source_kind") or "")
    channel = "voice_memo" if source_kind == "voice_memo" or payload.get("type") == "voice_memo_semantic_summary" else "field_capture"
    upload_id = str(payload.get("upload_id") or payload.get("capture_id") or "")
    metadata = {
        "channel": channel,
        "site_id": str(payload.get("site_id") or ""),
        "upload_id": upload_id,
        "area": str(payload.get("area") or ""),
        "phase": str(payload.get("phase") or ""),
        "semantic_engine": str(payload.get("semantic_engine") or ""),
        "issue_detected": bool(payload.get("issue_detected")),
        "issue_type": str(payload.get("issue_type") or ""),
        "urgency": str(payload.get("urgency") or ""),
        "visit_proposed": bool(payload.get("visit_proposed") or False),
        "visit_type": str(payload.get("visit_type") or ""),
        "submitter_person_id": str(payload.get("submitter_person_id") or ""),
        "suggested_tags": suggested_tags if isinstance(suggested_tags, list) else [],
    }
    if channel == "voice_memo":
        provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
        sidecar = provenance.get("sidecar") if isinstance(provenance.get("sidecar"), dict) else {}
        employees = sidecar.get("employees") if isinstance(sidecar.get("employees"), list) else []
        metadata.update(
            {
                "routing_flag": str(provenance.get("routing_flag") or ""),
                "audio_file": str(payload.get("audio_file") or payload.get("audio_asset_id") or ""),
                "employee_slugs": [
                    str(employee.get("slug") or employee.get("id") or "").strip()
                    for employee in employees
                    if isinstance(employee, dict) and str(employee.get("slug") or employee.get("id") or "").strip()
                ],
                "employee_names": [
                    str(employee.get("name") or employee.get("label") or "").strip()
                    for employee in employees
                    if isinstance(employee, dict) and str(employee.get("name") or employee.get("label") or "").strip()
                ],
            }
        )
    proposed_note_path = normalize_vault_relative_path(payload.get("proposed_note_path"))
    if proposed_note_path:
        metadata["proposed_note_path"] = proposed_note_path
    return metadata


def normalized_text(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def is_after_photo_summary(summary: str) -> bool:
    return "after photo" in normalized_text(summary) and "correct" in normalized_text(summary)


def is_generic_summary(summary: str) -> bool:
    return normalized_text(summary) == normalized_text(GENERIC_REVIEW_ACTION)


def is_non_field_operation_summary(summary: str) -> bool:
    text = normalized_text(summary)
    return any(term in text for term in NON_FIELD_OPERATION_ACTION_TERMS)


def is_supply_semantic(payload: dict[str, object]) -> bool:
    issue_type = normalized_text(payload.get("issue_type"))
    text = normalized_text(
        " ".join(
            [
                str(payload.get("cleaned_internal_note") or ""),
                str(payload.get("operational_summary") or ""),
                str(payload.get("client_safe_note") or ""),
            ]
        )
    )
    supply_terms = (
        "bathroom cleaner",
        "cleaner refill",
        "fresh mop head",
        "fresh mop heads",
        "mop head",
        "mop heads",
        "order",
        *load_brand_keywords("supply"),
    )
    return issue_type == "supplies" or any(term in text for term in supply_terms)


def needs_after_photo(payload: dict[str, object]) -> bool:
    text = normalized_text(
        " ".join(
            [
                str(payload.get("cleaned_internal_note") or ""),
                str(payload.get("operational_summary") or ""),
                str(payload.get("client_safe_note") or ""),
            ]
        )
    )
    return any(
        phrase in text
        for phrase in (
            "after photo",
            "before and after",
            "before photo",
            "once corrected",
            "when corrected",
            "after it is corrected",
            "take another photo",
            "capture another photo",
            "corrected photo",
            "remediate",
            "remediation",
        )
    )


def is_unclear_or_test_audio(payload: dict[str, object]) -> bool:
    text = normalized_text(
        " ".join(
            [
                str(payload.get("cleaned_internal_note") or ""),
                str(payload.get("operational_summary") or ""),
                str(payload.get("client_safe_note") or ""),
            ]
        )
    )
    junk_phrases = (
        "just a test recording",
        "test recording",
        "blah, blah",
        "blah blah",
        "august 8th, guys",
        "contract can scare",
        "staged to scare",
        "more and more staged",
    )
    return any(phrase in text for phrase in junk_phrases)


def quality_filtered_summaries(payload: dict[str, object]) -> list[str]:
    action_candidates = payload.get("action_candidates")
    if not isinstance(action_candidates, list):
        return []

    raw_summaries = [candidate.strip() for candidate in action_candidates if isinstance(candidate, str) and candidate.strip()]
    if not raw_summaries:
        return []

    if is_unclear_or_test_audio(payload):
        return []

    if is_supply_semantic(payload):
        supply_summaries = [
            summary
            for summary in raw_summaries
            if any(term in normalized_text(summary) for term in ("supply", "supplies", "restock", "order", "cleaner", "mop head", "mop heads"))
            and not is_after_photo_summary(summary)
        ]
        return list(dict.fromkeys(supply_summaries or [SUPPLY_REVIEW_ACTION]))

    filtered: list[str] = []
    allow_after_photo = needs_after_photo(payload)
    has_specific_after_photo = any(is_after_photo_summary(summary) and normalized_text(summary) != normalized_text(AFTER_PHOTO_ACTION) for summary in raw_summaries)
    for summary in raw_summaries:
        if is_non_field_operation_summary(summary):
            continue
        if is_after_photo_summary(summary) and not allow_after_photo:
            continue
        if normalized_text(summary) == normalized_text(AFTER_PHOTO_ACTION) and has_specific_after_photo:
            continue
        if is_generic_summary(summary) and any(not is_generic_summary(existing) for existing in raw_summaries):
            continue
        filtered.append(summary)
    return list(dict.fromkeys(filtered))


def has_extracted_actions(payload: dict[str, object]) -> bool:
    extracted_actions = payload.get(EXTRACTED_ACTIONS_KEY)
    return isinstance(extracted_actions, list) and bool(extracted_actions)


def source_excerpt_hash(source_excerpt: str) -> str:
    return hashlib.sha256(normalized_text(source_excerpt).encode("utf-8")).hexdigest()


def structured_action_source_kind(payload: dict[str, object], action: ExtractedAction) -> str:
    return action.source_kind or str(payload.get("source_kind") or payload.get("type") or "")


def structured_action_stable_id_key(payload: dict[str, object], action: ExtractedAction) -> dict[str, object]:
    target_identity = action.target_id or normalized_text(action.target_label)
    proposed_job_type = ""
    if isinstance(action.proposed_queue_job, dict):
        proposed_job_type = str(action.proposed_queue_job.get("job_type") or "")
    job_type = action.job_type or proposed_job_type
    return {
        "semantic_artifact_type": str(payload.get("type") or ""),
        "capture_id": str(payload.get("capture_id") or payload.get("upload_id") or ""),
        "source_kind": structured_action_source_kind(payload, action),
        "action_key": action.action_key,
        "source_excerpt_hash": source_excerpt_hash(action.source_excerpt),
        "job_type": job_type,
        "target_type": action.target_type,
        "target_identity": target_identity,
    }


def valid_proposed_queue_job(action: ExtractedAction) -> dict[str, object] | None:
    proposed = action.proposed_queue_job
    if proposed is None:
        return None
    try:
        if validate_job(proposed):
            return proposed
    except Exception:
        return None
    return None


def structured_payloads_from_semantic(path: Path, payload: dict[str, object]) -> list[dict[str, object]]:
    try:
        actions = validated_extracted_actions(payload.get(EXTRACTED_ACTIONS_KEY))
    except (TypeError, ValueError):
        return []

    provenance = semantic_provenance(path, payload)
    records: list[dict[str, object]] = []
    for action in actions:
        source_kind = structured_action_source_kind(payload, action)
        proposed_job_type = ""
        if isinstance(action.proposed_queue_job, dict):
            proposed_job_type = str(action.proposed_queue_job.get("job_type") or "")
        job_type = action.job_type or proposed_job_type
        channel_metadata = {
            **semantic_channel_metadata(payload),
            "target_type": action.target_type,
            "target_id": action.target_id,
            "target_label": action.target_label,
            "action_key": action.action_key,
            "job_type": job_type,
            "source_kind": source_kind,
            "extracted_action_schema": EXTRACTED_ACTION_SCHEMA_VERSION,
        }
        if action.payload_fields is not None:
            channel_metadata["payload_fields"] = action.payload_fields
        if action.evidence_terms:
            channel_metadata["evidence_terms"] = list(action.evidence_terms)
        if action.proposed_queue_job_error:
            channel_metadata["proposed_queue_job_error"] = action.proposed_queue_job_error

        record = action_candidate_payload(
            candidate_type=action.candidate_type,
            summary=action.summary,
            rationale=action.rationale,
            confidence=action.confidence,
            source_text=action.source_excerpt,
            source_context=str(payload.get("operational_summary") or payload.get("client_safe_note") or ""),
            provenance=provenance,
            channel_metadata=channel_metadata,
            id_key=structured_action_stable_id_key(payload, action),
        )
        proposed = valid_proposed_queue_job(action)
        if proposed is not None:
            record["approval_metadata"] = {
                "proposed_queue_job": proposed,
                "extracted_action": {
                    "action_key": action.action_key,
                    "schema": EXTRACTED_ACTION_SCHEMA_VERSION,
                },
            }
        records.append(record)
    return records


def payloads_from_semantic(path: Path, payload: dict[str, object]) -> list[dict[str, object]]:
    if has_extracted_actions(payload):
        return structured_payloads_from_semantic(path, payload)

    action_candidates = payload.get("action_candidates")
    if not isinstance(action_candidates, list):
        return [
            action_candidate_payload(
                candidate_type=FIELD_CAPTURE_CANDIDATE_TYPE,
                summary="",
                rationale="Malformed field-capture semantic action_candidates value.",
                source_text=str(payload.get("cleaned_internal_note") or ""),
                source_context=str(payload.get("operational_summary") or ""),
                provenance=semantic_provenance(path, payload),
                channel_metadata=semantic_channel_metadata(payload),
            )
        ]

    malformed_entries = [candidate for candidate in action_candidates if not isinstance(candidate, str) or not candidate.strip()]
    summaries = quality_filtered_summaries(payload)
    if malformed_entries and not summaries:
        return [
            action_candidate_payload(
                candidate_type=FIELD_CAPTURE_CANDIDATE_TYPE,
                summary="",
                rationale=str(payload.get("operational_summary") or ""),
                confidence="unknown",
                source_text=str(payload.get("cleaned_internal_note") or ""),
                source_context=str(payload.get("client_safe_note") or ""),
                provenance=semantic_provenance(path, payload),
                channel_metadata=semantic_channel_metadata(payload),
            )
        ]

    records: list[dict[str, object]] = []
    for summary in summaries:
        records.append(
            action_candidate_payload(
                candidate_type=FIELD_CAPTURE_CANDIDATE_TYPE,
                summary=summary,
                rationale=str(payload.get("operational_summary") or ""),
                confidence="unknown",
                source_text=str(payload.get("cleaned_internal_note") or ""),
                source_context=str(payload.get("client_safe_note") or ""),
                provenance=semantic_provenance(path, payload),
                channel_metadata=semantic_channel_metadata(payload),
            )
        )
    return records


def candidate_collection_result(
    *,
    semantic_path: Path,
    status: str,
    reason: str,
    candidate: dict[str, object] | None = None,
    candidate_path: Path | None = None,
) -> dict[str, object]:
    return {
        "semantic_path": str(semantic_path),
        "candidate_id": "" if candidate is None else str(candidate.get("candidate_id") or ""),
        "candidate_path": "" if candidate_path is None else str(candidate_path),
        "status": status,
        "reason": reason,
        "candidate": candidate,
    }


def plan_candidates_from_semantic(
    semantic_path: Path,
    semantic_payload: dict[str, object],
    candidate_dir: Path,
) -> list[dict[str, object]]:
    semantic_type = semantic_payload.get("type")
    semantic_status = semantic_payload.get("status")
    if semantic_type not in ACCEPTED_SEMANTIC_TYPES or semantic_status != STATUS_COMPLETE:
        if semantic_type not in ACCEPTED_SEMANTIC_TYPES:
            reason = f"semantic type not accepted: {semantic_type!r}"
        else:
            reason = f"semantic status is not complete: {semantic_status!r}"
        return [
            candidate_collection_result(
                semantic_path=semantic_path,
                status=CANDIDATE_PLAN_SKIPPED,
                reason=reason,
            )
        ]

    action_candidates = semantic_payload.get("action_candidates")
    if not has_extracted_actions(semantic_payload) and isinstance(action_candidates, list) and not action_candidates:
        return [
            candidate_collection_result(
                semantic_path=semantic_path,
                status=CANDIDATE_PLAN_SKIPPED,
                reason="semantic artifact has no action candidates",
            )
        ]

    if isinstance(action_candidates, list) and not payloads_from_semantic(semantic_path, semantic_payload):
        return [
            candidate_collection_result(
                semantic_path=semantic_path,
                status=CANDIDATE_PLAN_SKIPPED,
                reason="semantic action candidates suppressed by quality rules",
            )
        ]

    results: list[dict[str, object]] = []
    for candidate in payloads_from_semantic(semantic_path, semantic_payload):
        candidate_id = str(candidate.get("candidate_id") or "")
        candidate_path = action_candidate_review_path(candidate_dir, candidate_id) if candidate_id else None
        if candidate.get("status") == "failed":
            error = candidate.get("error")
            reason = "candidate payload failed validation"
            if isinstance(error, dict) and error.get("message"):
                reason = str(error["message"])
            results.append(
                candidate_collection_result(
                    semantic_path=semantic_path,
                    status=CANDIDATE_PLAN_FAILED,
                    reason=reason,
                    candidate=candidate,
                    candidate_path=candidate_path,
                )
            )
        elif candidate_path is not None and candidate_path.exists():
            results.append(
                candidate_collection_result(
                    semantic_path=semantic_path,
                    status=CANDIDATE_PLAN_SKIPPED,
                    reason="action candidate already exists",
                    candidate=candidate,
                    candidate_path=candidate_path,
                )
            )
        else:
            results.append(
                candidate_collection_result(
                    semantic_path=semantic_path,
                    status=CANDIDATE_PLAN_WOULD_CREATE,
                    reason="would create action candidate review artifact",
                    candidate=candidate,
                    candidate_path=candidate_path,
                )
            )
    return results


def collect_action_candidates(
    semantic_dirs: Sequence[Path] | Path,
    candidate_dir: Path,
    *,
    runtime_root: Path | None = None,
) -> dict[str, int]:
    counts = result_counts("discovered", "skipped", "completed", "failed")
    dirs = (semantic_dirs,) if isinstance(semantic_dirs, Path) else tuple(semantic_dirs)
    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    if runtime_root is not None:
        runtime_resolved = runtime_root.expanduser().resolve(strict=False)
        resolve_within_root(candidate_root, runtime_resolved)

    for semantic_root_arg in dirs:
        semantic_root = semantic_root_arg.expanduser().resolve(strict=False)
        if runtime_root is not None:
            resolve_within_root(semantic_root, runtime_resolved)
        for semantic_path, semantic_payload in iter_semantic_artifacts(semantic_root):
            if semantic_payload.get("type") not in ACCEPTED_SEMANTIC_TYPES or semantic_payload.get("status") != STATUS_COMPLETE:
                counts["skipped"] += 1
                continue
            payloads = payloads_from_semantic(semantic_path, semantic_payload)
            if not payloads:
                counts["skipped"] += 1
                continue
            counts["discovered"] += len(payloads)
            for payload in payloads:
                candidate_id = str(payload.get("candidate_id") or "")
                candidate_path = action_candidate_review_path(candidate_root, candidate_id) if candidate_id else None
                if candidate_path is not None and candidate_path.exists():
                    counts["skipped"] += 1
                    continue
                write_action_candidate_review(candidate_root, payload)
                if payload.get("status") == "failed":
                    counts["failed"] += 1
                else:
                    counts["completed"] += 1
    return counts


def collect_action_candidates_report(
    semantic_dirs: Sequence[Path] | Path,
    candidate_dir: Path,
    *,
    runtime_root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    counts = result_counts("discovered", "skipped", "completed", "failed")
    results: list[dict[str, object]] = []
    dirs = (semantic_dirs,) if isinstance(semantic_dirs, Path) else tuple(semantic_dirs)
    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    if runtime_root is not None:
        runtime_resolved = runtime_root.expanduser().resolve(strict=False)
        resolve_within_root(candidate_root, runtime_resolved)

    for semantic_root_arg in dirs:
        semantic_root = semantic_root_arg.expanduser().resolve(strict=False)
        if runtime_root is not None:
            resolve_within_root(semantic_root, runtime_resolved)
        for semantic_path, semantic_payload in iter_semantic_artifacts(semantic_root):
            planned = plan_candidates_from_semantic(semantic_path, semantic_payload, candidate_root)
            results.extend(planned)
            if semantic_payload.get("type") not in ACCEPTED_SEMANTIC_TYPES or semantic_payload.get("status") != STATUS_COMPLETE:
                counts["skipped"] += 1
                continue
            candidate_results = [result for result in planned if result.get("candidate") is not None]
            if not candidate_results:
                counts["skipped"] += 1
                continue
            counts["discovered"] += len(candidate_results)
            for result in candidate_results:
                if result["status"] == CANDIDATE_PLAN_WOULD_CREATE:
                    counts["completed"] += 1
                elif result["status"] == CANDIDATE_PLAN_FAILED:
                    counts["failed"] += 1
                else:
                    counts["skipped"] += 1
    return {"dry_run": dry_run, "counts": counts, "results": results}


def find_candidate_artifacts(candidate_dir: Path, candidate_id: str) -> list[tuple[Path, dict[str, object]]]:
    root = candidate_dir.expanduser().resolve(strict=False)
    matches: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(root.glob("*.json")):
        resolved = path.resolve(strict=False)
        try:
            resolve_within_root(resolved, root)
        except ValueError:
            continue
        payload = read_json_object(resolved)
        if payload is not None and payload.get("candidate_id") == candidate_id:
            matches.append((resolved, payload))
    return matches


def iter_candidate_artifacts(candidate_dir: Path) -> Iterable[tuple[Path, dict[str, object]]]:
    root = candidate_dir.expanduser().resolve(strict=False)
    for path in sorted(root.glob("*.json")):
        resolved = path.resolve(strict=False)
        try:
            resolve_within_root(resolved, root)
        except ValueError:
            continue
        payload = read_json_object(resolved)
        if payload is not None:
            yield resolved, payload


def preview_text(value: object, *, limit: int = 96) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def candidate_list_item(path: Path, payload: dict[str, object], *, include_source: bool = False) -> dict[str, object]:
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    item: dict[str, object] = {
        "candidate_id": str(payload.get("candidate_id") or ""),
        "status": str(payload.get("status") or ""),
        "candidate_type": str(payload.get("candidate_type") or payload.get("type") or ""),
        "summary": str(payload.get("summary") or ""),
        "confidence": payload.get("confidence"),
        "rationale": str(payload.get("rationale") or ""),
        "source_text_preview": preview_text(payload.get("source_text")),
        "source_context_preview": preview_text(payload.get("source_context")),
        "semantic_artifact_path": str(provenance.get("semantic_artifact_path") or ""),
        "artifact_path": str(path),
        "reviewer": str(payload.get("reviewer") or ""),
        "reviewed_at": str(payload.get("reviewed_at") or ""),
        "review_rationale": str(payload.get("review_rationale") or ""),
    }
    if include_source:
        item["source_text"] = str(payload.get("source_text") or "")
        item["source_context"] = str(payload.get("source_context") or "")
    return item


def list_action_candidates_report(
    candidate_dir: Path,
    *,
    status: str | None = None,
    limit: int | None = None,
    include_source: bool = False,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    if runtime_root is not None:
        resolve_within_root(candidate_root, runtime_root.expanduser().resolve(strict=False))

    counts: dict[str, int] = {"total": 0}
    for candidate_status in LISTABLE_STATUSES:
        counts[candidate_status] = 0

    items: list[dict[str, object]] = []
    for path, payload in iter_candidate_artifacts(candidate_root):
        payload_status = str(payload.get("status") or "")
        counts["total"] += 1
        if payload_status in counts:
            counts[payload_status] += 1
        else:
            counts[payload_status] = counts.get(payload_status, 0) + 1
        if status is not None and payload_status != status:
            continue
        items.append(candidate_list_item(path, payload, include_source=include_source))

    items.sort(key=lambda item: (str(item["candidate_id"]), str(item["artifact_path"])))
    if limit is not None:
        items = items[: max(0, limit)]
    return {"channel": "field_capture", "counts": counts, "candidates": items}


def review_candidate(
    candidate_dir: Path,
    *,
    candidate_id: str,
    status: str,
    reviewer: str,
    rationale: str,
    reviewed_at: str | None = None,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    if status not in REVIEWABLE_STATUSES:
        raise CandidateReviewError(f"unsupported review status: {status}")
    if not candidate_id.strip():
        raise CandidateReviewError("candidate_id is required")
    if not reviewer.strip():
        raise CandidateReviewError("reviewer is required")
    if not rationale.strip():
        raise CandidateReviewError("review rationale is required")

    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    if runtime_root is not None:
        resolve_within_root(candidate_root, runtime_root.expanduser().resolve(strict=False))
    matches = find_candidate_artifacts(candidate_root, candidate_id)
    if not matches:
        raise CandidateReviewError(f"candidate not found: {candidate_id}")
    if len(matches) > 1:
        raise CandidateReviewError(f"multiple candidate artifacts found for candidate_id: {candidate_id}")

    path, payload = matches[0]
    if payload.get("type") != "action_candidate_review":
        raise CandidateReviewError("candidate artifact type is not action_candidate_review")
    if payload.get("status") == "failed" or payload.get("error") is not None:
        raise CandidateReviewError("failed or malformed candidate cannot be reviewed")
    if not str(payload.get("summary") or "").strip() or not str(payload.get("candidate_type") or "").strip():
        raise CandidateReviewError("malformed candidate cannot be reviewed")

    prior_status = str(payload.get("status") or "")
    timestamp = reviewed_at or datetime.now(timezone.utc).isoformat()
    history = payload.get("review_history")
    review_history = list(history) if isinstance(history, list) else []
    review_entry = {
        "reviewer": reviewer,
        "reviewed_at": timestamp,
        "review_rationale": rationale,
        "prior_status": prior_status,
        "status": status,
    }
    review_history.append(review_entry)
    updated = {
        **payload,
        "status": status,
        "reviewer": reviewer,
        "reviewed_at": timestamp,
        "review_rationale": rationale,
        "prior_status": prior_status,
        "review_history": review_history,
    }
    write_json_object(path, updated)
    return {
        "candidate_id": candidate_id,
        "candidate_path": str(path),
        "prior_status": prior_status,
        "status": status,
        "reviewer": reviewer,
        "changed": prior_status != status,
    }


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Collect field-capture semantic action candidates for review.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--semantic-dir", type=Path)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def build_review_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Approve or reject one field-capture action candidate.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--status", choices=sorted(REVIEWABLE_STATUSES), required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def build_list_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="List field-capture action candidates for operator review.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--status", choices=LISTABLE_STATUSES)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--include-source", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    semantic_target = args.semantic_dir.expanduser() if args.semantic_dir is not None else default_semantic_dirs(runtime_root)
    candidate_dir = args.candidate_dir.expanduser() if args.candidate_dir is not None else default_candidate_dir(runtime_root)
    if args.dry_run:
        report = collect_action_candidates_report(semantic_target, candidate_dir, runtime_root=runtime_root, dry_run=True)
        counts = report["counts"]
    else:
        report = None
        counts = collect_action_candidates(semantic_target, candidate_dir, runtime_root=runtime_root)
    if args.json:
        print(json.dumps(report if args.dry_run else counts, indent=2, sort_keys=True))
    else:
        prefix = "field action candidates dry-run" if args.dry_run else "field action candidates"
        print(
            f"{prefix}: "
            f"discovered={counts['discovered']} skipped={counts['skipped']} "
            f"completed={counts['completed']} failed={counts['failed']}"
        )
        if args.dry_run and report is not None:
            for result in report["results"]:
                print(f"- {result['candidate_id']} {result['status']}: {result['reason']}")
    return 0


def run_list(argv: list[str] | None = None) -> int:
    args = build_list_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    candidate_dir = args.candidate_dir.expanduser() if args.candidate_dir is not None else default_candidate_dir(runtime_root)
    report = list_action_candidates_report(
        candidate_dir,
        status=args.status,
        limit=args.limit,
        include_source=args.include_source,
        runtime_root=runtime_root,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        counts = report["counts"]
        print(
            "field action candidates: "
            f"total={counts['total']} pending_review={counts.get('pending_review', 0)} "
            f"approved={counts.get('approved', 0)} rejected={counts.get('rejected', 0)} "
            f"failed={counts.get('failed', 0)}"
        )
        for item in report["candidates"]:
            print(
                f"- {item['candidate_id']} [{item['status']}] "
                f"{item['summary']} "
                f"(confidence={item['confidence']}; artifact={item['artifact_path']})"
            )
            if item["rationale"]:
                print(f"  rationale: {item['rationale']}")
            if item["reviewer"] or item["reviewed_at"]:
                print(f"  reviewed: {item['reviewer']} {item['reviewed_at']}".rstrip())
            if item["source_context_preview"]:
                print(f"  context: {item['source_context_preview']}")
            if args.include_source:
                if item.get("source_text"):
                    print(f"  source_text: {item['source_text']}")
                if item.get("source_context"):
                    print(f"  source_context: {item['source_context']}")
    return 0


def run_review(argv: list[str] | None = None) -> int:
    args = build_review_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    candidate_dir = args.candidate_dir.expanduser() if args.candidate_dir is not None else default_candidate_dir(runtime_root)
    try:
        result = review_candidate(
            candidate_dir,
            candidate_id=args.candidate_id,
            status=args.status,
            reviewer=args.reviewer,
            rationale=args.rationale,
            runtime_root=runtime_root,
        )
    except CandidateReviewError as exc:
        result = {"error": str(exc), "candidate_id": args.candidate_id, "changed": False}
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"field candidate review failed: {exc}")
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            "field candidate review: "
            f"candidate_id={result['candidate_id']} prior_status={result['prior_status']} "
            f"status={result['status']} reviewer={result['reviewer']} changed={result['changed']}"
        )
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
