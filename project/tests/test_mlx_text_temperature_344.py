"""Verification tests for prompt 344: MlxTextClient passes a deterministic
``temperature`` to mlx_vlm's ``stream_generate``.

The real client loads an MLX model (``mlx_vlm.load``) which only runs on the
Pro. Every test here installs a fake ``mlx_vlm`` module tree (mirroring the
established pattern in ``test_photo_vision.py``) so no model is ever loaded.
"""

from __future__ import annotations

import sys
import types

import pytest

import vision_backends


def _install_fake_mlx_vlm(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Install a fake ``mlx_vlm`` module tree; return a call-recording dict.

    Mirrors test_photo_vision.py's patching of load / stream_generate /
    apply_chat_template / load_config so ``MlxTextClient.__init__`` loads no
    real model.
    """
    calls: dict[str, object] = {}
    mlx_vlm = types.ModuleType("mlx_vlm")
    prompt_utils = types.ModuleType("mlx_vlm.prompt_utils")
    utils = types.ModuleType("mlx_vlm.utils")
    mlx_vlm.__path__ = []  # type: ignore[attr-defined]
    model_obj = object()
    processor_obj = object()
    calls["model_obj"] = model_obj
    calls["processor_obj"] = processor_obj

    def load(model: str, *, use_fast: bool = True) -> tuple[object, object]:
        calls["load"] = (model, use_fast)
        return model_obj, processor_obj

    def generate(*args: object, **kwargs: object) -> str:
        # Needed by MlxVisionClient.__init__ (imports ``generate``); never
        # invoked in these tests, only imported.
        calls["generate"] = (args, kwargs)
        return "{}"

    def stream_generate(*args: object, **kwargs: object) -> list[object]:
        # Real stream_generate is replaced per-test by a spy; this is only a
        # fallback so the module attribute exists at import time.
        calls["stream_generate"] = (args, kwargs)
        return []

    def apply_chat_template(*args: object, **kwargs: object) -> str:
        calls["template"] = (args, kwargs)
        return "formatted text prompt"

    def load_config(model: str) -> dict[str, object]:
        calls["config"] = model
        return {"model": model}

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
    return calls


class _Chunk:
    def __init__(self, text: str) -> None:
        self.text = text


def _spy_stream_generate(recorded: dict[str, object]):
    """Return a stream_generate spy that records kwargs and yields valid JSON."""

    def spy(*args: object, **kwargs: object):
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        # A single chunk whose .text is a complete, valid JSON object so the
        # generate loop terminates and returns successfully.
        yield _Chunk('{"issue_type": "other"}')

    return spy


def test_mlx_text_client_default_temperature_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no env override, stream_generate receives temperature == 0.0."""
    monkeypatch.delenv("BTQ_FIELD_CAPTURE_SEMANTIC_TEMPERATURE", raising=False)
    _install_fake_mlx_vlm(monkeypatch)

    client = vision_backends.MlxTextClient(
        model="mlx-community/test-vlm", max_tokens=123
    )
    assert client.temperature == 0.0

    recorded: dict[str, object] = {}
    client._stream_generate = _spy_stream_generate(recorded)

    result = client._generate_response("prompt")
    assert result == '{"issue_type": "other"}'

    kwargs = recorded["kwargs"]
    assert kwargs["temperature"] == 0.0
    # max_tokens must still be forwarded unchanged.
    assert kwargs["max_tokens"] == 123


def test_mlx_text_client_env_override_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BTQ_FIELD_CAPTURE_SEMANTIC_TEMPERATURE=0.4 flows through to stream_generate."""
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_TEMPERATURE", "0.4")
    assert vision_backends._semantic_text_temperature() == 0.4

    _install_fake_mlx_vlm(monkeypatch)
    client = vision_backends.MlxTextClient(
        model="mlx-community/test-vlm", max_tokens=64
    )
    assert client.temperature == 0.4

    recorded: dict[str, object] = {}
    client._stream_generate = _spy_stream_generate(recorded)
    client._generate_response("prompt")

    assert recorded["kwargs"]["temperature"] == 0.4
    assert recorded["kwargs"]["max_tokens"] == 64


def test_semantic_text_temperature_negative_clamps_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative env value clamps to >= 0.0."""
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_TEMPERATURE", "-1")
    assert vision_backends._semantic_text_temperature() == 0.0


def test_semantic_text_temperature_empty_defaults_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty env value falls back to 0.0 (the ``or 0.0`` guard)."""
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_TEMPERATURE", "")
    assert vision_backends._semantic_text_temperature() == 0.0


def test_semantic_text_temperature_garbage_raises_valueerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric garbage is NOT silently clamped; float() raises ValueError.

    This pins the helper's *actual* behavior: ``"abc"`` is a non-empty string,
    so the ``or 0.0`` short-circuit does not fire and ``float("abc")`` raises.
    """
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_TEMPERATURE", "abc")
    with pytest.raises(ValueError):
        vision_backends._semantic_text_temperature()


def test_mlx_vision_and_ollama_clients_have_no_semantic_temperature_coupling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The temperature change is isolated to the MlxTextClient path.

    MlxVisionClient and the ollama clients must not gain a
    ``_semantic_text_temperature`` coupling (no ``temperature`` attribute set
    from that helper, and they don't call it during construction).
    """
    # The helper is module-level and not referenced by the other client
    # classes' __init__ / _generate_response bodies.
    import inspect

    for cls_name in ("OllamaVisionClient", "OllamaTextClient", "MlxVisionClient"):
        cls = getattr(vision_backends, cls_name)
        src = inspect.getsource(cls)
        assert "_semantic_text_temperature" not in src, cls_name

    # MlxVisionClient instances carry no self.temperature from the helper.
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_TEMPERATURE", "0.9")
    _install_fake_mlx_vlm(monkeypatch)
    vision_client = vision_backends.MlxVisionClient(model="mlx-community/test-vlm")
    assert not hasattr(vision_client, "temperature")
