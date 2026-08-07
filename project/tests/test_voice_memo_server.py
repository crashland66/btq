from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from capture_ingest import parse_multipart
from token_store import TokenStore
from voice_memo.core import UploadedVoiceMemo
from voice_memo.server import (
    authenticate_request,
    bearer_token,
    load_employees,
    load_sites,
    save_submission,
)


def multipart_body(fields: dict[str, str], audio: bytes = b"voice") -> tuple[bytes, str]:
    boundary = "----voice-memo-test"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"memo.webm\"\r\nContent-Type: audio/webm\r\n\r\n".encode()
        + audio
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def make_store(tmp: str, *, can_submit: bool = True, revoked: bool = False) -> tuple[TokenStore, str]:
    store = TokenStore(Path(tmp) / "field_capture_tokens.sqlite3")
    created = store.create_token(person_id="per_voice01", label="voice", can_submit=can_submit)
    if revoked:
        store.revoke_token(created.record.token_id)
    return store, created.token_value


def voice_memo_upload(body: bytes, content_type: str) -> tuple[dict[str, str], UploadedVoiceMemo]:
    fields, uploads = parse_multipart(body, content_type)
    audio = next(upload for upload in uploads if upload.field_name == "audio")
    return fields, UploadedVoiceMemo(filename=audio.filename, content_type=audio.content_type, content=audio.content)


class BearerTokenTests(unittest.TestCase):
    def test_extracts_bearer_value(self) -> None:
        self.assertEqual(bearer_token("Bearer fc_abc"), "fc_abc")

    def test_rejects_missing_prefix(self) -> None:
        self.assertIsNone(bearer_token("fc_abc"))

    def test_rejects_empty(self) -> None:
        self.assertIsNone(bearer_token(""))
        self.assertIsNone(bearer_token("Bearer "))


class AuthenticateRequestTests(unittest.TestCase):
    def test_request_without_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = make_store(tmp)
            self.assertIsNone(authenticate_request("", store))

    def test_request_with_unknown_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = make_store(tmp)
            self.assertIsNone(authenticate_request("Bearer fc_unknown", store))

    def test_valid_token_authenticates_and_carries_person_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, token = make_store(tmp)
            record = authenticate_request(f"Bearer {token}", store)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.person_id, "per_voice01")

    def test_revoked_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, token = make_store(tmp, revoked=True)
            self.assertIsNone(authenticate_request(f"Bearer {token}", store))

    def test_token_without_submit_permission_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, token = make_store(tmp, can_submit=False)
            self.assertIsNone(authenticate_request(f"Bearer {token}", store))


class SubmissionTests(unittest.TestCase):
    def test_save_submission_attributes_to_token_person_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs: list[dict] = []
            body, content_type = multipart_body(
                {"captured_at": "2026-05-10T10:30:00-04:00", "duration_seconds": "12", "note": "hello"}
            )
            fields, memo = voice_memo_upload(body, content_type)
            result = save_submission(
                Path(tmp),
                fields,
                memo,
                couchdb_put=lambda doc: docs.append(doc) or {"ok": True},
                person_id="per_voice01",
            )
            self.assertEqual(result.record["duration_seconds"], 12)
            self.assertEqual(docs[0]["type"], "personal_voice_memo")
            self.assertEqual(docs[0]["person_id"], "per_voice01")

    def test_save_submission_defaults_person_id_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs: list[dict] = []
            body, content_type = multipart_body(
                {"captured_at": "2026-05-10T10:30:00-04:00", "duration_seconds": "5", "note": "x"}
            )
            fields, memo = voice_memo_upload(body, content_type)
            save_submission(
                Path(tmp),
                fields,
                memo,
                couchdb_put=lambda doc: docs.append(doc) or {"ok": True},
            )
            self.assertEqual(docs[0].get("person_id", ""), "")


class PickerTests(unittest.TestCase):
    def test_get_sites_returns_active_vault_sourced_only(self) -> None:
        def fake_find(database: str, selector: dict) -> dict:
            self.assertEqual(database, "btq_sites")
            return {
                "docs": [
                    {"_id": "7060", "job": "7060", "account": "Contworks", "location": "Continental Metalworks", "synced_from_vault": True, "status": "active"},
                    {"_id": "site_legacy", "account": "Legacy", "location": "Old", "synced_from_vault": False, "status": "active"},
                    {"_id": "1300", "job": "1300", "account": "Inactive", "location": "Closed", "synced_from_vault": True, "status": "inactive"},
                    {"_id": "7030", "job": "7030", "account": "Alpha", "location": "First", "synced_from_vault": True, "status": "active"},
                ]
            }

        sites = load_sites(fake_find)
        self.assertEqual([site["id"] for site in sites], ["7030", "7060"])
        self.assertEqual(sites[0]["label"], "Alpha - First (7030)")
        self.assertEqual(sites[1]["label"], "Contworks - Continental Metalworks (7060)")

    def test_get_employees_omits_sensitive_identifier(self) -> None:
        def fake_find(database: str, selector: dict) -> dict:
            self.assertEqual(database, "btq_vault")
            self.assertEqual(selector["selector"], {"type": "employee", "status": "active"})
            return {
                "docs": [
                    {"_id": "employee_hutton_maria", "person_id": "hutton_maria", "type": "employee", "first": "Maria", "last": "Hutton", "preferred_name": None, "job": "7050", "status": "active"},
                    {"_id": "employee_hidden_person", "person_id": "hidden_person", "type": "employee", "first": "Hidden", "last": "Person", "preferred_name": None, "job": "1000", "status": "inactive"},
                ]
            }

        employees = load_employees(fake_find)
        payload = json.dumps({"employees": employees})
        self.assertEqual(employees[0]["id"], "hutton_maria")
        self.assertNotIn("5272", payload)
        self.assertNotIn("ehub", payload.lower())

    def test_get_employees_falls_back_to_doc_id_and_tolerates_list_preferred_name(self) -> None:
        def fake_find(database: str, selector: dict) -> dict:
            return {
                "docs": [
                    {"_id": "employee_dawson_erin", "type": "employee", "first": "Erin", "last": "Dawson", "preferred_name": [], "job": "1200", "status": "active"},
                ]
            }

        employees = load_employees(fake_find)
        self.assertEqual(employees[0]["id"], "dawson_erin")
        self.assertEqual(employees[0]["label"], "Erin Dawson — 1200")


if __name__ == "__main__":
    unittest.main()
