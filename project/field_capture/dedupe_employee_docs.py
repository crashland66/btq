from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import re
from typing import Any

from token_store import TokenStore, parse_timestamp


logger = logging.getLogger(__name__)

PROTECTED_FIELDS = frozenset({"_id", "_rev", "type", "person_id", "vault_path", "content", "btq_job_ids"})


@dataclass
class DedupeItemReport:
    group_key: str
    status: str
    survivor: str | None = None
    merged_from: list[str] = field(default_factory=list)
    job_ids_before: dict[str, list[str]] = field(default_factory=dict)
    job_ids_after: list[str] = field(default_factory=list)
    content_actions: dict[str, str] = field(default_factory=dict)
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_key": self.group_key,
            "status": self.status,
            "survivor": self.survivor,
            "merged_from": self.merged_from,
            "job_ids_before": self.job_ids_before,
            "job_ids_after": self.job_ids_after,
            "content_actions": self.content_actions,
            "message": self.message,
        }


@dataclass
class DedupeReport:
    dry_run: bool
    employee_docs: int = 0
    candidate_groups: int = 0
    planned_merges: int = 0
    applied_merges: int = 0
    deleted_docs: int = 0
    skipped_groups: int = 0
    items: list[DedupeItemReport] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "employee_docs": self.employee_docs,
            "candidate_groups": self.candidate_groups,
            "planned_merges": self.planned_merges,
            "applied_merges": self.applied_merges,
            "deleted_docs": self.deleted_docs,
            "skipped_groups": self.skipped_groups,
            "items": [item.as_dict() for item in self.items],
            "errors": self.errors,
        }


def _is_active(record: Any, *, now: datetime) -> bool:
    if bool(getattr(record, "revoked", False)):
        return False
    expires_at = parse_timestamp(getattr(record, "expires_at", None))
    return expires_at is None or expires_at > now


def active_token_person_ids(token_store: TokenStore) -> set[str]:
    now = datetime.now(timezone.utc)
    person_ids: set[str] = set()
    for record in token_store.list_tokens():
        if not _is_active(record, now=now):
            continue
        person_id = str(record.person_id or "").strip()
        if person_id:
            person_ids.add(person_id)
    return person_ids


def _doc_id(doc: dict[str, Any]) -> str:
    return str(doc.get("_id") or "").strip()


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = [str(item).strip() for item in value]
    else:
        values = [str(value).strip()]
    return [value for value in values if value]


