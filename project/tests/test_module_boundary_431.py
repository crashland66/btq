"""INDEPENDENT VERIFIER gating tests for the module-boundary refactor (btq 431).

Two behavior-preserving structure fixes are under test:

  PART 1 - broke a capture_ingest <-> media_store import cycle. `atomic_write_bytes`
           now lives in `media_store`; `capture_ingest` no longer defines or
           exports it.
  PART 2 - removed a field_capture -> queue_processor private-symbol import. A
           public `append_json_line` now lives in `processing_core.artifacts`;
           `queue_processor.metrics._append_json_line` delegates to it and
           `field_capture.deep_analysis` imports the public symbol.

These tests are authored from the contract, not from the diff, and assert
byte-exact behavior so a regression goes red.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# `project/` is on sys.path via pyproject `pythonpath = ["project"]`, so these
# top-level modules import directly.
import media_store
from media_store import LocalFilesystemStore, atomic_write_bytes
from processing_core.artifacts import append_json_line

# Absolute path to the `project/` dir so the structural subprocess can replicate
# the same import root that pytest configures.
_PROJECT_DIR = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Contract 1: no import cycle / dead-symbol removal (structural, in subprocess).
# --------------------------------------------------------------------------- #
def _run_probe(code: str) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter with `project/` on sys.path.

    A clean subprocess guarantees `sys.modules` is empty, so "did importing X
    load Y?" is a real answer and not contaminated by this test process having
    already imported everything.
    """
    bootstrap = f"import sys; sys.path.insert(0, {str(_PROJECT_DIR)!r})\n"
    return subprocess.run(
        [sys.executable, "-c", bootstrap + code],
        capture_output=True,
        text=True,
    )


