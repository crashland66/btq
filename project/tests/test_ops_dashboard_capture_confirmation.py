"""Independent gating tests for the capture upload-confirmation change.

Covers three surfaces that the change touches:
  * field_photos._pending_photo_records  — terminal-capture exclusion
  * photos._pending_photo_records        — terminal-capture exclusion (parity)
  * site_detail._site_capture_processing_counts / _capture_processing_breakdown
  * site_detail.render graceful degradation when the couch lookup raises

Fixture identity is sandbox-only: site_id "SANDBOX", person "sandbox-user".
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from field_capture.photo_vision import FieldPhotoAsset

import ops_dashboard.sections.field_photos as field_photos
import ops_dashboard.sections.photos as photos
import ops_dashboard.sections.site_detail as site_detail


def _asset(capture_id: str, asset_id: str) -> FieldPhotoAsset:
    return FieldPhotoAsset(
        capture_id=capture_id,
        site_id="SANDBOX",
        area="lobby",
        phase="arrival",
        photo_asset_id=asset_id,
        photo_id=asset_id,
        filename=f"{asset_id}.jpg",
        image_path=Path(f"/tmp/sandbox/{asset_id}.jpg"),
        image_media_url=f"/media/{asset_id}",
        mime_type="image/jpeg",
        size_bytes=1024,
        intake_json_path=Path(f"/tmp/sandbox/{asset_id}.json"),
        captured_at="2026-06-15T08:00:00Z",
        note="",
    )


def _drive_pending(module, assets, terminal_capture_ids):
    """Drive a module's real _pending_photo_records over crafted assets."""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch(
            "field_capture.photo_vision.discover_photo_assets", return_value=list(assets)
        ), mock.patch.object(module, "load_photo_vision_sidecars", return_value=[]), mock.patch.object(
            module, "submitters_by_capture", return_value={}
        ):
            return module._pending_photo_records(
                Path(tmp),
                processed_asset_ids=set(),
                terminal_capture_ids=terminal_capture_ids,
                q="",
                site_id="",
                area_guess="",
                date_from="",
                date_to="",
            )


# --------------------------------------------------------------------------
# Part 1 — terminal-capture exclusion on BOTH pending surfaces
# --------------------------------------------------------------------------
class PendingExclusionTests(unittest.TestCase):
    def _scenario(self, module):
        assets = [
            _asset("cap-failed", "fcp-failed"),
            _asset("cap-complete", "fcp-complete"),
            _asset("cap-completed", "fcp-completed"),
            _asset("cap-pending", "fcp-pending"),
            _asset("cap-unknown", "fcp-unknown"),  # missing/empty processing_state
        ]
        # The render layer would have collected these terminal ids from couch;
        # here we hand them in directly to exercise the exclusion branch.
        terminal = {"cap-failed", "cap-complete", "cap-completed"}
        records = _drive_pending(module, assets, terminal)
        kept = {r["capture_id"] for r in records}

        # Terminal captures excluded from the "waiting" list.
        self.assertNotIn("cap-failed", kept)
        self.assertNotIn("cap-complete", kept)
        self.assertNotIn("cap-completed", kept)
        # Genuinely pending captures remain.
        self.assertIn("cap-pending", kept)
        # Edge: a capture with empty/unknown/missing state is NOT terminal -> still waiting.
        self.assertIn("cap-unknown", kept)

    def test_field_photos_excludes_terminal(self) -> None:
        self._scenario(field_photos)

    def test_photos_excludes_terminal(self) -> None:
        self._scenario(photos)

    def test_no_terminal_ids_keeps_everything(self) -> None:
        # When the couch terminal-id query degraded (None), nothing is excluded.
        assets = [_asset("cap-a", "fcp-a"), _asset("cap-b", "fcp-b")]
        records = _drive_pending(field_photos, assets, None)
        self.assertEqual({r["capture_id"] for r in records}, {"cap-a", "cap-b"})


# --------------------------------------------------------------------------
# Part 1b — _query_terminal_capture_ids state bucketing (unit)
# --------------------------------------------------------------------------
class TerminalIdQueryTests(unittest.TestCase):
    def test_only_terminal_states_collected(self) -> None:
        docs = [
            {"capture_id": "cap-failed", "processing_state": "failed"},
            {"capture_id": "cap-complete", "processing_state": "complete"},
            {"capture_id": "cap-completed", "processing_state": "COMPLETED"},
            {"capture_id": "cap-pending", "processing_state": "pending"},
            {"capture_id": "cap-blank", "processing_state": ""},
            {"_id": "fc_doc_only", "processing_state": "failed"},  # capture_id falls back to _id
        ]
        with mock.patch(
            "voice_memo.couchdb.query_couchdb_find", return_value={"docs": docs, "bookmark": None}
        ), mock.patch("event_pipeline.couchdb_config.field_captures_database", return_value="btq_field_captures"):
            ids = field_photos._query_terminal_capture_ids(object())
        self.assertEqual(ids, {"cap-failed", "cap-complete", "cap-completed", "fc_doc_only"})

    def test_query_failure_degrades_to_none(self) -> None:
        with mock.patch(
            "voice_memo.couchdb.query_couchdb_find", side_effect=RuntimeError("couch down")
        ), mock.patch("event_pipeline.couchdb_config.field_captures_database", return_value="btq_field_captures"):
            self.assertIsNone(field_photos._query_terminal_capture_ids(object()))


