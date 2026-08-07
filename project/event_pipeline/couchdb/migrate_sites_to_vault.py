from __future__ import annotations

"""One-time fold of btq_sites into btq_vault (site-identity unification).

Retires the split site store: registration data from the btq_sites ``site_*``
docs folds onto the canonical ``location_<site_id>`` docs in btq_vault, which
become the ONE store for site identity. Per site doc it writes:

  site_id, aliases, capture_active (from site.active), vision_context,
  note_path, and capture_guidance / display_categories when present.

The vault's display name (``location`` field) is canonical and is NEVER
overwritten — where the old registry canonical (``site.account``) drifted
from it, the old name is added to ``aliases`` instead so voice/text captures
that used the old name keep resolving. Drift is reported per site.

Also migrated: the ``system_defaults`` doc and any ``prospect_*`` docs living
in btq_sites (both copied into btq_vault if absent there).

Skipped: ``site_SANDBOX`` (the demo persona is code-canonical — a builtin row
in CouchDBSiteRegistry — and must not enter the vault), the bare
``synced_from_vault`` artifacts (stale one-way copies of vault data; they die
with the database), and the design doc.

Dry-run by default; ``--execute`` to apply. Run once against the node that
holds the canonical mesh (the Pro) — btq_vault replication distributes the
result. Idempotent. Archiving/deleting btq_sites itself is a separate step
(retire_sites_db.py), done post-service after the repointed readers deploy.
"""

import argparse
import json
import sys
from typing import Any
from urllib import error, parse, request

from btq_vault.entity_types import current_operator_id
from event_pipeline import couchdb_config
from event_pipeline.couchdb_registry import normalize_for_match


SITES_DB = "btq_sites"

# Registration fields the fold owns on the location doc: the site_* doc is the
# authority for these ONCE (they did not exist on location docs before).
FOLD_FIELDS = ("site_id", "aliases", "capture_active", "vision_context", "note_path")
OPTIONAL_FOLD_FIELDS = ("capture_guidance", "display_categories")


class MigrateSitesToVaultError(Exception):
    pass


