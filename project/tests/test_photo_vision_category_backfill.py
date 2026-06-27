from __future__ import annotations

from unittest import mock

from event_pipeline import couchdb_config
from field_capture.photo_vision_category_backfill import backfill_photo_vision_categories


def _config() -> couchdb_config.CouchDBConfig:
    return couchdb_config.CouchDBConfig(
        base_url="http://couchdb.test:5984",
        username="admin",
        password="secret",
        timeout=5.0,
        heartbeat_ms=10000,
    )


def test_backfill_sets_vision_category_and_agreement_from_sidecar_fields() -> None:
    sidecar = {
        "_id": "fcp-1",
        "_rev": "1-sidecar",
        "doc_type": "photo_vision_sidecar",
        "area_guess": "restroom",
        "qc_category": "Restrooms",
    }

    with mock.patch(
        "field_capture.photo_vision_category_backfill.query_photo_vision",
        return_value={"docs": [sidecar]},
    ), mock.patch(
        "field_capture.photo_vision_category_backfill._put_couchdb_document",
    ) as put_doc:
        counts = backfill_photo_vision_categories(_config(), batch_size=50)

    assert counts == {"scanned": 1, "updated": 1, "dry_run": 0, "unchanged": 0, "failed": 0}
    written = put_doc.call_args[0][2]
    assert written["_id"] == "fcp-1"
    assert written["_rev"] == "1-sidecar"
    assert written["vision_category"] == "Restrooms"
    assert written["category_agreement"] == "match"


def test_backfill_is_idempotent_when_fields_are_current() -> None:
    sidecar = {
        "_id": "fcp-1",
        "_rev": "2-sidecar",
        "doc_type": "photo_vision_sidecar",
        "area_guess": "restroom",
        "qc_category": "Offices / Classrooms / Exam Rooms",
        "vision_category": "Restrooms",
        "category_agreement": "mismatch",
    }

    with mock.patch(
        "field_capture.photo_vision_category_backfill.query_photo_vision",
        return_value={"docs": [sidecar]},
    ), mock.patch(
        "field_capture.photo_vision_category_backfill._put_couchdb_document",
    ) as put_doc:
        counts = backfill_photo_vision_categories(_config())

    assert counts == {"scanned": 1, "updated": 0, "dry_run": 0, "unchanged": 1, "failed": 0}
    put_doc.assert_not_called()


def test_backfill_dry_run_and_missing_area_guess_are_non_mutating() -> None:
    sidecar = {
        "_id": "fcp-1",
        "_rev": "1-sidecar",
        "doc_type": "photo_vision_sidecar",
        "qc_category": "Restrooms",
    }

    with mock.patch(
        "field_capture.photo_vision_category_backfill.query_photo_vision",
        return_value={"docs": [sidecar]},
    ), mock.patch(
        "field_capture.photo_vision_category_backfill._put_couchdb_document",
    ) as put_doc:
        counts = backfill_photo_vision_categories(_config(), dry_run=True)

    assert counts == {"scanned": 1, "updated": 0, "dry_run": 1, "unchanged": 0, "failed": 0}
    put_doc.assert_not_called()
