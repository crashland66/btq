"""Independent verifier tests for 406c — operator "Analyze deeper" dashboard action.

Covers:
  * common.write_deep_analysis_job — staged queue doc passes queue_spec.validate_job
    (preset-only AND custom-only)
  * captures.handle_analyze_deeper_post — valid preset / valid custom stage exactly
    one job (correct payload), audit appended, 303 + success message; invalid input
    (no confirm / missing actor / neither prompt / both prompts / unknown preset)
    stages NOTHING and 303 + error.
  * captures.render_photo_processing_details — per-photo "Analyze deeper" form
    (7 preset labels + Custom, confirm box, hidden capture_id/photo_asset_id) and
    the deep_analysis result list (escaped; a failed entry shown clearly).

Sandbox-only fixture identity.
"""

from __future__ import annotations

import json
import urllib.parse
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from ops_dashboard.common import write_deep_analysis_job
from ops_dashboard.sections import captures
from queue_spec import DEEP_ANALYSIS_PRESET_IDS, validate_job

from field_capture.deep_analysis import DEEP_ANALYSIS_PRESETS

PRESET_LABELS = [str(p["label"]) for p in DEEP_ANALYSIS_PRESETS]


def _ctx(runtime_root: Path):
    return SimpleNamespace(runtime_root=runtime_root)


def _post(ctx, fields: dict) -> tuple:
    body = urllib.parse.urlencode(fields).encode()
    return captures.handle_analyze_deeper_post(ctx, body)


def _queue_files(runtime_root: Path) -> list[Path]:
    qdir = runtime_root / "queue"
    if not qdir.exists():
        return []
    return [p for p in qdir.glob("deep-analysis-*.json")]


def _audit_lines(runtime_root: Path) -> list[dict]:
    log = runtime_root / "logs" / "admin_audit.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# write_deep_analysis_job
# ---------------------------------------------------------------------------
def test_write_job_preset_only_validates(tmp_path):
    preset = DEEP_ANALYSIS_PRESET_IDS[0]
    path = write_deep_analysis_job(
        tmp_path,
        capture_id="cap-1",
        photo_asset_id="fcp-1",
        actor="Greg",
        preset_id=preset,
    )
    assert path.exists()
    assert path.parent == tmp_path / "queue"
    assert path.name.startswith("deep-analysis-") and path.suffix == ".json"
    job = json.loads(path.read_text())
    assert job["job_type"] == "deep_analysis"
    assert validate_job(job) is True
    payload = job["payload"]
    assert payload["capture_id"] == "cap-1"
    assert payload["photo_asset_id"] == "fcp-1"
    assert payload["actor"] == "Greg"
    assert payload["preset_id"] == preset
    assert "custom_prompt" not in payload


def test_write_job_custom_only_validates(tmp_path):
    path = write_deep_analysis_job(
        tmp_path,
        capture_id="cap-2",
        photo_asset_id="fcp-2",
        actor="Greg",
        custom_prompt="What brand is this vacuum?",
    )
    job = json.loads(path.read_text())
    assert job["job_type"] == "deep_analysis"
    assert validate_job(job) is True
    payload = job["payload"]
    assert payload["custom_prompt"] == "What brand is this vacuum?"
    assert "preset_id" not in payload


def test_write_job_each_call_is_distinct(tmp_path):
    preset = DEEP_ANALYSIS_PRESET_IDS[0]
    p1 = write_deep_analysis_job(tmp_path, capture_id="c", photo_asset_id="f", actor="a", preset_id=preset)
    p2 = write_deep_analysis_job(tmp_path, capture_id="c", photo_asset_id="f", actor="a", preset_id=preset)
    assert p1 != p2
    assert len(_queue_files(tmp_path)) == 2


# ---------------------------------------------------------------------------
# POST — valid
# ---------------------------------------------------------------------------
def test_post_valid_preset_stages_one_job(tmp_path):
    ctx = _ctx(tmp_path)
    preset = DEEP_ANALYSIS_PRESET_IDS[0]
    status, _ctype, _body, headers = _post(
        ctx,
        {
            "confirm": "1",
            "capture_id": "cap-9",
            "photo_asset_id": "fcp-9",
            "actor": "Greg",
            "preset_id": preset,
            "custom_prompt": "",
        },
    )
    files = _queue_files(tmp_path)
    assert len(files) == 1
    job = json.loads(files[0].read_text())
    assert validate_job(job) is True
    payload = job["payload"]
    assert payload["capture_id"] == "cap-9"
    assert payload["photo_asset_id"] == "fcp-9"
    assert payload["actor"] == "Greg"
    assert payload["preset_id"] == preset
    assert "custom_prompt" not in payload
    # redirect with success message
    assert status == HTTPStatus.SEE_OTHER or int(status) == 303
    assert "message=deep_analysis_queued" in headers["Location"]
    # audit appended with success
    audits = _audit_lines(tmp_path)
    assert any("success" in a["result_summary"] for a in audits)
    assert audits[-1]["route"] == "/captures/analyze-deeper"


