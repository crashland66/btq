"""Gating tests for prompt 298: MLX image-downscale resize + temp-file cleanup.

Covers _resize_image_for_mlx() and MlxVisionClient.describe()'s use of it
(vision_backends.py). The resize tests need Pillow to author/inspect images and
are skipped cleanly if Pillow is absent; the prod feature itself degrades to a
no-op without Pillow (verified by test_resize_pillow_missing_falls_back).
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

import vision_backends
from vision_backends import MAX_MLX_IMAGE_LONG_EDGE, MlxVisionClient, _resize_image_for_mlx


def _write_image(path: Path, size: tuple[int, int]) -> None:
    from PIL import Image

    Image.new("RGB", size, color=(123, 45, 67)).save(path)


def _make_client_with_capture(monkeypatch: pytest.MonkeyPatch, json_payload: str):
    """An MlxVisionClient that records the path handed to _generate_response.

    Built via __new__ to skip __init__ (which would require mlx-vlm + a real
    model load). captured["path"] holds the string the inference loop received.
    """
    client = MlxVisionClient.__new__(MlxVisionClient)
    captured: dict[str, str] = {}

    def fake_generate_response(self: MlxVisionClient, prompt: str, image_path: str) -> str:
        captured["path"] = image_path
        return json_payload

    monkeypatch.setattr(MlxVisionClient, "_generate_response", fake_generate_response)
    return client, captured


# --------------------------------------------------------------------------
# _resize_image_for_mlx
# --------------------------------------------------------------------------


def test_resize_large_image_returns_new_resized_path(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    src = tmp_path / "big.png"
    _write_image(src, (2000, 1000))
    original_bytes = src.read_bytes()

    out_path, was_resized = _resize_image_for_mlx(src)

    assert was_resized is True
    assert out_path != src
    assert out_path.exists()

    with Image.open(out_path) as img:
        w, h = img.size
    assert max(w, h) == MAX_MLX_IMAGE_LONG_EDGE  # 1280
    # aspect ratio preserved (2:1) within rounding -> ~1280x640
    assert (w, h) == (1280, 640)

    # original untouched
    assert src.exists()
    assert src.read_bytes() == original_bytes

    out_path.unlink(missing_ok=True)


def test_resize_large_image_jpg_suffix_preserved(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    src = tmp_path / "big.jpg"
    _write_image(src, (3000, 1500))

    out_path, was_resized = _resize_image_for_mlx(src)

    assert was_resized is True
    assert out_path.suffix == ".jpg"
    with Image.open(out_path) as img:
        assert max(img.size) == MAX_MLX_IMAGE_LONG_EDGE
    out_path.unlink(missing_ok=True)


def test_resize_small_image_is_identity_no_temp_file(tmp_path: Path) -> None:
    pytest.importorskip("PIL")

    src = tmp_path / "small.png"
    _write_image(src, (800, 600))
    before = set(tmp_path.iterdir())

    out_path, was_resized = _resize_image_for_mlx(src)

    assert was_resized is False
    assert out_path == src
    # no temp file created in the test's tmp dir; identity path returned
    assert set(tmp_path.iterdir()) == before


def test_resize_exactly_at_threshold_is_identity(tmp_path: Path) -> None:
    pytest.importorskip("PIL")

    src = tmp_path / "edge.png"
    _write_image(src, (MAX_MLX_IMAGE_LONG_EDGE, 700))  # long edge == 1280

    out_path, was_resized = _resize_image_for_mlx(src)

    assert was_resized is False
    assert out_path == src


def test_resize_pillow_missing_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "any.png"
    src.write_bytes(b"not-a-real-image-but-never-opened")

    real_import = builtins.__import__

    def fail_pil_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_pil_import)

    out_path, was_resized = _resize_image_for_mlx(src)

    assert was_resized is False
    assert out_path == src  # original returned, no exception raised


# --------------------------------------------------------------------------
# MlxVisionClient.describe()
# --------------------------------------------------------------------------


def test_describe_large_image_uses_resized_path_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PIL")

    src = tmp_path / "big.png"
    _write_image(src, (2000, 1000))

    client, captured = _make_client_with_capture(monkeypatch, json.dumps({"ok": True}))

    result = client.describe(src, "describe please")

    assert result == {"ok": True}
    used = Path(captured["path"])
    assert used != src  # inference ran against the resized temp file
    assert not used.exists()  # finally-block unlinked it
    assert src.exists()  # original survives


def test_describe_small_image_uses_original_path_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PIL")

    src = tmp_path / "small.png"
    _write_image(src, (800, 600))

    client, captured = _make_client_with_capture(monkeypatch, json.dumps({"ok": 1}))

    result = client.describe(src, "p")

    assert result == {"ok": 1}
    assert Path(captured["path"]) == src  # identity: original path used


def test_describe_cleans_up_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PIL")

    src = tmp_path / "big.png"
    _write_image(src, (2000, 1000))

    captured: dict[str, str] = {}

    def boom(self: MlxVisionClient, prompt: str, image_path: str) -> str:
        captured["path"] = image_path
        raise RuntimeError("inference exploded")

    monkeypatch.setattr(MlxVisionClient, "_generate_response", boom)
    client = MlxVisionClient.__new__(MlxVisionClient)

    with pytest.raises(RuntimeError, match="inference exploded"):
        client.describe(src, "p")

    used = Path(captured["path"])
    assert used != src
    assert not used.exists()  # finally unlinked despite the exception
    assert src.exists()
