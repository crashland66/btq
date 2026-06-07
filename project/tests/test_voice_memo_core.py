from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voice_memo.core import UploadedVoiceMemo, VoiceMemoError, parse_duration_seconds, record_upload, validate_audio
from voice_memo.couchdb import VoiceMemoCouchDBError


def sample_memo() -> UploadedVoiceMemo:
    return UploadedVoiceMemo(filename="memo.webm", content_type="audio/webm", content=b"voice-bytes")


def sample_sites() -> list[dict]:
    return [{"id": "7060", "account": "Contworks", "location": "Continental Metalworks"}]


def sample_employees() -> list[dict]:
    return [{"id": "hutton-maria", "first": "Maria", "last": "Hutton", "preferred_name": None, "job": "7050"}]


class CoreTests(unittest.TestCase):
    def test_record_upload_writes_couchdb_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            docs: list[dict] = []

            result = record_upload(
                data_dir=data_dir,
                captured_at="2026-05-10T10:30:00-04:00",
                note="  hello    memo  ",
                duration_seconds=38,
                memo=sample_memo(),
                couchdb_put=lambda doc: docs.append(doc) or {"ok": True},
            )

            doc = docs[0]
            self.assertEqual(doc["type"], "personal_voice_memo")
            self.assertEqual(doc["_id"], result.record["_id"])
            self.assertEqual(doc["captured_at"], "2026-05-10T14:30:00Z")
            self.assertEqual(doc["note"], "hello memo")
            self.assertEqual(doc["duration_seconds"], 38)
            self.assertEqual(doc["audio_filename"], "memo.webm")
            self.assertEqual(doc["audio_mime_type"], "audio/webm")
            self.assertEqual(doc["audio_size_bytes"], len(sample_memo().content))
            self.assertEqual(doc["processing_state"], "pending")
            self.assertTrue((data_dir / doc["audio_path"]).exists())
            self.assertIn("/05/", doc["audio_path"].replace("\\", "/"))

    def test_record_upload_includes_person_id_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs: list[dict] = []

            record_upload(
                data_dir=Path(tmp),
                captured_at="2026-05-10T10:30:00-04:00",
                note="hello",
                duration_seconds=5,
                memo=sample_memo(),
                couchdb_put=lambda doc: docs.append(doc) or {"ok": True},
                person_id="per_test123",
            )

            self.assertEqual(docs[0]["person_id"], "per_test123")

    def test_record_upload_omits_person_id_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs: list[dict] = []

            record_upload(
                data_dir=Path(tmp),
                captured_at="2026-05-10T10:30:00-04:00",
                note="hello",
                duration_seconds=5,
                memo=sample_memo(),
                couchdb_put=lambda doc: docs.append(doc) or {"ok": True},
            )

            self.assertNotIn("person_id", docs[0])

    def test_record_upload_rolls_back_on_couchdb_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            def fail(_: dict) -> dict:
                raise VoiceMemoCouchDBError("offline")

            with self.assertRaises(VoiceMemoError) as error:
                record_upload(
                    data_dir=data_dir,
                    captured_at="2026-05-10T10:30:00-04:00",
                    note="hello",
                    duration_seconds=5,
                    memo=sample_memo(),
                    couchdb_put=fail,
                )

            self.assertEqual(error.exception.code, "couchdb_unavailable")
            self.assertEqual([path for path in data_dir.rglob("*") if path.is_file()], [])

    def test_validate_audio_rejects_unsupported_type(self) -> None:
        with self.assertRaises(VoiceMemoError) as error:
            validate_audio(UploadedVoiceMemo(filename="memo.exe", content_type="text/plain", content=b"nope"))
        self.assertEqual(error.exception.code, "unsupported_audio_type")

    def test_parse_duration_seconds_clamps(self) -> None:
        self.assertEqual(parse_duration_seconds(0), 1)
        self.assertEqual(parse_duration_seconds(100000), 7200)
        self.assertIsNone(parse_duration_seconds("abc"))
        self.assertIsNone(parse_duration_seconds(None))

    def test_record_upload_personal_mode_clears_site_and_employees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs: list[dict] = []
            record_upload(
                data_dir=Path(tmp),
                captured_at="2026-05-10T10:30:00-04:00",
                note="personal",
                duration_seconds=5,
                memo=sample_memo(),
                mode="personal",
                site_id="7060",
                employee_slugs="hutton-maria",
                sites_lookup=sample_sites,
                employees_lookup=sample_employees,
                couchdb_put=lambda doc: docs.append(doc) or {"ok": True},
            )
            self.assertEqual(docs[0]["routing_flag"], "personal_journal")
            self.assertIsNone(docs[0]["site_id"])
            self.assertEqual(docs[0]["employee_slugs"], [])

    def test_record_upload_routing_flag_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs: list[dict] = []
            record_upload(
                data_dir=Path(tmp),
                captured_at="2026-05-10T10:30:00-04:00",
                note="ops",
                duration_seconds=5,
                memo=sample_memo(),
                site_id="7060",
                employee_slugs="hutton-maria",
                sites_lookup=sample_sites,
                employees_lookup=sample_employees,
                couchdb_put=lambda doc: docs.append(doc) or {"ok": True},
            )
            self.assertEqual(docs[0]["routing_flag"], "site_tagged")
            self.assertEqual(docs[0]["site_account"], "Contworks")
            self.assertEqual(docs[0]["employee_names"], ["Maria Hutton"])

    def test_record_upload_employee_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs: list[dict] = []
            record_upload(
                data_dir=Path(tmp),
                captured_at="2026-05-10T10:30:00-04:00",
                note="employee",
                duration_seconds=5,
                memo=sample_memo(),
                employee_slugs="hutton-maria",
                employees_lookup=sample_employees,
                couchdb_put=lambda doc: docs.append(doc) or {"ok": True},
            )
            self.assertEqual(docs[0]["routing_flag"], "employee_tagged")

    def test_record_upload_general(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs: list[dict] = []
            record_upload(
                data_dir=Path(tmp),
                captured_at="2026-05-10T10:30:00-04:00",
                note="general",
                duration_seconds=5,
                memo=sample_memo(),
                couchdb_put=lambda doc: docs.append(doc) or {"ok": True},
            )
            self.assertEqual(docs[0]["routing_flag"], "general")

    def test_record_upload_unknown_site_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(VoiceMemoError) as error:
                record_upload(
                    data_dir=Path(tmp),
                    captured_at="2026-05-10T10:30:00-04:00",
                    note="bad site",
                    duration_seconds=5,
                    memo=sample_memo(),
                    site_id="9999",
                    sites_lookup=sample_sites,
                    couchdb_put=lambda doc: {"ok": True},
                )
            self.assertEqual(error.exception.code, "unknown_site_id")

    def test_record_upload_unknown_employee_slug_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(VoiceMemoError) as error:
                record_upload(
                    data_dir=Path(tmp),
                    captured_at="2026-05-10T10:30:00-04:00",
                    note="bad employee",
                    duration_seconds=5,
                    memo=sample_memo(),
                    employee_slugs="missing-person",
                    employees_lookup=sample_employees,
                    couchdb_put=lambda doc: {"ok": True},
                )
            self.assertEqual(error.exception.code, "unknown_employee_slug")

    def test_record_upload_persists_geolocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs: list[dict] = []
            record_upload(
                data_dir=Path(tmp),
                captured_at="2026-05-10T10:30:00-04:00",
                note="geo",
                duration_seconds=5,
                memo=sample_memo(),
                geolocation_lat="40.41",
                geolocation_lng="-78.83",
                geolocation_accuracy_m="12",
                geolocation_captured_at="2026-05-10T17:20:23Z",
                couchdb_put=lambda doc: docs.append(doc) or {"ok": True},
            )
            self.assertEqual(docs[0]["geolocation"]["lat"], 40.41)
            self.assertEqual(docs[0]["geolocation"]["lng"], -78.83)
            self.assertEqual(docs[0]["geolocation"]["accuracy_m"], 12.0)


if __name__ == "__main__":
    unittest.main()
