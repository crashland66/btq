from __future__ import annotations

"""Archive the retired btq_people database on one CouchDB node.

Run once per peer that still carries the mirror (Pro and VPS; the Dell too
if it re-joins the mesh with a btq_people copy). Per node it:

  1. Deletes every ``_replicator`` doc that references btq_people — both the
     legacy ``pro_to_vps_btq_people`` naming and the mesh
     ``btq_people_pro_to_vps`` naming, plus any opaque-id doc whose
     source/target URL points at the database.
  2. Archives the database with a local one-shot replication to
     ``btq_people_bak_<date>`` (rename-by-copy; CouchDB has no rename).
  3. Verifies the archive's doc count matches, then deletes btq_people.

Dry-run by default; ``--execute`` to apply. Point ``--url`` at the peer
(default is this node's instance config), e.g. from the Pro:

    python3 -m event_pipeline.couchdb.retire_people_db --execute
    python3 -m event_pipeline.couchdb.retire_people_db \
        --url http://<vps-tailnet-ip>:5984 --execute

Prerequisites: the person_id unification (migrate_person_ids) has run and
the repointed readers are deployed — nothing may read btq_people anymore.
"""

import argparse
import json
import re
import sys
from datetime import date
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config


PEOPLE_DB = "btq_people"


class RetirePeopleDBError(Exception):
    pass


def _endpoint_url(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("url") or "")
    return str(value or "")


def references_people_db(doc: dict[str, Any]) -> bool:
    """True when a _replicator doc replicates btq_people (by id or endpoint)."""
    doc_id = str(doc.get("_id") or "")
    # Match the live db in any naming era, but never the btq_people_bak_*
    # archives (whose ids contain the same substring).
    if re.search(rf"{PEOPLE_DB}(?!_bak)", doc_id):
        return True
    for endpoint in (doc.get("source"), doc.get("target")):
        path = parse.urlparse(_endpoint_url(endpoint)).path
        if path.rstrip("/").endswith(f"/{PEOPLE_DB}"):
            return True
    return False


def _request_json(base_url: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = couchdb_config.from_env()
    url = f"{base_url.rstrip('/')}/{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(cfg.auth_header())
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise RetirePeopleDBError(f"CouchDB {method} {path} failed with HTTP {exc.code}: {detail}") from exc


def retire(base_url: str, *, execute: bool, archive_name: str) -> None:
    mode = "EXECUTE" if execute else "DRY RUN"

    # 1. Replicator cleanup.
    rows = _request_json(base_url, "GET", "_replicator/_all_docs?include_docs=true").get("rows", [])
    stale = [
        row["doc"] for row in rows
        if isinstance(row.get("doc"), dict)
        and not str(row["id"]).startswith("_design")
        and references_people_db(row["doc"])
    ]
    for doc in stale:
        print(f"[{mode}] delete replicator doc {doc['_id']}")
        if execute:
            _request_json(
                base_url, "DELETE",
                f"_replicator/{parse.quote(doc['_id'], safe='')}?rev={parse.quote(doc['_rev'], safe='')}",
            )

    # 2. Archive by local one-shot replication.
    info = _request_json(base_url, "GET", PEOPLE_DB)
    doc_count = int(info.get("doc_count") or 0)
    print(f"[{mode}] archive {PEOPLE_DB} ({doc_count} docs) -> {archive_name}")
    if execute:
        # The server-side replicator fetches the endpoints itself, so the
        # request's own auth does not carry over — embed it per endpoint.
        auth_headers = dict(couchdb_config.from_env().auth_header())
        _request_json(base_url, "POST", "_replicate", {
            "source": {"url": f"{base_url.rstrip('/')}/{PEOPLE_DB}", "headers": auth_headers},
            "target": {"url": f"{base_url.rstrip('/')}/{archive_name}", "headers": auth_headers},
            "create_target": True,
        })
        archived = int(_request_json(base_url, "GET", archive_name).get("doc_count") or 0)
        if archived != doc_count:
            raise RetirePeopleDBError(
                f"Archive doc count mismatch: {archive_name} has {archived}, expected {doc_count} — "
                f"{PEOPLE_DB} NOT deleted"
            )

    # 3. Delete the original.
    print(f"[{mode}] delete {PEOPLE_DB}")
    if execute:
        _request_json(base_url, "DELETE", PEOPLE_DB)
    print(f"[{mode}] done")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=None, help="CouchDB base URL of the peer (default: instance config).")
    parser.add_argument("--execute", action="store_true", help="Apply changes (default is dry run).")
    parser.add_argument(
        "--archive-name",
        default=f"btq_people_bak_{date.today().strftime('%Y%m%d')}",
        help="Archive database name (default: btq_people_bak_<today>).",
    )
    args = parser.parse_args(argv)
    base_url = args.url or couchdb_config.base_url()
    try:
        retire(base_url, execute=args.execute, archive_name=args.archive_name)
    except RetirePeopleDBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
