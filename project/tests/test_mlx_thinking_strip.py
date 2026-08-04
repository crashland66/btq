"""Gates for thinking-model support in the MLX clients (Qwen3.5 readiness).

Qwen3.x emits a reasoning block terminated by ``</think>`` before its answer
(the opening tag stays in the generation prompt). The clients must:

  * never let reasoning text reach JSON parsing or stored prose;
  * never early-exit the stream on JSON quoted INSIDE an open reasoning block;
  * keep the fast early-exit for non-thinking models that answer immediately;
  * honor BTQ_MLX_MAX_TOKENS so thinking models get budget headroom.

Fake mlx_vlm module tree mirrors test_mlx_text_temperature_344.py — no model
is ever loaded.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

import vision_backends
from vision_backends import mlx_max_tokens, strip_model_thinking


THINKING_PREFIX = (
    'Got it, the user wants JSON. Maybe {"item_name": "wrong", "quantity": 99} — '
    "no wait, let me reconsider the transcript."
)
REAL_ANSWER = '{"item_name": "paper towels", "quantity": 2}'
THINKING_RESPONSE = f"{THINKING_PREFIX}\n</think>\n\n```json\n{REAL_ANSWER}\n```"


# ---------------------------------------------------------------------------
# strip_model_thinking / mlx_max_tokens
# ---------------------------------------------------------------------------

def test_strip_removes_reasoning_and_keeps_answer() -> None:
    assert REAL_ANSWER in strip_model_thinking(THINKING_RESPONSE)
    assert "wrong" not in strip_model_thinking(THINKING_RESPONSE)


def test_strip_is_identity_for_non_thinking_output() -> None:
    assert strip_model_thinking(REAL_ANSWER) == REAL_ANSWER
    assert strip_model_thinking("") == ""


def test_strip_uses_last_close_tag() -> None:
    text = "a</think>b</think>final"
    assert strip_model_thinking(text) == "final"


def test_mlx_max_tokens_default_env_and_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_MLX_MAX_TOKENS", raising=False)
    assert mlx_max_tokens() == vision_backends.DEFAULT_MLX_MAX_TOKENS
    monkeypatch.setenv("BTQ_MLX_MAX_TOKENS", "1024")
    assert mlx_max_tokens() == 1024
    # An explicit caller value always wins over the env.
    assert mlx_max_tokens(64) == 64


# ---------------------------------------------------------------------------
# Fake mlx_vlm plumbing (mirrors test_mlx_text_temperature_344)
# ---------------------------------------------------------------------------

def _install_fake_mlx_vlm(monkeypatch: pytest.MonkeyPatch) -> None:
    mlx_vlm = types.ModuleType("mlx_vlm")
    prompt_utils = types.ModuleType("mlx_vlm.prompt_utils")
    utils = types.ModuleType("mlx_vlm.utils")
    mlx_vlm.__path__ = []  # type: ignore[attr-defined]
    mlx_vlm.load = lambda model, *, use_fast=True: (object(), object())  # type: ignore[attr-defined]
    mlx_vlm.generate = lambda *a, **k: "{}"  # type: ignore[attr-defined]
    mlx_vlm.stream_generate = lambda *a, **k: []  # type: ignore[attr-defined]
    mlx_vlm.prompt_utils = prompt_utils  # type: ignore[attr-defined]
    mlx_vlm.utils = utils  # type: ignore[attr-defined]
    prompt_utils.apply_chat_template = lambda *a, **k: "formatted"  # type: ignore[attr-defined]
    utils.load_config = lambda model: {"model": model}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.prompt_utils", prompt_utils)
    monkeypatch.setitem(sys.modules, "mlx_vlm.utils", utils)


class _Chunk:
    def __init__(self, text: str) -> None:
        self.text = text


def _stream_of(chunks: list[str], consumed: list[str]):
    def spy(*_args: object, **_kwargs: object):
        for chunk in chunks:
            consumed.append(chunk)
            yield _Chunk(chunk)

    return spy


def _text_client(monkeypatch: pytest.MonkeyPatch) -> object:
    _install_fake_mlx_vlm(monkeypatch)
    return vision_backends.MlxTextClient(model="mlx-community/test-vlm")


# ---------------------------------------------------------------------------
# MlxTextClient: stream discipline + JSON extraction
# ---------------------------------------------------------------------------

def test_generate_json_ignores_json_quoted_inside_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    # MUTATION GUARD: the spurious {"quantity": 99} inside the open reasoning
    # block must not become the answer, and the stream must not stop there.
    client = _text_client(monkeypatch)
    consumed: list[str] = []
    client._stream_generate = _stream_of(
        [THINKING_PREFIX, "\n</think>\n\n```json\n", REAL_ANSWER, "\n```"],
        consumed,
    )

    result = client.generate_json("prompt")

    assert result == {"item_name": "paper towels", "quantity": 2}
    # The stream ran past the reasoning chunk instead of early-exiting on it.
    assert len(consumed) >= 3


def test_generate_json_still_early_exits_for_immediate_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Non-thinking models answer by continuing the prefilled "{" directly; the
    # early exit must survive so streams don't always run to max_tokens.
    client = _text_client(monkeypatch)
    consumed: list[str] = []
    continuation = '"item_name": "paper towels", "quantity": 2}'
    client._stream_generate = _stream_of([continuation, "TRAILING-NEVER-CONSUMED"], consumed)

    result = client.generate_json("prompt")

    assert result == {"item_name": "paper towels", "quantity": 2}
    assert consumed == [continuation]


def test_text_generation_is_text_only_with_json_prefill(monkeypatch: pytest.MonkeyPatch) -> None:
    # MUTATION GUARD: no placeholder image (thinking models burned their token
    # budget reasoning about the blank image) and the prompt ends with the
    # prefilled "{" so generation starts inside the JSON object.
    client = _text_client(monkeypatch)
    recorded: dict[str, object] = {}

    def spy(*args: object, **kwargs: object):
        recorded["args"] = args
        yield _Chunk('"issue_type": "other"}')

    client._stream_generate = spy
    result = client.generate_json("prompt")

    assert result == {"issue_type": "other"}
    args = recorded["args"]
    assert str(args[2]).endswith("{")  # prefilled opening brace
    assert args[3] == []  # no images


def test_extract_json_takes_first_complete_object_over_trailing_junk() -> None:
    # MUTATION GUARD for the "Extra data" class: thinking models repeat the
    # JSON or append commentary; the first complete object must win.
    from vision_backends import extract_json_from_model_output

    raw = '{"a": 1} and also, for clarity: {"a": 999}'
    assert json.loads(extract_json_from_model_output(raw)) == {"a": 1}


def test_generate_json_strips_thinking_from_single_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _text_client(monkeypatch)
    client._stream_generate = _stream_of([THINKING_RESPONSE], [])

    assert client.generate_json("prompt") == {"item_name": "paper towels", "quantity": 2}


def test_text_client_honors_env_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_MLX_MAX_TOKENS", "1024")
    client = _text_client(monkeypatch)
    assert client.max_tokens == 1024


# ---------------------------------------------------------------------------
# MlxVisionClient: describe and prose paths
# ---------------------------------------------------------------------------

def _vision_client(responses: list[str]) -> object:
    client = vision_backends.MlxVisionClient.__new__(vision_backends.MlxVisionClient)
    client.max_tokens = 512
    client._generate_response = lambda _prompt, _image, **_kwargs: responses.pop(0)
    return client


def test_vision_describe_strips_thinking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_backends, "_resize_image_for_mlx", lambda p: (p, False))
    client = _vision_client([THINKING_RESPONSE])

    parsed = client.describe(tmp_path / "photo.jpg", "prompt")

    assert parsed == {"item_name": "paper towels", "quantity": 2}


def test_vision_generate_text_strips_thinking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_backends, "_resize_image_for_mlx", lambda p: (p, False))
    client = _vision_client(["I should describe the floor.\n</think>\nThe floor is clean and dry."])

    text = client.generate_text(tmp_path / "photo.jpg", "prompt")

    assert text == "The floor is clean and dry."
    assert "describe the floor" not in text


def test_vision_client_honors_env_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_MLX_MAX_TOKENS", "900")
    _install_fake_mlx_vlm(monkeypatch)
    client = vision_backends.MlxVisionClient(model="mlx-community/test-vlm")
    assert client.max_tokens == 900
