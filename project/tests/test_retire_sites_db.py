"""Gates for the btq_sites archive helper.

Replicator-doc matching must catch every naming era — legacy
``pro_to_vps_btq_sites``, mesh ``btq_sites_pro_to_vps``, and opaque-id
docs whose endpoint URLs point at the database — while never matching
replications of other databases (including the ``btq_sites_bak_*``
archives themselves, whose ids merely contain the substring).
"""

from __future__ import annotations

from event_pipeline.couchdb.retire_sites_db import references_sites_db


def test_matches_legacy_and_mesh_doc_ids():
    assert references_sites_db({"_id": "pro_to_vps_btq_sites"})
    assert references_sites_db({"_id": "vps_to_pro_btq_sites"})
    assert references_sites_db({"_id": "btq_sites_pro_to_vps"})


def test_matches_opaque_id_by_endpoint_url():
    doc = {
        "_id": "09e54be8c77d70f47450a8ddf5004b1d",
        "source": "http://203.0.113.10:5984/btq_sites",
        "target": {"url": "http://127.0.0.1:5984/btq_sites"},
    }
    assert references_sites_db(doc)
    assert references_sites_db({"_id": "x", "source": "http://127.0.0.1:5984/btq_sites/"})


def test_ignores_other_databases_and_archives():
    assert not references_sites_db({
        "_id": "btq_vault_pro_to_vps",
        "source": "http://127.0.0.1:5984/btq_vault",
        "target": "http://203.0.113.10:5984/btq_vault",
    })
    # An archive replication doc references btq_sites_bak_*, not btq_sites —
    # neither by endpoint URL nor by a bak-named doc id.
    assert not references_sites_db({
        "_id": "d68f12729ebee1b064f8a29f86000e64",
        "source": "http://127.0.0.1:5984/btq_sites_bak_20260609",
        "target": "http://127.0.0.1:5984/btq_sites_bak_20260609",
    })
    assert not references_sites_db({"_id": "btq_sites_bak_20260609_archive_copy"})
