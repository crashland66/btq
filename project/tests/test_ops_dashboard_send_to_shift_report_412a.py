"""INDEPENDENT VERIFIER tests for 412a — the "send to shift report" POST endpoint.

Covers captures.handle_send_to_shift_report:
  * valid POST (confirm=1 + content + actor + capture_id + photo_asset_id)
    -> exactly one shift-report-note queue file (passes validate_job), audit
    appended with success, 303 + success message
  * each invalid POST (no confirm / no content / no actor / no capture_id /
    no photo_asset_id) -> 0 files, 303 + error
  * safe return_to honored; open-redirect (https://evil.com, //evil) rejected
  * post-route dispatch maps /captures/send-to-shift-report to the handler

Run from repo root:
    .venv/bin/python -m pytest project/tests/test_ops_dashboard_send_to_shift_report_412a.py -q
"""

from __future__ import annotations

import json
import urllib.parse
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from ops_dashboard.sections import captures
from ops_dashboard import post_routes
from queue_spec import validate_job


def _ctx(runtime_root: Path):
    return SimpleNamespace(runtime_root=runtime_root)


def _post(ctx, fields: dict) -> tuple:
    body = urllib.parse.urlencode(fields).encode()
    return captures.handle_send_to_shift_report(ctx, body)


def _queue_files(runtime_root: Path) -> list[Path]:
    qdir = runtime_root / "queue"
    if not qdir.exists():
        return []
    return [p for p in qdir.glob("shift-report-note-*.json")]


def _audit_lines(runtime_root: Path) -> list[dict]:
    log = runtime_root / "logs" / "admin_audit.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _valid_fields(**overrides) -> dict:
    fields = {
        "confirm": "1",
        "capture_id": "cap-9",
        "photo_asset_id": "fcp-9",
        "actor": "Greg",
        "content": "Threshold damaged at west entrance.",
    }
    fields.update(overrides)
    return fields


# ---------------------------------------------------------------------------
# valid POST
# ---------------------------------------------------------------------------
def test_post_valid_stages_one_job(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _valid_fields(site_id="7050", prompt_id="damage_hazard", prompt_label="Damage / hazard"))
    files = _queue_files(tmp_path)
    assert len(files) == 1
    job = json.loads(files[0].read_text())
    assert job["job_type"] == "shift_report_note"
    assert validate_job(job) is True
    payload = job["payload"]
    assert payload["capture_id"] == "cap-9"
    assert payload["photo_asset_id"] == "fcp-9"
    assert payload["actor"] == "Greg"
    assert payload["content"] == "Threshold damaged at west entrance."
    assert payload["date"]  # computed (today) when not posted
    assert payload["site_id"] == "7050"
    assert payload["prompt_id"] == "damage_hazard"
    assert payload["prompt_label"] == "Damage / hazard"
    assert int(status) == 303
    assert "message=shift_report_note_queued" in headers["Location"]
    audits = _audit_lines(tmp_path)
    assert any("success" in a["result_summary"] for a in audits)
    assert audits[-1]["route"] == "/captures/send-to-shift-report"


def test_post_valid_honors_posted_date(tmp_path):
    ctx = _ctx(tmp_path)
    _post(ctx, _valid_fields(date="2026-06-10"))
    job = json.loads(_queue_files(tmp_path)[0].read_text())
    assert job["payload"]["date"] == "2026-06-10"


def test_post_valid_minimal_no_optionals(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _valid_fields())
    job = json.loads(_queue_files(tmp_path)[0].read_text())
    assert validate_job(job) is True
    assert "site_id" not in job["payload"]
    assert int(status) == 303


# ---------------------------------------------------------------------------
# invalid POST -> stage NOTHING, 303 + error
# ---------------------------------------------------------------------------
def _assert_rejected(tmp_path, headers, status):
    assert _queue_files(tmp_path) == []
    assert int(status) == 303
    assert "error=" in headers["Location"]
    assert "message=shift_report_note_queued" not in headers["Location"]


def test_post_no_confirm_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    fields = _valid_fields()
    del fields["confirm"]
    status, _c, _b, headers = _post(ctx, fields)
    _assert_rejected(tmp_path, headers, status)


def test_post_no_content_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _valid_fields(content=""))
    _assert_rejected(tmp_path, headers, status)


def test_post_no_actor_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _valid_fields(actor=""))
    _assert_rejected(tmp_path, headers, status)


def test_post_no_capture_id_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _valid_fields(capture_id=""))
    _assert_rejected(tmp_path, headers, status)


def test_post_no_photo_asset_id_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _valid_fields(photo_asset_id=""))
    _assert_rejected(tmp_path, headers, status)


def test_post_whitespace_content_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _valid_fields(content="   "))
    _assert_rejected(tmp_path, headers, status)


# ---------------------------------------------------------------------------
# return_to: safe honored, open-redirect rejected
# ---------------------------------------------------------------------------
def test_safe_return_to_honored(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _valid_fields(return_to="/captures?capture_id=cap-9"))
    loc = headers["Location"]
    assert loc.startswith("/captures")
    assert "message=shift_report_note_queued" in loc


def test_open_redirect_absolute_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _valid_fields(return_to="https://evil.com"))
    loc = headers["Location"]
    assert "evil.com" not in loc
    assert loc.startswith("/captures")  # fell back to safe base


def test_open_redirect_protocol_relative_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    status, _c, _b, headers = _post(ctx, _valid_fields(return_to="//evil.com/x"))
    loc = headers["Location"]
    assert "evil.com" not in loc
    assert loc.startswith("/captures")


def test_safe_return_to_helper_unit():
    assert captures._safe_return_to("/captures?x=1") == "/captures?x=1"
    assert captures._safe_return_to("https://evil.com") == ""
    assert captures._safe_return_to("//evil.com/x") == ""
    assert captures._safe_return_to("http://evil") == ""
    assert captures._safe_return_to("javascript:alert(1)") == ""


# ---------------------------------------------------------------------------
# post-route dispatch
# ---------------------------------------------------------------------------
def test_post_route_dispatch(tmp_path):
    ctx = _ctx(tmp_path)
    body = urllib.parse.urlencode(_valid_fields()).encode()
    result = post_routes.dispatch_post_route(
        "/captures/send-to-shift-report",
        ctx,
        body,
        "application/x-www-form-urlencoded",
        {},
    )
    assert result is not None
    status = result[0]
    assert int(status) == 303
    assert len(_queue_files(tmp_path)) == 1
