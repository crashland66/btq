from __future__ import annotations

"""One-shot migration: unify employee person_ids to ``lastname_firstname``.

Part of the btq_people retirement. The vault accumulated three id shapes:
dash-form ids in mixed name order (``brandon-shifflett``, ``schreiber-james``),
conforming ``lastname_firstname`` ids with no ``person_id`` field, and a
handful of fully conforming docs. Field-capture tokens and capture docs store
whichever shape the token was minted with, so the rename has to walk every
referencing database, not just the vault.

What it does, in order (dry-run by default; ``--execute`` to apply):

  1. Plan: derive the target ``lastname_firstname`` id for every vault
     employee doc via the same ``derive_person_id_base`` the queue processor
     mints new ids with, so the two can never drift.
  2. Rename: write the new ``employee_<new_id>`` doc (old ids preserved in
     ``aliases`` so field-capture auth keeps resolving not-yet-rewritten
     tokens), rewrite references, then delete the old doc.
  3. Backfill: conforming docs that merely lack ``person_id`` get it set.
  4. Rewrite: every doc in the referencing databases is deep-walked and any
     string equal to an old id (or ``employee_<old id>``) is replaced.
     Only exact full-string matches are rewritten — prose fields that
     merely mention an old slug are left alone.

``--print-token-sql`` emits the SQLite UPDATE statements for the
field-capture token stores (Pro and the VPS edge replica), which live outside
CouchDB and are rewritten by hand on each host.

``employee_sandbox-user`` is exempt: the sandbox persona's id is a fixture
constant across the test suite and is not a real person.

The queue database is deliberately untouched: old slugs appear there only
inside immutable job ids and idempotency keys, and rewriting those would
break the dedup ledger.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib import error, parse, request

from event_pipeline import couchdb_config
from queue_processor.handlers.people import derive_person_id_base


EXCLUDED_DOC_IDS = frozenset({"employee_sandbox-user"})

# Legacy opaque ids from the pre-slug eras, mapped by hand: the ULID-token
# admin tokens ("Greg iPhone", "Master Token", "Admin") and the tapedeck
# voice-memo submitter are both Greg.
EXTRA_LEGACY_PERSON_IDS: dict[str, str] = {
    "per_01KQK6NB7SM3K95NH3AW22Y12E": "stoltz_gregory",
    "prs_01KSGY3B8A0CZT6ZB05VQ9HP32": "stoltz_gregory",
}

# Databases whose docs may reference a person_id. btq_sites and
# btq_photo_vision were audited and hold no person references; btq_queue is
# excluded on purpose (see module docstring).
REWRITE_DATABASES: tuple[str, ...] = ("btq_vault", "btq_field_captures", "btq_voice_memos")


class PersonIdMigrationError(Exception):
    pass


@dataclass(frozen=True)
class EmployeeRename:
    old_doc_id: str
    new_doc_id: str
    old_person_id: str
    new_person_id: str


@dataclass
class MigrationPlan:
    renames: list[EmployeeRename] = field(default_factory=list)
    backfills: list[tuple[str, str]] = field(default_factory=list)  # (doc_id, person_id)
    unchanged: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)

    def person_id_mapping(self) -> dict[str, str]:
        """Old string -> new string, covering bare ids and employee_-prefixed ids."""
        mapping: dict[str, str] = {}
        for rename in self.renames:
            mapping[rename.old_person_id] = rename.new_person_id
            mapping[rename.old_doc_id] = rename.new_doc_id
        for old, new in EXTRA_LEGACY_PERSON_IDS.items():
            mapping[old] = new
        return mapping


def target_person_id(doc: dict[str, Any]) -> str | None:
    """The unified id this employee doc should carry, or None if underivable."""
    return derive_person_id_base({
        "first": doc.get("first"),
        "last": doc.get("last"),
        "name": doc.get("name"),
    })


def plan_employee_migration(docs: list[dict[str, Any]]) -> MigrationPlan:
    plan = MigrationPlan()
    docs = [doc for doc in docs if isinstance(doc, dict) and doc.get("type") == "employee"]
    existing_ids = {str(doc.get("_id") or "") for doc in docs}
    claimed_new_ids: dict[str, str] = {}
    for doc in sorted(docs, key=lambda item: str(item.get("_id") or "")):
        doc_id = str(doc.get("_id") or "").strip()
        if doc_id in EXCLUDED_DOC_IDS:
            plan.excluded.append(doc_id)
            continue
        target = target_person_id(doc)
        if not target:
            raise PersonIdMigrationError(f"Cannot derive lastname_firstname id for {doc_id}")
        new_doc_id = f"employee_{target}"
        current_person_id = str(doc.get("person_id") or "").strip()
        if doc_id == new_doc_id and current_person_id == target:
            plan.unchanged.append(doc_id)
            continue
        if new_doc_id != doc_id and (new_doc_id in existing_ids or new_doc_id in claimed_new_ids):
            other = claimed_new_ids.get(new_doc_id, new_doc_id)
            raise PersonIdMigrationError(
                f"Target id collision: {doc_id} -> {new_doc_id} (already used by {other})"
            )
        if doc_id == new_doc_id:
            plan.backfills.append((doc_id, target))
        else:
            claimed_new_ids[new_doc_id] = doc_id
            plan.renames.append(
                EmployeeRename(
                    old_doc_id=doc_id,
                    new_doc_id=new_doc_id,
                    old_person_id=current_person_id or doc_id.removeprefix("employee_"),
                    new_person_id=target,
                )
            )
    return plan


def renamed_employee_doc(doc: dict[str, Any], rename: EmployeeRename) -> dict[str, Any]:
    """The replacement doc: new id, old ids kept as aliases for auth fallback."""
    new_doc = {key: value for key, value in doc.items() if not key.startswith("_")}
    new_doc["_id"] = rename.new_doc_id
    new_doc["person_id"] = rename.new_person_id
    aliases = doc.get("aliases")
    alias_list = [str(item).strip() for item in aliases if str(item).strip()] if isinstance(aliases, list) else []
    for legacy in (rename.old_person_id, rename.old_doc_id.removeprefix("employee_")):
        if legacy and legacy not in alias_list and legacy != rename.new_person_id:
            alias_list.append(legacy)
    new_doc["aliases"] = alias_list
    return new_doc


def rewrite_value(value: Any, mapping: dict[str, str]) -> tuple[Any, int]:
    """Exact full-string replacement, recursing through dicts and lists."""
    if isinstance(value, str):
        replacement = mapping.get(value)
        return (replacement, 1) if replacement is not None else (value, 0)
    if isinstance(value, list):
        changed = 0
        rewritten = []
        for item in value:
            new_item, hits = rewrite_value(item, mapping)
            rewritten.append(new_item)
            changed += hits
        return (rewritten if changed else value), changed
    if isinstance(value, dict):
        changed = 0
        rewritten_dict = {}
        for key, item in value.items():
            new_item, hits = rewrite_value(item, mapping)
            rewritten_dict[key] = new_item
            changed += hits
        return (rewritten_dict if changed else value), changed
    return value, 0


def rewrite_doc(doc: dict[str, Any], mapping: dict[str, str]) -> tuple[dict[str, Any], int]:
    """Rewrite person references in a doc body. ``_id``/``_rev`` are never touched."""
    changed = 0
    rewritten = {}
    for key, value in doc.items():
        if key.startswith("_"):
            rewritten[key] = value
            continue
        new_value, hits = rewrite_value(value, mapping)
        rewritten[key] = new_value
        changed += hits
    return rewritten, changed


def person_name_mapping(employee_docs: list[dict[str, Any]]) -> dict[str, str]:
    """Display-name forms -> unified person_id, for the person_id-field-only pass.

    Some availability_constraint docs carry raw names ("West, Tracy") in their
    ``person_id`` field. Those are fixed by exact match against the roster's
    name forms — but only ever inside a ``person_id`` field, because the same
    strings legitimately appear in display-name fields elsewhere.
    """
    mapping: dict[str, str] = {}
    for doc in employee_docs:
        if not isinstance(doc, dict) or doc.get("type") != "employee":
            continue
        if str(doc.get("_id") or "") in EXCLUDED_DOC_IDS:
            continue
        target = target_person_id(doc)
        if not target:
            continue
        first = str(doc.get("first") or "").strip()
        last = str(doc.get("last") or "").strip()
        name = str(doc.get("name") or "").strip()
        for form in (f"{last}, {first}" if first and last else "", f"{first} {last}".strip(), name):
            if form and form not in ("", ","):
                mapping.setdefault(form, target)
    return mapping


def fix_name_form_person_id(doc: dict[str, Any], name_mapping: dict[str, str]) -> tuple[dict[str, Any], int]:
    """Replace a display-name person_id with the unified id. person_id field only."""
    value = doc.get("person_id")
    if isinstance(value, str) and value in name_mapping:
        fixed = dict(doc)
        fixed["person_id"] = name_mapping[value]
        return fixed, 1
    return doc, 0


def token_store_sql(mapping: dict[str, str]) -> str:
    """UPDATE statements for the SQLite field-capture token stores."""
    statements = ["BEGIN;"]
    for old, new in sorted(mapping.items()):
        if old.startswith("employee_"):
            continue
        old_quoted = old.replace("'", "''")
        new_quoted = new.replace("'", "''")
        statements.append(
            f"UPDATE field_capture_tokens SET person_id = '{new_quoted}' WHERE person_id = '{old_quoted}';"
        )
    statements.append("COMMIT;")
    return "\n".join(statements)


# --------------------------------------------------------------------------
# CouchDB plumbing (same urllib idiom as the sibling migration scripts).


def _request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = couchdb_config.from_env()
    url = f"{cfg.base_url.rstrip('/')}/{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(cfg.auth_header())
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=max(cfg.timeout, 60.0)) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise PersonIdMigrationError(f"CouchDB {method} {path} failed with HTTP {exc.code}: {detail}") from exc


def _all_docs(database: str) -> list[dict[str, Any]]:
    response = _request_json("GET", f"{parse.quote(database, safe='')}/_all_docs?include_docs=true")
    docs = []
    for row in response.get("rows", []):
        doc = row.get("doc")
        if isinstance(doc, dict) and not str(doc.get("_id", "")).startswith("_design"):
            docs.append(doc)
    return docs


def _put_doc(database: str, doc: dict[str, Any]) -> None:
    _request_json("PUT", f"{parse.quote(database, safe='')}/{parse.quote(doc['_id'], safe='')}", doc)


def _delete_doc(database: str, doc_id: str, rev: str) -> None:
    _request_json(
        "DELETE",
        f"{parse.quote(database, safe='')}/{parse.quote(doc_id, safe='')}?rev={parse.quote(rev, safe='')}",
    )


def run_migration(*, execute: bool, log: Callable[[str], None] = print) -> MigrationPlan:
    vault_db = couchdb_config.vault_database()
    employee_docs = [doc for doc in _all_docs(vault_db) if doc.get("type") == "employee"]
    plan = plan_employee_migration(employee_docs)
    mapping = plan.person_id_mapping()
    mode = "EXECUTE" if execute else "DRY RUN"

    log(f"[{mode}] {len(employee_docs)} employee docs: "
        f"{len(plan.renames)} renames, {len(plan.backfills)} person_id backfills, "
        f"{len(plan.unchanged)} already conforming, {len(plan.excluded)} excluded")
    for rename in plan.renames:
        log(f"  rename {rename.old_doc_id} -> {rename.new_doc_id}")
    for doc_id, person_id in plan.backfills:
        log(f"  backfill {doc_id} person_id={person_id}")

    docs_by_id = {str(doc["_id"]): doc for doc in employee_docs}

    # 1. Create the renamed employee docs first so every id resolves
    #    throughout the run.
    for rename in plan.renames:
        new_doc = renamed_employee_doc(docs_by_id[rename.old_doc_id], rename)
        if execute:
            _put_doc(vault_db, new_doc)
    log(f"[{mode}] wrote {len(plan.renames)} renamed employee docs")

    # 2. Rewrite references everywhere (the old employee docs rewrite too,
    #    but they are deleted in step 4). Vault docs additionally get the
    #    person_id-field-only fix for display-name values.
    name_mapping = person_name_mapping(employee_docs)
    skip_ids = {rename.old_doc_id for rename in plan.renames} | {rename.new_doc_id for rename in plan.renames}
    for database in REWRITE_DATABASES:
        touched = 0
        replacements = 0
        for doc in _all_docs(database):
            if database == vault_db and str(doc.get("_id")) in skip_ids:
                continue
            rewritten, hits = rewrite_doc(doc, mapping)
            if database == vault_db and rewritten.get("type") != "employee":
                rewritten, name_hits = fix_name_form_person_id(rewritten, name_mapping)
                hits += name_hits
            if hits:
                touched += 1
                replacements += hits
                if execute:
                    _put_doc(database, rewritten)
        log(f"[{mode}] {database}: {touched} docs rewritten ({replacements} replacements)")

    # 3. Backfill person_id on conforming docs that lack it.
    for doc_id, person_id in plan.backfills:
        doc = dict(docs_by_id[doc_id])
        doc["person_id"] = person_id
        if execute:
            _put_doc(vault_db, doc)
    log(f"[{mode}] backfilled person_id on {len(plan.backfills)} docs")

    # 4. Delete the old employee docs last so nothing dangles mid-run.
    for rename in plan.renames:
        old = docs_by_id[rename.old_doc_id]
        if execute:
            _delete_doc(vault_db, rename.old_doc_id, str(old["_rev"]))
    log(f"[{mode}] deleted {len(plan.renames)} old employee docs")

    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="Apply changes (default is dry run).")
    parser.add_argument(
        "--print-token-sql",
        action="store_true",
        help="Print SQLite UPDATE statements for the field-capture token stores and exit.",
    )
    args = parser.parse_args(argv)

    if args.print_token_sql:
        vault_db = couchdb_config.vault_database()
        employee_docs = [doc for doc in _all_docs(vault_db) if doc.get("type") == "employee"]
        plan = plan_employee_migration(employee_docs)
        print(token_store_sql(plan.person_id_mapping()))
        return 0

    try:
        run_migration(execute=args.execute)
    except PersonIdMigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
