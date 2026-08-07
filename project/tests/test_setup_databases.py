from __future__ import annotations

import json
import unittest
from urllib import error
from unittest import mock

from event_pipeline.couchdb import setup_databases


class FakeResponse:
    def __init__(self, payload: dict[str, object] | None = None, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        if self.payload is None:
            return b""
        return json.dumps(self.payload).encode("utf-8")


def http_error(code: int) -> error.HTTPError:
    return error.HTTPError("http://couchdb.test", code, "error", hdrs=None, fp=None)


class SetupDatabasesTests(unittest.TestCase):
    def test_verify_connection_success(self) -> None:
        with mock.patch("event_pipeline.couchdb.setup_databases.request.urlopen", return_value=FakeResponse({"version": "3.3.3"})):
            version = setup_databases.verify_connection("http://couchdb.test", {})

        self.assertEqual(version, "3.3.3")

    def test_verify_connection_auth_failure(self) -> None:
        with mock.patch("event_pipeline.couchdb.setup_databases.request.urlopen", side_effect=http_error(401)):
            with self.assertRaises(setup_databases.CouchDBSetupError):
                setup_databases.verify_connection("http://couchdb.test", {})

    def test_create_database_created(self) -> None:
        with mock.patch("event_pipeline.couchdb.setup_databases.request.urlopen", return_value=FakeResponse({"ok": True}, status=201)):
            outcome = setup_databases.create_database("btq_sites", "http://couchdb.test", {})

        self.assertEqual(outcome, "created")

    def test_create_database_exists(self) -> None:
        with mock.patch("event_pipeline.couchdb.setup_databases.request.urlopen", side_effect=http_error(412)):
            outcome = setup_databases.create_database("btq_sites", "http://couchdb.test", {})

        self.assertEqual(outcome, "exists")

    def test_create_database_error(self) -> None:
        with mock.patch("event_pipeline.couchdb.setup_databases.request.urlopen", side_effect=http_error(500)):
            with self.assertRaises(setup_databases.CouchDBSetupError):
                setup_databases.create_database("btq_sites", "http://couchdb.test", {})

    def test_create_required_databases(self) -> None:
        with mock.patch(
            "event_pipeline.couchdb.setup_databases.create_database",
            side_effect=["created", "created", "exists", "created", "created"],
        ) as create_mock:
            outcomes = setup_databases.create_required_databases("http://couchdb.test", {})

        self.assertEqual(
            outcomes,
            {
                "btq_field_captures": "created",
                "btq_voice_memos": "created",
                "btq_queue": "exists",
                "btq_vault": "created",
                "btq_personal_journal": "created",
            },
        )
        self.assertEqual(create_mock.call_count, 5)


class ProvisionVaultIndexesTests(unittest.TestCase):
    def test_provision_vault_indexes_creates_indexes(self) -> None:
        with mock.patch.object(setup_databases, "create_database") as create_db_mock:
            with mock.patch.object(setup_databases, "create_mango_index", return_value="created") as create_idx_mock:
                outcomes = setup_databases.provision_vault_indexes("http://couchdb.test", {})

        self.assertEqual(
            outcomes,
            {str(index_def.get("name") or ""): "created" for index_def in setup_databases.VAULT_MANGO_INDEXES},
        )
        self.assertEqual(create_idx_mock.call_count, len(setup_databases.VAULT_MANGO_INDEXES))
        for call in create_idx_mock.call_args_list:
            self.assertEqual(call.args[0], "btq_vault")
        create_db_mock.assert_not_called()

    def test_provision_vault_indexes_idempotent(self) -> None:
        with mock.patch.object(setup_databases, "create_mango_index", return_value="exists"):
            outcomes = setup_databases.provision_vault_indexes("http://couchdb.test", {})

        self.assertEqual(set(outcomes.values()), {"exists"})

    def test_vault_index_selector_shape(self) -> None:
        index_defs = {str(index_def.get("name") or ""): index_def for index_def in setup_databases.VAULT_MANGO_INDEXES}

        self.assertIn("idx-type-status", index_defs)
        self.assertEqual(index_defs["idx-type-status"].get("index", {}).get("fields"), ["type", "status"])


if __name__ == "__main__":
    unittest.main()
