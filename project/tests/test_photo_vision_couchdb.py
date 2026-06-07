from __future__ import annotations

import json
import unittest
from unittest import mock
from urllib import error

from event_pipeline import couchdb_config
from field_capture.photo_vision import ADVISORY_WARNING, STATUS_COMPLETED, STATUS_FAILED
from field_capture.photo_vision_couchdb import (
    DOC_TYPE,
    SCHEMA_VERSION,
    PhotoVisionCouchDBError,
    build_photo_vision_document,
    put_photo_vision_document,
    query_photo_vision,
)


def _make_config() -> couchdb_config.CouchDBConfig:
    return couchdb_config.CouchDBConfig(
        base_url="http://couchdb.test:5984",
        username="admin",
        password="secret",
        timeout=5.0,
        heartbeat_ms=10000,
    )


def _sample_sidecar(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "photo_asset_id": "fcp-abc123",
        "capture_id": "cap-2026-05-01",
        "site_id": "site-001",
        "photo_id": "photo-001",
        "status": STATUS_COMPLETED,
        "generated_at": "2026-05-01T12:00:00Z",
        "description": "A tiled restroom with three urinals.",
        "area_guess": "restroom",
        "submitted_area": "restroom",
        "submitted_phase": "completed",
        "visible_objects": ["urinal", "tile floor"],
        "possible_conditions": ["paper towel on floor"],
        "possible_issues": [],
        "confidence": 0.9,
        "needs_human_review": False,
        "warnings": [ADVISORY_WARNING],
        "quality_flags": [],
        "quality": {
            "analyzable": True,
            "severity": "ok",
            "flags": [],
            "confidence": 0.9,
            "needs_human_review": False,
            "source": "model",
        },
        "model_name": "gemma4:26b",
        "model_provider": "ollama",
        "source_image_hash": "abc123",
        "provenance": {
            "capture_id": "cap-2026-05-01",
            "photo_id": "photo-001",
            "photo_asset_id": "fcp-abc123",
            "image_media_url": "/media/upload-001",
            "image_filename": "photo.jpg",
            "image_mime_type": "image/jpeg",
            "image_size_bytes": 1024,
            "source_image_path": "/uploads/photo.jpg",
            "source_image_hash": "abc123",
            "captured_at": "2026-05-01T10:00:00Z",
            "intake_json_path": "/intake/cap.json",
        },
    }
    payload.update(overrides)
    return payload


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def http_error(code: int) -> error.HTTPError:
    return error.HTTPError("http://couchdb.test", code, "error", hdrs=None, fp=None)


class BuildPhotoVisionDocumentTests(unittest.TestCase):
    def test_basic_fields(self) -> None:
        doc = build_photo_vision_document(_sample_sidecar())
        self.assertEqual(doc["_id"], "fcp-abc123")
        self.assertEqual(doc["doc_type"], DOC_TYPE)
        self.assertEqual(doc["schema_version"], SCHEMA_VERSION)
        self.assertEqual(doc["site_id"], "site-001")
        self.assertEqual(doc["capture_id"], "cap-2026-05-01")
        self.assertEqual(doc["photo_asset_id"], "fcp-abc123")
        self.assertEqual(doc["status"], STATUS_COMPLETED)

    def test_search_text_built(self) -> None:
        doc = build_photo_vision_document(_sample_sidecar())
        search_text = str(doc["search_text"])
        self.assertIn("restroom", search_text)
        self.assertIn("urinal", search_text)
        self.assertIn("paper towel on floor", search_text)
        self.assertEqual(search_text, search_text.lower())

    def test_quality_block_carried(self) -> None:
        doc = build_photo_vision_document(_sample_sidecar())
        self.assertIsInstance(doc["quality"], dict)
        self.assertEqual(doc["quality"]["severity"], "ok")

    def test_error_included_when_present(self) -> None:
        sidecar = _sample_sidecar(
            status=STATUS_FAILED,
            error={"type": "timeout", "message": "timed out", "can_retry": True},
        )
        doc = build_photo_vision_document(sidecar)
        self.assertIn("error", doc)
        self.assertEqual(doc["error"]["type"], "timeout")

    def test_no_error_field_when_absent(self) -> None:
        doc = build_photo_vision_document(_sample_sidecar())
        self.assertNotIn("error", doc)

    def test_missing_photo_asset_id_raises(self) -> None:
        sidecar = _sample_sidecar(photo_asset_id="")
        with self.assertRaises(PhotoVisionCouchDBError):
            build_photo_vision_document(sidecar)

    def test_visible_objects_list(self) -> None:
        doc = build_photo_vision_document(_sample_sidecar())
        self.assertEqual(doc["visible_objects"], ["urinal", "tile floor"])

    def test_provenance_carried(self) -> None:
        doc = build_photo_vision_document(_sample_sidecar())
        self.assertIsInstance(doc["provenance"], dict)
        self.assertEqual(doc["provenance"]["capture_id"], "cap-2026-05-01")