# --------------------------------------------------------------------------
# Part 2 — per-site processing breakdown partition + accent rendering
# --------------------------------------------------------------------------
class SiteBreakdownTests(unittest.TestCase):
    def _counts(self, docs):
        with mock.patch.object(
            site_detail, "_cdb", return_value=("http://couch.invalid", {}, "btq_vault", 5.0)
        ), mock.patch("event_pipeline.couchdb_config.field_captures_database", return_value="btq_field_captures"), mock.patch.object(
            site_detail.urlrequest, "urlopen"
        ) as urlopen:
            cm = mock.MagicMock()
            cm.read.return_value = (
                b'{"docs": ' + _json_docs(docs) + b', "bookmark": null}'
            )
            urlopen.return_value.__enter__.return_value = cm
            return site_detail._site_capture_processing_counts("SANDBOX")

    def test_partition_invariant(self) -> None:
        docs = [
            {"processing_state": "complete"},
            {"processing_state": "completed"},
            {"processing_state": "failed"},
            {"processing_state": "pending"},
            {"processing_state": "uploading"},  # novel/unknown -> in_flight, not vanished
            {"processing_state": ""},
        ]
        counts = self._counts(docs)
        self.assertEqual(counts["received"], 6)
        self.assertEqual(counts["complete"], 2)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["in_flight"], 3)  # pending + uploading + blank
        # Invariant: received == complete + failed + in_flight
        self.assertEqual(
            counts["received"], counts["complete"] + counts["failed"] + counts["in_flight"]
        )

    def test_unknown_state_counts_as_in_flight_not_dropped(self) -> None:
        counts = self._counts([{"processing_state": "quarantined"}])
        self.assertEqual(counts["received"], 1)
        self.assertEqual(counts["in_flight"], 1)
        self.assertEqual(counts["complete"], 0)
        self.assertEqual(counts["failed"], 0)

    def test_breakdown_accents_failures_and_neutral_zero(self) -> None:
        accented = site_detail._capture_processing_breakdown(
            {"received": 3, "complete": 2, "failed": 1, "in_flight": 0}
        )
        # Failed > 0 renders the danger accent badge.
        self.assertIn("count-danger", accented)

        neutral = site_detail._capture_processing_breakdown(
            {"received": 2, "complete": 2, "failed": 0, "in_flight": 0}
        )
        # Failed == 0 renders a neutral zero — no danger accent anywhere in the breakdown.
        self.assertNotIn("count-danger", neutral)
        self.assertIn("count-neutral", neutral)
        # And the accented case genuinely uses the danger badge class.
        self.assertIn("count-danger", accented)

    def test_none_counts_renders_nothing(self) -> None:
        self.assertEqual(site_detail._capture_processing_breakdown(None), "")


# --------------------------------------------------------------------------
# Part 3 — graceful degradation: render() never raises out
# --------------------------------------------------------------------------
class GracefulDegradationTests(unittest.TestCase):
    def test_counts_helper_degrades_when_config_raises(self) -> None:
        with mock.patch.object(site_detail, "_cdb", side_effect=RuntimeError("no couch")):
            self.assertIsNone(site_detail._site_capture_processing_counts("SANDBOX"))

    def _render_with_breakdown_couch_raising(self):
        """Render /sites/SANDBOX with ONLY the per-site breakdown helper's couch
        config lookup raising — everything else on the page loads normally."""
        tmp = tempfile.mkdtemp()
        ctx = SimpleNamespace(
            runtime_root=Path(tmp),
            query={},
            config=SimpleNamespace(vault_root=Path(tmp) / "vault"),
        )
        # Builtin SANDBOX doc supplies the page; isolate the breakdown helper's
        # couch lookup as the only thing that explodes.
        return ctx, mock.patch.multiple(
            site_detail,
            _load_location=mock.DEFAULT,
            _related_data=mock.DEFAULT,
            _site_capture_records=mock.DEFAULT,
        )

    def test_render_still_emits_h1_when_capture_counts_raise(self) -> None:
        ctx, patches = self._render_with_breakdown_couch_raising()
        with patches as mocks, mock.patch.object(
            site_detail, "_demo_mode", return_value=False
        ), mock.patch.object(
            site_detail.couchdb_config,
            "field_captures_database",
            side_effect=RuntimeError("field-captures config exploded"),
        ):
            mocks["_load_location"].return_value = None  # forces builtin SANDBOX doc
            mocks["_related_data"].return_value = {
                "notes": [],
                "employee_rows": [],
                "opportunity_rows": [],
                "visit_rows": [],
                "recent_visits": [],
            }
            mocks["_site_capture_records"].return_value = ([], False, 0)
            result = site_detail.render(ctx, "SANDBOX")
        # The page renders fully — H1 present, breakdown simply omitted, NO error shell.
        self.assertIn("<h1>Sandbox Site</h1>", result)
        self.assertNotIn('<section class="error">', result)
        # The upload-confirmation breakdown is absent (helper returned None -> "").
        self.assertNotIn("Field capture upload confirmation", result)


def _json_docs(docs) -> bytes:
    import json as _json

    return _json.dumps(docs).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
