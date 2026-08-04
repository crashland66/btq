from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path

import pytest
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-reuse-installed]

import vision_backends
from field_capture import photo_vision
from field_capture.photo_vision import FieldPhotoAsset, MlxVisionClient, VisionDescription


INSTALL_HINT = "pip install 'bt-pipeline[vision-mlx]'"


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _install_fake_mlx(monkeypatch: pytest.MonkeyPatch) -> None:
    mlx_vlm = types.ModuleType("mlx_vlm")
    prompt_utils = types.ModuleType("mlx_vlm.prompt_utils")
    utils = types.ModuleType("mlx_vlm.utils")
    mlx_vlm.__path__ = []  # type: ignore[attr-defined]

    def load(model: str, *, use_fast: bool = True) -> tuple[object, object]:
        return object(), object()

    class _GenerationResult:
        # mlx-vlm >=0.6.0 returns an object with a .text attr, not a bare str.
        def __init__(self, text: str) -> None:
            self.text = text

    def generate(*args: object, **kwargs: object) -> object:
        return _GenerationResult("{}")

    def stream_generate(*args: object, **kwargs: object) -> list[object]:
        return []

    def apply_chat_template(*args: object, **kwargs: object) -> str:
        return "formatted"

    def load_config(model: str) -> dict[str, object]:
        return {}

    mlx_vlm.load = load  # type: ignore[attr-defined]
    mlx_vlm.generate = generate  # type: ignore[attr-defined]
    mlx_vlm.stream_generate = stream_generate  # type: ignore[attr-defined]
    mlx_vlm.prompt_utils = prompt_utils  # type: ignore[attr-defined]
    mlx_vlm.utils = utils  # type: ignore[attr-defined]
    prompt_utils.apply_chat_template = apply_chat_template  # type: ignore[attr-defined]
    utils.load_config = load_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.prompt_utils", prompt_utils)
    monkeypatch.setitem(sys.modules, "mlx_vlm.utils", utils)


def _asset(image_path: Path) -> FieldPhotoAsset:
    return FieldPhotoAsset(
        capture_id="capture-1",
        site_id="site-1",
        area="restroom",
        phase="after",
        photo_asset_id="fcp_1",
        photo_id="photo-1",
        filename="photo.jpg",
        image_path=image_path,
        image_media_url="/media/photo.jpg",
        mime_type="image/jpeg",
        size_bytes=image_path.stat().st_size,
        intake_json_path=image_path.with_suffix(".json"),
        captured_at="2026-05-30T00:00:00Z",
    )


def test_mlx_backend_missing_dependency_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def fail_mlx_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "mlx_vlm" or name.startswith("mlx_vlm."):
            raise ModuleNotFoundError("No module named 'mlx_vlm'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_mlx_import)

    with pytest.raises(RuntimeError) as exc_info:
        MlxVisionClient(model="mlx-community/test-model")
    assert INSTALL_HINT in str(exc_info.value)


def test_vision_backends_ollama_client_describe_returns_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"image")

    def urlopen(req: object, timeout: float) -> _Response:
        return _Response({"response": "{\"a\":1}"})

    monkeypatch.setattr(vision_backends.request, "urlopen", urlopen)

    client = vision_backends.OllamaVisionClient(model="qwen-test")
    assert client.describe(image_path, "p") == {"a": 1}


def test_vision_backends_ollama_describe_retries_then_raises_on_bad_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"image")
    calls = 0

    def urlopen(req: object, timeout: float) -> _Response:
        nonlocal calls
        calls += 1
        return _Response({"response": "not json"})

    monkeypatch.setattr(vision_backends.request, "urlopen", urlopen)

    client = vision_backends.OllamaVisionClient(model="qwen-test")
    with pytest.raises(json.JSONDecodeError):
        client.describe(image_path, "p")
    assert calls == 3


def test_vision_backends_ollama_timeout_maps_to_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"image")

    def urlopen(req: object, timeout: float) -> _Response:
        raise TimeoutError

    monkeypatch.setattr(vision_backends.request, "urlopen", urlopen)

    client = vision_backends.OllamaVisionClient(model="qwen-test")
    with pytest.raises(vision_backends.VisionModelTimeoutError):
        client.describe(image_path, "p")


def test_vision_backends_build_client_defaults_per_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_mlx(monkeypatch)

    mlx_client = vision_backends.build_vision_client("mlx")
    ollama_client = vision_backends.build_vision_client("ollama")

    assert isinstance(mlx_client, vision_backends.MlxVisionClient)
    assert mlx_client.model == vision_backends.DEFAULT_MLX_MODEL
    assert isinstance(ollama_client, vision_backends.OllamaVisionClient)
    assert ollama_client.model == vision_backends.DEFAULT_OLLAMA_MODEL


