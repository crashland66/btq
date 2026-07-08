from __future__ import annotations

from pathlib import Path
from typing import Any

from btq_vault.contacts import remove_contact_by_id, upsert_contact_by_id
from queue_processor.canonical_rmw import (
    CanonicalEntityState,
    CanonicalMutation,
    CanonicalTarget,
    apply_canonical_rmw,
    resolve_site_target,
)

from . import _shared
from ._shared import QueueJob, RunContext


def _canonical_write_error(job: QueueJob, entity_id: str, exc: Exception) -> _shared.QueueJobError:
    return _shared.QueueJobError(
        "canonical couchdb write failed "
        f"job_type={job.job_type} job_id={job.job_id} entity_id={entity_id}: {exc}"
    )


def process_set_contact_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    action = str(payload["action"]).strip()
    actor = str(payload["actor"]).strip()
    target_payload = payload["target"]
    target_type = str(target_payload["type"]).strip()
    target_id = str(target_payload["id"]).strip()
    contact = payload["contact"]
    contact_id = str(contact["id"]).strip()
    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise _shared.QueueProcessorError(f"Destination already exists: {processed_destination}")

    store = _shared._vault_store()
    try:
        target = _resolve_contact_target(store, target_type, target_id)
    except Exception as exc:
        raise _canonical_write_error(job, target_id, exc) from exc

    collection = "account_contacts" if target_type == "account" else "site_contacts"
    evidence_text = f"contact {action} target={target.doc_id} actor={actor} contact_id={contact_id}"

    print(f"Job {job.job_id}: validated")
    print(f"Job {job.job_id}: target {target.doc_id}")
    if context.dry_run:
        print(f"Job {job.job_id}: would {action} contact")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action}-contact status=success error=")
        return

    def transform(state: CanonicalEntityState) -> CanonicalMutation:
        outgoing = dict(state.doc or {})
        if action == "remove":
            outgoing[collection] = remove_contact_by_id(outgoing, collection, contact_id)
        else:
            outgoing[collection] = upsert_contact_by_id(outgoing, collection, contact)
        return CanonicalMutation(doc=outgoing, evidence_text=evidence_text)

    try:
        canonical_doc = apply_canonical_rmw(store, target, job.job_id, transform)
    except Exception as exc:
        raise _canonical_write_error(job, target.doc_id, exc) from exc

    if canonical_doc is None:
        print(f"Job {job.job_id}: job_id marker already present - skipping")
        moved_path = _shared.move_job_file(job_path, processed_dir)
        print(f"Job {job.job_id}: moved queue file to {moved_path}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
        return

    _shared.write_mutation_evidence(context, job, canonical_doc, evidence_text)
    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: updated {target.doc_id}")
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action={action}-contact status=success error=")


def _resolve_contact_target(store: Any, target_type: str, target_id: str) -> CanonicalTarget:
    if target_type == "site":
        return resolve_site_target(target_id)
    if target_type == "account":
        return _resolve_account_target(store, target_id)
    raise _shared.QueueProcessorError(f"Unsupported contact target type: {target_type}")


def _resolve_account_target(store: Any, value: str) -> CanonicalTarget:
    normalized = value.strip()
    if not normalized:
        raise _shared.QueueProcessorError("Cannot resolve account target without value")
    if normalized.startswith("account_"):
        return CanonicalTarget(doc_id=normalized, doc_type="account")

    docs = store.find_account_docs()
    expected_doc_id = f"account_{_account_slug(normalized)}"
    for doc in docs:
        if str(doc.get("_id") or "").strip() == expected_doc_id:
            return _account_target_from_doc(doc)

    query_key = _account_lookup_key(normalized)
    matches = [doc for doc in docs if query_key in _account_lookup_keys(doc)]
    if len(matches) == 1:
        return _account_target_from_doc(matches[0])
    if len(matches) > 1:
        raise _shared.QueueProcessorError(f"Ambiguous canonical account target: {value}")
    raise _shared.QueueProcessorError(f"Could not resolve canonical account target: {value}")


def _account_target_from_doc(doc: dict[str, Any]) -> CanonicalTarget:
    doc_id = str(doc.get("_id") or "").strip()
    if not doc_id:
        raise _shared.QueueProcessorError("Resolved account document is missing _id")
    return CanonicalTarget(doc_id=doc_id, doc_type="account")


def _account_lookup_keys(doc: dict[str, Any]) -> set[str]:
    keys = {
        _account_lookup_key(str(doc.get("_id") or "").removeprefix("account_")),
        _account_lookup_key(str(doc.get("account") or "")),
        _account_lookup_key(str(doc.get("name") or "")),
        _account_lookup_key(str(doc.get("canonical") or "")),
    }
    for field in ("aliases", "account_aliases"):
        raw_aliases = doc.get(field)
        if isinstance(raw_aliases, list):
            keys.update(_account_lookup_key(str(alias)) for alias in raw_aliases)
    return {key for key in keys if key}


def _account_lookup_key(value: str) -> str:
    return " ".join(value.strip().lower().replace("_", " ").replace("-", " ").split())


def _account_slug(value: str) -> str:
    return "_".join("".join(ch if ch.isalnum() else " " for ch in value.lower()).split())
