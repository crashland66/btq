"""Independent verifier tests for 409 — /field-photos deep-analysis glyphs.

Covers the contract:
  * field_photos render: each card gets a "run deeper analysis" glyph opening a
    shared RUN <dialog> (7 preset labels + Custom option + custom_prompt textarea,
    form action=/captures/analyze-deeper, hidden capture_id/photo_asset_id/confirm/
    return_to); the card's capture_id/photo_asset_id are available to JS.
  * cards WITH a non-empty deep_analysis list ALSO render a "view" glyph; cards
    WITHOUT it do NOT.
  * XSS guard (critical): model-generated result / prompt / actor text must NOT be
    injected unescaped into the page.
  * return_to (critical): handle_analyze_deeper_post honors a safe local return_to
    (/field-photos…) but REJECTS open-redirect values (https://, //host, absent →
    capture-detail default); valid submission stages exactly one job, invalid none.

Sandbox-only fixture identity. No live CouchDB.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from field_capture.deep_analysis import DEEP_ANALYSIS_PRESETS
from ops_dashboard.common import SectionContext
from ops_dashboard.sections import captures
from ops_dashboard.sections import field_photos
from queue_spec import DEEP_ANALYSIS_PRESET_IDS, validate_job

PRESET_LABELS = [str(p["label"]) for p in DEEP_ANALYSIS_PRESETS]

# The static dialog JS always contains the selector strings
# ('[data-deep-analysis-run]' / '[data-deep-analysis-view]'), so presence of the
# bare attribute name is NOT a reliable signal. Match the rendered icon-button
# markup instead (aria-label only appears on actual per-card buttons).
RUN_BTN = 'aria-label="Run deeper analysis"'
VIEW_BTN = 'aria-label="View deeper analysis"'


# ---------------------------------------------------------------------------
# render harness — drive the CouchDB path (where deep_analysis actually lives;
# the disk-cache summary drops deep_analysis on purpose). We stub the CouchDB
# config + the doc/id queries so render() consumes raw sidecar docs verbatim.
# ---------------------------------------------------------------------------
def _section_ctx(runtime_root: Path) -> SectionContext:
    vault = runtime_root / "vault"
    return SectionContext(runtime_root, lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))


def _sidecar_doc(
    name: str,
    *,
    deep_analysis: object | None = None,
    description: str = "A clean photo.",
) -> dict:
    doc = {
        "doc_type": "photo_vision_sidecar",
        "status": "completed",
        "capture_id": f"cap-{name}",
        "photo_id": f"photo-{name}",
        "photo_asset_id": name,
        "site_id": "7050",
        "generated_at": "2026-05-01T12:00:00Z",
        "description": description,
        "area_guess": "restroom",
        "visible_objects": [],
        "possible_conditions": [],
        "possible_issues": [],
        "confidence": 0.8,
        "provenance": {"image_media_url": f"/media/{name}.jpg"},
    }
    if deep_analysis is not None:
        doc["deep_analysis"] = deep_analysis
    return doc


def _render_page(runtime_root: Path, monkeypatch, docs: list[dict]) -> str:
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr(field_photos, "_query_couchdb", lambda _cfg, _mango: docs)
    monkeypatch.setattr(
        field_photos,
        "_query_processed_asset_ids",
        lambda _cfg: {str(d.get("photo_asset_id") or "") for d in docs},
    )
    monkeypatch.setattr(field_photos, "_query_terminal_capture_ids", lambda _cfg: set())
    return field_photos.render(_section_ctx(runtime_root))


def _good_entry(**over):
    base = {
        "prompt_id": "condition_detail",
        "status": "complete",
        "result": "Floor looks freshly mopped.",
        "model_provider": "mlx",
        "model_name": "qwen2-vl",
        "actor": "Greg",
        "generated_at": "2026-06-21T10:00:00Z",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Card WITH deep_analysis → both glyphs + run dialog contract
# ---------------------------------------------------------------------------
def test_card_with_analysis_renders_both_glyphs_and_run_dialog(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    docs = [_sidecar_doc("with-analysis", deep_analysis=[_good_entry()])]

    html = _render_page(runtime_root, monkeypatch, docs)

    # run glyph present (per-card button) — match the rendered button, not the JS selector
    assert RUN_BTN in html
    assert 'data-capture-id="cap-with-analysis"' in html
    assert 'data-photo-asset-id="with-analysis"' in html
    # view glyph present because deep_analysis is a non-empty list
    assert VIEW_BTN in html
    assert "data-analysis=" in html

    # RUN dialog: form action + all 7 preset labels + Custom + textarea
    assert 'action="/captures/analyze-deeper"' in html
    for label in PRESET_LABELS:
        assert label in html, f"missing preset label: {label}"
    assert len(PRESET_LABELS) == 7
    assert 'value="custom"' in html
    assert 'name="custom_prompt"' in html
    assert "<textarea" in html
    # hidden contract fields
    assert 'name="capture_id"' in html
    assert 'name="photo_asset_id"' in html
    assert 'name="confirm"' in html and 'value="1"' in html
    assert 'name="return_to"' in html
    assert 'value="/field-photos"' in html
    # preset_id select present
    assert 'name="preset_id"' in html


# ---------------------------------------------------------------------------
# Card WITHOUT deep_analysis → run glyph present, view glyph ABSENT
# ---------------------------------------------------------------------------
def test_card_without_analysis_has_run_glyph_but_no_view_glyph(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    docs = [_sidecar_doc("no-analysis", deep_analysis=None)]

    html = _render_page(runtime_root, monkeypatch, docs)

    assert RUN_BTN in html, "run glyph must always be present"
    assert VIEW_BTN not in html, "view glyph must NOT render without analysis"


def test_card_with_empty_analysis_list_has_no_view_glyph(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    docs = [_sidecar_doc("empty-analysis", deep_analysis=[])]

    html = _render_page(runtime_root, monkeypatch, docs)

    assert RUN_BTN in html
    assert VIEW_BTN not in html, "empty list is not a non-empty list"


# ---------------------------------------------------------------------------
# XSS guard (critical) — model-generated text must be escaped
# ---------------------------------------------------------------------------
def test_view_glyph_escapes_malicious_result_text(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    malicious = "<script>alert('x')</script></script>"
    entry = _good_entry(result=malicious, actor="Greg<b>", label="<img src=x onerror=alert(1)>")
    docs = [_sidecar_doc("xss-card", deep_analysis=[entry])]

    html = _render_page(runtime_root, monkeypatch, docs)

    # raw dangerous markup must NOT appear anywhere in the page
    assert "<script>alert('x')</script>" not in html
    assert "</script></script>" not in html
    assert "Greg<b>" not in html
    assert "<img src=x onerror=alert(1)>" not in html
    # the data is carried as an HTML-escaped JSON attribute
    assert "&lt;script&gt;" in html
    assert "&lt;/script&gt;" in html


def test_malicious_description_escaped(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    docs = [
        _sidecar_doc(
            "desc-card",
            deep_analysis=[_good_entry()],
            description="<script>alert('desc')</script>",
        )
    ]

    html = _render_page(runtime_root, monkeypatch, docs)

    assert "<script>alert('desc')</script>" not in html
    assert "&lt;script&gt;alert(&#x27;desc&#x27;)&lt;/script&gt;" in html


# ---------------------------------------------------------------------------
# return_to (critical) — open-redirect guard
# ---------------------------------------------------------------------------
def _ctx(runtime_root: Path):
    return SimpleNamespace(runtime_root=runtime_root)


def _post(ctx, fields: dict) -> tuple:
    body = urllib.parse.urlencode(fields).encode()
    return captures.handle_analyze_deeper_post(ctx, body)


def _queue_files(runtime_root: Path) -> list[Path]:
    qdir = runtime_root / "queue"
    return list(qdir.glob("deep-analysis-*.json")) if qdir.exists() else []


def _base_valid(**over) -> dict:
    fields = {
        "confirm": "1",
        "capture_id": "cap-9",
        "photo_asset_id": "fcp-9",
        "actor": "Greg",
        "preset_id": DEEP_ANALYSIS_PRESET_IDS[0],
        "custom_prompt": "",
    }
    fields.update(over)
    return fields


def test_return_to_safe_local_path_honored(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _base_valid(return_to="/field-photos"))
    assert int(status) == 303
    loc = headers["Location"]
    assert loc.startswith("/field-photos")
    assert "message=deep_analysis_queued" in loc
    # exactly one job staged
    assert len(_queue_files(tmp_path)) == 1


def test_return_to_with_query_string_honored(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _base_valid(return_to="/field-photos?site_id=7050"))
    loc = headers["Location"]
    assert loc.startswith("/field-photos?")
    assert "site_id=7050" in loc
    assert "message=deep_analysis_queued" in loc


@pytest.mark.parametrize(
    "evil",
    [
        "https://evil.com",
        "http://evil.com/path",
        "//evil.com",
        "//evil.com/x",
        "javascript:alert(1)",
        "ftp://evil.com",
        "////evil.com",
    ],
)
def test_return_to_open_redirect_rejected_falls_back_to_default(tmp_path, evil):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _base_valid(return_to=evil))
    loc = headers["Location"]
    assert int(status) == 303
    # must NOT redirect off-site / to the attacker value
    assert "evil.com" not in loc
    assert not loc.startswith("//")
    assert "://" not in loc
    # falls back to the capture-detail default
    assert loc.startswith("/captures?capture_id=cap-9")
    assert "message=deep_analysis_queued" in loc
    # job still staged (validation only changes redirect target, not behaviour)
    assert len(_queue_files(tmp_path)) == 1


def test_return_to_absent_uses_capture_detail_default(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _base_valid())  # no return_to
    loc = headers["Location"]
    assert loc.startswith("/captures?capture_id=cap-9")
    assert "message=deep_analysis_queued" in loc


def test_invalid_submission_errors_without_staging_even_with_return_to(tmp_path):
    ctx = _ctx(tmp_path)
    # missing confirm → reject
    status, _c, _b, headers = _post(
        ctx,
        {
            "capture_id": "cap-x",
            "photo_asset_id": "fcp-x",
            "actor": "Greg",
            "preset_id": DEEP_ANALYSIS_PRESET_IDS[0],
            "return_to": "/field-photos",
        },
    )
    assert int(status) == 303
    assert "error=" in headers["Location"]
    assert "message=deep_analysis_queued" not in headers["Location"]
    assert _queue_files(tmp_path) == []


def test_error_redirect_honors_safe_return_to(tmp_path):
    """An invalid submission with a safe return_to should error back to /field-photos."""
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(
        ctx,
        {
            "confirm": "1",
            "capture_id": "cap-x",
            "photo_asset_id": "fcp-x",
            "actor": "",  # missing actor → fail
            "preset_id": DEEP_ANALYSIS_PRESET_IDS[0],
            "return_to": "/field-photos",
        },
    )
    loc = headers["Location"]
    assert loc.startswith("/field-photos")
    assert "error=" in loc
    assert _queue_files(tmp_path) == []


def test_valid_custom_submission_stages_one_job_with_return_to(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(
        ctx,
        {
            "confirm": "1",
            "capture_id": "cap-c",
            "photo_asset_id": "fcp-c",
            "actor": "Greg",
            "preset_id": "custom",
            "custom_prompt": "Count the chairs",
            "return_to": "/field-photos",
        },
    )
    files = _queue_files(tmp_path)
    assert len(files) == 1
    job = json.loads(files[0].read_text())
    assert validate_job(job) is True
    assert job["payload"]["custom_prompt"] == "Count the chairs"
    assert headers["Location"].startswith("/field-photos")
    assert "message=deep_analysis_queued" in headers["Location"]
