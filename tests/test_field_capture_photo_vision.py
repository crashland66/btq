from __future__ import annotations

import json
from pathlib import Path

import pytest

from event_pipeline import sites as event_sites
from event_pipeline.couchdb_registry import CouchDBRegistryError
from field_capture import photo_vision


@pytest.fixture(autouse=True)
def no_couchdb_registry(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    monkeypatch.setattr(event_sites, "_registry_instance", None)


def write_capture_with_photo(
    intake_dir: Path,
    upload_dir: Path,
    *,
    capture_id: str = "cap-photo-1",
    site_id: str = "7050",
    qc_category: str = "Offices / Classrooms / Exam Rooms",
    filename: str = "wide-reset.jpg",
    image_bytes: bytes = b"image bytes",
    write_image: bool = True,
) -> Path:
    image_path = upload_dir / "2026-05-06" / capture_id / filename
    if write_image:
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(image_bytes)
    job = {
        "job_id": f"photo-job-{capture_id}",
        "job_type": "photo_capture",
        "metadata": {
            "capture_id": capture_id,
            "site_id": site_id,
        },
        "payload": {
            "site": "Summit Wire",
            "qc_category": qc_category,
            "phase": "completed",
            "captured_at": "2026-05-06T08:15:00-04:00",
            "exported_at": "2026-05-06T08:15:05-04:00",
            "photos": [
                {
                    "filename": filename,
                    "mime_type": "image/jpeg",
                    "stored_path": str(image_path),
                    "upload_id": f"2026-05-06/{capture_id}/{filename}",
                    "size_bytes": len(image_bytes),
                }
            ],
            "audio": [],
        },
    }
    intake_dir.mkdir(parents=True, exist_ok=True)
    (intake_dir / f"{capture_id}.json").write_text(json.dumps(job), encoding="utf-8")
    return image_path


class FakeVisionClient:
    provider = "local-test"
    engine_name = "local-test:vision"

    def __init__(self) -> None:
        self.calls: list[photo_vision.FieldPhotoAsset] = []

    def __call__(self, asset: photo_vision.FieldPhotoAsset) -> photo_vision.VisionDescription:
        self.calls.append(asset)
        return photo_vision.VisionDescription(
            description="A wide office area appears reset with a trash bin visible.",
            area_guess="office",
            visible_objects=["desk", "trash bin"],
            possible_conditions=["appears reset"],
            possible_issues=["possible paper towels low"],
            confidence=0.72,
            needs_human_review=True,
            warnings=[],
        )


class LaneAwareVisionClient(FakeVisionClient):
    def __init__(self) -> None:
        super().__init__()
        self.qc_category_calls: list[tuple[photo_vision.FieldPhotoAsset, object]] = []

    def describe_for_qc_category(
        self,
        asset: photo_vision.FieldPhotoAsset,
        qc_category: object,
    ) -> photo_vision.VisionDescription:
        self.qc_category_calls.append((asset, qc_category))
        return photo_vision.VisionDescription(
            description="A QC capture photo is visible.",
            area_guess="office",
            visible_objects=["desk"],
            possible_conditions=[],
            possible_issues=[],
            confidence=0.8,
            needs_human_review=False,
            warnings=[],
        )


class MalformedVisionClient(FakeVisionClient):
    engine_name = "local-test:malformed"

    def __call__(self, asset: photo_vision.FieldPhotoAsset) -> photo_vision.VisionDescription:
        self.calls.append(asset)
        raise ValueError("model did not return strict JSON")


class TimeoutVisionClient(FakeVisionClient):
    engine_name = "local-test:timeout"
    timeout_seconds = 12.5

    def __call__(self, asset: photo_vision.FieldPhotoAsset) -> photo_vision.VisionDescription:
        self.calls.append(asset)
        raise photo_vision.VisionModelTimeoutError("local Ollama vision request timed out after 12.5 seconds")


class JudgyVisionClient(FakeVisionClient):
    engine_name = "local-test:judgy"

    def __call__(self, asset: photo_vision.FieldPhotoAsset) -> photo_vision.VisionDescription:
        self.calls.append(asset)
        return photo_vision.VisionDescription(
            description="The restroom cleanliness is poor and needs cleaning.",
            area_guess="Restrooms",
            visible_objects=["toilet", "cleaning supplies"],
            possible_conditions=["Cleanliness", "dirty floor"],
            possible_issues=["General tidiness", "needs attention"],
            confidence=0.55,
            needs_human_review=True,
            warnings=["needs cleaning"],
        )


class FakeVisionRegistry:
    def __init__(self, context: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.context = context
        self.error = error
        self.calls: list[str] = []

    def get_vision_context(self, site_id: str) -> dict[str, object] | None:
        self.calls.append(site_id)
        if self.error is not None:
            raise self.error
        return self.context


def install_summit_vision_registry(monkeypatch) -> FakeVisionRegistry:
    registry = FakeVisionRegistry(
        {
            "context_id": "7050",
            "label": "Summit Wire",
            "environment": "industrial manufacturing / wire facility",
            "summary": "industrial manufacturing / wire facility context for visible-facts-only photo descriptions.",
        }
    )
    monkeypatch.setattr(event_sites, "_get_registry", lambda: registry)
    return registry


def joined_payload_text(payload: dict[str, object]) -> str:
    fields = [
        str(payload.get("description") or ""),
        str(payload.get("area_guess") or ""),
        " ".join(str(item) for item in payload.get("visible_objects", [])),
        " ".join(str(item) for item in payload.get("possible_conditions", [])),
        " ".join(str(item) for item in payload.get("possible_issues", [])),
        " ".join(str(item) for item in payload.get("warnings", [])),
    ]
    return " ".join(fields)


def test_site_vision_context_for_returns_none_without_registry(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    monkeypatch.setattr(event_sites, "_registry_instance", None)

    assert photo_vision.site_vision_context_for("7050") is None


def test_site_vision_context_for_uses_couchdb_registry(monkeypatch) -> None:
    registry = FakeVisionRegistry(
        {
            "context_id": "7050",
            "label": "Summit Wire",
            "environment": "industrial manufacturing / wire facility",
            "summary": "Industrial facility context for visible-facts-only photo descriptions.",
        }
    )
    monkeypatch.setattr(event_sites, "_get_registry", lambda: registry)

    context = photo_vision.site_vision_context_for("7050")

    assert context == photo_vision.SiteVisionContext(
        site_context_id="7050",
        site_context_name="Summit Wire",
        facility_type="industrial manufacturing / wire facility",
        context="Industrial facility context for visible-facts-only photo descriptions.",
    )
    assert registry.calls == ["7050"]


def test_site_vision_context_for_logs_and_returns_none_on_registry_error(monkeypatch, caplog) -> None:
    registry = FakeVisionRegistry(error=CouchDBRegistryError("registry unavailable"))
    monkeypatch.setattr(event_sites, "_get_registry", lambda: registry)
    caplog.set_level("WARNING", logger="field_capture.photo_vision")

    assert photo_vision.site_vision_context_for("7050") is None

    assert registry.calls == ["7050"]
    assert "CouchDB site vision context unavailable" in caplog.text
    assert "7050" in caplog.text
    assert "registry unavailable" in caplog.text


def test_prompt_invites_candid_condition_assessment_and_keeps_privacy(tmp_path: Path, monkeypatch) -> None:
    install_summit_vision_registry(monkeypatch)
    intake_dir = tmp_path / "runtime" / "field_capture" / "intake"
    upload_dir = tmp_path / "runtime" / "uploads"
    write_capture_with_photo(intake_dir, upload_dir)
    asset = photo_vision.discover_photo_assets(intake_dir, upload_dir)[0]

    prompt = photo_vision.prompt_for(asset)

    # Anti-hallucination is kept.
    assert "Describe only what is visible" in prompt
    # Candid-mode instruction is present.
    assert "Assess the condition directly and honestly" in prompt
    assert "Do not soften, hedge, sanitize, or omit observations" in prompt
    # Privacy is kept; people are still flagged, not described.
    assert "do not identify any specific person" in prompt
    # The old judgment gag is gone.
    assert "Do not rate, judge, score" not in prompt
    assert "cleanliness, tidy, tidiness, dirty, clean, cleaner" not in prompt
    assert "Do not say needs cleaning." not in prompt
    assert "must be visible facts only" not in prompt
    # Site context still threaded in.
    assert "Summit Wire" in prompt
    assert "industrial manufacturing / wire facility" in prompt
    assert "Do not invent details from the context" in prompt


def test_prompt_contains_screenshot_app_ui_guard(tmp_path: Path) -> None:
    intake_dir = tmp_path / "runtime" / "field_capture" / "intake"
    upload_dir = tmp_path / "runtime" / "uploads"
    write_capture_with_photo(intake_dir, upload_dir)
    asset = photo_vision.discover_photo_assets(intake_dir, upload_dir)[0]

    prompt = photo_vision.prompt_for(asset)

    assert "phone screenshot, an app screen, a browser/computer UI, or an error message/dialog" in prompt
    assert f'"{photo_vision.SCREENSHOT_OR_APP_UI_WARNING}" in warnings' in prompt
    assert 'set area_guess to "other"' in prompt
    assert 'include "unanalyzable" in quality_flags' in prompt
    assert "set possible_issues to []" in prompt
    assert "Do not classify thumbnails embedded inside the screenshot as the actual field scene." in prompt
    assert "Example output for a phone screenshot or app UI" in prompt
    assert f'"warnings": ["{photo_vision.SCREENSHOT_OR_APP_UI_WARNING}"]' in prompt
    assert '"quality_flags": ["unanalyzable"]' in prompt


def test_prompt_uses_fallback_context_for_unknown_site(tmp_path: Path) -> None:
    intake_dir = tmp_path / "runtime" / "field_capture" / "intake"
    upload_dir = tmp_path / "runtime" / "uploads"
    write_capture_with_photo(intake_dir, upload_dir, site_id="9999")
    asset = photo_vision.discover_photo_assets(intake_dir, upload_dir)[0]

    prompt = photo_vision.prompt_for(asset)

    assert "No site-specific background context is available." in prompt
    assert "Do not invent site details." in prompt
    assert "Describe only what is visible" in prompt


def test_vision_lane_for_uses_canonical_qc_category() -> None:
    assert photo_vision.vision_lane_for("qc") == "qc"
    assert photo_vision.vision_lane_for("Restrooms") == "pow"
    assert photo_vision.vision_lane_for("Offices / Classrooms / Exam Rooms") == "pow"
    assert photo_vision.vision_lane_for("") == "pow"
    assert photo_vision.vision_lane_for(None) == "pow"


def test_pow_prompt_is_byte_identical_when_category_is_threaded(tmp_path: Path) -> None:
    intake_dir = tmp_path / "runtime" / "field_capture" / "intake"
    upload_dir = tmp_path / "runtime" / "uploads"
    write_capture_with_photo(intake_dir, upload_dir, qc_category="Restrooms")
    asset = photo_vision.discover_photo_assets(intake_dir, upload_dir)[0]

    legacy_prompt = photo_vision.prompt_for(asset)
    threaded_pow_prompt = photo_vision.prompt_for(asset, asset.qc_category)

    assert threaded_pow_prompt == legacy_prompt


def test_taxonomy_prompt_block_is_injected_only_for_qc_lane(tmp_path: Path, monkeypatch) -> None:
    intake_dir = tmp_path / "runtime" / "field_capture" / "intake"
    upload_dir = tmp_path / "runtime" / "uploads"
    write_capture_with_photo(intake_dir, upload_dir, qc_category="qc")
    asset = photo_vision.discover_photo_assets(intake_dir, upload_dir)[0]
    monkeypatch.setattr(photo_vision, "defect_taxonomy_prompt_block", lambda _qc_category: "QC TAXONOMY BLOCK\n")

    qc_prompt = photo_vision.prompt_for(asset, "qc")
    pow_prompt = photo_vision.prompt_for(asset, "Restrooms")

    assert "QC TAXONOMY BLOCK\nHard rules:" in qc_prompt
    assert "QC TAXONOMY BLOCK" not in pow_prompt
    assert pow_prompt == photo_vision.prompt_for(asset)


def test_ollama_client_rejects_non_local_endpoints() -> None:
    for url in (
        "https://api.openai.com/v1/images",
        "https://vision.googleapis.com/v1/images:annotate",
        "https://api.anthropic.com",
        "https://generativelanguage.googleapis.com",
        "http://example.com:11434",
    ):
        try:
            photo_vision.OllamaVisionClient(model="qwen2.5vl:7b", ollama_url=url)
        except ValueError as exc:
            assert "local Ollama" in str(exc)
        else:
            raise AssertionError(f"non-local endpoint was accepted: {url}")

    client = photo_vision.OllamaVisionClient(model="qwen2.5vl:7b", ollama_url="http://127.0.0.1:11434")
    assert client.ollama_url == "http://127.0.0.1:11434"


def test_timeout_config_is_honored(monkeypatch) -> None:
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_VISION_TIMEOUT_SECONDS", "222")

    assert photo_vision.default_timeout_seconds() == 222.0
    client = photo_vision.OllamaVisionClient(model="qwen2.5vl:7b", ollama_url="http://127.0.0.1:11434", timeout_seconds=33.0)
    assert client.timeout_seconds == 33.0


def test_ollama_qc_lane_threads_category_into_prompt(tmp_path: Path, monkeypatch) -> None:
    captured_categories: list[object] = []
    client = photo_vision.OllamaVisionClient(model="gemma4:26b", ollama_url="http://127.0.0.1:11434")

    def capture_prompt(_asset: photo_vision.FieldPhotoAsset, qc_category: object | None = None) -> str:
        captured_categories.append(qc_category)
        return "PROMPT"

    monkeypatch.setattr(photo_vision, "prompt_for", capture_prompt)
    monkeypatch.setattr(
        client._client,
        "describe",
        lambda _image_path, _prompt: {
            "description": "A QC photo.",
            "area_guess": "office",
            "visible_objects": [],
            "possible_conditions": [],
            "possible_issues": [],
            "confidence": 0.8,
            "needs_human_review": False,
            "warnings": [],
        },
    )

    client.describe_for_qc_category(_vision_asset(tmp_path), "qc")

    assert captured_categories == ["qc"]


def _vision_asset(tmp_path: Path) -> photo_vision.FieldPhotoAsset:
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"fake-image-bytes")
    return photo_vision.FieldPhotoAsset(
        capture_id="cap", site_id="1", area="kitchen", phase="",
        photo_asset_id="fcp_test", photo_id="p", filename="photo.jpg",
        image_path=image_path, image_media_url="", mime_type="image/jpeg",
        size_bytes=16, intake_json_path=tmp_path / "intake.json", captured_at="",
    )


def test_ollama_client_retries_on_malformed_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(photo_vision, "prompt_for", lambda _asset: "PROMPT")
    good = json.dumps({
        "description": "A double-basin sink.", "area_guess": "kitchen",
        "visible_objects": ["sink"], "possible_conditions": [],
        "possible_issues": [], "confidence": 0.8,
        "needs_human_review": False, "warnings": [],
    })
    responses = iter(['{"description": "x" "area_guess": "kitchen"}', good])
    client = photo_vision.OllamaVisionClient(model="gemma4:26b", ollama_url="http://127.0.0.1:11434")
    # _request_response now lives on the shared vision_backends core client the
    # field adapter delegates to (client._client).
    monkeypatch.setattr(client._client, "_request_response", lambda prompt, image_b64: next(responses))

    result = client(_vision_asset(tmp_path))

    assert result.description == "A double-basin sink."
    assert result.visible_objects == ["sink"]


def test_ollama_client_raises_after_exhausting_json_retries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(photo_vision, "prompt_for", lambda _asset: "PROMPT")
    attempts: list[int] = []

    def always_bad(prompt: str, image_b64: str) -> str:
        attempts.append(1)
        return "{not valid json"

    client = photo_vision.OllamaVisionClient(model="gemma4:26b", ollama_url="http://127.0.0.1:11434")
    monkeypatch.setattr(client._client, "_request_response", always_bad)

    with pytest.raises(json.JSONDecodeError):
        client(_vision_asset(tmp_path))
    assert len(attempts) == photo_vision._MAX_VISION_JSON_ATTEMPTS


def test_image_from_intake_produces_vision_sidecar_with_provenance(tmp_path: Path, monkeypatch) -> None:
    install_summit_vision_registry(monkeypatch)
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    image_path = write_capture_with_photo(intake_dir, upload_dir, image_bytes=b"stable image")
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, client, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 1, "failed": 0, "skipped": 0}
    assert len(client.calls) == 1
    sidecars = sorted(vision_dir.glob("*.json"))
    assert len(sidecars) == 1
    payload = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "field_capture_photo_vision"
    assert payload["type"] == "field_capture_photo_vision"
    assert payload["status"] == "completed"
    assert payload["capture_id"] == "cap-photo-1"
    assert payload["photo_id"] == "2026-05-06/cap-photo-1/wide-reset.jpg"
    assert payload["photo_asset_id"].startswith("fcp_")
    assert payload["source_image_path"] == str(image_path.resolve(strict=False))
    assert payload["source_image_hash"] == "75f1f0ae740173b33a405289d2ad0fa4f0261401a6510a64c071b75c3e45d656"
    assert payload["site_id"] == "7050"
    assert payload["qc_category"] == "Offices / Classrooms / Exam Rooms"
    assert payload["vision_lane"] == "pow"
    assert payload["submitted_area"] == "Offices / Classrooms / Exam Rooms"
    assert payload["submitted_phase"] == "completed"
    assert payload["site_context_used"] is True
    assert payload["site_context_id"] == "7050"
    assert payload["site_context_name"] == "Summit Wire"
    assert "industrial manufacturing" in payload["site_context_summary"]
    assert payload["model_name"] == "local-test:vision"
    assert payload["model_provider"] == "local-test"
    assert payload["description"].startswith("A wide office area appears")
    assert payload["area_guess"] == "office"
    assert payload["vision_category"] == "Offices / Classrooms / Exam Rooms"
    assert payload["category_agreement"] == "match"
    assert payload["visible_objects"] == ["desk", "trash bin"]
    assert payload["possible_conditions"] == ["appears reset"]
    assert payload["possible_issues"] == ["possible paper towels low"]
    assert payload["confidence"] == 0.72
    assert payload["needs_human_review"] is True
    assert "intake_json_path" in payload["provenance"]
    assert payload["provenance"]["capture_id"] == "cap-photo-1"
    assert payload["provenance"]["photo_id"] == "2026-05-06/cap-photo-1/wide-reset.jpg"
    assert payload["provenance"]["source_image_hash"] == payload["source_image_hash"]
    assert photo_vision.ADVISORY_WARNING in payload["warnings"]


def test_qc_capture_routes_through_qc_lane_and_marks_sidecar(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir, qc_category="qc")
    client = LaneAwareVisionClient()

    counts = photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, client, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 1, "failed": 0, "skipped": 0}
    assert client.calls == []
    assert [(asset.qc_category, qc_category) for asset, qc_category in client.qc_category_calls] == [("qc", "qc")]
    payload = json.loads(next(vision_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["qc_category"] == "qc"
    assert payload["vision_lane"] == "qc"
    assert payload["status"] == "completed"


def test_photo_vision_couchdb_document_carries_lane_and_defaults_absent_to_pow() -> None:
    from field_capture.photo_vision_couchdb import build_photo_vision_document

    sidecar = {
        "photo_asset_id": "fcp_lane",
        "status": "completed",
        "qc_category": "qc",
        "vision_lane": "qc",
    }

    doc = build_photo_vision_document(sidecar)
    legacy_sidecar = {key: value for key, value in sidecar.items() if key != "vision_lane"}
    fallback_doc = build_photo_vision_document({**legacy_sidecar, "photo_asset_id": "fcp_legacy"})

    assert doc["vision_lane"] == "qc"
    assert fallback_doc["vision_lane"] == "pow"


def test_judgment_language_is_preserved_not_neutralized(tmp_path: Path) -> None:
    # Candid mode (2026-06-08): the vision model's own assessment of condition is
    # passed through verbatim — no word-scrubbing, no JUDGMENT_LANGUAGE_REMOVED flag.
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)

    counts = photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, JudgyVisionClient(), runtime_root=runtime_root)

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 1, "failed": 0, "skipped": 0}
    payload = json.loads(next(vision_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["description"] == "The restroom cleanliness is poor and needs cleaning."
    assert payload["area_guess"] == "Restrooms"
    assert payload["vision_category"] == "Restrooms"
    assert payload["category_agreement"] == "mismatch"
    assert payload["visible_objects"] == ["toilet", "cleaning supplies"]
    assert payload["possible_conditions"] == ["Cleanliness", "dirty floor"]
    assert payload["possible_issues"] == ["General tidiness", "needs attention"]
    assert payload["confidence"] == 0.55
    # Candid language survives; the removed-language warning is no longer emitted.
    assert photo_vision.JUDGMENT_LANGUAGE_REMOVED_WARNING not in payload["warnings"]
    assert "needs cleaning" in payload["warnings"]
    # Advisory framing (vision is advisory, human review authoritative) is retained.
    assert photo_vision.ADVISORY_WARNING in payload["warnings"]


def test_concrete_visible_facts_pass_without_sanitizer_warning() -> None:
    description = photo_vision.VisionDescription(
        description="Visible paper on floor near the doorway.",
        area_guess="Entryway",
        visible_objects=["paper", "doorway", "floor"],
        possible_conditions=["visible streaks on mirror", "trash liner appears full"],
        possible_issues=["supplies visible on counter"],
        confidence=0.81,
        needs_human_review=True,
        warnings=[],
    )

    sanitized = photo_vision.sanitize_vision_description(description)

    assert sanitized.description == description.description
    assert sanitized.possible_conditions == description.possible_conditions
    assert sanitized.possible_issues == description.possible_issues
    assert photo_vision.JUDGMENT_LANGUAGE_REMOVED_WARNING not in sanitized.warnings
    assert photo_vision.ADVISORY_WARNING in sanitized.warnings


def test_visible_objects_are_deduped_case_insensitively() -> None:
    description = photo_vision.VisionDescription(
        description="A double-basin sink with two soap dispensers and two cabinets.",
        area_guess="kitchen",
        visible_objects=["sink", "sink", "Soap dispenser", "soap dispenser", "countertop", "cabinet", "cabinet"],
        possible_conditions=[],
        possible_issues=[],
        confidence=0.95,
        needs_human_review=False,
        warnings=[],
    )

    sanitized = photo_vision.sanitize_vision_description(description)

    assert sanitized.visible_objects == ["sink", "Soap dispenser", "countertop", "cabinet"]
    assert photo_vision.JUDGMENT_LANGUAGE_REMOVED_WARNING not in sanitized.warnings


def test_existing_sidecar_is_skipped(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)
    client = FakeVisionClient()
    photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, client, runtime_root=runtime_root)
    second = FakeVisionClient()

    counts = photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, second, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 0, "failed": 0, "skipped": 1}
    assert second.calls == []
    assert len(list(vision_dir.glob("*.json"))) == 1


