from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from field_capture.photo_vision import ADVISORY_WARNING, STATUS_COMPLETED, STATUS_FAILED
from field_capture.photo_vision_couchdb import PhotoVisionCouchDBError
from field_capture.photo_vision_couchdb_backfill import backfill_photo_vision


def _write_sidecar(directory: Path, asset_id: str, payload: dict) -> Path:
    path = directory / f"{asset_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sample_sidecar(asset_id: str = "fcp-abc123", **overrides: object) -> dict:
    payload: dict = {
        "photo_asset_id": asset_id,
        "capture_id": "cap-001",
        "site_id": "site-001",
        "photo_id": "photo-001",
        "status": STATUS_COMPLETED,
        "generated_at": "2026-05-01T12:00:00Z",
        "description": "A tiled restroom.",
        "area_guess": "restroom",
        "submitted_area": "restroom",
        "submitted_phase": "completed",
        "visible_objects": ["urinal"],
        "possible_conditions": [],
        "possible_issues": [],
        "confidence": 0.85,
        "needs_human_review": False,
        "warnings": [ADVISORY_WARNING],
        "quality_flags": [],
        "quality": {
            "analyzable": True,
            "severity": "ok",
            "flags": [],
            "confidence": 0.85,
            "needs_human_review": False,
            "source": "model",
        },
        "model_name": "gemma4:26b",
        "model_provider": "ollama",
        "source_image_hash": "abc123",
        "provenance": {},
    }
    payload.update(overrides)
    return payload


class BackfillDryRunTests(unittest.TestCase):
    def test_dry_run_counts_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp)
            _write_sidecar(photo_dir, "fcp-001", _sample_sidecar("fcp-001"))
            _write_sidecar(photo_dir, "fcp-002", _sample_sidecar("fcp-002"))

            with mock.patch("field_capture.photo_vision_couchdb_backfill.put_photo_vision_document") as put_mock:
                counts = backfill_photo_vision(photo_dir, cdb_config=None, dry_run=True)

            put_mock.assert_not_called()
            self.assertEqual(counts["found"], 2)
            self.assertEqual(counts["dry_run"], 2)
            self.assertEqual(counts["put"], 0)
            self.assertEqual(counts["failed"], 0)

    def test_dry_run_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp)
            for i in range(5):
                _write_sidecar(photo_dir, f"fcp-{i:03d}", _sample_sidecar(f"fcp-{i:03d}"))

            counts = backfill_photo_vision(photo_dir, cdb_config=None, dry_run=True, limit=3)

        self.assertEqual(counts["dry_run"], 3)
        self.assertEqual(counts["skipped"], 2)

    def test_empty_dir_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            counts = backfill_photo_vision(Path(tmp), cdb_config=None, dry_run=True)
        self.assertEqual(counts["found"], 0)
        self.assertEqual(counts["dry_run"], 0)

    def test_nonexistent_dir_returns_zero(self) -> None:
        counts = backfill_photo_vision(Path("/nonexistent/path/xyz"), cdb_config=None, dry_run=True)
        self.assertEqual(counts["found"], 0)

    def test_malformed_sidecar_counted_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp)
            (photo_dir / "bad.json").write_text("not json", encoding="utf-8")
            _write_sidecar(photo_dir, "fcp-001", _sample_sidecar("fcp-001"))

            counts = backfill_photo_vision(photo_dir, cdb_config=None, dry_run=True)

        self.assertEqual(counts["found"], 2)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["dry_run"], 1)

    def test_sidecar_without_quality_gets_derived(self) -> None:
        """Pre-Stage-1 sidecar without 'quality' key gets derive_quality() applied."""
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp)
            sidecar = _sample_sidecar("fcp-001")
            del sidecar["quality"]
            _write_sidecar(photo_dir, "fcp-001", sidecar)

            with mock.patch("field_capture.photo_vision_couchdb_backfill.put_photo_vision_document") as put_mock:
                counts = backfill_photo_vision(photo_dir, cdb_config=object(), dry_run=False)

            # Document should have been built and put
            self.assertEqual(counts["put"], 1)
            doc = put_mock.call_args[0][1]
            self.assertIsInstance(doc.get("quality"), dict)
            self.assertIn("severity", doc["quality"])

    def test_missing_photo_asset_id_counted_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp)
            sidecar = _sample_sidecar("fcp-001")
            sidecar["photo_asset_id"] = ""
            _write_sidecar(photo_dir, "fcp-001", sidecar)

            counts = backfill_photo_vision(photo_dir, cdb_config=None, dry_run=True)

        self.assertEqual(counts["failed"], 1)


class BackfillLiveTests(unittest.TestCase):
    def test_live_puts_each_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp)
            _write_sidecar(photo_dir, "fcp-001", _sample_sidecar("fcp-001"))
            _write_sidecar(photo_dir, "fcp-002", _sample_sidecar("fcp-002"))

            fake_config = object()
            with mock.patch("field_capture.photo_vision_couchdb_backfill.put_photo_vision_document") as put_mock:
                put_mock.return_value = {"ok": True}
                counts = backfill_photo_vision(photo_dir, cdb_config=fake_config, dry_run=False)

        self.assertEqual(counts["put"], 2)
        self.assertEqual(counts["failed"], 0)
        self.assertEqual(put_mock.call_count, 2)

    def test_couchdb_error_counted_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp)
            _write_sidecar(photo_dir, "fcp-001", _sample_sidecar("fcp-001"))

            with mock.patch(
                "field_capture.photo_vision_couchdb_backfill.put_photo_vision_document",
                side_effect=PhotoVisionCouchDBError("network error"),
            ):
                counts = backfill_photo_vision(photo_dir, cdb_config=object(), dry_run=False)

        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["put"], 0)

    def test_failed_sidecar_also_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp)
            failed = _sample_sidecar("fcp-001", status=STATUS_FAILED, description="", confidence=None)
            _write_sidecar(photo_dir, "fcp-001", failed)

            with mock.patch("field_capture.photo_vision_couchdb_backfill.put_photo_vision_document") as put_mock:
                put_mock.return_value = {"ok": True}
                counts = backfill_photo_vision(photo_dir, cdb_config=object(), dry_run=False)

        self.assertEqual(counts["put"], 1)


if __name__ == "__main__":
    unittest.main()
