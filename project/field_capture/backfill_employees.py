from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

from btq_vault.entity_types import current_operator_id
from event_pipeline.couchdb.migrate_vault import _employee_display_name, _employee_site_ids, _stable_id
from field_capture.person_slugs import employee_slug_candidates, last_first_person_slug, person_slug
from token_store import TokenRecord, TokenStore, normalize_site_ids
from vault_markdown import frontmatter_str, read_markdown_note


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActiveTokenPerson:
    person_id: str
    labels: tuple[str, ...] = ()
    site_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PeopleMatch:
    person_id: str
    path: Path
    relative_path: str
    frontmatter: dict[str, Any]
    body: str


@dataclass(frozen=True)
class PeopleMatchIndex:
    matches: dict[str, PeopleMatch]
    ambiguous: dict[str, list[PeopleMatch]]


@dataclass
class BackfillReport:
    active_tokens: int = 0
    distinct_people: int = 0
    created: int = 0
    would_create: int = 0
    skipped_existing: int = 0
    missing_people: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    unkeyable_people: list[dict[str, str]] = field(default_factory=list)
    created_docs: list[dict[str, Any]] = field(default_factory=list)
    would_create_docs: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_tokens": self.active_tokens,
            "distinct_people": self.distinct_people,
            "created": self.created,
            "would_create": self.would_create,
            "skipped_existing": self.skipped_existing,
            "missing_people": self.missing_people,
            "ambiguous": self.ambiguous,
            "unkeyable_people": self.unkeyable_people,
            "created_docs": self.created_docs,
            "would_create_docs": self.would_create_docs,
            "errors": self.errors,
        }


def active_token_people(token_store: TokenStore) -> tuple[int, list[ActiveTokenPerson]]:
    records = [record for record in token_store.list_tokens() if not record.revoked]
    by_person: dict[str, dict[str, Any]] = {}
    for record in records:
        person_id = str(record.person_id).strip()
        if not person_id:
            continue
        entry = by_person.setdefault(person_id, {"labels": [], "site_ids": []})
        if record.label and record.label not in entry["labels"]:
            entry["labels"].append(record.label)
        for site_id in record.site_ids:
            if site_id not in entry["site_ids"]:
                entry["site_ids"].append(site_id)
    people = [
        ActiveTokenPerson(
            person_id=person_id,
            labels=tuple(entry["labels"]),
            site_ids=normalize_site_ids(entry["site_ids"]),
        )
        for person_id, entry in sorted(by_person.items())
    ]
    return len(records), people


def _normalize_quoted_empty_string(value: Any) -> Any:
    if isinstance(value, str) and value.strip() in {'""', "''"}:
        return ""
    return value


def _normalized_frontmatter(frontmatter: dict[str, Any]) -> dict[str, Any]:
    return {key: _normalize_quoted_empty_string(value) for key, value in frontmatter.items()}


def _candidate_slugs(path: Path, frontmatter: dict[str, Any]) -> set[str]:
    return employee_slug_candidates(
        first=frontmatter_str(frontmatter, "first"),
        last=frontmatter_str(frontmatter, "last"),
        filename_stem=path.stem,
    )


def _is_person_people_file(frontmatter: dict[str, Any]) -> bool:
    note_type = (frontmatter_str(frontmatter, "type") or "").strip().lower()
    if note_type in {"dashboard", "index", "non-person", "non_person"}:
        return False
    return bool(frontmatter_str(frontmatter, "first") or frontmatter_str(frontmatter, "last"))


def people_matches(vault_root: Path) -> PeopleMatchIndex:
    people_dir = vault_root / "People"
    if not people_dir.exists():
        return PeopleMatchIndex(matches={}, ambiguous={})

    resolved_root = vault_root.resolve(strict=False)
    matches_by_slug: dict[str, dict[str, PeopleMatch]] = {}
    for path in sorted(people_dir.glob("*.md")):
        if not path.is_file():
            continue
        frontmatter, body, has_frontmatter = read_markdown_note(path)
        if not has_frontmatter:
            continue
        if not _is_person_people_file(frontmatter):
            continue
        relative_path = path.resolve(strict=False).relative_to(resolved_root).as_posix()
        for slug in _candidate_slugs(path, frontmatter):
            slug_matches = matches_by_slug.setdefault(slug, {})
            slug_matches.setdefault(
                relative_path,
                PeopleMatch(
                    person_id=slug,
                    path=path,
                    relative_path=relative_path,
                    frontmatter=frontmatter,
                    body=body,
                ),
            )

    matches: dict[str, PeopleMatch] = {}
    ambiguous: dict[str, list[PeopleMatch]] = {}
    for slug, slug_matches in matches_by_slug.items():
        ordered_matches = [slug_matches[path] for path in sorted(slug_matches)]
        if len(ordered_matches) == 1:
            matches[slug] = ordered_matches[0]
        else:
            ambiguous[slug] = ordered_matches
    return PeopleMatchIndex(matches=matches, ambiguous=ambiguous)


