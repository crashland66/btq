from __future__ import annotations

from unittest import mock

from event_pipeline import couchdb_config
from field_capture.photo_vision_qc_category_backfill import backfill_photo_vision_qc_categories


def _config() -> couchdb_config.CouchDBConfig:
    return couchdb_config.CouchDBConfig(
        base_url="http://couchdb.test:5984",
        username="admin",
        password="secret",
        timeout=5.0,
        heartbeat_ms=10000,
    )


def test_backfill_sets_qc_category_from_field_capture() -> None:
    sidecar = {
        "_id": "fcp-1",
        "_rev": "1-sidecar",
        "doc_type": "photo_vision_sidecar",
        "capture_id": "cap-1",
        "area_guess": "restroom",
    }

    with mock.patch(
        "field_capture.photo_vision_qc_category_backfill.query_photo_vision",
        return_value={"docs": [sidecar]},
    ) as query_sidecars, mock.patch(
        "field_capture.photo_vision_qc_category_backfill.query_couchdb_find",
        return_value={"docs": [{"_id": "field-cap-1", "capture_id": "cap-1", "qc_category": "Restrooms"}]},
    ) as query_captures, mock.patch(
        "field_capture.photo_vision_qc_category_backfill._put_couchdb_document",
    ) as put_doc:
        counts = backfill_photo_vision_qc_categories(_config(), batch_size=50)

    assert counts["scanned"] == 1
    assert counts["updated"] == 1
    assert counts["failed"] == 0
    query_sidecars.assert_called_once()
    query_captures.assert_called_once()
    written = put_doc.call_args[0][2]
    assert written["_id"] == "fcp-1"
    assert written["_rev"] == "1-sidecar"
    assert written["qc_category"] == "Restrooms"


def test_backfill_dry_run_and_missing_capture_are_non_mutating() -> None:
    docs = [
        {"_id": "fcp-1", "_rev": "1-a", "doc_type": "photo_vision_sidecar", "capture_id": "cap-1"},
        {"_id": "fcp-2", "_rev": "1-b", "doc_type": "photo_vision_sidecar", "capture_id": "cap-missing"},
    ]

    with mock.patch(
        "field_capture.photo_vision_qc_category_backfill.query_photo_vision",
        return_value={"docs": docs},
    ), mock.patch(
        "field_capture.photo_vision_qc_category_backfill.query_couchdb_find",
        return_value={"docs": [{"capture_id": "cap-1", "qc_category": "qc"}]},
    ), mock.patch(
        "field_capture.photo_vision_qc_category_backfill._put_couchdb_document",
    ) as put_doc:
        counts = backfill_photo_vision_qc_categories(_config(), dry_run=True)

    assert counts["scanned"] == 2
    assert counts["dry_run"] == 1
    assert counts["missing_capture"] == 1
    put_doc.assert_not_called()


def test_backfill_idempotent_rerun_is_noop_when_no_sidecars_lack_category() -> None:
    with mock.patch(
        "field_capture.photo_vision_qc_category_backfill.query_photo_vision",
        return_value={"docs": []},
    ), mock.patch(
        "field_capture.photo_vision_qc_category_backfill.query_couchdb_find",
    ) as query_captures, mock.patch(
        "field_capture.photo_vision_qc_category_backfill._put_couchdb_document",
    ) as put_doc:
        counts = backfill_photo_vision_qc_categories(_config())

    assert counts["scanned"] == 0
    assert counts["updated"] == 0
    query_captures.assert_not_called()
    put_doc.assert_not_called()
