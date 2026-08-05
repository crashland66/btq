"""Gates for the two-pass vision strategy (rich description + classification).

Contract: pass 1 writes free operator-grade prose (no JSON constraint); pass 2
makes the structured judgment with that prose in context, JSON-prefilled so a
thinking model's reasoning never eats the token budget. The merged result is a
standard VisionDescription whose description field carries the rich prose —
downstream consumers see the same shape. Default strategy stays single-pass
until the env flips.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import vision_backends
from field_capture import photo_vision
from field_capture.photo_vision import (
    FieldPhotoAsset,
    TwoPassMlxVisionClient,
    build_mlx_vision_client,
    classification_prompt_for,
    rich_description_prompt_for,
    vision_strategy,
)
from field_capture.photo_vision_categories import derive_vision_category_fields


def _asset(**overrides: object) -> FieldPhotoAsset:
    values: dict = {
        "capture_id": "cap-1",
        "site_id": "7050",
        "area": "",
        "phase": "",
        "photo_asset_id": "pa-1",
        "photo_id": "p1",
        "filename": "photo.jpg",
        "image_path": Path("/tmp/photo.jpg"),
        "image_media_url": "",
        "mime_type": "image/jpeg",
        "size_bytes": 100,
        "intake_json_path": Path("/tmp/intake.json"),
        "captured_at": "2026-07-28T12:00:00Z",
        "qc_category": "Restrooms",
    }
    values.update(overrides)
    return FieldPhotoAsset(**values)


@pytest.fixture()
def industrial_context(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        photo_vision,
        "site_vision_context_for",
        lambda _sid: photo_vision.SiteVisionContext(
            site_context_id="7050",
            site_context_name="Maple Plaza",
            facility_type="Industrial",
            context="Manufacturing floor with attached offices.",
        ),
    )


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def test_rich_prompt_is_prose_only_with_context(industrial_context) -> None:
    prompt = rich_description_prompt_for(_asset(), "Restrooms")
    assert "facility_type: Industrial" in prompt
    assert "filed this photo under the category: Restrooms" in prompt
    assert "4-8 sentences" in prompt
    assert "no JSON" in prompt
    # The prose pass must NOT carry the structured schema.
    assert "Return strict JSON" not in prompt
    assert "area_guess" not in prompt
    # Privacy and honesty rules survive the redesign.
    assert "do not identify any specific person" in prompt
    assert "be candid" in prompt.lower()


def test_classification_prompt_embeds_description_and_category_labels(
    industrial_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        photo_vision,
        "default_qc_categories",
        lambda: [
            {"canonical": "Restrooms", "label": "Restrooms"},
            {"canonical": "Hallways", "label": "Hallways"},
        ],
    )
    prose = "A tiled restroom with two urinals; the floor is visibly dusty near the drain."
    prompt = classification_prompt_for(_asset(), "Restrooms", prose)
    # MUTATION GUARD: the pass-1 prose is the evidence for the judgment.
    assert prose in prompt
    # Direct category selection from the active label set.
    assert "Restrooms; Hallways" in prompt
    assert "EXACT label" in prompt
    # Structured schema present, description key absent (it comes from pass 1).
    assert "area_guess (string)" in prompt
    assert "description (string)" not in prompt
    assert "facility_type: Industrial" in prompt


def test_exact_category_label_area_guess_maps_directly() -> None:
    # The classify prompt tells the model to answer with an exact category
    # label; this pins the mapping machinery that promise relies on.
    categories = [{"canonical": "Restrooms", "label": "Restrooms"}]
    fields = derive_vision_category_fields("Restrooms", "Restrooms", categories)
    assert fields["vision_category"] == "Restrooms"
    assert fields["category_agreement"] == "match"


# ---------------------------------------------------------------------------
# Two-pass client behavior
# ---------------------------------------------------------------------------

class _FakeInnerClient:
    def __init__(self) -> None:
        self.model = "fake-model"
        self.model_path = "fake-model"
        self.max_tokens = 512
        self.engine_name = "mlx:fake-model"
        self.describe_calls: list[dict] = []

    def generate_text(self, image_path: Path, prompt: str, *, response_prefix: str = "") -> str:
        self.prose_prompt = prompt
        self.prose_response_prefix = response_prefix
        return "Rich prose: a dusty industrial restroom with two urinals and a streaked mirror."

    def describe(self, image_path: Path, prompt: str, *, json_prefill: bool = False) -> dict:
        self.describe_calls.append({"prompt": prompt, "json_prefill": json_prefill})
        return {
            "area_guess": "Restrooms",
            "visible_objects": ["urinal", "mirror"],
            "possible_conditions": ["dust on floor"],
            "possible_issues": ["mirror is streaked"],
            "confidence": 0.8,
            "needs_human_review": False,
            "warnings": [],
            "quality_flags": [],
        }


def _two_pass_with_fake(monkeypatch: pytest.MonkeyPatch) -> tuple[TwoPassMlxVisionClient, _FakeInnerClient]:
    client = TwoPassMlxVisionClient.__new__(TwoPassMlxVisionClient)
    fake = _FakeInnerClient()
    client._client = fake
    client.model = fake.model
    client.engine_name = "mlx:fake-model:two-pass"
    return client, fake


def test_two_pass_merges_prose_into_description(
    industrial_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = _two_pass_with_fake(monkeypatch)

    description = client(_asset())

    # MUTATION GUARD: the rich prose IS the description field downstream.
    assert description.description.startswith("Rich prose: a dusty industrial restroom")
    assert description.area_guess == "Restrooms"
    assert description.possible_issues == ["mirror is streaked"]
    # The classify pass saw the prose and ran JSON-prefilled.
    assert len(fake.describe_calls) == 1
    assert "Rich prose: a dusty industrial restroom" in fake.describe_calls[0]["prompt"]
    assert fake.describe_calls[0]["json_prefill"] is True
    # MUTATION GUARD: the prose pass runs with the response prefix so a
    # thinking model describes instead of planning out loud.
    assert fake.prose_response_prefix == "This photo shows"


def test_two_pass_result_is_standard_shape(industrial_context, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake = _two_pass_with_fake(monkeypatch)
    description = client.describe_for_qc_category(_asset(), "Restrooms")
    assert isinstance(description, photo_vision.VisionDescription)
    # normalize_vision_description ran (advisory warning appended).
    assert any("advisory" in w.lower() for w in description.warnings)


# ---------------------------------------------------------------------------
# Planning-leak detection, retry, and salvage
# ---------------------------------------------------------------------------

# Condensed from a real production leak (Liberty Wire trash-can photo,
# 2026-07-28): the model's numbered work-through arrived as the description.
LEAKED_PROSE = (
    "This photo shows a corner of a restroom. 1. **Identify the main object:** "
    "A grey, rectangular trash can with a lid. 2. **Identify the lid:** open. "
    "*Draft:* This image shows a corner of a restroom containing a grey trash "
    "receptacle. *Final check:* 4-8 sentences? Yes."
)
CLEAN_PROSE = (
    "This photo shows a restroom corner with a grey pedal-operated trash "
    "receptacle, its lid open and a black liner sitting loose inside. The "
    "vanity cabinet to the left is clean, and the wood-look floor shows no debris."
)


def test_planning_leak_detector() -> None:
    assert photo_vision.prose_planning_leak(LEAKED_PROSE)
    assert photo_vision.prose_planning_leak("1. Identify the object\n2. Draft it")
    assert not photo_vision.prose_planning_leak(CLEAN_PROSE)


def test_sanitize_leaked_prose_keeps_only_prose_lines() -> None:
    text = "**Plan:** describe the bin\nThe trash receptacle is empty and clean.\n1. check floor\nThe floor shows no debris."
    cleaned = photo_vision.sanitize_leaked_prose(text)
    assert "The trash receptacle is empty and clean." in cleaned
    assert "The floor shows no debris." in cleaned
    assert "**" not in cleaned
    assert "1." not in cleaned


class _LeakySequenceClient(_FakeInnerClient):
    def __init__(self, prose_sequence: list[str]) -> None:
        super().__init__()
        self._prose_sequence = list(prose_sequence)
        self.prose_calls = 0

    def generate_text(self, image_path: Path, prompt: str, *, response_prefix: str = "") -> str:
        self.prose_calls += 1
        return self._prose_sequence.pop(0)


def test_leaked_prose_retries_once_and_uses_clean_result(
    industrial_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MUTATION GUARD: one leak -> one retry -> clean result, no review flag.
    client, _ = _two_pass_with_fake(monkeypatch)
    fake = _LeakySequenceClient([LEAKED_PROSE, CLEAN_PROSE])
    client._client = fake

    description = client(_asset())

    assert fake.prose_calls == 2
    assert description.description == CLEAN_PROSE
    assert not description.needs_human_review
    assert not any("sanitized" in w for w in description.warnings)


def test_double_leak_sanitizes_and_flags_for_review(
    industrial_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _two_pass_with_fake(monkeypatch)
    fake = _LeakySequenceClient([LEAKED_PROSE, LEAKED_PROSE])
    client._client = fake

    description = client(_asset())

    assert fake.prose_calls == 2
    assert "**" not in description.description
    assert "*Draft:*" not in description.description
    assert description.needs_human_review
    assert any("sanitized" in w for w in description.warnings)


def test_clean_prose_generates_exactly_once(
    industrial_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _two_pass_with_fake(monkeypatch)
    fake = _LeakySequenceClient([CLEAN_PROSE])
    client._client = fake

    description = client(_asset())

    assert fake.prose_calls == 1
    assert description.description == CLEAN_PROSE


def test_rich_prompt_forbids_planning_out_loud(industrial_context) -> None:
    prompt = rich_description_prompt_for(_asset(), "Restrooms")
    assert "never plan, number steps, draft" in prompt


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------

class _StubSinglePass:
    def __init__(self, model: str, max_tokens: int | None = None) -> None:
        self.kind = "single"


class _StubTwoPass:
    def __init__(self, model: str, max_tokens: int | None = None) -> None:
        self.kind = "two_pass"


def test_default_strategy_is_single_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_VISION_STRATEGY", raising=False)
    assert vision_strategy() == "single"
    monkeypatch.setattr(photo_vision, "MlxVisionClient", _StubSinglePass)
    monkeypatch.setattr(photo_vision, "TwoPassMlxVisionClient", _StubTwoPass)
    assert build_mlx_vision_client("m").kind == "single"


def test_env_selects_two_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_VISION_STRATEGY", "two_pass")
    assert vision_strategy() == "two_pass"
    monkeypatch.setattr(photo_vision, "MlxVisionClient", _StubSinglePass)
    monkeypatch.setattr(photo_vision, "TwoPassMlxVisionClient", _StubTwoPass)
    assert build_mlx_vision_client("m").kind == "two_pass"


def test_unknown_strategy_falls_back_to_single(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_VISION_STRATEGY", "wild_mode")
    assert vision_strategy() == "single"


# ---------------------------------------------------------------------------
# vision_backends: JSON prefill on the vision path
# ---------------------------------------------------------------------------

def test_vision_describe_json_prefill_wires_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_backends, "_resize_image_for_mlx", lambda p: (p, False))
    client = vision_backends.MlxVisionClient.__new__(vision_backends.MlxVisionClient)
    client.max_tokens = 512
    recorded: dict[str, object] = {}

    def fake_template(_processor, _config, prompt, **_kwargs):
        return "formatted-vision-prompt"

    def fake_generate(_model, _processor, formatted, images, **_kwargs):
        recorded["formatted"] = formatted
        recorded["images"] = images
        return '"area_guess": "Restrooms", "visible_objects": [], "possible_conditions": [], "possible_issues": [], "confidence": 0.9, "needs_human_review": false, "warnings": [], "quality_flags": []}'

    client._apply_chat_template = fake_template
    client._config = {}
    client._processor = object()
    client._model = object()
    client._generate = fake_generate

    parsed = client.describe(tmp_path / "photo.jpg", "prompt", json_prefill=True)

    # MUTATION GUARD: the formatted prompt ends with the prefilled brace and
    # the reply is reassembled into a complete object.
    assert str(recorded["formatted"]).endswith("{")
    assert parsed["area_guess"] == "Restrooms"


def test_vision_describe_without_prefill_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_backends, "_resize_image_for_mlx", lambda p: (p, False))
    client = vision_backends.MlxVisionClient.__new__(vision_backends.MlxVisionClient)
    client.max_tokens = 512
    recorded: dict[str, object] = {}

    def fake_generate(_model, _processor, formatted, images, **_kwargs):
        recorded["formatted"] = formatted
        return '{"area_guess": "hallway"}'

    client._apply_chat_template = lambda *_a, **_k: "formatted-vision-prompt"
    client._config = {}
    client._processor = object()
    client._model = object()
    client._generate = fake_generate

    parsed = client.describe(tmp_path / "photo.jpg", "prompt")

    assert not str(recorded["formatted"]).endswith("{")
    assert parsed == {"area_guess": "hallway"}
