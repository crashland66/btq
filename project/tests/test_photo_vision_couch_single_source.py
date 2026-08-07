"""Gates for photo-vision single-source reads (offline-dashboard prereq #1).

Contract: CouchDB (btq_photo_vision) is the dashboard's read source for
photo-vision state — every view that consumed disk sidecars now flows through
the same loader backed by fetch_all_photo_vision_docs, with the disk scan
kept solely as a Couch-outage fallback. The mirror document stores the FULL
sidecar payload (schema 2) so fields can never silently drop again, and the
reconcile sweep upgrades the historical corpus.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from field_capture import photo_vision_couchdb as pvc
from ops_dashboard import common


# ---------------------------------------------------------------------------
# Schema 2: full payload, derived plumbing only
# ---------------------------------------------------------------------------

def test_document_is_full_sidecar_passthrough() -> None:
    # MUTATION GUARD: the subset mapping silently dropped every field added
    # after it was written (summary, quality_flags, source_image_path, site
    # context). Schema 2 must carry arbitrary sidecar fields verbatim.
    sidecar = {
        "photo_asset_id": "fcp_x",
        "status": "completed",
        "summary": "A clean restroom.",
        "quality_flags": ["blurry"],
        "source_image_path": "/uploads/p.jpg",
        "site_context_name": "Liberty Wire",
        "a_field_invented_next_year": {"nested": True},
        "_rev": "must-not-leak",
    }
    doc = pvc.build_photo_vision_document(sidecar)
    assert doc["quality_flags"] == ["blurry"]
    assert doc["source_image_path"] == "/uploads/p.jpg"
    assert doc["site_context_name"] == "Liberty Wire"
    assert doc["a_field_invented_next_year"] == {"nested": True}
    assert doc["schema_version"] == 2
    assert "_rev" not in doc
    assert "clean restroom" in doc["search_text"]


def test_document_normalizes_missing_vision_lane_to_pow() -> None:
    doc = pvc.build_photo_vision_document({"photo_asset_id": "fcp_legacy"})
    assert doc["vision_lane"] == "pow"


# ---------------------------------------------------------------------------
# Paginated fetch
# ---------------------------------------------------------------------------

def test_fetch_all_paginates_with_bookmarks(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        {"docs": [{"_id": f"fcp_{i}"} for i in range(3)], "bookmark": "bm1"},
        {"docs": [{"_id": "fcp_last"}], "bookmark": "bm2"},
    ]
    calls: list[dict] = []

    def fake_query(config, mango, *, database=None):
        calls.append(mango)
        return pages[len(calls) - 1]

    monkeypatch.setattr(pvc, "query_photo_vision", fake_query)
    docs = pvc.fetch_all_photo_vision_docs(object(), page_size=3)
    assert len(docs) == 4
    assert calls[1]["bookmark"] == "bm1"  # second page carried the bookmark


# ---------------------------------------------------------------------------
# Loader: Couch first, disk only on outage
# ---------------------------------------------------------------------------

def _clear_cache() -> None:
    common._PHOTO_VISION_CACHE.clear()


def test_loader_reads_couch_and_shapes_summaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    couch_docs = [
        {"_id": "fcp_new", "photo_asset_id": "fcp_new", "status": "completed",
         "description": "Rich prose.", "summary": "Short caption.",
         "generated_at": "2026-08-07T01:00:00Z"},
        {"_id": "fcp_old", "photo_asset_id": "fcp_old", "status": "completed",
         "generated_at": "2026-08-01T01:00:00Z"},
    ]
    monkeypatch.setattr(
        "field_capture.photo_vision_couchdb.fetch_all_photo_vision_docs",
        lambda _cfg: couch_docs,
    )
    summaries = common.load_photo_vision_sidecars(tmp_path / "photo_vision")
    # Newest first, summary carried, path constructed for the runtime viewer.
    assert [s["photo_asset_id"] for s in summaries] == ["fcp_new", "fcp_old"]
    assert summaries[0]["summary"] == "Short caption."
    assert summaries[0]["name"] == "fcp_new.json"
    assert str(tmp_path) in summaries[0]["path"]


def test_loader_falls_back_to_disk_on_couch_outage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # MUTATION GUARD: outage must degrade, not 500 — and must actually read disk.
    _clear_cache()
    disk_dir = tmp_path / "photo_vision"
    disk_dir.mkdir()
    (disk_dir / "fcp_disk.json").write_text(json.dumps({
        "photo_asset_id": "fcp_disk", "status": "completed", "description": "from disk",
    }))

    def boom(_cfg):
        raise RuntimeError("couch down")

    monkeypatch.setattr("field_capture.photo_vision_couchdb.fetch_all_photo_vision_docs", boom)
    summaries = common.load_photo_vision_sidecars(disk_dir)
    assert [s["photo_asset_id"] for s in summaries] == ["fcp_disk"]
    assert summaries[0]["description"] == "from disk"


def test_loader_caches_within_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    calls = {"n": 0}

    def counting_fetch(_cfg):
        calls["n"] += 1
        return [{"_id": "fcp_a", "photo_asset_id": "fcp_a", "generated_at": "2026-08-07"}]

    monkeypatch.setattr("field_capture.photo_vision_couchdb.fetch_all_photo_vision_docs", counting_fetch)
    directory = tmp_path / "photo_vision"
    common.load_photo_vision_sidecars(directory)
    common.load_photo_vision_sidecars(directory)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Reconcile sweep
# ---------------------------------------------------------------------------

def test_reconcile_pushes_disk_sidecars_with_revs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(3):
        (tmp_path / f"fcp_{index}.json").write_text(json.dumps({
            "photo_asset_id": f"fcp_{index}", "status": "completed", "summary": f"caption {index}",
        }))
    (tmp_path / "broken.json").write_text("{not json")

    pushed: list[dict] = []
    monkeypatch.setattr(pvc, "_bulk_revs", lambda _c, _db, ids: {"fcp_1": "3-abc"})
    def fake_bulk_put(_c, _db, docs):
        pushed.extend(docs)
        return len(docs)
    monkeypatch.setattr(pvc, "_bulk_put", fake_bulk_put)
    monkeypatch.setattr(pvc.couchdb_config, "photo_vision_database", lambda: "btq_photo_vision")

    counts = pvc.reconcile_photo_vision_from_disk(object(), tmp_path, batch_size=2)

    assert counts == {"disk_sidecars": 4, "written": 3, "unreadable": 1}
    by_id = {str(d["_id"]): d for d in pushed}
    assert by_id["fcp_1"]["_rev"] == "3-abc"  # existing doc updated in place
    assert "_rev" not in by_id["fcp_0"]  # new doc created
    assert by_id["fcp_2"]["schema_version"] == 2