def _request_json(base_url: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    cfg = couchdb_config.from_env()
    url = f"{base_url.rstrip('/')}/{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(cfg.auth_header())
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return None
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise MigrateSitesToVaultError(f"CouchDB {method} {path} failed with HTTP {exc.code}: {detail}") from exc


def _all_docs(base_url: str, database: str) -> list[dict[str, Any]]:
    payload = _request_json(base_url, "GET", f"{parse.quote(database, safe='')}/_all_docs?include_docs=true")
    if payload is None:
        raise MigrateSitesToVaultError(f"database {database} not found")
    return [row["doc"] for row in payload.get("rows", []) if isinstance(row.get("doc"), dict)]


def _put_doc(base_url: str, database: str, doc: dict[str, Any]) -> None:
    _request_json(base_url, "PUT", f"{parse.quote(database, safe='')}/{parse.quote(str(doc['_id']), safe='')}", doc)


def folded_location_doc(site_doc: dict[str, Any], location_doc: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Fold one site_* doc onto its location doc. Returns (doc, notes)."""
    site_id = str(site_doc.get("site_id") or str(site_doc.get("_id", "")).removeprefix("site_")).strip()
    notes: list[str] = []
    if location_doc is None:
        location_doc = {
            "_id": f"location_{site_id}",
            "type": "location",
            "operator": current_operator_id(),
            "status": "active",
            "location": str(site_doc.get("account") or site_doc.get("location") or site_id),
            "account": str(site_doc.get("account") or ""),
        }
        notes.append("created minimal location doc")
    merged = dict(location_doc)

    old_canonical = str(site_doc.get("canonical_name") or site_doc.get("account") or "").strip()
    vault_canonical = str(merged.get("location") or merged.get("name") or "").strip()
    aliases = [normalize_for_match(str(a)) for a in site_doc.get("aliases", []) if str(a).strip()]
    if old_canonical and vault_canonical and normalize_for_match(old_canonical) != normalize_for_match(vault_canonical):
        # Canonical drift: the vault name wins; the retired registry name
        # becomes an alias so existing capture habits keep resolving.
        aliases.append(normalize_for_match(old_canonical))
        notes.append(f"drift: registry '{old_canonical}' vs vault '{vault_canonical}' (old name aliased)")
    merged["site_id"] = site_id
    merged["aliases"] = sorted(set(aliases))
    merged["capture_active"] = bool(site_doc.get("active", False))
    merged["vision_context"] = site_doc.get("vision_context") or None
    merged["note_path"] = str(site_doc.get("note_path") or "") or None
    for key in OPTIONAL_FOLD_FIELDS:
        value = site_doc.get(key)
        if value not in (None, "", []):
            merged[key] = value
    return merged, notes


def migrate(base_url: str, *, execute: bool) -> None:
    mode = "EXECUTE" if execute else "DRY RUN"
    vault_db = couchdb_config.vault_database()
    sites_docs = _all_docs(base_url, SITES_DB)
    vault_docs = _all_docs(base_url, vault_db)
    locations = {str(d["_id"]): d for d in vault_docs if str(d.get("_id", "")).startswith("location_")}
    vault_ids = {str(d["_id"]) for d in vault_docs}

    site_docs = [d for d in sites_docs if str(d.get("_id", "")).startswith("site_")]
    prospect_docs = [d for d in sites_docs if str(d.get("_id", "")).startswith("prospect_")]
    system_defaults = next((d for d in sites_docs if d.get("_id") == "system_defaults"), None)
    artifacts = [d for d in sites_docs if d.get("synced_from_vault") is True]
    print(f"[{mode}] {SITES_DB}: {len(site_docs)} site docs, {len(prospect_docs)} prospects, "
          f"{len(artifacts)} stale synced_from_vault artifacts (left to die with the db)")

    changed = 0
    for site_doc in sorted(site_docs, key=lambda d: str(d["_id"])):
        site_id = str(site_doc.get("site_id") or str(site_doc["_id"]).removeprefix("site_")).strip()
        if site_id == "SANDBOX":
            print(f"[{mode}] skip site_SANDBOX (code-canonical demo persona; kept out of the vault)")
            continue
        existing = locations.get(f"location_{site_id}")
        merged, notes = folded_location_doc(site_doc, existing)
        already = existing is not None and all(existing.get(k) == merged.get(k) for k in (*FOLD_FIELDS, *OPTIONAL_FOLD_FIELDS) if k in merged)
        for note in notes:
            print(f"[{mode}] {site_id}: {note}")
        if already:
            print(f"[{mode}] {site_id}: already folded")
            continue
        print(f"[{mode}] {site_id}: fold registration -> location_{site_id} "
              f"(capture_active={merged['capture_active']}, {len(merged['aliases'])} aliases)")
        if execute:
            _put_doc(base_url, vault_db, merged)
        changed += 1

    for doc in prospect_docs:
        if str(doc["_id"]) in vault_ids:
            print(f"[{mode}] prospect {doc['_id']}: already in {vault_db}")
            continue
        copy = {k: v for k, v in doc.items() if k != "_rev"}
        copy.setdefault("operator", current_operator_id())
        print(f"[{mode}] prospect {doc['_id']}: copy -> {vault_db}")
        if execute:
            _put_doc(base_url, vault_db, copy)

    if system_defaults is not None:
        if "system_defaults" in vault_ids:
            print(f"[{mode}] system_defaults: already in {vault_db}")
        else:
            copy = {k: v for k, v in system_defaults.items() if k != "_rev"}
            print(f"[{mode}] system_defaults: copy -> {vault_db}")
            if execute:
                _put_doc(base_url, vault_db, copy)

    # Anomaly sweep: location docs whose bare id has no site_* counterpart are
    # strays (e.g. location_about duplicating a real site) — flagged, not touched.
    site_ids = {str(d.get("site_id") or str(d["_id"]).removeprefix("site_")) for d in site_docs}
    for loc_id in sorted(locations):
        bare = loc_id.removeprefix("location_")
        if bare not in site_ids:
            job = locations[loc_id].get("job")
            print(f"[{mode}] ANOMALY {loc_id}: no site_* counterpart (job={job!r}) — review at cutover")

    print(f"[{mode}] done: {changed} location docs folded")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=None, help="CouchDB base URL (default: instance config).")
    parser.add_argument("--execute", action="store_true", help="Apply changes (default is dry run).")
    args = parser.parse_args(argv)
    base_url = args.url or couchdb_config.base_url()
    try:
        migrate(base_url, execute=args.execute)
    except MigrateSitesToVaultError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
