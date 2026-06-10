"""Regression tests for the field-capture semantic HTTP read timeout.

On 2026-06-10 a single deadlocked Ollama generation on the Dell inference node
wedged the entire field-capture pipeline for ~1 hour: the watcher blocked
forever in sock_recv_into -> poll on an ESTABLISHED socket because the semantic
HTTP request had no effective read timeout. These tests pin two invariants so it
cannot silently regress:

  1. ``build_semantic_engine`` wires ``BTQ_FIELD_CAPTURE_SEMANTIC_TIMEOUT_SECONDS``
     through to the Ollama client's ``timeout_seconds``.
  2. ``OllamaTextClient.generate_json`` against a server that accepts the
     connection but never responds fails (as a typed timeout) within ~the
     configured timeout rather than blocking indefinitely.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

import vision_backends
from processing_core import capture_semantics


def test_build_semantic_engine_wires_timeout_into_ollama_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "ollama")
    monkeypatch.setenv("BTQ_SEMANTIC_MODEL", "qwen3:4b")
    # Loopback URL passes validate_local_ollama_url; the constructor makes no
    # network call so no server is needed for this assertion.
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_TIMEOUT_SECONDS", "42")

    engine = capture_semantics.build_semantic_engine()

    client = engine._client  # type: ignore[attr-defined]
    assert isinstance(client, vision_backends.OllamaTextClient)
    assert client.timeout_seconds == 42.0


def test_build_semantic_engine_rejects_non_numeric_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "ollama")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_TIMEOUT_SECONDS", "not-a-number")

    with pytest.raises(ValueError):
        capture_semantics.build_semantic_engine()


def test_ollama_text_client_read_timeout_against_unresponsive_server() -> None:
    """A server that accepts the connection but never sends a byte must NOT
    wedge the caller. ``generate_json`` must fail within ~the configured timeout.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    # Accept the connection and hold it open without ever responding. This
    # reproduces a hung Ollama generation (ESTABLISHED socket, no body sent).
    held: list[socket.socket] = []

    def _accept_and_hold() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        held.append(conn)  # keep the connection open; never reply

    accepter = threading.Thread(target=_accept_and_hold, daemon=True)
    accepter.start()

    timeout_seconds = 1.5
    client = vision_backends.OllamaTextClient(
        model="qwen3:4b",
        base_url=f"http://127.0.0.1:{port}",
        timeout_seconds=timeout_seconds,
        disable_thinking=True,
    )

    started = time.monotonic()
    try:
        with pytest.raises(vision_backends.VisionModelTimeoutError):
            client.generate_json("extract actions")
        elapsed = time.monotonic() - started
        # The call must return promptly after the timeout, not hang. Allow ample
        # slack for slow CI, but far below the old "blocks forever" behavior.
        assert elapsed < 30.0, f"generate_json took {elapsed:.1f}s; read timeout not honored"
    finally:
        for conn in held:
            conn.close()
        accepter.join(timeout=5.0)
        server.close()