def test_photo_asset_id_processes_only_requested_asset(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir, capture_id="cap-a", filename="a.jpg")
    write_capture_with_photo(intake_dir, upload_dir, capture_id="cap-b", filename="b.jpg")
    assets = photo_vision.discover_photo_assets(intake_dir, upload_dir)
    requested = next(asset for asset in assets if asset.capture_id == "cap-b")
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(
        intake_dir,
        upload_dir,
        vision_dir,
        client,
        runtime_root=runtime_root,
        photo_asset_id=requested.photo_asset_id,
    )

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 1, "failed": 0, "skipped": 0}
    assert [call.photo_asset_id for call in client.calls] == [requested.photo_asset_id]
    assert sorted(path.stem for path in vision_dir.glob("*.json")) == [requested.photo_asset_id]


def test_photo_asset_id_missing_asset_fails_closed(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)

    try:
        photo_vision.process_photo_assets(
            intake_dir,
            upload_dir,
            vision_dir,
            FakeVisionClient(),
            runtime_root=runtime_root,
            photo_asset_id="fcp_missing",
        )
    except ValueError as exc:
        assert "photo asset not found" in str(exc)
    else:
        raise AssertionError("missing photo asset id should fail closed")
    assert not vision_dir.exists()


def test_replace_failed_regenerates_failed_sidecar_only(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir, capture_id="cap-failed", filename="failed.jpg")
    write_capture_with_photo(intake_dir, upload_dir, capture_id="cap-complete", filename="complete.jpg")
    photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, MalformedVisionClient(), runtime_root=runtime_root, capture_id="cap-failed")
    photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, FakeVisionClient(), runtime_root=runtime_root, capture_id="cap-complete")
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(
        intake_dir,
        upload_dir,
        vision_dir,
        client,
        runtime_root=runtime_root,
        replace_failed=True,
    )

    assert counts == {"discovered": 2, "would_create": 0, "would_replace": 0, "completed": 1, "failed": 0, "skipped": 1}
    assert [call.capture_id for call in client.calls] == ["cap-failed"]
    failed_asset = next(asset for asset in photo_vision.discover_photo_assets(intake_dir, upload_dir) if asset.capture_id == "cap-failed")
    payload = json.loads(photo_vision.photo_vision_path_for(vision_dir, failed_asset.photo_asset_id).read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["replaced_existing_sidecar"] is True
    assert payload["replacement_reason"] == "failed"
    assert payload["previous_status"] == "failed"


def test_replace_failed_does_not_replace_completed_sidecar(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)
    photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, FakeVisionClient(), runtime_root=runtime_root)
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, client, runtime_root=runtime_root, replace_failed=True)

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 0, "failed": 0, "skipped": 1}
    assert client.calls == []


