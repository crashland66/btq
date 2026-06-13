from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from field_capture.photo_vision import ADVISORY_WARNING, STATUS_COMPLETED, STATUS_FAILED
from ops_dashboard.sections.photos import (
    _build_mango_selector,
    _in_memory_filter,
    render,
)


def _make_sidecar(asset_id: str = "fcp-001", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "photo_asset_id": asset_id,
        "capture_id": "cap-001",
        "site_id": "site-001",
        "status": STATUS_COMPLETED,
        "generated_at": "2026-05-01T12:00:00Z",
        "description": "A tiled restroom with urinals.",
        "area_guess": "restroom",
        "submitted_area": "restroom",
        "submitted_phase": "completed",
        "visible_objects": ["urinal", "tile floor"],
        "possible_conditions": ["paper towel on floor"],
        "possible_issues": [],
        "confidence": 0.85,
        "needs_human_review": False,
        "warnings": [ADVISORY_WARNING],
        "quality": {
            "analyzable": True,
            "severity": "ok",
            "flags": [],
            "confidence": 0.85,
            "needs_human_review": False,
            "source": "model",
        },
        "image_media_url": "/media/photo-001",
    }
    base.update(overrides)
    return base


def _make_ctx(query: dict[str, list[str]] | None = None, runtime_root: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        query=query or {},
        runtime_root=runtime_root or Path("."),
    )


class MangoSelectorTests(unittest.TestCase):
    def test_no_filters_produces_doc_type_selector(self) -> None:
        mango = _build_mango_selector("", "", "", "", "", "", "", "", "", "")
        self.assertEqual(mango["selector"], {"doc_type": "photo_vision_sidecar"})

    def test_text_query_added(self) -> None:
        mango = _build_mango_selector("urinal", "", "", "", "", "", "", "", "", "")
        selector = mango["selector"]
        self.assertIn("$and", selector)
        clauses = selector["$and"]
        search_clause = next(c for c in clauses if "search_text" in c)
        self.assertEqual(search_clause["search_text"]["$regex"], "urinal")

    def test_severity_filter(self) -> None:
        mango = _build_mango_selector("", "", "", "unusable", "", "", "", "", "", "")
        clauses = mango["selector"]["$and"]
        self.assertIn({"quality.severity": "unusable"}, clauses)

    def test_flag_filter(self) -> None:
        mango = _build_mango_selector("", "", "", "", "blurry", "", "", "", "", "")
        clauses = mango["selector"]["$and"]
        flag_clause = next(c for c in clauses if "quality.flags" in c)
        self.assertEqual(flag_clause["quality.flags"]["$elemMatch"], {"$eq": "blurry"})

    def test_confidence_range(self) -> None:
        mango = _build_mango_selector("", "", "", "", "", "", "0.3", "0.8", "", "")
        clauses = mango["selector"]["$and"]
        conf_clause = next(c for c in clauses if "confidence" in c)
        self.assertEqual(conf_clause["confidence"]["$gte"], 0.3)
        self.assertEqual(conf_clause["confidence"]["$lte"], 0.8)

    def test_invalid_confidence_ignored(self) -> None:
        mango = _build_mango_selector("", "", "", "", "", "", "notanumber", "", "", "")
        selector = mango["selector"]
        if "$and" in selector:
            no_conf = all("confidence" not in c for c in selector["$and"])
            self.assertTrue(no_conf)

    def test_limit_applied(self) -> None:
        from ops_dashboard.sections.photos import PAGE_LIMIT
        mango = _build_mango_selector("", "", "", "", "", "", "", "", "", "")
        self.assertEqual(mango["limit"], PAGE_LIMIT + 1)


class InMemoryFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sidecars = [
            _make_sidecar("fcp-001", site_id="site-A", area_guess="restroom", status=STATUS_COMPLETED, confidence=0.9),
            _make_sidecar("fcp-002", site_id="site-B", area_guess="kitchen", status=STATUS_FAILED, confidence=0.1,
                          description="Dark kitchen.",
                          quality={"analyzable": False, "severity": "unusable", "flags": ["too_dark"], "confidence": 0.1, "needs_human_review": True, "source": "model"}),
            _make_sidecar("fcp-003", site_id="site-A", area_guess="office", status=STATUS_COMPLETED, confidence=0.5,
                          quality={"analyzable": True, "severity": "degraded", "flags": ["blurry"], "confidence": 0.5, "needs_human_review": False, "source": "model"}),
        ]

    def _filter(self, **kwargs: str) -> list[dict[str, object]]:
        return _in_memory_filter(
            self.sidecars,
            kwargs.get("q", ""),
            kwargs.get("site_id", ""),
            kwargs.get("area_guess", ""),
            kwargs.get("severity", ""),
            kwargs.get("flag", ""),
            kwargs.get("status", ""),
            kwargs.get("confidence_min", ""),
            kwargs.get("confidence_max", ""),
            kwargs.get("date_from", ""),
            kwargs.get("date_to", ""),
        )

    def test_no_filters_returns_all(self) -> None:
        self.assertEqual(len(self._filter()), 3)

    def test_text_query_matches_description(self) -> None:
        results = self._filter(q="dark kitchen")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["photo_asset_id"], "fcp-002")

    def test_site_id_filter(self) -> None:
        results = self._filter(site_id="site-A")
        self.assertEqual(len(results), 2)

    def test_area_guess_filter(self) -> None:
        results = self._filter(area_guess="restroom")
        self.assertEqual(len(results), 1)

    def test_severity_filter_unusable(self) -> None:
        results = self._filter(severity="unusable")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["photo_asset_id"], "fcp-002")

    def test_flag_filter(self) -> None:
        results = self._filter(flag="blurry")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["photo_asset_id"], "fcp-003")

    def test_status_filter(self) -> None:
        results = self._filter(status=STATUS_FAILED)
        self.assertEqual(len(results), 1)

    def test_confidence_min_filter(self) -> None:
        results = self._filter(confidence_min="0.8")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["photo_asset_id"], "fcp-001")

    def test_confidence_max_filter(self) -> None:
        results = self._filter(confidence_max="0.5")
        self.assertEqual(len(results), 2)

    def test_combined_filters(self) -> None:
        results = self._filter(site_id="site-A", severity="degraded")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["photo_asset_id"], "fcp-003")


class PhotosSectionRenderTests(unittest.TestCase):
    def test_renders_html_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp) / "field_capture" / "photo_vision"
            photo_dir.mkdir(parents=True)
            sidecar = _make_sidecar("fcp-001")
            (photo_dir / "fcp-001.json").write_text(json.dumps(sidecar), encoding="utf-8")

            ctx = _make_ctx(runtime_root=Path(tmp))
            with mock.patch.dict("os.environ", {}, clear=True):
                # No BTQ_COUCHDB_URL — falls back to disk
                with mock.patch.dict("os.environ", {"BTQ_COUCHDB_URL": ""}):
                    result = render(ctx)

        self.assertIn("Field Photo Search", result)
        self.assertIn("fcp-001", result)

    def test_renders_with_couchdb_docs(self) -> None:
        docs = [_make_sidecar("fcp-cdb-001")]
        cdb_response = {"docs": docs}

        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_ctx(runtime_root=Path(tmp))
            with mock.patch("ops_dashboard.sections.photos._photo_vision_couchdb_config", return_value=object()):
                with mock.patch("ops_dashboard.sections.photos._query_couchdb", return_value=docs):
                    result = render(ctx)

        self.assertIn("Field Photo Search", result)
        self.assertIn("fcp-cdb-001", result)

    def test_couchdb_failure_falls_back_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp) / "field_capture" / "photo_vision"
            photo_dir.mkdir(parents=True)
            sidecar = _make_sidecar("fcp-disk-001")
            (photo_dir / "fcp-disk-001.json").write_text(json.dumps(sidecar), encoding="utf-8")

            ctx = _make_ctx(runtime_root=Path(tmp))
            with mock.patch("ops_dashboard.sections.photos._photo_vision_couchdb_config", return_value=object()):
                with mock.patch("ops_dashboard.sections.photos._query_couchdb", return_value=None):
                    result = render(ctx)

        self.assertIn("CouchDB unavailable", result)
        self.assertIn("fcp-disk-001", result)

    def test_empty_results_show_zero_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp) / "field_capture" / "photo_vision"
            photo_dir.mkdir(parents=True)
            ctx = _make_ctx(runtime_root=Path(tmp))
            with mock.patch.dict("os.environ", {"BTQ_COUCHDB_URL": ""}):
                result = render(ctx)

        self.assertIn("No photos match this query", result)

    def test_severity_filter_in_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo_dir = Path(tmp) / "field_capture" / "photo_vision"
            photo_dir.mkdir(parents=True)
            ok_sidecar = _make_sidecar("fcp-ok")
            unusable_sidecar = _make_sidecar("fcp-bad", quality={"analyzable": False, "severity": "unusable", "flags": ["unanalyzable"], "confidence": None, "needs_human_review": True, "source": "model"})
            (photo_dir / "fcp-ok.json").write_text(json.dumps(ok_sidecar), encoding="utf-8")
            (photo_dir / "fcp-bad.json").write_text(json.dumps(unusable_sidecar), encoding="utf-8")

            ctx = _make_ctx(query={"severity": ["unusable"]}, runtime_root=Path(tmp))
            with mock.patch.dict("os.environ", {"BTQ_COUCHDB_URL": ""}):
                result = render(ctx)

        self.assertIn("fcp-bad", result)
        self.assertNotIn("fcp-ok", result)


if __name__ == "__main__":
    unittest.main()