def test_importing_media_store_does_not_load_capture_ingest() -> None:
    proc = _run_probe(
        "import media_store\n"
        "import sys\n"
        "assert 'capture_ingest' not in sys.modules, "
        "'media_store import pulled in capture_ingest (cycle not broken)'\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "OK"


def test_capture_ingest_has_no_atomic_write_bytes() -> None:
    proc = _run_probe(
        "import capture_ingest\n"
        "assert not hasattr(capture_ingest, 'atomic_write_bytes'), "
        "'capture_ingest still exports atomic_write_bytes (no shim expected)'\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "OK"


def test_atomic_write_bytes_is_defined_in_media_store() -> None:
    assert atomic_write_bytes.__module__ == "media_store"


def test_field_capture_server_imports_clean() -> None:
    # server.py dropped the dead `atomic_write_bytes` import; importing it must
    # still succeed (the import line must be valid).
    proc = _run_probe(
        "import field_capture.server\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "OK"


# --------------------------------------------------------------------------- #
# Contract 2: LocalFilesystemStore.write atomic + confinement preserved.
# --------------------------------------------------------------------------- #
def test_local_store_write_persists_exact_bytes(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    data = b"\x00\x01\x02 hello \xff\xfe binary payload"
    store.write("2026-06-24/cap-1/photo.jpg", data)

    written = (tmp_path.resolve(strict=False) / "2026-06-24/cap-1/photo.jpg").read_bytes()
    assert written == data
    # And the store reads it back byte-identical via its own API.
    assert store.read("2026-06-24/cap-1/photo.jpg") == data


def test_local_store_write_creates_parent_dirs(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    # Deeply nested key with no pre-existing dirs.
    store.write("a/b/c/d/e.bin", b"deep")
    assert (tmp_path.resolve(strict=False) / "a/b/c/d/e.bin").read_bytes() == b"deep"


def test_local_store_write_leaves_no_temp_file(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    store.write("dir/x.bin", b"payload")
    target_dir = tmp_path.resolve(strict=False) / "dir"
    leftovers = [p.name for p in target_dir.iterdir() if p.name != "x.bin"]
    assert leftovers == [], f"temp file(s) not cleaned up after atomic write: {leftovers}"


def test_local_store_write_overwrites_atomically(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    store.write("k.bin", b"first")
    store.write("k.bin", b"second-longer-value")
    assert store.read("k.bin") == b"second-longer-value"


def test_local_store_rejects_relative_escape(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    with pytest.raises(ValueError):
        store.write("../escape.bin", b"nope")
    assert not (tmp_path.parent / "escape.bin").exists()


def test_local_store_rejects_absolute_key(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    # An absolute key resolves outside upload_root -> confinement must trip.
    with pytest.raises(ValueError):
        store.write("/etc/passwd", b"nope")


def test_local_store_rejects_deep_traversal(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    with pytest.raises(ValueError):
        store.write("ok/../../escape.bin", b"nope")


# --------------------------------------------------------------------------- #
# Contract 3 & 4: append_json_line byte-exact format, order, parent dirs.
# --------------------------------------------------------------------------- #
def _expected_line(payload: dict) -> bytes:
    """Recompute the contractual line independently of the implementation."""
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def test_append_json_line_byte_exact_single(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    payload = {"b": 2, "a": 1, "stage": "deep_analysis"}
    append_json_line(path, payload)
    assert path.read_bytes() == _expected_line(payload)


def test_append_json_line_compact_no_spaces(tmp_path: Path) -> None:
    # Belt-and-suspenders: the literal compact separators (no spaces) must hold.
    path = tmp_path / "m.jsonl"
    append_json_line(path, {"a": 1, "b": 2})
    assert path.read_bytes() == b'{"a":1,"b":2}\n'


def test_append_json_line_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deep" / "metrics.jsonl"
    payload = {"k": "v"}
    append_json_line(path, payload)
    assert path.read_bytes() == _expected_line(payload)


def test_append_json_line_appends_in_order(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    first = {"n": 1}
    second = {"n": 2}
    append_json_line(path, first)
    append_json_line(path, second)
    assert path.read_bytes() == _expected_line(first) + _expected_line(second)


def test_append_json_line_key_order_independent(tmp_path: Path) -> None:
    # sort_keys=True: serialized line must not depend on dict insertion order,
    # including nested dicts.
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    append_json_line(p1, {"z": {"y": 1, "x": 2}, "a": 3})
    append_json_line(p2, {"a": 3, "z": {"x": 2, "y": 1}})
    assert p1.read_bytes() == p2.read_bytes()
    assert p1.read_bytes() == b'{"a":3,"z":{"x":2,"y":1}}\n'


def test_append_json_line_non_ascii_payload(tmp_path: Path) -> None:
    # Default ensure_ascii=True -> non-ASCII escaped; compare bytes to the
    # independently computed expectation.
    path = tmp_path / "m.jsonl"
    payload = {"name": "José", "emoji": "🚜", "note": "café"}
    append_json_line(path, payload)
    assert path.read_bytes() == _expected_line(payload)
    # Round-trips back to the original values.
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_metrics_delegates_to_artifacts_byte_identical(tmp_path: Path) -> None:
    # The metrics._append_json_line wrapper must produce output byte-identical to
    # the public append_json_line for the same payload (single implementation).
    from queue_processor import metrics

    payload = {"ts": "2026-06-24T00:00:00+00:00", "queue_depth": 3, "error_rate": 0.0}
    via_metrics = tmp_path / "via_metrics.jsonl"
    via_public = tmp_path / "via_public.jsonl"
    metrics._append_json_line(via_metrics, payload)
    append_json_line(via_public, payload)
    assert via_metrics.read_bytes() == via_public.read_bytes()
    assert via_metrics.read_bytes() == _expected_line(payload)


def test_deep_analysis_imports_public_symbol() -> None:
    # deep_analysis must bind the public append_json_line (PART 2 repoint), not
    # the queue_processor private one. Same object identity confirms single impl.
    import field_capture.deep_analysis as da
    import processing_core.artifacts as artifacts

    assert da.append_json_line is artifacts.append_json_line
    assert not hasattr(da, "_append_json_line")