class PutPhotoVisionDocumentTests(unittest.TestCase):
    def _urlopen_sequence(self, responses: list[FakeResponse | error.HTTPError]) -> mock.MagicMock:
        side_effects = []
        for resp in responses:
            if isinstance(resp, error.HTTPError):
                side_effects.append(resp)
            else:
                side_effects.append(resp)
        return mock.patch(
            "field_capture.photo_vision_couchdb.request.urlopen",
            side_effect=side_effects,
        )

    def test_create_new_document(self) -> None:
        get_404 = http_error(404)
        put_ok = FakeResponse({"ok": True, "id": "fcp-abc123", "rev": "1-abc"}, status=201)
        config = _make_config()
        doc = build_photo_vision_document(_sample_sidecar())

        with mock.patch("field_capture.photo_vision_couchdb.request.urlopen") as urlopen_mock:
            urlopen_mock.side_effect = [get_404, put_ok]
            result = put_photo_vision_document(config, doc, database="btq_photo_vision")

        self.assertEqual(result.get("rev"), "1-abc")
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_update_existing_document(self) -> None:
        existing_doc = FakeResponse({"_id": "fcp-abc123", "_rev": "3-xyz"}, status=200)
        put_ok = FakeResponse({"ok": True, "id": "fcp-abc123", "rev": "4-new"}, status=200)
        config = _make_config()
        doc = build_photo_vision_document(_sample_sidecar())

        with mock.patch("field_capture.photo_vision_couchdb.request.urlopen") as urlopen_mock:
            urlopen_mock.side_effect = [existing_doc, put_ok]
            result = put_photo_vision_document(config, doc, database="btq_photo_vision")

        self.assertEqual(result.get("rev"), "4-new")
        put_body = json.loads(urlopen_mock.call_args_list[1][0][0].data.decode("utf-8"))
        self.assertEqual(put_body["_rev"], "3-xyz")

    def test_retries_on_409_conflict(self) -> None:
        existing_doc_1 = FakeResponse({"_id": "fcp-abc123", "_rev": "2-old"}, status=200)
        conflict = http_error(409)
        existing_doc_2 = FakeResponse({"_id": "fcp-abc123", "_rev": "3-newer"}, status=200)
        put_ok = FakeResponse({"ok": True, "rev": "4-final"}, status=200)
        config = _make_config()
        doc = build_photo_vision_document(_sample_sidecar())

        with mock.patch("field_capture.photo_vision_couchdb.request.urlopen") as urlopen_mock:
            urlopen_mock.side_effect = [existing_doc_1, conflict, existing_doc_2, put_ok]
            result = put_photo_vision_document(config, doc, database="btq_photo_vision")

        self.assertEqual(result.get("rev"), "4-final")

    def test_missing_id_raises(self) -> None:
        config = _make_config()
        with self.assertRaises(PhotoVisionCouchDBError):
            put_photo_vision_document(config, {"doc_type": DOC_TYPE}, database="btq_photo_vision")

    def test_network_error_raises(self) -> None:
        config = _make_config()
        doc = build_photo_vision_document(_sample_sidecar())
        with mock.patch(
            "field_capture.photo_vision_couchdb.request.urlopen",
            side_effect=ConnectionRefusedError("refused"),
        ):
            with self.assertRaises(PhotoVisionCouchDBError):
                put_photo_vision_document(config, doc, database="btq_photo_vision")

    def test_uses_default_database_from_config(self) -> None:
        get_404 = http_error(404)
        put_ok = FakeResponse({"ok": True, "rev": "1-a"}, status=201)
        config = _make_config()
        doc = build_photo_vision_document(_sample_sidecar())

        with mock.patch("field_capture.photo_vision_couchdb.request.urlopen") as urlopen_mock:
            urlopen_mock.side_effect = [get_404, put_ok]
            with mock.patch.dict("os.environ", {"BTQ_COUCHDB_PHOTO_VISION_DB": "custom_pv_db"}):
                put_photo_vision_document(config, doc)

        all_urls = [call[0][0].full_url for call in urlopen_mock.call_args_list]
        self.assertTrue(all(("custom_pv_db" in url) for url in all_urls))


