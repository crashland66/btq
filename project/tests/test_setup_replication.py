from __future__ import annotations

import json
import os
import unittest
from typing import Any
from urllib import error
from unittest import mock

from event_pipeline.couchdb import setup_replication


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


def request_payload(req: object) -> dict[str, Any]:
    data = getattr(req, "data", None)
    if not data:
        return {}
    payload = json.loads(data.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def http_error(code: int) -> error.HTTPError:
    return error.HTTPError("http://couchdb.test", code, "error", hdrs=None, fp=None)


class SetupReplicationTests(unittest.TestCase):
    def test_build_replication_doc_shape(self) -> None:
        doc = setup_replication.build_replication_doc(
            "http://vps.test",
            "vps_user",
            "vps_pass",
            "http://dell.test",
            "dell_user",
            "dell_pass",
        )

        self.assertEqual(doc["_id"], "btq_field_captures_vps_to_dell")
        self.assertEqual(doc["source"]["url"], "http://vps.test/btq_field_captures")
        self.assertEqual(doc["target"]["url"], "http://dell.test/btq_field_captures")
        self.assertTrue(doc["continuous"])
        self.assertNotIn("_rev", doc)

    def test_setup_replication_creates_new(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        def fake_urlopen(req: object, timeout: float = 30) -> FakeResponse:
            method = getattr(req, "method")
            if method == "GET":
                calls.append((method, {}))
                raise http_error(404)
            calls.append((method, request_payload(req)))
            return FakeResponse({"ok": True, "id": "btq_field_captures_vps_to_dell", "rev": "1-a"}, status=201)

        with mock.patch("event_pipeline.couchdb.setup_replication.request.urlopen", side_effect=fake_urlopen):
            outcome = setup_replication.setup_replication(
                "http://vps.test",
                "vps_user",
                "vps_pass",
                "http://dell.test",
                "dell_user",
                "dell_pass",
            )

        self.assertEqual(outcome, "created")
        self.assertEqual([call[0] for call in calls], ["GET", "PUT"])
        self.assertNotIn("_rev", calls[1][1])

    def test_setup_replication_skips_unchanged(self) -> None:
        existing = setup_replication.build_replication_doc(
            "http://vps.test",
            "vps_user",
            "vps_pass",
            "http://dell.test",
            "dell_user",
            "dell_pass",
        )
        existing["_rev"] = "2-existing"
        existing["_replication_state"] = "triggered"
        existing["_replication_state_time"] = "2026-05-09T00:00:00Z"
        existing["_replication_id"] = "replication-id"

        with mock.patch("event_pipeline.couchdb.setup_replication.request.urlopen", return_value=FakeResponse(existing)) as urlopen_mock:
            outcome = setup_replication.setup_replication(
                "http://vps.test",
                "vps_user",
                "vps_pass",
                "http://dell.test",
                "dell_user",
                "dell_pass",
            )

        self.assertEqual(outcome, "exists")
        self.assertEqual(urlopen_mock.call_count, 1)

    def test_setup_replication_updates_changed(self) -> None:
        existing = setup_replication.build_replication_doc(
            "http://vps.test",
            "vps_user",
            "vps_pass",
            "http://old-dell.test",
            "dell_user",
            "dell_pass",
        )
        existing["_rev"] = "2-existing"
        payloads: list[dict[str, Any]] = []

        def fake_urlopen(req: object, timeout: float = 30) -> FakeResponse:
            method = getattr(req, "method")
            if method == "GET":
                return FakeResponse(existing)
            payloads.append(request_payload(req))
            return FakeResponse({"ok": True, "id": "btq_field_captures_vps_to_dell", "rev": "3-updated"})

        with mock.patch("event_pipeline.couchdb.setup_replication.request.urlopen", side_effect=fake_urlopen):
            outcome = setup_replication.setup_replication(
                "http://vps.test",
                "vps_user",
                "vps_pass",
                "http://dell.test",
                "dell_user",
                "dell_pass",
            )

        self.assertEqual(outcome, "updated")
        self.assertEqual(payloads[0]["_rev"], "2-existing")
        self.assertEqual(payloads[0]["target"]["url"], "http://dell.test/btq_field_captures")

    def test_setup_replication_missing_target_env(self) -> None:
        env = {
            "BTQ_COUCHDB_URL": "http://vps.test",
            "BTQ_COUCHDB_USER": "vps_user",
            "BTQ_COUCHDB_PASSWORD": "vps_pass",
            "BTQ_COUCHDB_REPLICATION_TARGET_USER": "dell_user",
            "BTQ_COUCHDB_REPLICATION_TARGET_PASSWORD": "dell_pass",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("event_pipeline.couchdb.setup_replication.request.urlopen") as urlopen_mock:
                self.assertEqual(setup_replication.main(), 1)

        urlopen_mock.assert_not_called()

    def test_setup_all_replications_covers_every_btq_database_both_directions(self) -> None:
        calls: list[dict[str, str]] = []

        def fake_setup_replication(**kwargs: str) -> str:
            calls.append(kwargs)
            return "created"

        with mock.patch("event_pipeline.couchdb.setup_replication.setup_replication", side_effect=fake_setup_replication):
            outcomes = setup_replication.setup_all_replications(
                "http://pro.test",
                "pro_user",
                "pro_pass",
                "http://vps.test",
                "vps_user",
                "vps_pass",
            )

        expected_doc_ids = {
            f"{database}_{source}_to_{target}"
            for database in setup_replication.REPLICATION_DATABASES
            for source, target in (("pro", "vps"), ("vps", "pro"))
        }
        self.assertEqual(set(outcomes), expected_doc_ids)
        self.assertIn("btq_vault_pro_to_vps", outcomes)
        self.assertIn("btq_vault_vps_to_pro", outcomes)
        self.assertEqual(len(calls), len(expected_doc_ids))

    def test_setup_mesh_replications_includes_dell_peer(self) -> None:
        calls: list[str] = []

        def fake_setup_replication(**kwargs: str) -> str:
            calls.append(str(kwargs["doc_id"]))
            return "exists"

        peers = [
            setup_replication.CouchDBPeer("pro", "http://pro.test", "pro_user", "pro_pass"),
            setup_replication.CouchDBPeer("vps", "http://vps.test", "vps_user", "vps_pass"),
            setup_replication.CouchDBPeer("dell", "http://dell.test", "dell_user", "dell_pass"),
        ]

        with mock.patch("event_pipeline.couchdb.setup_replication.setup_replication", side_effect=fake_setup_replication):
            outcomes = setup_replication.setup_mesh_replications(peers, databases=("btq_vault",))

        self.assertEqual(
            set(outcomes),
            {
                "btq_vault_pro_to_vps",
                "btq_vault_pro_to_dell",
                "btq_vault_vps_to_pro",
                "btq_vault_vps_to_dell",
                "btq_vault_dell_to_pro",
                "btq_vault_dell_to_vps",
            },
        )
        self.assertEqual(set(calls), set(outcomes))


if __name__ == "__main__":
    unittest.main()