def test_flagged_judgment_language_replacement_is_noop_in_candid_mode(tmp_path: Path) -> None:
    # Candid mode (2026-06-08): condition words like "Cleanliness"/"need for cleaning"
    # are no longer "flagged", so the opt-in reprocess pass leaves the sidecar alone.
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)
    photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, FakeVisionClient(), runtime_root=runtime_root)
    sidecar_path = next(vision_dir.glob("*.json"))
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["possible_conditions"] = ["Cleanliness"]
    payload["possible_issues"] = ["need for cleaning"]
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(
        intake_dir,
        upload_dir,
        vision_dir,
        client,
        runtime_root=runtime_root,
        replace_flagged_judgment_language=True,
    )

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 0, "failed": 0, "skipped": 1}
    assert client.calls == []
    # The injected condition language is left intact.
    after = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert after["possible_conditions"] == ["Cleanliness"]
    assert after["possible_issues"] == ["need for cleaning"]


def test_replace_flagged_judgment_language_does_not_replace_clean_completed_sidecar(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)
    photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, FakeVisionClient(), runtime_root=runtime_root)
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(
        intake_dir,
        upload_dir,
        vision_dir,
        client,
        runtime_root=runtime_root,
        replace_flagged_judgment_language=True,
    )

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 0, "failed": 0, "skipped": 1}
    assert client.calls == []


