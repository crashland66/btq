from __future__ import annotations

import contextlib
import io
import os
from collections.abc import Callable

from event_pipeline.couchdb import migrate_sites, push_design_doc, setup_databases, setup_replication


def compact_output(text: str) -> str:
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def env_required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise setup_replication.CouchDBReplicationError(f"{name} is required")
    return value


def run_main_with_output(func: Callable[[], int]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = func()
    return int(code), compact_output(buffer.getvalue())


def run_setup(
    *,
    verify_only: bool = False,
    with_replication: bool = False,
    skip_migrate: bool = False,
) -> int:
    total = 1 if verify_only else 3 if skip_migrate else 4
    if with_replication and not verify_only:
        total += 1
    step = 1
    base_url = setup_databases.couchdb_url()
    headers = setup_databases.auth_headers()

    try:
        version = setup_databases.verify_connection(base_url, headers)
        print(f"[{step}/{total}] Verifying CouchDB connection... ok (CouchDB {version})")
        if verify_only:
            return 0

        step += 1
        outcomes = setup_databases.create_required_databases(base_url, headers)
        outcome_text = " ".join(f"{database}={outcome}" for database, outcome in outcomes.items())
        # Provision Mango indexes whose databases are created above. Idempotent.
        # Folded into this step (not a new numbered step) so the step counter is
        # unchanged; without this the operator-facing `btq setup-couchdb` never
        # creates the btq_vault indexes (they previously lived only in
        # setup_databases.main(), which this command does not invoke).
        vault_index_outcomes = setup_databases.provision_vault_indexes(base_url, headers)
        vault_index_text = " ".join(f"{name}={outcome}" for name, outcome in vault_index_outcomes.items())
        field_capture_index_outcomes = (
            setup_databases.provision_field_capture_indexes(base_url, headers)
            if setup_databases.couchdb_config.DEFAULT_FIELD_CAPTURES_DB in outcomes
            else {}
        )
        field_capture_index_text = " ".join(
            f"{name}={outcome}" for name, outcome in field_capture_index_outcomes.items()
        )
        # Provision photo-vision database/indexes as part of database creation.
        # Folded into this step (not a new numbered step) so the step counter is
        # unchanged; without this the operator-facing `btq setup-couchdb` never
        # provisions btq_photo_vision indexes (they previously lived only in
        # setup_databases.main(), which this command does not invoke).
        photo_vision_outcome = setup_databases.provision_photo_vision_database(base_url, headers)
        pv_db = photo_vision_outcome.get("database", "")
        pv_index_text = " ".join(
            f"{name}={outcome}" for name, outcome in (photo_vision_outcome.get("indexes") or {}).items()
        )
        print(
            f"[{step}/{total}] Creating databases... {outcome_text} | btq_vault indexes: {vault_index_text} "
            f"| btq_field_captures indexes: {field_capture_index_text} "
            f"| btq_photo_vision: {pv_db} indexes: {pv_index_text}"
        )

        step += 1
        # Pass an explicit empty argv: push_design_doc.main defaults argv to None,
        # which makes argparse read sys.argv (here ["setup-couchdb", ...]) and abort
        # with "unrecognized arguments: setup-couchdb". An empty argv pushes all
        # design documents.
        code, output = run_main_with_output(lambda: push_design_doc.main([]))
        if code != 0:
            raise setup_databases.CouchDBSetupError(output or "push_design_doc failed")
        print(f"[{step}/{total}] Pushing design document... {output}")

        if not skip_migrate:
            step += 1
            code, output = run_main_with_output(migrate_sites.main)
            if code != 0:
                raise setup_databases.CouchDBSetupError(output or "migrate_sites failed")
            print(f"[{step}/{total}] Migrating site data... {output}")

        if with_replication:
            step += 1
            if os.environ.get("BTQ_PRO_COUCHDB_URL") and os.environ.get("BTQ_VPS_COUCHDB_URL"):
                peers = [
                    setup_replication.CouchDBPeer(
                        "pro",
                        env_required("BTQ_PRO_COUCHDB_URL"),
                        env_required("BTQ_PRO_COUCHDB_USER"),
                        env_required("BTQ_PRO_COUCHDB_PASSWORD"),
                    ),
                    setup_replication.CouchDBPeer(
                        "vps",
                        env_required("BTQ_VPS_COUCHDB_URL"),
                        env_required("BTQ_VPS_COUCHDB_USER"),
                        env_required("BTQ_VPS_COUCHDB_PASSWORD"),
                    ),
                ]
                dell = setup_replication.optional_peer(
                    "dell",
                    "BTQ_DELL_COUCHDB_URL",
                    "BTQ_DELL_COUCHDB_USER",
                    "BTQ_DELL_COUCHDB_PASSWORD",
                )
                if dell is not None:
                    peers.append(dell)
                outcomes = setup_replication.setup_mesh_replications(peers)
                outcome = f"{len(outcomes)} docs " + " ".join(f"{doc_id}={status}" for doc_id, status in outcomes.items())
            else:
                outcome = setup_replication.setup_replication(
                    source_url=env_required("BTQ_COUCHDB_URL"),
                    source_user=os.environ.get("BTQ_COUCHDB_USER", ""),
                    source_password=os.environ.get("BTQ_COUCHDB_PASSWORD", ""),
                    target_url=env_required("BTQ_COUCHDB_REPLICATION_TARGET_URL"),
                    target_user=env_required("BTQ_COUCHDB_REPLICATION_TARGET_USER"),
                    target_password=env_required("BTQ_COUCHDB_REPLICATION_TARGET_PASSWORD"),
                )
            print(f"[{step}/{total}] Configuring replication... {outcome}")
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}")
        return 1
    return 0