def test_post_valid_custom_stages_one_job(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(
        ctx,
        {
            "confirm": "1",
            "capture_id": "cap-10",
            "photo_asset_id": "fcp-10",
            "actor": "Greg",
            "preset_id": "custom",
            "custom_prompt": "Count the chairs",
        },
    )
    files = _queue_files(tmp_path)
    assert len(files) == 1
    job = json.loads(files[0].read_text())
    assert validate_job(job) is True
    payload = job["payload"]
    assert payload["custom_prompt"] == "Count the chairs"
    assert "preset_id" not in payload
    assert int(status) == 303
    assert "message=deep_analysis_queued" in headers["Location"]


# ---------------------------------------------------------------------------
# POST — invalid (stage NOTHING, 303 + error)
# ---------------------------------------------------------------------------
def _assert_rejected(tmp_path, headers, status):
    assert _queue_files(tmp_path) == []
    assert int(status) == 303
    assert "error=" in headers["Location"]
    assert "message=deep_analysis_queued" not in headers["Location"]


def test_post_no_confirm_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(
        ctx,
        {
            "capture_id": "cap-x",
            "photo_asset_id": "fcp-x",
            "actor": "Greg",
            "preset_id": DEEP_ANALYSIS_PRESET_IDS[0],
        },
    )
    _assert_rejected(tmp_path, headers, status)


def test_post_missing_actor_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(
        ctx,
        {
            "confirm": "1",
            "capture_id": "cap-x",
            "photo_asset_id": "fcp-x",
            "actor": "",
            "preset_id": DEEP_ANALYSIS_PRESET_IDS[0],
        },
    )
    _assert_rejected(tmp_path, headers, status)


def test_post_neither_prompt_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(
        ctx,
        {
            "confirm": "1",
            "capture_id": "cap-x",
            "photo_asset_id": "fcp-x",
            "actor": "Greg",
            "preset_id": "custom",
            "custom_prompt": "",
        },
    )
    _assert_rejected(tmp_path, headers, status)


def test_post_both_prompts_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(
        ctx,
        {
            "confirm": "1",
            "capture_id": "cap-x",
            "photo_asset_id": "fcp-x",
            "actor": "Greg",
            "preset_id": DEEP_ANALYSIS_PRESET_IDS[0],
            "custom_prompt": "also custom",
        },
    )
    _assert_rejected(tmp_path, headers, status)


def test_post_unknown_preset_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(
        ctx,
        {
            "confirm": "1",
            "capture_id": "cap-x",
            "photo_asset_id": "fcp-x",
            "actor": "Greg",
            "preset_id": "not_a_real_preset",
            "custom_prompt": "",
        },
    )
    _assert_rejected(tmp_path, headers, status)


# ---------------------------------------------------------------------------
# render — form + result display
# ---------------------------------------------------------------------------
def _record(deep_analysis=None):
    rec = {
        "capture_id": "cap-render",
        "photo_asset_id": "fcp-render",
        "area_guess": "Lobby",
        "submitter_name": "sandbox-user",
    }
    if deep_analysis is not None:
        rec["deep_analysis"] = deep_analysis
    return rec


def test_render_form_present_with_all_presets():
    html = captures.render_photo_processing_details(_record())
    assert 'action="/captures/analyze-deeper"' in html
    for label in PRESET_LABELS:
        assert label in html, f"missing preset label: {label}"
    assert len(PRESET_LABELS) == 7
    # custom option
    assert 'value="custom"' in html
    # confirm checkbox
    assert 'name="confirm"' in html
    assert 'type="checkbox"' in html
    # hidden capture_id / photo_asset_id
    assert 'name="capture_id"' in html and 'value="cap-render"' in html
    assert 'name="photo_asset_id"' in html and 'value="fcp-render"' in html


def test_render_deep_analysis_results_escaped_and_failed_shown():
    entries = [
        {
            "prompt_id": "condition_detail",
            "status": "complete",
            "result": "<script>alert('xss')</script> & done",
            "model_provider": "mlx",
            "model_name": "qwen2-vl",
            "actor": "Greg<b>",
            "generated_at": "2026-06-21T10:00:00Z",
        },
        {
            "prompt_id": "custom",
            "status": "failed",
            "result": "",
            "error": {"type": "VisionError", "message": "model timed out <oops>"},
            "actor": "Greg",
            "generated_at": "2026-06-21T11:00:00Z",
        },
    ]
    html = captures.render_photo_processing_details(_record(deep_analysis=entries))
    # raw dangerous markup must NOT appear; escaped form must
    assert "<script>alert('xss')</script>" not in html
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in html
    # actor escaped
    assert "Greg<b>" not in html
    assert "Greg&lt;b&gt;" in html
    # failed entry shown clearly with escaped error
    assert "Failed" in html
    assert "model timed out &lt;oops&gt;" in html
    assert "model timed out <oops>" not in html
    # model + generated_at surfaced
    assert "qwen2-vl" in html
    assert "2026-06-21T10:00:00Z" in html


def test_render_base_unchanged_when_no_deep_analysis():
    html = captures.render_photo_processing_details(_record())
    # No results block when the list is absent.
    assert "Deep analysis results" not in html