def test_dry_run_with_replacement_flags_reports_would_replace_and_writes_nothing(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)
    photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, MalformedVisionClient(), runtime_root=runtime_root)
    sidecar_path = next(vision_dir.glob("*.json"))
    before = sidecar_path.read_text(encoding="utf-8")
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(
        intake_dir,
        upload_dir,
        vision_dir,
        client,
        runtime_root=runtime_root,
        dry_run=True,
        replace_failed=True,
    )

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 1, "completed": 0, "failed": 0, "skipped": 0}
    assert client.calls == []
    assert sidecar_path.read_text(encoding="utf-8") == before


def test_advisory_warning_text_avoids_search_noisy_terms() -> None:
    noisy_terms = ("rating", "score", "quality", "cleanliness", "judgment", "judge", "best")

    assert not any(term in photo_vision.ADVISORY_WARNING.lower() for term in noisy_terms)


def test_sanitizer_preserves_candid_condition_language() -> None:
    # Candid mode (2026-06-08): the sanitizer no longer rewrites condition phrases;
    # it only appends the advisory framing and de-dupes lists.
    description = photo_vision.VisionDescription(
        description="The blinds may need cleaning and the vent requires cleaning.",
        area_guess="Office",
        visible_objects=["blinds", "vent"],
        possible_conditions=["might need cleaning", "should be cleaned"],
        possible_issues=["needs cleaning"],
        confidence=0.5,
        needs_human_review=True,
        warnings=[],
    )

    sanitized = photo_vision.sanitize_vision_description(description)

    assert sanitized.description == "The blinds may need cleaning and the vent requires cleaning."
    assert sanitized.possible_conditions == ["might need cleaning", "should be cleaned"]
    assert sanitized.possible_issues == ["needs cleaning"]
    assert photo_vision.JUDGMENT_LANGUAGE_REMOVED_WARNING not in sanitized.warnings
    assert photo_vision.ADVISORY_WARNING in sanitized.warnings


