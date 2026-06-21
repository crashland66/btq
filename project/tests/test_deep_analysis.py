from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from field_capture import deep_analysis
from field_capture import photo_vision


# ----------------------------------------------------------------------------
# Stubs
# ----------------------------------------------------------------------------


class StubVisionClient:
    """Records the (path, prompt) it was asked to describe and returns a canned dict."""

    provider = "local"
    model = "stub-vlm"

    def __init__(self, result: dict | None = None, raises: Exception | None = None) -> None:
        self.result = result if result is not None else {"description": "canned", "confidence": 0.9}
        self.raises = raises
        self.calls: list[tuple[Path, str]] = []

    def describe(self, image_path, prompt):  # signature: (Path, str) -> dict
        self.calls.append((Path(image_path), prompt))
        if self.raises is not None:
            raise self.raises
        # Prove the temp file actually carries the image bytes the store returned.
        assert Path(image_path).exists(), "vision client must receive a real temp file path"
        return dict(self.result)


class StubMediaStore:
    """`.read(key)` returns canned PNG bytes; records the keys requested."""

    # 1x1 PNG.
    PNG = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises
        self.keys: list[str] = []

    def read(self, key: str) -> bytes:
        self.keys.append(key)
        if self.raises is not None:
            raise self.raises
        return self.PNG


def _fixed_now() -> datetime:
    return datetime(2026, 6, 21, 12, 30, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------------------
# Fixture: a real intake job + media so discover_photo_assets resolves the asset
# ----------------------------------------------------------------------------


def _seed_capture(runtime: Path) -> tuple[str, str, photo_vision.FieldPhotoAsset]:
    """Write a real photo_capture intake job + media file. Returns (runtime, upload_root, asset)."""
    cap = "cap-deep-1"
    date = "2026-06-21"
    filename = "photo1.jpg"
    intake_dir = runtime / "field_capture" / "intake"
    uploads = runtime / "uploads"
    intake_dir.mkdir(parents=True, exist_ok=True)
    media_dir = uploads / date / cap
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / filename).write_bytes(b"\xff\xd8\xff\xd9")
    stored_path = f"{date}/{cap}/{filename}"
    intake = {
        "job_type": "photo_capture",
        "metadata": {"capture_id": cap, "site_id": "SANDBOX"},
        "payload": {
            "site": "SANDBOX",
            "captured_at": f"{date}T12:00:00Z",
            "area": "restroom",
            "phase": "after",
            "photos": [
                {"filename": filename, "stored_path": stored_path, "upload_id": stored_path}
            ],
        },
    }
    (intake_dir / f"{cap}.json").write_text(json.dumps(intake))

    assets = photo_vision.discover_photo_assets(intake_dir, uploads, capture_id=cap)
    assert assets, "fixture should yield a discovered asset"
    return cap, uploads, assets[0]


def _run(runtime, upload_root, asset, *, vision, store, put, **kw):
    return deep_analysis.run_deep_analysis(
        capture_id=asset.capture_id,
        photo_asset_id=asset.photo_asset_id,
        actor="operator-greg",
        runtime_root=runtime,
        upload_root=upload_root,
        vision_client=vision,
        media_store=store,
        couchdb_put=put,
        now=_fixed_now,
        **kw,
    )


def _metric_lines(runtime: Path) -> list[dict]:
    path = runtime / "metrics" / deep_analysis.DEEP_ANALYSIS_METRICS_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ----------------------------------------------------------------------------
# 1. Prompt library
# ----------------------------------------------------------------------------


def test_seven_presets_with_stable_ids_labels_nonempty_prompts():
    presets = deep_analysis.DEEP_ANALYSIS_PRESETS
    assert len(presets) == 7
    ids = [p["id"] for p in presets]
    assert ids == [
        "condition_detail",
        "damage_hazard",
        "cleanliness_qc",
        "text_ocr",
        "inventory_count",
        "equipment_id",
        "shift_report",
    ]
    assert len(set(ids)) == 7
    for p in presets:
        assert p["label"].strip(), f"{p['id']} must have a non-empty label"
        assert p["prompt"].strip(), f"{p['id']} must have a non-empty prompt"
    # spot-check a couple of stable labels
    by_id = {p["id"]: p for p in presets}
    assert by_id["text_ocr"]["label"] == "Read text (log book / label / meter)"
    assert by_id["shift_report"]["label"] == "Describe in detail for the shift report"


# ----------------------------------------------------------------------------
# 2. resolve_prompt
# ----------------------------------------------------------------------------


