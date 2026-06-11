from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from event_pipeline import couchdb_config
from event_pipeline.couchdb_job_draft_writer import (
    JOB_DRAFT_REVIEW_STATUS_DEFAULT,
    CouchDBJobDraftWriterError,
    get_job_draft,
    upsert_job_draft,
)
from field_capture.action_candidates import (
    ACCEPTED_SEMANTIC_TYPES,
    couchdb_candidate_config_or_none,
    iter_semantic_artifacts,
    payloads_from_semantic,
)
from field_capture.approved_job_drafts import proposed_queue_jobs
from processing_core.status import STATUS_COMPLETE


LOGGER = logging.getLogger(__name__)


def job_drafts_from_semantic(
    semantic_path: Path,
    semantic_payload: dict[str, object],
    *,
    runtime_root: Path | None = None,
) -> list[dict[str, object]]:
    if (
        semantic_payload.get("type") not in ACCEPTED_SEMANTIC_TYPES
        or semantic_payload.get("status") != STATUS_COMPLETE
    ):
        return []

    payloads = payloads_from_semantic(semantic_path, semantic_payload)
    group_id = _group_id(semantic_path, payloads)
    created_at = datetime.now(timezone.utc).isoformat()
    drafts: list[dict[str, object]] = []

    for candidate in payloads:
        for job_type, payload_dict, error in proposed_queue_jobs(candidate, runtime_root=runtime_root):
            job_type_text = str(job_type or "").strip()
            if not job_type_text:
                continue
            ordinal = len(drafts)
            channel_metadata = _dict_field(candidate, "channel_metadata")
            drafts.append(
                {
                    "draft_id": f"{group_id}-{job_type_text}-{ordinal}",
                    "job_type": job_type_text,
                    "payload": payload_dict if isinstance(payload_dict, dict) else {},
                    "review_status": JOB_DRAFT_REVIEW_STATUS_DEFAULT,
                    "validation_error": str(error) if error else None,
                    "message": _message(candidate),
                    "site_id": _site_id(candidate),
                    "source_kind": _source_kind(candidate),
                    "source_capture_id": _capture_id(candidate),
                    "submitter_name": str(
                        channel_metadata.get("person_name") or channel_metadata.get("person_id") or ""
                    ),
                    "confidence": candidate.get("confidence"),
                    "group_id": group_id,
                    "created_at": created_at,
                    "source": "field_capture_pipeline",
                }
            )

    return drafts


def collect_job_drafts(
    semantic_dirs: Sequence[Path] | Path,
    *,
    runtime_root: Path | None = None,
) -> dict[str, int]:
    counts = {"discovered": 0, "emitted": 0, "skipped": 0, "existing": 0}
    dirs = (semantic_dirs,) if isinstance(semantic_dirs, Path) else tuple(semantic_dirs)
    config = couchdb_candidate_config_or_none()
    db = couchdb_config.field_captures_database() if config is not None else ""

    for semantic_root_arg in dirs:
        semantic_root = semantic_root_arg.expanduser().resolve(strict=False)
        for semantic_path, semantic_payload in iter_semantic_artifacts(semantic_root):
            drafts = job_drafts_from_semantic(
                semantic_path,
                semantic_payload,
                runtime_root=runtime_root,
            )
            if not drafts:
                counts["skipped"] += 1
                continue
            counts["discovered"] += len(drafts)
            if config is None:
                continue
            for draft in drafts:
                draft_id = str(draft.get("draft_id") or "")
                if couchdb_job_draft_exists(config, db, draft_id):
                    counts["existing"] += 1
                    continue
                try:
                    upsert_job_draft(config, db, draft)
                except CouchDBJobDraftWriterError as exc:
                    LOGGER.error(
                        "field job draft CouchDB write failed: draft_id=%s error=%s",
                        draft.get("draft_id"),
                        exc,
                    )
                    raise
                counts["emitted"] += 1

    return counts


def couchdb_job_draft_exists(config: couchdb_config.CouchDBConfig, db: str, draft_id: str) -> bool:
    try:
        return get_job_draft(config, db, draft_id) is not None
    except CouchDBJobDraftWriterError as exc:
        LOGGER.error(
            "field job draft CouchDB existence check failed: draft_id=%s error=%s",
            draft_id,
            exc,
        )
        raise


def _dict_field(candidate: dict[str, object], field: str) -> dict[str, object]:
    value = candidate.get(field)
    return value if isinstance(value, dict) else {}


def _capture_id(candidate: dict[str, object]) -> str:
    channel_metadata = _dict_field(candidate, "channel_metadata")
    provenance = _dict_field(candidate, "provenance")
    return str(
        candidate.get("capture_id")
        or channel_metadata.get("capture_id")
        or channel_metadata.get("upload_id")
        or provenance.get("capture_id")
        or provenance.get("upload_id")
        or ""
    )


def _group_id(semantic_path: Path, payloads: Sequence[dict[str, object]]) -> str:
    for candidate in payloads:
        capture_id = _capture_id(candidate).strip()
        if capture_id:
            return capture_id
    digest = hashlib.sha256(str(semantic_path).encode("utf-8")).hexdigest()
    return f"semantic-{digest}"


def _message(candidate: dict[str, object]) -> str:
    return str(
        candidate.get("summary")
        or candidate.get("source_text")
        or candidate.get("operational_summary")
        or candidate.get("source_context")
        or ""
    )


def _site_id(candidate: dict[str, object]) -> str:
    channel_metadata = _dict_field(candidate, "channel_metadata")
    provenance = _dict_field(candidate, "provenance")
    return str(
        candidate.get("site_id")
        or channel_metadata.get("site_id")
        or provenance.get("site_id")
        or ""
    )


def _source_kind(candidate: dict[str, object]) -> str:
    channel_metadata = _dict_field(candidate, "channel_metadata")
    provenance = _dict_field(candidate, "provenance")
    return str(
        candidate.get("source_kind")
        or channel_metadata.get("source_kind")
        or provenance.get("source_kind")
        or provenance.get("semantic_artifact_type")
        or ""
    )
