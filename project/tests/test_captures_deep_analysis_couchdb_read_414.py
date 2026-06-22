"""Verifier-authored gating tests for prompt 414.

The captures ops-dashboard section now reads deep-analysis from CouchDB when
photo-vision CouchDB is CONFIGURED, in ONE batched call per rendered set of
captures, matching each photo-vision record to its doc by photo_asset_id (then
photo_id). When CouchDB is UNCONFIGURED, or the config/query raises, captures
must fall back to the local sidecar reader with identical behavior and must
never crash the page.

Tests drive captures.photo_card_records_for_capture() directly (the function
that owns the deep-analysis read), seeding orphan sidecars whose asset is not
discovered so the records flow through deterministically without depending on
intake-metadata discovery keying.

Mutation checks (tamper the impl -> a gating test reddens -> restore):
  1. Force the Couch branch to always fall back (make
     _query_deep_analysis_docs_by_capture_ids return None) ->
     test_couch_path_renders_couch_deep_analysis_and_skips_sidecar fails.
  2. Break the photo_asset_id match (compare doc["_id"] to the wrong field) ->
     test_record_maps_to_correct_doc_by_photo_asset_id fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ops_dashboard.sections import captures
from processing_core.artifacts import write_json_object


CAPTURE_ID = "cap_da_414"


def _write_orphan_sidecar(
    runtime_root: Path,
    *,
    photo_asset_id: str,
    photo_id: str = "",
    deep_analysis: list | None = None,
) -> Path:
    """Write a photo-vision sidecar whose asset is NOT discovered (no intake/
    upload media for it), so photo_card_records_for_capture routes it through
    the orphan-append branch as a real record keyed off the sidecar fields."""
    sidecar = {
        "type": "field_capture_photo_vision",
        "status": "completed",
        "photo_asset_id": photo_asset_id,
        "photo_id": photo_id,
        "capture_id": CAPTURE_ID,
        "site_id": "7050",
        "area_guess": "restroom",
        "description": "A clean restroom.",
    }
    if deep_analysis is not None:
        sidecar["deep_analysis"] = deep_analysis
    path = runtime_root / "field_capture" / "photo_vision" / f"{photo_asset_id}.json"
    write_json_object(path, sidecar)
    return path


def _deep_entry(prompt_id: str, result: str) -> dict[str, object]:
    return {
        "preset_id": prompt_id,
        "prompt_id": prompt_id,
        "result": result,
        "generated_at": "2026-06-21T12:00:00Z",
        "model_name": "test-model",
    }


# ---------------------------------------------------------------------------
# CouchDB-configured path
# ---------------------------------------------------------------------------


def test_couch_path_renders_couch_deep_analysis_and_skips_sidecar(monkeypatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    # Sidecar on disk carries a DIFFERENT deep_analysis so we can prove the
    # Couch value (not the disk value) is what gets surfaced.
    _write_orphan_sidecar(
        runtime_root,
        photo_asset_id="fcp_couch_a",
        deep_analysis=[_deep_entry("disk", "FROM_DISK_SIDECAR")],
    )

    monkeypatch.setattr(captures, "_photo_vision_couchdb_config", lambda: object())

    query_calls: list[list[str]] = []

    def fake_query(config, capture_ids):
        query_calls.append(list(capture_ids))
        return {
            CAPTURE_ID: [
                {
                    "_id": "fcp_couch_a",
                    "photo_asset_id": "fcp_couch_a",
                    "capture_id": CAPTURE_ID,
                    "deep_analysis": [_deep_entry("couch", "FROM_COUCHDB_DOC")],
                }
            ]
        }

    monkeypatch.setattr(captures, "query_photo_vision_by_capture_ids", fake_query)

    # The local sidecar reader must NOT be touched on the Couch path.
    def boom_sidecar(*_a, **_k):
        raise AssertionError("local sidecar reader must not run on the CouchDB path")

    monkeypatch.setattr(captures, "_deep_analysis_entries_for_sidecar", boom_sidecar)

    records = captures.photo_card_records_for_capture(runtime_root, CAPTURE_ID)

    rec = next(r for r in records if r.get("photo_asset_id") == "fcp_couch_a")
    entries = rec.get("deep_analysis")
    assert isinstance(entries, list) and entries, "Couch deep_analysis not surfaced"
    assert entries[0]["result"] == "FROM_COUCHDB_DOC"
    assert entries[0]["result"] != "FROM_DISK_SIDECAR"
    # Batched: exactly ONE Couch query for the rendered captures (no N+1).
    assert len(query_calls) == 1, f"expected one batched Couch query, got {query_calls}"
    assert query_calls[0] == [CAPTURE_ID]


def test_couch_query_called_once_for_multiple_photos(monkeypatch, tmp_path: Path) -> None:
    """A capture with several photos must still trigger only ONE Couch query."""
    runtime_root = tmp_path / "runtime"
    for asset in ("fcp_m1", "fcp_m2", "fcp_m3"):
        _write_orphan_sidecar(runtime_root, photo_asset_id=asset)

    monkeypatch.setattr(captures, "_photo_vision_couchdb_config", lambda: object())

    query_calls: list[list[str]] = []

    def fake_query(config, capture_ids):
        query_calls.append(list(capture_ids))
        return {}

    monkeypatch.setattr(captures, "query_photo_vision_by_capture_ids", fake_query)
    monkeypatch.setattr(
        captures,
        "_deep_analysis_entries_for_sidecar",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("sidecar reader ran on Couch path")),
    )

    captures.photo_card_records_for_capture(runtime_root, CAPTURE_ID)

    assert len(query_calls) == 1, f"expected one batched query for 3 photos, got {len(query_calls)}"


# ---------------------------------------------------------------------------
# Record -> doc matching (photo_asset_id, then photo_id)
# ---------------------------------------------------------------------------


def test_record_maps_to_correct_doc_by_photo_asset_id(monkeypatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _write_orphan_sidecar(runtime_root, photo_asset_id="fcp_a")
    _write_orphan_sidecar(runtime_root, photo_asset_id="fcp_b")

    monkeypatch.setattr(captures, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr(
        captures,
        "query_photo_vision_by_capture_ids",
        lambda _c, _ids: {
            CAPTURE_ID: [
                {"_id": "fcp_a", "photo_asset_id": "fcp_a", "capture_id": CAPTURE_ID,
                 "deep_analysis": [_deep_entry("p", "RESULT_FOR_A")]},
                {"_id": "fcp_b", "photo_asset_id": "fcp_b", "capture_id": CAPTURE_ID,
                 "deep_analysis": [_deep_entry("p", "RESULT_FOR_B")]},
            ]
        },
    )
    monkeypatch.setattr(captures, "_deep_analysis_entries_for_sidecar", lambda *_a, **_k: [])

    records = captures.photo_card_records_for_capture(runtime_root, CAPTURE_ID)
    by_asset = {r.get("photo_asset_id"): r for r in records}

    assert by_asset["fcp_a"]["deep_analysis"][0]["result"] == "RESULT_FOR_A"
    assert by_asset["fcp_b"]["deep_analysis"][0]["result"] == "RESULT_FOR_B"


def test_record_falls_back_to_photo_id_match(monkeypatch, tmp_path: Path) -> None:
    """When photo_asset_id does not match any doc, matching falls back to
    photo_id."""
    runtime_root = tmp_path / "runtime"
    _write_orphan_sidecar(runtime_root, photo_asset_id="fcp_assetX", photo_id="pid_42")

    monkeypatch.setattr(captures, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr(
        captures,
        "query_photo_vision_by_capture_ids",
        lambda _c, _ids: {
            CAPTURE_ID: [
                # asset id differs (no asset match) but photo_id matches.
                {"_id": "other_doc", "photo_asset_id": "other_doc", "photo_id": "pid_42",
                 "capture_id": CAPTURE_ID, "deep_analysis": [_deep_entry("p", "MATCHED_BY_PHOTO_ID")]},
            ]
        },
    )
    monkeypatch.setattr(captures, "_deep_analysis_entries_for_sidecar", lambda *_a, **_k: [])

    records = captures.photo_card_records_for_capture(runtime_root, CAPTURE_ID)
    rec = next(r for r in records if r.get("photo_asset_id") == "fcp_assetX")
    assert rec["deep_analysis"][0]["result"] == "MATCHED_BY_PHOTO_ID"


# ---------------------------------------------------------------------------
# Fallback path: unconfigured / query raises -> local sidecar reader
# ---------------------------------------------------------------------------


def test_fallback_reads_sidecar_when_couch_unconfigured(tmp_path: Path) -> None:
    """Default test env unsets BTQ_COUCHDB_URL (autouse fixture), so config is
    None -> captures reads deep_analysis from the local sidecar on disk."""
    runtime_root = tmp_path / "runtime"
    _write_orphan_sidecar(
        runtime_root,
        photo_asset_id="fcp_disk_only",
        deep_analysis=[_deep_entry("disk", "FROM_DISK_FALLBACK")],
    )

    records = captures.photo_card_records_for_capture(runtime_root, CAPTURE_ID)
    rec = next(r for r in records if r.get("photo_asset_id") == "fcp_disk_only")
    entries = rec.get("deep_analysis")
    assert isinstance(entries, list) and entries
    assert entries[0]["result"] == "FROM_DISK_FALLBACK"


def test_fallback_uses_sidecar_reader_function_when_unconfigured(monkeypatch, tmp_path: Path) -> None:
    """Prove the sidecar reader IS the path used when Couch is unconfigured."""
    runtime_root = tmp_path / "runtime"
    _write_orphan_sidecar(runtime_root, photo_asset_id="fcp_uses_reader")

    calls: list[object] = []
    real_reader = captures._deep_analysis_entries_for_sidecar

    def spy(sidecar):
        calls.append(sidecar)
        return real_reader(sidecar)

    monkeypatch.setattr(captures, "_deep_analysis_entries_for_sidecar", spy)
    # Couch query MUST NOT run when unconfigured.
    monkeypatch.setattr(
        captures,
        "query_photo_vision_by_capture_ids",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Couch query ran while unconfigured")),
    )

    captures.photo_card_records_for_capture(runtime_root, CAPTURE_ID)
    assert calls, "sidecar reader was not used on the unconfigured/fallback path"


def test_fallback_when_query_raises_does_not_crash(monkeypatch, tmp_path: Path) -> None:
    """Configured, but the Couch query raises -> fall back to sidecar reader,
    page still renders (no exception escapes)."""
    runtime_root = tmp_path / "runtime"
    _write_orphan_sidecar(
        runtime_root,
        photo_asset_id="fcp_query_raises",
        deep_analysis=[_deep_entry("disk", "FROM_DISK_ON_QUERY_FAILURE")],
    )

    monkeypatch.setattr(captures, "_photo_vision_couchdb_config", lambda: object())

    def exploding_query(_config, _ids):
        raise RuntimeError("CouchDB unreachable")

    monkeypatch.setattr(captures, "query_photo_vision_by_capture_ids", exploding_query)

    # Must not raise.
    records = captures.photo_card_records_for_capture(runtime_root, CAPTURE_ID)
    rec = next(r for r in records if r.get("photo_asset_id") == "fcp_query_raises")
    assert rec["deep_analysis"][0]["result"] == "FROM_DISK_ON_QUERY_FAILURE"


def test_fallback_when_config_raises_does_not_crash(monkeypatch, tmp_path: Path) -> None:
    """Config gate itself raises -> fall back to sidecar, no 500."""
    runtime_root = tmp_path / "runtime"
    _write_orphan_sidecar(
        runtime_root,
        photo_asset_id="fcp_config_raises",
        deep_analysis=[_deep_entry("disk", "FROM_DISK_ON_CONFIG_FAILURE")],
    )

    def exploding_config():
        raise RuntimeError("env misconfigured")

    monkeypatch.setattr(captures, "_photo_vision_couchdb_config", exploding_config)
    monkeypatch.setattr(
        captures,
        "query_photo_vision_by_capture_ids",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("query ran after config raised")),
    )

    records = captures.photo_card_records_for_capture(runtime_root, CAPTURE_ID)
    rec = next(r for r in records if r.get("photo_asset_id") == "fcp_config_raises")
    assert rec["deep_analysis"][0]["result"] == "FROM_DISK_ON_CONFIG_FAILURE"