class QueryPhotoVisionTests(unittest.TestCase):
    def test_returns_docs(self) -> None:
        response = FakeResponse({"docs": [{"_id": "fcp-abc123", "site_id": "site-001"}]})
        config = _make_config()
        selector = {"selector": {"site_id": "site-001"}}

        with mock.patch("field_capture.photo_vision_couchdb.request.urlopen", return_value=response):
            result = query_photo_vision(config, selector, database="btq_photo_vision")

        self.assertIn("docs", result)
        self.assertEqual(result["docs"][0]["_id"], "fcp-abc123")

    def test_http_error_raises(self) -> None:
        config = _make_config()
        with mock.patch(
            "field_capture.photo_vision_couchdb.request.urlopen",
            side_effect=http_error(500),
        ):
            with self.assertRaises(PhotoVisionCouchDBError):
                query_photo_vision(config, {"selector": {}}, database="btq_photo_vision")


class ProvisionPhotoVisionDatabaseTests(unittest.TestCase):
    def test_provision_creates_db_and_indexes(self) -> None:
        from event_pipeline.couchdb import setup_databases

        with mock.patch.object(setup_databases, "create_database", return_value="created") as create_db_mock:
            with mock.patch.object(setup_databases, "create_mango_index", return_value="created") as create_idx_mock:
                result = setup_databases.provision_photo_vision_database("http://couchdb.test", {})

        self.assertEqual(result["database"], "created")
        create_db_mock.assert_called_once_with("btq_photo_vision", "http://couchdb.test", {})
        self.assertEqual(create_idx_mock.call_count, len(setup_databases.PHOTO_VISION_MANGO_INDEXES))

    def test_provision_idempotent_when_db_exists(self) -> None:
        from event_pipeline.couchdb import setup_databases

        with mock.patch.object(setup_databases, "create_database", return_value="exists"):
            with mock.patch.object(setup_databases, "create_mango_index", return_value="exists"):
                result = setup_databases.provision_photo_vision_database("http://couchdb.test", {})

        self.assertEqual(result["database"], "exists")

    def test_create_mango_index_success(self) -> None:
        from event_pipeline.couchdb import setup_databases

        fake_resp = {"result": "created"}
        with mock.patch("event_pipeline.couchdb.setup_databases.request_json", return_value=(200, fake_resp)):
            outcome = setup_databases.create_mango_index("btq_photo_vision", PHOTO_VISION_MANGO_INDEXES[0], "http://couchdb.test", {})

        self.assertEqual(outcome, "created")

    def test_create_mango_index_exists(self) -> None:
        from event_pipeline.couchdb import setup_databases

        fake_resp = {"result": "exists"}
        with mock.patch("event_pipeline.couchdb.setup_databases.request_json", return_value=(200, fake_resp)):
            outcome = setup_databases.create_mango_index("btq_photo_vision", PHOTO_VISION_MANGO_INDEXES[0], "http://couchdb.test", {})

        self.assertEqual(outcome, "exists")


PHOTO_VISION_MANGO_INDEXES = [
    {"index": {"fields": ["site_id", "generated_at"]}, "name": "idx-site-generated", "type": "json"},
]


if __name__ == "__main__":
    unittest.main()