def test_mlx_text_client_uses_vlm_text_only_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}
    mlx_vlm = types.ModuleType("mlx_vlm")
    prompt_utils = types.ModuleType("mlx_vlm.prompt_utils")
    utils = types.ModuleType("mlx_vlm.utils")
    mlx_vlm.__path__ = []  # type: ignore[attr-defined]
    model_obj = object()
    processor_obj = object()

    def load(model: str, *, use_fast: bool = True) -> tuple[object, object]:
        calls["load"] = (model, use_fast)
        return model_obj, processor_obj

    class Chunk:
        def __init__(self, text: str) -> None:
            self.text = text

    def stream_generate(*args: object, **kwargs: object) -> list[Chunk]:
        calls["stream_generate"] = (args, kwargs)
        # Chunks CONTINUE the prefilled "{" (text-only + JSON prefill contract).
        return [Chunk('"issue_type"'), Chunk(':"other"}'), Chunk(" ignored")]

    def apply_chat_template(*args: object, **kwargs: object) -> str:
        calls["template"] = (args, kwargs)
        return "formatted text prompt"

    def load_config(model: str) -> dict[str, object]:
        calls["config"] = model
        return {"model": model}

    mlx_vlm.load = load  # type: ignore[attr-defined]
    mlx_vlm.stream_generate = stream_generate  # type: ignore[attr-defined]
    mlx_vlm.prompt_utils = prompt_utils  # type: ignore[attr-defined]
    mlx_vlm.utils = utils  # type: ignore[attr-defined]
    prompt_utils.apply_chat_template = apply_chat_template  # type: ignore[attr-defined]
    utils.load_config = load_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.prompt_utils", prompt_utils)
    monkeypatch.setitem(sys.modules, "mlx_vlm.utils", utils)

    client = vision_backends.MlxTextClient(model="mlx-community/test-vlm", max_tokens=123)

    assert client.generate_json("Return JSON") == {"issue_type": "other"}
    assert calls["load"] == ("mlx-community/test-vlm", False)
    template_args, template_kwargs = calls["template"]
    assert template_args[0] is processor_obj
    assert template_kwargs["num_images"] == 0
    generate_args, generate_kwargs = calls["stream_generate"]
    assert generate_args[:3] == (model_obj, processor_obj, "formatted text prompt{")
    # Text-only: no placeholder image travels with the request anymore.
    assert generate_args[3] == []
    assert generate_kwargs["max_tokens"] == 123


def test_photo_vision_ollama_adapter_calls_core_and_normalizes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"image")
    asset = _asset(image_path)

    class FakeCoreOllamaClient:
        def __init__(self, model: str, ollama_url: str, timeout_seconds: float) -> None:
            self.model = model
            self.ollama_url = ollama_url.rstrip("/")
            self.timeout_seconds = timeout_seconds
            self.engine_name = f"ollama:{model}"
            self.describe_calls: list[tuple[Path, str]] = []

        def describe(self, image_path: Path, prompt: str) -> dict[str, object]:
            self.describe_calls.append((image_path, prompt))
            return {
                "description": "Paper towel on floor.",
                "area_guess": "restroom",
                "visible_objects": ["paper towel"],
                "possible_conditions": ["paper towel on floor"],
                "possible_issues": [],
                "confidence": 0.7,
                "needs_human_review": False,
                "warnings": [],
            }

    monkeypatch.setattr(vision_backends, "OllamaVisionClient", FakeCoreOllamaClient)

    client = photo_vision.OllamaVisionClient(model="qwen-test")
    result = client(asset)

    assert isinstance(result, VisionDescription)
    assert result.description == "Paper towel on floor."
    assert photo_vision.ADVISORY_WARNING in result.warnings
    assert client._client.describe_calls[0][0] == image_path
    assert "Return strict JSON" in client._client.describe_calls[0][1]
    assert client.engine_name == "ollama:qwen-test"
    assert client.provider == "ollama"


def test_photo_vision_reexports_resolve() -> None:
    assert photo_vision.VisionModelTimeoutError is vision_backends.VisionModelTimeoutError
    assert photo_vision.DEFAULT_MODEL == "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"


def test_pyproject_declares_vision_mlx_extra() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    optional_dependencies = pyproject["project"]["optional-dependencies"]

    # Pillow is required by the MLX image-downscale OOM fix (vision_backends), so a
    # vision venv built from this extra cannot silently disable it (prompt 298).
    assert optional_dependencies["vision-mlx"] == ["mlx-vlm", "Pillow"]
    assert "mlx-vlm" not in dependencies
    assert "Pillow" not in dependencies
