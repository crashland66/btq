"""Durable verifier gate for field-photo drag-as-a-real-file behavior.

The browser scenarios execute the real card markup and initializer rendered by
``field_photos.render``.  Media is synthetic and fetch is fully local/stubbed;
no runtime data, credentials, or remote service is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.common import SectionContext
from ops_dashboard.sections import field_photos


_HERE = Path(__file__).resolve().parent
_HARNESS = _HERE / "photo_file_drag_gate.mjs"
_JSDOM = (
    _HERE.parent
    / "unified_capture"
    / "public"
    / "tests"
    / "node_modules"
    / "jsdom"
    / "lib"
    / "api.js"
)
_ADMIN_CSS = _HERE.parent / "ops_dashboard" / "static" / "admin.css"


def _sidecar(index: int) -> dict[str, object]:
    return {
        "doc_type": "photo_vision_sidecar",
        "status": "completed",
        "capture_id": f"cap-{index:02d}",
        "photo_id": f"photo-{index:02d}",
        "photo_asset_id": f"asset-{index:02d}",
        "site_id": "7050",
        "generated_at": f"2026-07-15T12:{index % 60:02d}:00Z",
        "description": "Synthetic verifier JPEG.",
        "area_guess": "lobby",
        "visible_objects": [],
        "possible_conditions": [],
        "possible_issues": [],
        "confidence": 0.9,
        "source_filename": f"camera-{index:02d}.jpg",
        "provenance": {
            "image_media_url": f"/media/raw-media-key-{index:02d}",
            "source_path": f"/srv/private/uploads/raw-media-key-{index:02d}.jpg",
        },
    }


def _render_docs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, docs: list[dict[str, object]]) -> str:
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr(field_photos, "_query_couchdb", lambda _cfg, _mango: docs)
    monkeypatch.setattr(
        field_photos,
        "_query_processed_asset_ids",
        lambda _cfg: {str(doc["photo_asset_id"]) for doc in docs},
    )
    monkeypatch.setattr(field_photos, "_query_terminal_capture_ids", lambda _cfg: set())
    runtime_root = tmp_path / "runtime"
    vault_root = runtime_root / "vault"
    ctx = SectionContext(
        runtime_root,
        lambda: SimpleNamespace(vault_dir=vault_root, vault_root=vault_root),
    )
    return field_photos.render(ctx)


def _render(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, count: int = 50) -> str:
    return _render_docs(monkeypatch, tmp_path, [_sidecar(index) for index in range(count)])


class _DragMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.controls: list[dict[str, str | None]] = []
        self.selection_filename_hints: list[str] = []
        self._control_depth = 0
        self.visible_control_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and "data-photo-file-drag" in values:
            self.controls.append(values)
            self._control_depth = 1
        if values.get("data-filename-hint"):
            self.selection_filename_hints.append(values["data-filename-hint"] or "")
        elif self._control_depth:
            self._control_depth += 1

    def handle_endtag(self, _tag: str) -> None:
        if self._control_depth:
            self._control_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._control_depth:
            self.visible_control_text.append(data)


@pytest.fixture
def rendered_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    return _render(monkeypatch, tmp_path)


def _run_browser_scenario(tmp_path: Path, rendered_page: str, scenario: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if not node:
        pytest.fail("node is required for the field-photo drag behavior gate")
    if not _JSDOM.exists():
        pytest.fail(f"locked jsdom dependency is missing: {_JSDOM}")
    fixture = tmp_path / f"field-photo-drag-{scenario}.json"
    fixture.write_text(
        json.dumps({"html": rendered_page, "script": field_photos.render_photo_file_drag_script()}),
        encoding="utf-8",
    )
    return subprocess.run(
        [node, str(_HARNESS), str(fixture), str(_JSDOM), scenario],
        cwd=_HERE.parents[1],
        env={**os.environ, "NO_PROXY": "*"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("scenario", ["prepared_drag", "failure_states", "lazy_bounded"])
def test_real_rendered_file_drag_browser_contract(
    tmp_path: Path,
    rendered_page: str,
    scenario: str,
) -> None:
    result = _run_browser_scenario(tmp_path, rendered_page, scenario)
    assert result.returncode == 0, (
        f"real rendered field-photo drag scenario {scenario!r} failed\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert f"ALL_OK {scenario}" in result.stdout


def test_rendered_drag_markup_is_accessible_safe_and_preserves_existing_controls(rendered_page: str) -> None:
    parser = _DragMarkupParser()
    parser.feed(rendered_page)
    assert len(parser.controls) == 50
    assert parser.selection_filename_hints == [
        control.get("data-photo-drag-filename") for control in parser.controls
    ], "drag downloads and existing selection/export controls must reuse the same filename helper"
    for control in parser.controls:
        href = control.get("href") or ""
        filename = control.get("data-photo-drag-filename") or ""
        assert href.startswith("/media/")
        assert control.get("data-photo-drag-url") == href
        assert control.get("download") == filename
        assert filename.endswith(".jpg")
        assert control.get("draggable") == "true"
        assert control.get("aria-label")
        assert control.get("title")
        operator_text = " ".join(
            [
                filename,
                control.get("aria-label") or "",
                control.get("title") or "",
            ]
        )
        assert "raw-media-key" not in operator_text
        assert "/srv/" not in operator_text
        assert "user:password" not in operator_text

    assert "raw-media-key" not in " ".join(parser.visible_control_text)
    assert 'onclick="openLb(' in rendered_page, "lightbox behavior must remain rendered"
    assert 'data-filename-hint' in rendered_page, "per-photo selection must remain rendered"
    assert 'data-photo-export-form' in rendered_page, "bulk/zip export form must remain rendered"
    assert 'data-export-selected-photos' in rendered_page, "zip export action must remain rendered"
    assert 'name="site_id"' in rendered_page, "field-photo filtering must remain rendered"
    assert field_photos.render_photo_file_drag_script() in rendered_page


def test_filename_fallback_and_path_like_metadata_never_expose_storage_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fallback = _sidecar(0)
    fallback.pop("source_filename")

    path_like = _sidecar(1)
    path_like["source_filename"] = "/srv/private/user:password/../../friendly capture.PNG"

    unsupported = _sidecar(2)
    unsupported["source_filename"] = "/srv/private/user:password/credentials.txt"

    provenance_name = _sidecar(3)
    provenance_name.pop("source_filename")
    provenance = dict(provenance_name["provenance"])
    provenance["original_filename"] = r"C:\private\user-password\front desk.webp"
    provenance_name["provenance"] = provenance

    page = _render_docs(monkeypatch, tmp_path, [fallback, path_like, unsupported, provenance_name])
    parser = _DragMarkupParser()
    parser.feed(page)
    names = [control.get("data-photo-drag-filename") or "" for control in parser.controls]

    assert names[0] == "lobby-001-photo-001.jpg"
    assert names[1] == "lobby-002-friendly-capture.jpg"
    assert names[2] == "lobby-003-photo-003.jpg"
    assert names[3] == "lobby-004-front-desk.jpg"
    operator_text = " ".join(
        " ".join(
            [
                control.get("data-photo-drag-filename") or "",
                control.get("aria-label") or "",
                control.get("title") or "",
            ]
        )
        for control in parser.controls
    )
    for forbidden in ("raw-media-key", "/srv/", "C:\\", "user:password", "credentials.txt", "<", ">"):
        assert forbidden not in operator_text


def test_drag_control_css_keeps_keyboard_focus_and_theme_tokens() -> None:
    css = _ADMIN_CSS.read_text(encoding="utf-8")
    drag_section = css.split("/* ---- Field photo file drag", 1)[1].split("@media", 1)[0]
    assert ".field-photo-file-drag:focus-visible" in drag_section
    assert "outline:" in drag_section
    assert "min-height: 36px" in drag_section
    assert "var(--btn-bg)" in drag_section
    assert "var(--text)" in drag_section
    assert "var(--accent)" in drag_section
    assert "var(--danger)" in drag_section
    for selector in (
        ".field-photo-file-drag.is-preparing",
        ".field-photo-file-drag.is-ready",
        ".field-photo-file-drag.is-fallback",
    ):
        state_rules = drag_section.split(selector + " {", 1)[1].split("}", 1)[0]
        assert "color: var(--text)" in state_rules
        assert "border-color:" in state_rules
        assert "background:" in state_rules