def _union_job_ids(docs: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for doc in docs:
        for job_id in _as_string_list(doc.get("btq_job_ids")):
            if job_id not in seen:
                merged.append(job_id)
                seen.add(job_id)
    return merged


def _missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _normalized_name(doc: dict[str, Any]) -> str:
    first = str(doc.get("first") or "").strip()
    last = str(doc.get("last") or "").strip()
    if not first or not last:
        name = str(doc.get("name") or doc.get("canonical") or doc.get("location") or "").strip()
        parts = name.split()
        if len(parts) >= 2:
            first = first or " ".join(parts[:-1])
            last = last or parts[-1]
    if not first or not last:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", f"{last}-{first}".lower()).strip("-")


def _group_key(doc: dict[str, Any]) -> str:
    vault_path = str(doc.get("vault_path") or "").strip()
    if vault_path:
        return f"vault_path:{vault_path}"
    normalized_name = _normalized_name(doc)
    return f"name:{normalized_name}" if normalized_name else ""


def _employee_docs(store: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    try:
        docs = [dict(doc) for doc in store.find_employee_docs()]
    except Exception as exc:
        logger.warning("skipped employee dedupe discovery: %s", exc)
        raise
    full_docs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for doc in docs:
        doc_id = _doc_id(doc)
        full_doc = None
        if doc_id:
            try:
                full_doc = store.get_optional(doc_id)
            except Exception as exc:
                message = str(exc)
                logger.warning(
                    "using employee dedupe summary doc after full-doc lookup failed for %s: %s",
                    doc_id,
                    message,
                )
                errors.append({"doc_id": doc_id, "message": message})
                full_doc = None
        full_docs.append(dict(full_doc) if isinstance(full_doc, dict) else doc)
    return full_docs, errors


def duplicate_groups(docs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        if doc.get("type") != "employee" or doc.get("_deleted"):
            continue
        key = _group_key(doc)
        if key:
            grouped.setdefault(key, []).append(doc)
    return {key: docs for key, docs in grouped.items() if len(docs) >= 2}


def _token_matched_doc_ids(active_person_ids: set[str]) -> set[str]:
    return {f"employee_{person_id}" for person_id in active_person_ids}


def _protected_doc_ids(docs: list[dict[str, Any]], active_person_ids: set[str]) -> set[str]:
    exact_ids = _token_matched_doc_ids(active_person_ids)
    protected = set(exact_ids)
    for doc in docs:
        person_id = str(doc.get("person_id") or "").strip()
        if person_id in active_person_ids:
            protected.add(_doc_id(doc))
    return {doc_id for doc_id in protected if doc_id}


def _merge_content(current: str, duplicate: str, duplicate_id: str) -> tuple[str, str]:
    current = current.strip()
    duplicate = duplicate.strip()
    if not duplicate:
        return current, "unchanged_duplicate_empty"
    if not current:
        return duplicate, "filled_from_duplicate"
    if current == duplicate:
        return current, "unchanged_identical"
    if duplicate in current:
        return current, "unchanged_duplicate_contained"
    if current in duplicate:
        return duplicate, "replaced_with_longer_duplicate"

    if len(duplicate) > len(current):
        longer = duplicate
        append_text = current
        action = "kept_longer_duplicate_and_appended_prior_survivor"
    else:
        longer = current
        append_text = duplicate
        action = "kept_survivor_and_appended_duplicate"
    marker = f"## Merged from duplicate {duplicate_id}"
    return f"{longer.rstrip()}\n\n{marker}\n\n{append_text.strip()}", action


def merge_employee_docs(
    survivor: dict[str, Any],
    duplicates: list[dict[str, Any]],
    *,
    survivor_person_id: str,
) -> tuple[dict[str, Any], DedupeItemReport]:
    original_survivor = dict(survivor)
    merged = dict(survivor)
    merged["person_id"] = survivor_person_id
    merged["btq_job_ids"] = _union_job_ids([survivor, *duplicates])

    content = str(merged.get("content") or "").strip()
    content_actions: dict[str, str] = {}
    for duplicate in duplicates:
        duplicate_id = _doc_id(duplicate)
        for key, value in duplicate.items():
            if key in PROTECTED_FIELDS:
                continue
            if _missing(merged.get(key)) and not _missing(value):
                merged[key] = value
        content, action = _merge_content(content, str(duplicate.get("content") or ""), duplicate_id)
        content_actions[duplicate_id] = action

    merged["content"] = content
    merged["_id"] = original_survivor["_id"]
    if original_survivor.get("_rev"):
        merged["_rev"] = original_survivor["_rev"]
    merged["person_id"] = survivor_person_id
    merged["vault_path"] = original_survivor.get("vault_path")
    merged["type"] = "employee"

    item = DedupeItemReport(
        group_key="",
        status="planned",
        survivor=_doc_id(survivor),
        merged_from=[_doc_id(doc) for doc in duplicates],
        job_ids_before={_doc_id(doc): _as_string_list(doc.get("btq_job_ids")) for doc in [survivor, *duplicates]},
        job_ids_after=list(merged["btq_job_ids"]),
        content_actions=content_actions,
    )
    return merged, item


def dedupe_employee_docs(store: Any, token_store: TokenStore, *, dry_run: bool = True) -> DedupeReport:
    active_person_ids = active_token_person_ids(token_store)
    token_doc_ids = _token_matched_doc_ids(active_person_ids)
    report = DedupeReport(dry_run=dry_run)
    try:
        docs, discovery_errors = _employee_docs(store)
    except Exception as exc:
        report.errors.append({"group_key": "find_employee_docs", "message": str(exc)})
        return report
    for error in discovery_errors:
        report.errors.append(
            {
                "group_key": str(error.get("doc_id") or "get_employee_doc"),
                "message": str(error.get("message") or ""),
            }
        )
    groups = duplicate_groups(docs)
    report.employee_docs = len(docs)
    report.candidate_groups = len(groups)

    for group_key, group_docs in sorted(groups.items()):
        token_matches = [doc for doc in group_docs if _doc_id(doc) in token_doc_ids]
        if len(token_matches) != 1:
            message = f"expected exactly one active-token survivor, found {len(token_matches)}"
            report.skipped_groups += 1
            report.errors.append({"group_key": group_key, "message": message})
            report.items.append(DedupeItemReport(group_key=group_key, status="skipped", message=message))
            continue

        survivor = token_matches[0]
        survivor_id = _doc_id(survivor)
        survivor_person_id = survivor_id.removeprefix("employee_")
        duplicates = sorted((doc for doc in group_docs if _doc_id(doc) != survivor_id), key=_doc_id)
        protected_ids = _protected_doc_ids(group_docs, active_person_ids)
        protected_duplicates = sorted(_doc_id(doc) for doc in duplicates if _doc_id(doc) in protected_ids)
        if protected_duplicates:
            message = f"refusing to delete protected active-token/auth-resolving docs: {', '.join(protected_duplicates)}"
            report.skipped_groups += 1
            report.errors.append({"group_key": group_key, "message": message})
            report.items.append(
                DedupeItemReport(group_key=group_key, status="skipped", survivor=survivor_id, message=message)
            )
            continue
        if not duplicates:
            continue
        if not dry_run:
            missing_rev_ids = [_doc_id(doc) for doc in [survivor, *duplicates] if not doc.get("_rev")]
            if missing_rev_ids:
                message = f"missing _rev for live dedupe: {', '.join(missing_rev_ids)}"
                report.skipped_groups += 1
                report.errors.append({"group_key": group_key, "message": message})
                report.items.append(
                    DedupeItemReport(group_key=group_key, status="skipped", survivor=survivor_id, message=message)
                )
                continue

        merged, item = merge_employee_docs(survivor, duplicates, survivor_person_id=survivor_person_id)
        item.group_key = group_key
        report.planned_merges += 1
        if dry_run:
            report.items.append(item)
            continue

        try:
            expected_rev = survivor.get("_rev")
            store.put_with_rev(merged, expected_rev=expected_rev)
            for duplicate in duplicates:
                duplicate_id = _doc_id(duplicate)
                duplicate_rev = duplicate.get("_rev")
                store.put_with_rev({"_id": duplicate_id, "_deleted": True}, expected_rev=str(duplicate_rev))
                report.deleted_docs += 1
            report.applied_merges += 1
            item.status = "applied"
            report.items.append(item)
        except Exception as exc:
            message = str(exc)
            logger.warning("failed employee dedupe merge for %s: %s", group_key, message)
            report.errors.append({"group_key": group_key, "message": message})
            item.status = "failed"
            item.message = message
            report.items.append(item)

    return report
