from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote_plus

import pytest

from event_pipeline.couchdb import system_defaults
from ops_dashboard.app import route_response, route_response_with_headers
from ops_dashboard.sections import system
from tests.test_ops_dashboard import request_text


def defaults_doc(*, rev: str | None = "1-a") -> dict[str, object]:
    doc = system_defaults.default_skeleton()
    doc["default_vision_context"] = "Vision"
    doc["default_capture_guidance"] = "Guidance"
    doc["default_display_categories"] = [{"label": "Restrooms", "canonical": "restrooms"}]
    doc["default_capture_advice"] = ["Take clear photos"]
    if rev:
        doc["_rev"] = rev
    return doc


def test_system_route_renders_form_from_couchdb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system.system_defaults, "load_system_defaults", defaults_doc)

    body = request_text("GET", "/system", tmp_path / "runtime")[2]

    assert "Vision" in body
    assert "Guidance" in body


def test_system_route_renders_skeleton_when_doc_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system.system_defaults, "load_system_defaults", lambda: defaults_doc(rev=None))

    body = request_text("GET", "/system", tmp_path / "runtime")[2]

    assert system_defaults.DEFAULT_VOICE_NOTE_FORMULA in body


def test_system_route_shows_banner_when_doc_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system.system_defaults, "load_system_defaults", lambda: defaults_doc(rev=None))

    body = request_text("GET", "/system", tmp_path / "runtime")[2]

    assert "system_defaults document not yet created" in body


def test_system_form_shows_human_label_vision_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system.system_defaults, "load_system_defaults", defaults_doc)

    body = request_text("GET", "/system", tmp_path / "runtime")[2]

    assert "Vision Context" in body
    assert "<label>default_vision_context" not in body
    assert 'name="default_vision_context"' in body


def test_system_form_shows_human_label_voice_note_formula(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system.system_defaults, "load_system_defaults", defaults_doc)

    body = request_text("GET", "/system", tmp_path / "runtime")[2]

    assert "Voice Note Formula" in body


def test_system_display_categories_renders_as_row_editor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system.system_defaults, "load_system_defaults", defaults_doc)

    body = request_text("GET", "/system", tmp_path / "runtime")[2]

    assert '<input type="text" name="default_display_categories_label"' in body
    assert '<input type="text" name="default_display_categories_canonical"' in body
    assert '<textarea name="default_display_categories"' not in body


def test_system_save_creates_doc_when_rev_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[dict[str, object], str | None]] = []
    monkeypatch.setattr(system.system_defaults, "save_system_defaults", lambda doc, rev: calls.append((doc, rev)) or {**doc, "_rev": "1-a"})

    status, _content_type, _body, headers = route_response_with_headers("POST", "/system/save", tmp_path / "runtime", valid_body(_rev=""))

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/system?message=saved"
    assert calls[0][1] is None


def test_system_save_updates_doc_with_correct_rev(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[dict[str, object], str | None]] = []
    monkeypatch.setattr(system.system_defaults, "save_system_defaults", lambda doc, rev: calls.append((doc, rev)) or {**doc, "_rev": "2-a"})

    route_response("POST", "/system/save", tmp_path / "runtime", valid_body(_rev="1-a"))

    assert calls[0][1] == "1-a"


def test_system_save_rev_conflict_rerenders_form_with_notice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def conflict(_doc: dict[str, object], _rev: str | None) -> dict[str, object]:
        raise system_defaults.SystemDefaultsConflictError(defaults_doc(rev="server"))

    monkeypatch.setattr(system.system_defaults, "save_system_defaults", conflict)

    status, _content_type, body = route_response("POST", "/system/save", tmp_path / "runtime", valid_body(_rev="stale"))

    assert status == HTTPStatus.OK
    assert b"edited elsewhere" in body


def test_system_save_preserves_submitted_values_on_validation_error(tmp_path: Path) -> None:
    status, _content_type, body, headers = route_response_with_headers("POST", "/system/save", tmp_path / "runtime", valid_body(default_display_categories_label="Submitted label", default_display_categories_canonical=""))

    assert status == HTTPStatus.OK
    assert b'value="Submitted label"' in body
    assert "Location" not in headers


def test_system_save_audit_log_summarizes_long_strings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system.system_defaults, "save_system_defaults", lambda doc, rev: {**doc, "_rev": "1-a"})

    route_response("POST", "/system/save", tmp_path / "runtime", valid_body(default_vision_context="x" * 300))
    payload = json.loads((tmp_path / "runtime" / "logs" / "admin_audit.log").read_text(encoding="utf-8"))

    assert len(payload["payload"]["default_vision_context"]) == 200


def valid_body(**overrides: str) -> bytes:
    data = {
        "default_vision_context": "Vision",
        "default_capture_guidance": "Guidance",
        "default_display_categories_label": "Restrooms",
        "default_display_categories_canonical": "restrooms",
        "default_voice_note_formula": system_defaults.DEFAULT_VOICE_NOTE_FORMULA,
        "default_capture_advice": "Take clear photos",
        "_rev": "1-a",
    }
    data.update(overrides)
    return "&".join(f"{key}={quote_plus(value)}" for key, value in data.items()).encode()