def test_resolve_prompt_each_preset_returns_its_text():
    for p in deep_analysis.DEEP_ANALYSIS_PRESETS:
        prompt_id, prompt_text = deep_analysis.resolve_prompt(p["id"], None)
        assert prompt_id == p["id"]
        assert prompt_text == p["prompt"]


def test_resolve_prompt_custom_trims():
    prompt_id, prompt_text = deep_analysis.resolve_prompt("custom", "  count the chairs  ")
    assert prompt_id == "custom"
    assert prompt_text == "count the chairs"


def test_resolve_prompt_none_id_with_custom_text():
    prompt_id, prompt_text = deep_analysis.resolve_prompt(None, "  whatever  ")
    assert prompt_id == "custom"
    assert prompt_text == "whatever"


def test_resolve_prompt_unknown_id_raises():
    with pytest.raises(deep_analysis.DeepAnalysisError):
        deep_analysis.resolve_prompt("not_a_real_preset", None)


def test_resolve_prompt_empty_custom_raises():
    with pytest.raises(deep_analysis.DeepAnalysisError):
        deep_analysis.resolve_prompt("custom", "   ")
    with pytest.raises(deep_analysis.DeepAnalysisError):
        deep_analysis.resolve_prompt(None, None)


# ----------------------------------------------------------------------------
# 3. run_deep_analysis — completed path
# ----------------------------------------------------------------------------


def test_run_completed_describes_with_resolved_prompt_and_returns_entry(tmp_path):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)
    vision = StubVisionClient(result={"description": "rich detail", "confidence": 0.7})
    store = StubMediaStore()
    puts: list[dict] = []

    entry = _run(runtime, upload_root, asset, vision=vision, store=store, put=puts.append,
                 preset_id="condition_detail")

    # describe() received the RESOLVED preset prompt text (not the id, not a fixed string).
    assert len(vision.calls) == 1
    _, prompt_seen = vision.calls[0]
    expected = next(p["prompt"] for p in deep_analysis.DEEP_ANALYSIS_PRESETS if p["id"] == "condition_detail")
    assert prompt_seen == expected

    # store fetched the image by the asset's media key.
    assert store.keys == [f"2026-06-21/{cap}/photo1.jpg"]

    # completed entry shape.
    assert entry["status"] == "completed"
    assert entry["prompt_id"] == "condition_detail"
    assert entry["prompt_text"] == expected
    assert entry["result"] == {"description": "rich detail", "confidence": 0.7}
    assert entry["actor"] == "operator-greg"
    assert entry["generated_at"] == "2026-06-21T12:30:00Z"
    assert entry["label"] == "Detailed condition assessment"


def test_run_completed_appends_to_sidecar_list_and_puts_doc(tmp_path):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)
    puts: list[dict] = []

    entry = _run(runtime, upload_root, asset, vision=StubVisionClient(), store=StubMediaStore(),
                 put=puts.append, preset_id="text_ocr")

    sidecar_path = photo_vision.photo_vision_path_for(
        photo_vision.default_photo_vision_dir(runtime), asset.photo_asset_id
    )
    sidecar = json.loads(sidecar_path.read_text())
    assert isinstance(sidecar["deep_analysis"], list)
    assert len(sidecar["deep_analysis"]) == 1
    assert sidecar["deep_analysis"][0] == entry

    # The injected couchdb_put received the built document carrying the list.
    assert len(puts) == 1
    assert isinstance(puts[0]["deep_analysis"], list)
    assert len(puts[0]["deep_analysis"]) == 1


def test_run_completed_writes_exactly_one_metric(tmp_path):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)

    _run(runtime, upload_root, asset, vision=StubVisionClient(), store=StubMediaStore(),
         put=lambda d: None, preset_id="damage_hazard")

    lines = _metric_lines(runtime)
    assert len(lines) == 1
    m = lines[0]
    assert m["capture_id"] == cap
    assert m["photo_asset_id"] == asset.photo_asset_id
    assert m["prompt_id"] == "damage_hazard"
    assert m["is_custom"] is False
    assert m["status"] == "completed"
    assert m["actor"] == "operator-greg"
    assert "duration_ms" in m
    assert "model_provider" in m
    assert "model_name" in m


# ----------------------------------------------------------------------------
# 4. History preservation — second run appends
# ----------------------------------------------------------------------------