def test_missing_image_fails_closed_with_failed_artifact(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir, write_image=False)
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, client, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 0, "failed": 1, "skipped": 0}
    assert client.calls == []
    payload = json.loads(next(vision_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["source_image_hash"] == ""
    assert payload["vision_category"] is None
    assert payload["category_agreement"] == "unverifiable"
    assert payload["needs_human_review"] is True
    assert any("failed closed" in warning for warning in payload["warnings"])


def test_malformed_model_output_creates_failed_artifact(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)
    client = MalformedVisionClient()

    counts = photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, client, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 0, "failed": 1, "skipped": 0}
    assert len(client.calls) == 1
    payload = json.loads(next(vision_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["model_name"] == "local-test:malformed"
    assert any("strict JSON" in warning for warning in payload["warnings"])
    assert payload["error"]["type"] == "ValueError"
    assert payload["error"]["can_retry"] is False


def test_timeout_failure_writes_retryable_error_metadata(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)
    client = TimeoutVisionClient()

    counts = photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, client, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 0, "failed": 1, "skipped": 0}
    payload = json.loads(next(vision_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"] == {
        "type": "timeout",
        "message": "local Ollama vision request timed out after 12.5 seconds",
        "model_name": "local-test:timeout",
        "timeout_seconds": 12.5,
        "can_retry": True,
    }


def test_timeout_failed_sidecar_is_eligible_for_replace_failed(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)
    photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, TimeoutVisionClient(), runtime_root=runtime_root)
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(
        intake_dir,
        upload_dir,
        vision_dir,
        client,
        runtime_root=runtime_root,
        replace_failed=True,
    )

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 1, "failed": 0, "skipped": 0}
    assert len(client.calls) == 1
    payload = json.loads(next(vision_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["replaced_existing_sidecar"] is True
    assert payload["replacement_reason"] == "failed"
    assert payload["previous_status"] == "failed"


def test_dry_run_writes_nothing_and_does_not_call_vision(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir)
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, client, runtime_root=runtime_root, dry_run=True)

    assert counts == {"discovered": 1, "would_create": 1, "would_replace": 0, "completed": 0, "failed": 0, "skipped": 0}
    assert client.calls == []
    assert not vision_dir.exists()


def test_filters_and_limit_are_applied_before_model_call(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    write_capture_with_photo(intake_dir, upload_dir, capture_id="cap-a", site_id="7050", filename="a.jpg")
    write_capture_with_photo(intake_dir, upload_dir, capture_id="cap-b", site_id="9999", filename="b.jpg")
    client = FakeVisionClient()

    counts = photo_vision.process_photo_assets(
        intake_dir,
        upload_dir,
        vision_dir,
        client,
        runtime_root=runtime_root,
        site_id="7050",
        date="2026-05-06",
        limit=1,
    )

    assert counts == {"discovered": 1, "would_create": 0, "would_replace": 0, "completed": 1, "failed": 0, "skipped": 0}
    assert [call.capture_id for call in client.calls] == ["cap-a"]


def test_photo_vision_does_not_write_queue_reviews_or_vault_and_does_not_call_cloud(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads"
    vision_dir = runtime_root / "field_capture" / "photo_vision"
    vault_root = tmp_path / "vault"
    write_capture_with_photo(intake_dir, upload_dir)

    def fail_cloud_call(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("tests must not call network or cloud APIs")

    # The HTTP call now lives in the shared vision_backends core, not photo_vision.
    monkeypatch.setattr(photo_vision.vision_backends.request, "urlopen", fail_cloud_call)

    counts = photo_vision.process_photo_assets(intake_dir, upload_dir, vision_dir, FakeVisionClient(), runtime_root=runtime_root)

    assert counts["completed"] == 1
    assert not (runtime_root / "queue").exists()
    assert not (runtime_root / "reviews" / "action_candidates" / "field_capture").exists()
    assert not (runtime_root / "reviews" / "approved_drafts" / "field_capture").exists()
    assert not (runtime_root / "reviews" / "staging" / "field_capture").exists()
    assert not vault_root.exists()
