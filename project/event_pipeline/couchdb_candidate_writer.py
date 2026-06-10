from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config


ACTION_CANDIDATE_TYPE = "action_candidate"
ACTION_CANDIDATE_ID_PREFIX = "action_candidate_"
ALLOWED_STATUSES = frozenset({"pending_review", "approved", "rejected", "failed"})
DEFAULT_FIND_LIMIT = 500


class CouchDBCandidateWriterError(Exception):
    pass


class AlreadyDecided(CouchDBCandidateWriterError):
    """Raised when an action candidate review loses optimistic concurrency."""


def action_candidate_doc_id(candidate_id: str) -> str:
    candidate_id_text = str(candidate_id or "").strip()
    if not candidate_id_text:
        raise CouchDBCandidateWriterError("candidate_id is required")
    return f"{ACTION_CANDIDATE_ID_PREFIX}{candidate_id_text}"


def get_action_candidate_document(
    config: couchdb_config.CouchDBConfig,
    database: str,
    candidate_id: str,
) -> dict[str, Any] | None:
    """Fetch an action_candidate document by candidate_id, returning None for 404."""
    return _get_document(config, database, action_candidate_doc_id(candidate_id))


def get_action_candidate(
    config: couchdb_config.CouchDBConfig,
    db: str,
    candidate_id: str,
) -> dict[str, Any] | None:
    """Fetch an action_candidate document by candidate_id, including _rev."""
    return get_action_candidate_document(config, db, candidate_id)