def test_second_run_appends_and_preserves_prior_entry_and_base_fields(tmp_path):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)

    # Seed a base vision sidecar with real base fields BEFORE any deep analysis.
    sidecar_path = photo_vision.photo_vision_path_for(
        photo_vision.default_photo_vision_dir(runtime), asset.photo_asset_id
    )
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "artifact_type": photo_vision.ARTIFACT_TYPE,
        "capture_id": cap,
        "photo_asset_id": asset.photo_asset_id,
        "description": "BASE VISION DESCRIPTION",
        "area_guess": "restroom",
        "status": "completed",
    }
    sidecar_path.write_text(json.dumps(base))

    first = _run(runtime, upload_root, asset, vision=StubVisionClient(result={"description": "first"}),
                 store=StubMediaStore(), put=lambda d: None, preset_id="condition_detail")
    second = _run(runtime, upload_root, asset, vision=StubVisionClient(result={"description": "second"}),
                  store=StubMediaStore(), put=lambda d: None, custom_prompt="custom one")

    sidecar = json.loads(sidecar_path.read_text())
    assert len(sidecar["deep_analysis"]) == 2
    assert sidecar["deep_analysis"][0] == first
    assert sidecar["deep_analysis"][1] == second
    assert sidecar["deep_analysis"][0]["result"] == {"description": "first"}
    assert sidecar["deep_analysis"][1]["result"] == {"description": "second"}
    # Base vision fields preserved.
    assert sidecar["description"] == "BASE VISION DESCRIPTION"
    assert sidecar["area_guess"] == "restroom"

    # Two distinct metric events.
    assert len(_metric_lines(runtime)) == 2


# ----------------------------------------------------------------------------
# 5. Custom prompt path
# ----------------------------------------------------------------------------


def test_custom_prompt_path(tmp_path):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)
    vision = StubVisionClient()

    entry = _run(runtime, upload_root, asset, vision=vision, store=StubMediaStore(),
                 put=lambda d: None, custom_prompt="  How many mop buckets?  ")

    assert entry["prompt_id"] == "custom"
    assert entry["prompt_text"] == "How many mop buckets?"
    assert entry["status"] == "completed"
    # No preset label for custom.
    assert "label" not in entry
    _, prompt_seen = vision.calls[0]
    assert prompt_seen == "How many mop buckets?"

    lines = _metric_lines(runtime)
    assert len(lines) == 1
    assert lines[0]["is_custom"] is True
    assert lines[0]["prompt_id"] == "custom"


# ----------------------------------------------------------------------------
# 6. Fail-closed
# ----------------------------------------------------------------------------


def test_media_read_failure_returns_failed_entry_logs_metric_no_raise(tmp_path):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)
    store = StubMediaStore(raises=OSError("media gone"))

    entry = _run(runtime, upload_root, asset, vision=StubVisionClient(), store=store,
                 put=lambda d: None, preset_id="cleanliness_qc")

    assert entry["status"] == "failed"
    assert entry["prompt_id"] == "cleanliness_qc"
    lines = _metric_lines(runtime)
    assert len(lines) == 1
    assert lines[0]["status"] == "failed"
    assert "duration_ms" in lines[0]


def test_describe_failure_returns_failed_entry_logs_metric_no_raise(tmp_path):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)
    vision = StubVisionClient(raises=RuntimeError("model exploded"))

    entry = _run(runtime, upload_root, asset, vision=vision, store=StubMediaStore(),
                 put=lambda d: None, preset_id="equipment_id")

    assert entry["status"] == "failed"
    # A failed metric is still recorded.
    lines = _metric_lines(runtime)
    assert len(lines) == 1
    assert lines[0]["status"] == "failed"
    assert lines[0]["prompt_id"] == "equipment_id"


# ----------------------------------------------------------------------------
# 7. build_photo_vision_document back-compat
# ----------------------------------------------------------------------------


def test_build_doc_backcompat_no_deep_analysis():
    from field_capture.photo_vision_couchdb import build_photo_vision_document

    payload = {
        "photo_asset_id": "fcp_x",
        "capture_id": "cap-x",
        "description": "base",
        "status": "completed",
    }
    doc = build_photo_vision_document(payload)
    assert "deep_analysis" not in doc
    assert doc["_id"] == "fcp_x"
    assert doc["description"] == "base"


def test_build_doc_carries_deep_analysis_list_when_present():
    from field_capture.photo_vision_couchdb import build_photo_vision_document

    entries = [{"prompt_id": "text_ocr", "status": "completed"}]
    payload = {
        "photo_asset_id": "fcp_x",
        "capture_id": "cap-x",
        "description": "base",
        "deep_analysis": entries,
    }
    doc = build_photo_vision_document(payload)
    assert doc["deep_analysis"] == entries


def test_build_doc_ignores_non_list_deep_analysis():
    from field_capture.photo_vision_couchdb import build_photo_vision_document

    payload = {
        "photo_asset_id": "fcp_x",
        "capture_id": "cap-x",
        "deep_analysis": {"not": "a list"},
    }
    doc = build_photo_vision_document(payload)
    assert "deep_analysis" not in doc