def employee_doc_for_token_person(person: ActiveTokenPerson, match: PeopleMatch) -> dict[str, Any]:
    frontmatter = _normalized_frontmatter(match.frontmatter)
    frontmatter["person_id"] = person.person_id
    doc: dict[str, Any] = {
        "_id": _stable_id("employee", frontmatter, match.relative_path),
        "type": "employee",
        "operator": current_operator_id(),
        "content": match.body.strip(),
    }
    for key, value in match.frontmatter.items():
        if key == "type":
            continue
        doc[key] = value
    doc["person_id"] = person.person_id
    doc["site_ids"] = _employee_site_ids(frontmatter) or list(person.site_ids)
    doc["name"] = _employee_display_name(frontmatter) or match.path.stem
    return doc


def _person_name_parts(path: Path, frontmatter: dict[str, Any]) -> tuple[str, str]:
    first = frontmatter_str(frontmatter, "first") or ""
    last = frontmatter_str(frontmatter, "last") or ""
    if first and last:
        return first, last

    stem = path.stem.strip()
    if "," in stem:
        filename_last, filename_first = stem.split(",", 1)
        first = first or filename_first.strip()
        last = last or filename_last.strip()
    else:
        parts = stem.split()
        if len(parts) >= 2:
            first = first or " ".join(parts[:-1]).strip()
            last = last or parts[-1].strip()
    return first, last


def _person_id_for_people_file(path: Path, frontmatter: dict[str, Any]) -> str:
    explicit = frontmatter_str(frontmatter, "person_id")
    if explicit:
        return explicit
    first, last = _person_name_parts(path, frontmatter)
    if first and last:
        return last_first_person_slug(first=first, last=last)
    stem_slug = person_slug(path.stem)
    return "" if stem_slug == "unknown" else stem_slug


def _employee_doc_id_variants(person_id: str) -> list[str]:
    suffix = str(person_id).strip().removeprefix("employee_")
    if not suffix:
        return []
    variants = [f"employee_{suffix}"]
    for alternate in (suffix.replace("_", "-"), suffix.replace("-", "_")):
        doc_id = f"employee_{alternate}"
        if doc_id not in variants:
            variants.append(doc_id)
    return variants


def employee_doc_for_people_file(match: PeopleMatch) -> dict[str, Any] | None:
    frontmatter = _normalized_frontmatter(match.frontmatter)
    person_id = _person_id_for_people_file(match.path, frontmatter)
    if not person_id:
        return None

    first, last = _person_name_parts(match.path, frontmatter)
    if first and not frontmatter_str(frontmatter, "first"):
        frontmatter["first"] = first
    if last and not frontmatter_str(frontmatter, "last"):
        frontmatter["last"] = last
    frontmatter["person_id"] = person_id

    doc: dict[str, Any] = {
        "_id": f"employee_{person_id}",
        "type": "employee",
        "operator": current_operator_id(),
        "person_id": person_id,
        "content": match.body.strip(),
        "site_ids": _employee_site_ids(frontmatter),
        "name": _employee_display_name(frontmatter) or match.path.stem,
    }
    for key, value in frontmatter.items():
        if key == "type":
            continue
        doc[key] = value
    doc.setdefault("preferred_name", frontmatter_str(frontmatter, "preferred_name") or "")
    doc.setdefault("first", first)
    doc.setdefault("last", last)
    return doc


def _employee_doc_exists(store: Any, doc: dict[str, Any], existing_docs: list[dict[str, Any]]) -> bool:
    for doc_id in _employee_doc_id_variants(str(doc.get("person_id") or "")):
        if store.get_optional(doc_id) is not None:
            return True

    person_id = str(doc.get("person_id") or "").strip()
    for existing in existing_docs:
        if person_id and str(existing.get("person_id") or "").strip() == person_id:
            return True
    return False


def _existing_employee_docs(store: Any) -> list[dict[str, Any]]:
    finder = getattr(store, "find_employee_docs", None)
    if finder is None:
        return []
    try:
        return [dict(doc) for doc in finder()]
    except Exception as exc:
        logger.warning("skipped existing employee docs scan: %s", exc)
        raise


