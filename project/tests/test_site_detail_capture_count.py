"""Gating test for 381 — site-detail "Field captures" counts distinct captures, not photos.

A photo_vision_sidecar is one-per-photo; the metric is labelled "Field captures", so the
count must be distinct capture_id, not len(sidecars).
"""

import unittest
from unittest.mock import patch

from ops_dashboard.sections import field_photos, site_detail


def _sidecar(capture_id, photo_asset_id):
    d = {"photo_asset_id": photo_asset_id, "generated_at": "2026-06-01T00:00:00Z"}
    if capture_id is not None:
        d["capture_id"] = capture_id
    return d


class SiteCaptureCountTest(unittest.TestCase):
    def _run(self, sidecars):
        with patch.object(field_photos, "_photo_vision_couchdb_config", return_value=object()), \
             patch.object(field_photos, "_query_couchdb", return_value=sidecars):
            return site_detail._site_capture_records(object(), "1337")

    def test_counts_distinct_captures_not_photos(self) -> None:
        sidecars = [
            _sidecar("capA", "p1"),
            _sidecar("capA", "p2"),   # same capture, second photo
            _sidecar("capB", "p3"),
            _sidecar(None, "p4"),     # no capture_id — must not inflate
        ]
        records, fallback, count = self._run(sidecars)
        self.assertEqual(count, 2, "should count 2 distinct captures (capA, capB), not 4 photos")
        self.assertFalse(fallback)

    def test_gallery_slice_unchanged(self) -> None:
        sidecars = [_sidecar(f"cap{i}", f"p{i}") for i in range(20)]
        records, _, count = self._run(sidecars)
        self.assertEqual(count, 20)
        self.assertEqual(len(records), site_detail._CAPTURE_GALLERY_LIMIT)

    def test_empty_returns_zero(self) -> None:
        records, fallback, count = self._run([])
        self.assertEqual(count, 0)
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
