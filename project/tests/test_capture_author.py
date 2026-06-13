"""INDEPENDENT VERIFIER behavioral tests for the non-PWA text/email capture
authoring path: event_pipeline.capture_author.author_text_capture.

All I/O is stubbed via monkeypatch on the names capture_author imports:
  - capture_author.resolve_site_id
  - capture_author.get_field_capture_document
  - capture_author.put_field_capture_document
  - capture_author.couchdb_config.from_env (so no env/CouchDBConfig needed)

We did NOT write the implementation. These assertions are written against the
stated contract; defects are wins.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from event_pipeline import capture_author
from event_pipeline import couchdb_config


# --------------------------------------------------------------------------- #
# Recorders / stubs
# --------------------------------------------------------------------------- #
class PutRecorder:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result if result is not None else {"ok": True, "id": "x", "rev": "1-abc"}

    def __call__(self, config: Any, doc: dict[str, Any], *, database: str) -> dict[str, Any]:
        self.calls.append({"config": config, "doc": doc, "database": database})
        return self.result


class GetRecorder:
    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[Any, str, str]] = []
        self.existing = existing

    def __call__(self, config: Any, database: str, doc_id: str) -> dict[str, Any] | None:
        self.calls.append((config, database, doc_id))
        return self.existing


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch):
    """Install canned site resolution + recording writer/reader + fake config.

    Returns a small namespace with the recorders + the fixed site_id so each
    test can assert against the I/O boundary.
    """
    site_id = "7050"

    def fake_resolve(site_name: str) -> str | None:
        fake_resolve.calls.append(site_name)
        return site_id
    fake_resolve.calls = []  # type: ignore[attr-defined]

    get_rec = GetRecorder(existing=None)
    put_rec = PutRecorder()

    # A sentinel config object: from_env must NOT be relied on to touch env.
    sentinel_config = object()

    monkeypatch.setattr(capture_author, "resolve_site_id", fake_resolve)
    monkeypatch.setattr(capture_author, "get_field_capture_document", get_rec)
    monkeypatch.setattr(capture_author, "put_field_capture_document", put_rec)
    monkeypatch.setattr(capture_author.couchdb_config, "from_env", lambda: sentinel_config)

    class NS:
        pass

    ns = NS()
    ns.site_id = site_id
    ns.resolve = fake_resolve
    ns.get = get_rec
    ns.put = put_rec
    ns.config = sentinel_config
    return ns


# --------------------------------------------------------------------------- #
# Contract 1: Envelope shape (dry_run)
# --------------------------------------------------------------------------- #
def test_envelope_shape_dry_run(stubbed):
    doc = capture_author.author_text_capture(
        text="Door broken",
        site="Some Resolvable Site",
        source_kind="email",
        source_ref="msg-123",
        dry_run=True,
    )
    assert doc["type"] == "field_capture"
    assert doc["processing_state"] == "pending"
    assert doc["site_id"] == stubbed.site_id
    assert doc["note"] == "Door broken"
    assert doc["photos"] == []
    # No audio key (text-only intake) OR explicitly empty.
    assert ("audio" not in doc) or (not doc["audio"])
    # Provenance.
    assert doc["source_kind"] == "email"
    assert doc["source_ref"] == "msg-123"
    assert doc["source"] == "email_import"
    # Envelope carries an _id (schema requirement) == capture_id.
    assert doc["_id"]
    assert doc["_id"] == doc["capture_id"]


def test_dry_run_does_no_io(stubbed):
    capture_author.author_text_capture(
        text="Door broken",
        site="Some Resolvable Site",
        source_kind="email",
        source_ref="msg-123",
        dry_run=True,
    )
    assert stubbed.get.calls == []
    assert stubbed.put.calls == []


# --------------------------------------------------------------------------- #
# Contract 2: Deterministic capture_id
# --------------------------------------------------------------------------- #
def test_capture_id_deterministic_same_ref(stubbed):
    kwargs = dict(text="A", site="S", source_kind="email", source_ref="msg-123", dry_run=True)
    a = capture_author.author_text_capture(**kwargs)
    b = capture_author.author_text_capture(**kwargs)
    assert a["capture_id"] == b["capture_id"]
    # Stable across a different text body too (id is keyed on source, not text).
    c = capture_author.author_text_capture(
        text="totally different body", site="S", source_kind="email",
        source_ref="msg-123", dry_run=True,
    )
    assert c["capture_id"] == a["capture_id"]


def test_capture_id_differs_for_different_ref(stubbed):
    a = capture_author.author_text_capture(
        text="A", site="S", source_kind="email", source_ref="msg-123", dry_run=True,
    )
    b = capture_author.author_text_capture(
        text="A", site="S", source_kind="email", source_ref="msg-999", dry_run=True,
    )
    assert a["capture_id"] != b["capture_id"]


def test_capture_id_not_random_via_public_helper():
    # The deterministic helper must be a pure function of its inputs.
    one = capture_author.deterministic_capture_id(source_kind="email", source_ref="msg-123")
    two = capture_author.deterministic_capture_id(source_kind="email", source_ref="msg-123")
    assert one == two
    assert one  # non-empty
    # source_kind participates in the id.
    assert capture_author.deterministic_capture_id(
        source_kind="sms", source_ref="msg-123"
    ) != one


# --------------------------------------------------------------------------- #
# Contract 3: Fail-closed site resolution
# --------------------------------------------------------------------------- #
def test_fail_closed_unresolved_site_raises_no_write(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(capture_author, "resolve_site_id", lambda s: None)
    put_rec = PutRecorder()
    get_rec = GetRecorder()
    monkeypatch.setattr(capture_author, "put_field_capture_document", put_rec)
    monkeypatch.setattr(capture_author, "get_field_capture_document", get_rec)
    monkeypatch.setattr(capture_author.couchdb_config, "from_env", lambda: object())

    with pytest.raises(capture_author.CaptureAuthorError):
        capture_author.author_text_capture(
            text="Door broken",
            site="Nonexistent Place",
            source_kind="email",
            source_ref="msg-1",
            dry_run=False,
        )
    assert put_rec.calls == []
    assert get_rec.calls == []


def test_fail_closed_even_in_dry_run(monkeypatch: pytest.MonkeyPatch):
    # Fail-closed must apply even on dry_run (no silent unresolved doc).
    monkeypatch.setattr(capture_author, "resolve_site_id", lambda s: None)
    with pytest.raises(capture_author.CaptureAuthorError):
        capture_author.author_text_capture(
            text="Door broken", site="Nope", source_kind="email",
            source_ref="msg-1", dry_run=True,
        )


# --------------------------------------------------------------------------- #
# Contract 5: Idempotent duplicate skip
# --------------------------------------------------------------------------- #
def test_duplicate_skips_write(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(capture_author, "resolve_site_id", lambda s: "7050")
    existing = {"_id": "email-msg-123-deadbeef", "type": "field_capture"}
    get_rec = GetRecorder(existing=existing)
    put_rec = PutRecorder()
    monkeypatch.setattr(capture_author, "get_field_capture_document", get_rec)
    monkeypatch.setattr(capture_author, "put_field_capture_document", put_rec)
    monkeypatch.setattr(capture_author.couchdb_config, "from_env", lambda: object())

    result = capture_author.author_text_capture(
        text="Door broken",
        site="Some Site",
        source_kind="email",
        source_ref="msg-123",
        dry_run=False,
    )
    assert result.get("skipped") == "duplicate"
    assert put_rec.calls == []  # never write on duplicate
    assert len(get_rec.calls) == 1  # pre-check happened


# --------------------------------------------------------------------------- #
# Contract 6: Writes the captures DB (and ONLY that DB), once, with the doc.
# --------------------------------------------------------------------------- #
def test_write_targets_field_captures_db_once(stubbed):
    result = capture_author.author_text_capture(
        text="Door broken",
        site="Some Site",
        source_kind="email",
        source_ref="msg-123",
        dry_run=False,
    )
    assert len(stubbed.put.calls) == 1
    call = stubbed.put.calls[0]
    assert call["database"] == couchdb_config.field_captures_database()
    assert call["database"] == "btq_field_captures"
    # The exact built doc (with _id) was written.
    assert call["doc"]["_id"]
    assert call["doc"]["type"] == "field_capture"
    assert call["doc"]["processing_state"] == "pending"
    # Config passed through is the one from from_env (our sentinel).
    assert call["config"] is stubbed.config
    # get pre-check ran exactly once against the same DB.
    assert len(stubbed.get.calls) == 1
    assert stubbed.get.calls[0][1] == "btq_field_captures"
    # Return value is the writer's response (not a skip).
    assert "skipped" not in result


def test_does_not_import_or_use_btq_client():
    # The capture authoring path must be separate from the queue client.
    import sys
    src = Path(capture_author.__file__).read_text(encoding="utf-8")
    assert "btq_client" not in src, "capture_author must not reference btq_client"
    # And importing capture_author must not have pulled btq_client in transitively
    # *through this module's own imports*. (Other already-loaded tests may have
    # imported it, so we only assert the source does not reference it.)


def test_no_write_to_other_database(stubbed):
    capture_author.author_text_capture(
        text="hello", site="S", source_kind="manual", source_ref="ref-1", dry_run=False,
    )
    databases_written = {c["database"] for c in stubbed.put.calls}
    assert databases_written == {"btq_field_captures"}


# --------------------------------------------------------------------------- #
# Contract 7: Schema acceptance (validate_doc_update requirements)
# --------------------------------------------------------------------------- #
def test_doc_satisfies_validate_doc_update(stubbed):
    doc = capture_author.author_text_capture(
        text="Door broken", site="S", source_kind="email", source_ref="msg-7",
        dry_run=True,
    )
    # validate_doc_update for btq_field_captures: type must be field_capture
    # (an allowed type) and a non-_design _id.
    assert doc["type"] == "field_capture"
    assert "_id" in doc and doc["_id"]
    assert not str(doc["_id"]).startswith("_design/")


# --------------------------------------------------------------------------- #
# Beyond-spec probes
# --------------------------------------------------------------------------- #
def test_unknown_source_kind_rejected(stubbed):
    with pytest.raises(capture_author.CaptureAuthorError):
        capture_author.author_text_capture(
            text="x", site="S", source_kind="carrier-pigeon", source_ref="r1",
            dry_run=True,
        )


def test_empty_text_rejected(stubbed):
    with pytest.raises(capture_author.CaptureAuthorError):
        capture_author.author_text_capture(
            text="", site="S", source_kind="email", source_ref="r1", dry_run=True,
        )


def test_whitespace_text_rejected(stubbed):
    with pytest.raises(capture_author.CaptureAuthorError):
        capture_author.author_text_capture(
            text="   \n\t  ", site="S", source_kind="email", source_ref="r1",
            dry_run=True,
        )


def test_empty_source_ref_rejected(stubbed):
    with pytest.raises(capture_author.CaptureAuthorError):
        capture_author.author_text_capture(
            text="x", site="S", source_kind="email", source_ref="   ", dry_run=True,
        )


def test_captured_at_passthrough(stubbed):
    doc = capture_author.author_text_capture(
        text="x", site="S", source_kind="email", source_ref="r1",
        captured_at="2026-01-02T03:04:05Z", dry_run=True,
    )
    assert doc["captured_at"] == "2026-01-02T03:04:05Z"
    # created_at envelope timestamp follows the supplied captured_at.
    assert doc["created_at"] == "2026-01-02T03:04:05Z"


def test_captured_at_default_when_absent(stubbed):
    doc = capture_author.author_text_capture(
        text="x", site="S", source_kind="email", source_ref="r1", dry_run=True,
    )
    # Defaulted to a non-empty ISO-ish timestamp.
    assert isinstance(doc["captured_at"], str) and doc["captured_at"]
    assert doc["captured_at"] != ""


def test_site_whitespace_trimmed_before_resolution(monkeypatch: pytest.MonkeyPatch):
    seen: list[str] = []

    def fake_resolve(site_name: str) -> str | None:
        seen.append(site_name)
        return "7050"

    monkeypatch.setattr(capture_author, "resolve_site_id", fake_resolve)
    monkeypatch.setattr(capture_author, "get_field_capture_document", GetRecorder())
    monkeypatch.setattr(capture_author, "put_field_capture_document", PutRecorder())
    monkeypatch.setattr(capture_author.couchdb_config, "from_env", lambda: object())

    capture_author.author_text_capture(
        text="x", site="  Padded Site  ", source_kind="email", source_ref="r1",
        dry_run=True,
    )
    assert seen == ["Padded Site"], f"site was not trimmed before resolution: {seen!r}"


def test_source_kind_case_insensitive(stubbed):
    doc = capture_author.author_text_capture(
        text="x", site="S", source_kind="EMAIL", source_ref="r1", dry_run=True,
    )
    assert doc["source_kind"] == "email"
    assert doc["source"] == "email_import"