def backfill_all_people_employees(
    store: Any,
    vault_root: Path,
    *,
    dry_run: bool,
) -> BackfillReport:
    people_dir = vault_root / "People"
    paths = sorted(people_dir.glob("*.md")) if people_dir.exists() else []
    report = BackfillReport()
    resolved_root = vault_root.resolve(strict=False)
    try:
        existing_docs = _existing_employee_docs(store)
    except Exception as exc:
        report.errors.append({"id": "find_employee_docs", "message": str(exc)})
        return report

    for path in paths:
        if not path.is_file():
            continue
        frontmatter, body, has_frontmatter = read_markdown_note(path)
        relative_path = path.resolve(strict=False).relative_to(resolved_root).as_posix()
        if not has_frontmatter:
            continue
        if not _is_person_people_file(frontmatter):
            continue
        report.distinct_people += 1

        match = PeopleMatch(
            person_id="",
            path=path,
            relative_path=relative_path,
            frontmatter=frontmatter,
            body=body,
        )
        doc = employee_doc_for_people_file(match)
        if doc is None:
            report.unkeyable_people.append({"path": relative_path, "message": "could not derive person_id"})
            continue

        try:
            if _employee_doc_exists(store, doc, existing_docs):
                report.skipped_existing += 1
                continue
        except Exception as exc:
            logger.warning("skipped employee backfill candidate %s: %s", doc.get("_id") or relative_path, exc)
            report.errors.append({"id": str(doc.get("_id") or relative_path), "message": str(exc)})
            continue

        summary = {
            "_id": doc["_id"],
            "person_id": doc["person_id"],
            "name": doc["name"],
            "site_ids": doc["site_ids"],
        }
        if dry_run:
            report.would_create += 1
            report.would_create_docs.append(summary)
            continue

        try:
            store.put_with_rev(doc, expected_rev=None)
            report.created += 1
            report.created_docs.append(summary)
            existing_docs.append(dict(doc))
        except Exception as exc:
            logger.warning("skipped employee backfill write %s: %s", doc["_id"], exc)
            report.errors.append({"id": str(doc["_id"]), "message": str(exc)})

    return report


def backfill_field_capture_employees(
    store: Any,
    token_store: TokenStore,
    vault_root: Path,
    *,
    dry_run: bool,
) -> BackfillReport:
    active_count, people = active_token_people(token_store)
    match_index = people_matches(vault_root)
    report = BackfillReport(active_tokens=active_count, distinct_people=len(people))

    for person in people:
        doc_id = f"employee_{person.person_id}"
        try:
            if store.get_optional(doc_id) is not None:
                report.skipped_existing += 1
                continue
        except Exception as exc:
            logger.warning("skipped field-capture employee backfill candidate %s: %s", doc_id, exc)
            report.errors.append({"id": doc_id, "message": str(exc)})
            continue

        slug = person_slug(person.person_id)
        ambiguous_matches = match_index.ambiguous.get(slug)
        if ambiguous_matches is not None:
            paths = [match.relative_path for match in ambiguous_matches]
            report.ambiguous.append(
                {
                    "person_id": person.person_id,
                    "labels": list(person.labels),
                    "site_ids": list(person.site_ids),
                    "matches": paths,
                }
            )
            report.errors.append(
                {
                    "id": doc_id,
                    "message": f"ambiguous People match for {slug}: {', '.join(paths)}",
                }
            )
            continue

        match = match_index.matches.get(slug)
        if match is None:
            report.missing_people.append(
                {
                    "person_id": person.person_id,
                    "labels": list(person.labels),
                    "site_ids": list(person.site_ids),
                }
            )
            continue

        doc = employee_doc_for_token_person(person, match)
        if doc["_id"] != doc_id:
            report.errors.append({"id": doc_id, "message": f"derived unexpected _id {doc['_id']}"})
            continue
        summary = {
            "_id": doc["_id"],
            "person_id": doc["person_id"],
            "name": doc["name"],
            "site_ids": doc["site_ids"],
        }
        if dry_run:
            report.would_create += 1
            report.would_create_docs.append(summary)
            continue

        try:
            store.put_with_rev(doc, expected_rev=None)
            report.created += 1
            report.created_docs.append(summary)
        except Exception as exc:
            logger.warning("skipped field-capture employee backfill write %s: %s", doc_id, exc)
            report.errors.append({"id": doc_id, "message": str(exc)})

    return report