def list_action_candidates(
    config: couchdb_config.CouchDBConfig,
    db: str,
    *,
    status: str | None = None,
    person_id: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List action_candidate documents from CouchDB.

    This is the single canonical candidate listing reader for approval
    surfaces. It expects the field-captures database to have the
    idx-type-status Mango index provisioned by the CouchDB setup command.
    """
    selector: dict[str, Any] = {"type": ACTION_CANDIDATE_TYPE}
    if status == "archived":
        selector["archived"] = True
    elif status is not None:
        selector["status"] = str(status)
    if status != "archived" and not include_archived:
        selector["$or"] = [
            {"archived": {"$exists": False}},
            {"archived": False},
            {"archived": None},
        ]
    if person_id is not None:
        selector["person_id"] = str(person_id)

    docs: list[dict[str, Any]] = []
    bookmark = ""
    while True:
        payload: dict[str, Any] = {
            "selector": selector,
            "limit": DEFAULT_FIND_LIMIT,
        }
        if bookmark:
            payload["bookmark"] = bookmark
        result = _request_json(config, db, "POST", "_find", payload)
        result_docs = result.get("docs")
        if not isinstance(result_docs, list):
            raise CouchDBCandidateWriterError("CouchDB action candidate _find returned no docs list")
        page = [
            doc
            for doc in result_docs
            if isinstance(doc, dict)
            and str(doc.get("type") or "") == ACTION_CANDIDATE_TYPE
        ]
        docs.extend(page)
        next_bookmark = str(result.get("bookmark") or "")
        if len(result_docs) < DEFAULT_FIND_LIMIT or not next_bookmark or next_bookmark == bookmark:
            break
        bookmark = next_bookmark
    docs.sort(key=lambda doc: (str(doc.get("created_at") or ""), str(doc.get("candidate_id") or "")))
    return docs


def set_action_candidate_status(
    config: couchdb_config.CouchDBConfig,
    db: str,
    candidate_id: str,
    *,
    status: str,
    reviewed_by: str,
    rationale: str,
    expected_rev: str | None = None,
    expected_prior_statuses: set[str] | frozenset[str] | tuple[str, ...] = ("pending_review",),
    reason: str | None = None,
) -> dict[str, Any]:
    if status not in {"approved", "rejected"}:
        raise CouchDBCandidateWriterError(f"unsupported action candidate review status: {status}")
    doc = get_action_candidate(config, db, candidate_id)
    if doc is None:
        raise CouchDBCandidateWriterError(f"candidate not found: {candidate_id}")
    current_rev = str(doc.get("_rev") or "")
    if not current_rev:
        raise CouchDBCandidateWriterError(f"candidate has no _rev: {candidate_id}")
    if expected_rev is not None and str(expected_rev) != current_rev:
        raise AlreadyDecided(f"candidate _rev changed: {candidate_id}")
    allowed_prior_statuses = {str(item) for item in expected_prior_statuses}
    if allowed_prior_statuses and str(doc.get("status") or "") not in allowed_prior_statuses:
        raise AlreadyDecided(f"candidate already decided: {candidate_id}")

    timestamp = datetime.now(timezone.utc).isoformat()
    prior_status = str(doc.get("status") or "")
    history = doc.get("review_history")
    review_history = list(history) if isinstance(history, list) else []
    history_entry = {
        "reviewer": reviewed_by,
        "reviewed_at": timestamp,
        "review_rationale": rationale,
        "prior_status": prior_status,
        "status": status,
    }
    if reason:
        history_entry["reason"] = str(reason)
    review_history.append(history_entry)
    updated = dict(doc)
    updated.update(
        {
            "status": status,
            "reviewed_at": timestamp,
            "reviewed_by": reviewed_by,
            "reviewer": reviewed_by,
            "review_rationale": rationale,
            "prior_status": prior_status,
            "review_history": review_history,
        }
    )
    return _put_document(config, db, str(updated["_id"]), updated, conflict_as_already_decided=True)


def set_action_candidate_archived(
    config: couchdb_config.CouchDBConfig,
    db: str,
    candidate_id: str,
    *,
    archived: bool,
    by: str = "",
    at: str | None = None,
    expected_rev: str | None = None,
) -> dict[str, Any]:
    doc = get_action_candidate(config, db, candidate_id)
    if doc is None:
        raise CouchDBCandidateWriterError(f"candidate not found: {candidate_id}")
    current_rev = str(doc.get("_rev") or "")
    if not current_rev:
        raise CouchDBCandidateWriterError(f"candidate has no _rev: {candidate_id}")
    if expected_rev is not None and str(expected_rev) != current_rev:
        raise AlreadyDecided(f"candidate _rev changed: {candidate_id}")

    updated = dict(doc)
    if archived:
        timestamp = at or datetime.now(timezone.utc).isoformat()
        updated["archived"] = True
        updated["archived_at"] = str(timestamp)
        updated["archived_by"] = str(by or "")
    else:
        updated["archived"] = False
        updated.pop("archived_at", None)
        updated.pop("archived_by", None)
    return _put_document(config, db, str(updated["_id"]), updated, conflict_as_already_decided=True)


def set_action_candidate_staged_at(
    config: couchdb_config.CouchDBConfig,
    db: str,
    candidate_id: str,
    *,
    staged_at: str,
    expected_rev: str | None = None,
) -> dict[str, Any]:
    doc = get_action_candidate(config, db, candidate_id)
    if doc is None:
        raise CouchDBCandidateWriterError(f"candidate not found: {candidate_id}")
    if doc.get("staged_at"):
        raise AlreadyDecided(f"candidate already staged: {candidate_id}")
    current_rev = str(doc.get("_rev") or "")
    if expected_rev is not None and str(expected_rev) != current_rev:
        raise AlreadyDecided(f"candidate _rev changed before staging mark: {candidate_id}")
    updated = dict(doc)
    updated["staged_at"] = staged_at
    return _put_document(config, db, str(updated["_id"]), updated, conflict_as_already_decided=True)


def upsert_action_candidate(
    config: couchdb_config.CouchDBConfig,
    db: str,
    candidate: dict[str, Any],
) -> None:
    """
    Idempotently write an action_candidate document to CouchDB.

    The filesystem review artifact remains the pipeline source in Phase 1a; this
    writer normalizes it into the CouchDB action_candidate schema and carries the
    current _rev on PUT so re-runs update the stable document.
    """
    doc = build_action_candidate_document(candidate)
    doc_id = str(doc["_id"])
    db_quoted = parse.quote(db, safe="")
    quoted_id = parse.quote(doc_id, safe="")
    url = f"{config.base_url}/{db_quoted}/{quoted_id}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())

    for attempt in range(1, 3):
        existing = _get_document(config, db, doc_id)
        doc_to_put = dict(doc)
        if existing is not None and existing.get("_rev"):
            doc_to_put["_rev"] = str(existing["_rev"])

        body = json.dumps(doc_to_put, sort_keys=True).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method="PUT")
        try:
            with request.urlopen(req, timeout=config.timeout) as response:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                raw = response.read()
        except error.HTTPError as exc:
            if exc.code == 409 and attempt < 2:
                continue
            raise CouchDBCandidateWriterError(
                f"CouchDB action candidate PUT failed with HTTP {exc.code} for {doc_id}"
            ) from exc
        except (error.URLError, OSError) as exc:
            raise CouchDBCandidateWriterError(
                f"CouchDB action candidate PUT failed for {doc_id}: {exc}"
            ) from exc

        if not 200 <= status < 300:
            raise CouchDBCandidateWriterError(
                f"CouchDB action candidate PUT failed with HTTP {status} for {doc_id}"
            )
        _parse_response_json(raw, "CouchDB action candidate PUT")
        return

    raise CouchDBCandidateWriterError(f"CouchDB action candidate PUT exhausted retries for {doc_id}")


def build_action_candidate_document(candidate: dict[str, Any]) -> dict[str, Any]:
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    if not candidate_id:
        raise CouchDBCandidateWriterError("candidate is missing candidate_id")

    status = str(candidate.get("status") or "").strip()
    if status not in ALLOWED_STATUSES:
        raise CouchDBCandidateWriterError(f"invalid action candidate status for {candidate_id}: {status!r}")

    provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    approval_metadata = candidate.get("approval_metadata") if isinstance(candidate.get("approval_metadata"), dict) else {}
    proposed_queue_job = approval_metadata.get("proposed_queue_job")
    if not isinstance(proposed_queue_job, dict):
        proposed_queue_job = candidate.get("proposed_queue_job")
    proposed_queue_job_doc = _proposed_queue_job_doc(proposed_queue_job)

    source_kind = str(
        candidate.get("source_kind")
        or channel_metadata.get("source_kind")
        or provenance.get("source_kind")
        or provenance.get("semantic_artifact_type")
        or ""
    )
    capture_id = str(
        candidate.get("capture_id")
        or channel_metadata.get("capture_id")
        or channel_metadata.get("upload_id")
        or provenance.get("capture_id")
        or provenance.get("upload_id")
        or ""
    )

    return {
        "_id": action_candidate_doc_id(candidate_id),
        "type": ACTION_CANDIDATE_TYPE,
        "candidate_id": candidate_id,
        "capture_id": capture_id,
        "status": status,
        "candidate_type": str(candidate.get("candidate_type") or ""),
        "source_kind": source_kind,
        "summary": str(candidate.get("summary") or ""),
        "evidence": _evidence(candidate),
        "site_id": str(candidate.get("site_id") or channel_metadata.get("site_id") or provenance.get("site_id") or ""),
        "proposed_queue_job": proposed_queue_job_doc,
        "proposed_queue_job_error": _optional_text(
            candidate.get("proposed_queue_job_error") or channel_metadata.get("proposed_queue_job_error")
        ),
        "person_id": str(candidate.get("person_id") or channel_metadata.get("person_id") or provenance.get("person_id") or ""),
        "created_at": str(candidate.get("created_at") or ""),
        "reviewed_at": str(candidate.get("reviewed_at") or ""),
        "reviewed_by": str(candidate.get("reviewed_by") or candidate.get("reviewer") or ""),
        "review_rationale": str(candidate.get("review_rationale") or ""),
        "archived": bool(candidate.get("archived") is True),
        "archived_at": str(candidate.get("archived_at") or ""),
        "archived_by": str(candidate.get("archived_by") or ""),
        "source": "field_capture_pipeline",
        "source_detail": {
            "artifact_type": str(candidate.get("type") or ""),
            "rationale": str(candidate.get("rationale") or ""),
            "confidence": candidate.get("confidence"),
            "source_text": str(candidate.get("source_text") or ""),
            "source_context": str(candidate.get("source_context") or ""),
            "provenance": dict(provenance),
            "channel_metadata": dict(channel_metadata),
            "approval_metadata": dict(approval_metadata),
            "error": candidate.get("error"),
        },
    }


def _get_document(
    config: couchdb_config.CouchDBConfig,
    database: str,
    doc_id: str,
) -> dict[str, Any] | None:
    db = parse.quote(database, safe="")
    quoted_id = parse.quote(doc_id, safe="")
    url = f"{config.base_url}/{db}/{quoted_id}"
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            raw = response.read()
    except error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise CouchDBCandidateWriterError(
            f"CouchDB action candidate GET failed for {doc_id}: HTTP {exc.code}"
        ) from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBCandidateWriterError(
            f"CouchDB action candidate GET failed for {doc_id}: {exc}"
        ) from exc

    parsed = _parse_response_json(raw, "CouchDB action candidate GET")
    if not isinstance(parsed, dict):
        raise CouchDBCandidateWriterError("CouchDB action candidate GET returned non-object JSON")
    return parsed


def _put_document(
    config: couchdb_config.CouchDBConfig,
    database: str,
    doc_id: str,
    doc: dict[str, Any],
    *,
    conflict_as_already_decided: bool = False,
) -> dict[str, Any]:
    db = parse.quote(database, safe="")
    quoted_id = parse.quote(doc_id, safe="")
    url = f"{config.base_url}/{db}/{quoted_id}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(
        url,
        data=json.dumps(doc, sort_keys=True).encode("utf-8"),
        headers=headers,
        method="PUT",
    )
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        if exc.code == 409 and conflict_as_already_decided:
            raise AlreadyDecided(f"CouchDB action candidate conflict for {doc_id}") from exc
        raise CouchDBCandidateWriterError(
            f"CouchDB action candidate PUT failed with HTTP {exc.code} for {doc_id}"
        ) from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBCandidateWriterError(
            f"CouchDB action candidate PUT failed for {doc_id}: {exc}"
        ) from exc
    if not 200 <= status < 300:
        raise CouchDBCandidateWriterError(f"CouchDB action candidate PUT failed with HTTP {status} for {doc_id}")
    parsed = _parse_response_json(raw, "CouchDB action candidate PUT")
    rev = parsed.get("rev") if isinstance(parsed, dict) else None
    updated = dict(doc)
    if rev:
        updated["_rev"] = str(rev)
    return updated


def _request_json(
    config: couchdb_config.CouchDBConfig,
    db: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    db_quoted = parse.quote(db, safe="")
    quoted_path = "/".join(parse.quote(part, safe="") for part in path.split("/"))
    url = f"{config.base_url}/{db_quoted}/{quoted_path}"
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    headers.update(config.auth_header())
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status_code = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        raise CouchDBCandidateWriterError(
            f"CouchDB action candidate query failed with HTTP {exc.code}"
        ) from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBCandidateWriterError(f"CouchDB action candidate query failed: {exc}") from exc
    if not 200 <= status_code < 300:
        raise CouchDBCandidateWriterError(f"CouchDB action candidate query failed with HTTP {status_code}")
    parsed = _parse_response_json(raw, "CouchDB action candidate query")
    if not isinstance(parsed, dict):
        raise CouchDBCandidateWriterError("CouchDB action candidate query returned non-object JSON")
    return parsed


def _parse_response_json(raw: bytes, context: str) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CouchDBCandidateWriterError(f"{context} returned invalid JSON") from exc


def _proposed_queue_job_doc(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    job_type = str(value.get("job_type") or "").strip()
    payload = value.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return {"job_type": job_type, "payload": dict(payload)}


def _evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "source_text": str(candidate.get("source_text") or ""),
        "source_context": str(candidate.get("source_context") or ""),
        "rationale": str(candidate.get("rationale") or ""),
        "confidence": candidate.get("confidence"),
    }
    error_payload = candidate.get("error")
    if error_payload is not None:
        evidence["error"] = error_payload
    return evidence


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
