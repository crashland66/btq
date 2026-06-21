"""INDEPENDENT VERIFIER tests for 412b — the "Send to shift report" control on
each deep-analysis entry across BOTH dashboard surfaces.

Surface B (capture-detail, server-rendered):
  * captures.render_deep_analysis_results / render_photo_processing_details emit a
    per-entry <form action="/captures/send-to-shift-report"> with the hidden
    fields the 412a endpoint requires (content == raw entry result, capture_id,
    photo_asset_id, site_id, prompt_id, prompt_label, actor, confirm=1, return_to)
    and an onsubmit confirm.
  * XSS: an entry whose result contains <script>/" is HTML-escaped in the hidden
    content value (no live tag, no attribute breakout).

Surface A (/field-photos view popup, JS-rendered):
  * the data-analysis payload carries per-entry result + prompt_id (+ prompt label),
    the view button carries site_id, and the popup JS references
    /captures/send-to-shift-report + confirm( + the field set the send needs.

End-to-end field agreement:
  * the field set the detail form posts, fed to handle_send_to_shift_report (412a),
    stages exactly one shift_report_note job (i.e. the surfaces and the endpoint
    agree on field names).

Run from repo root:
    .venv/bin/python -m pytest project/tests/test_ops_dashboard_send_to_shift_report_glyph_412b.py -q
"""

from __future__ import annotations

import json
import re
import urllib.parse
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from ops_dashboard.sections import captures
from ops_dashboard.sections import field_photos
from queue_spec import validate_job


SEND_ACTION = "/captures/send-to-shift-report"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _record(deep_analysis=None, **overrides):
    rec = {
        "capture_id": "cap-77",
        "photo_asset_id": "fcp-77",
        "site_id": "7050",
        "area_guess": "Lobby",
        "submitter_name": "sandbox-user",
    }
    rec.update(overrides)
    if deep_analysis is not None:
        rec["deep_analysis"] = deep_analysis
    return rec


def _entry(**overrides):
    e = {
        "prompt_id": "damage_hazard",
        "label": "Damage / hazard",
        "status": "complete",
        "result": "Threshold damaged at west entrance. Needs repair.",
        "model_provider": "mlx",
        "model_name": "qwen2-vl",
        "actor": "Greg",
        "generated_at": "2026-06-21T10:00:00Z",
    }
    e.update(overrides)
    return e


class _FormFieldParser(HTMLParser):
    """Collect (form-action -> {hidden-name: hidden-value, _onsubmit: ...})."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {
                "action": a.get("action", ""),
                "method": (a.get("method") or "").lower(),
                "onsubmit": a.get("onsubmit", ""),
                "hidden": {},
            }
            self.forms.append(self._cur)
        elif tag == "input" and self._cur is not None and a.get("type") == "hidden":
            self._cur["hidden"][a.get("name", "")] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def _send_forms(markup: str) -> list[dict]:
    p = _FormFieldParser()
    p.feed(markup)
    return [f for f in p.forms if f["action"] == SEND_ACTION]


# ---------------------------------------------------------------------------
# Surface B — capture-detail server-rendered send form
# ---------------------------------------------------------------------------
def test_detail_renders_send_form_with_required_fields():
    entry = _entry()
    markup = captures.render_deep_analysis_results(_record(deep_analysis=[entry]))
    forms = _send_forms(markup)
    assert len(forms) == 1, "expected exactly one send-to-shift-report form for one entry"
    form = forms[0]
    assert form["method"] == "post"
    # onsubmit confirm present
    assert "confirm(" in form["onsubmit"]
    # hidden fields the 412a endpoint requires
    h = form["hidden"]
    # content == RAW entry result (parser unescapes char refs already)
    assert h["content"] == entry["result"]
    assert h["capture_id"] == "cap-77"
    assert h["photo_asset_id"] == "fcp-77"
    assert h["site_id"] == "7050"
    assert h["prompt_id"] == "damage_hazard"
    assert h["prompt_label"] == "Damage / hazard"
    assert h["actor"]  # default_actor() prefilled, non-empty
    assert h["confirm"] == "1"
    # return_to points back at the capture detail
    assert h["return_to"].startswith("/captures?capture_id=")
    assert "cap-77" in h["return_to"]


def test_detail_form_present_via_processing_details_wrapper():
    # the surface operators actually see goes through render_photo_processing_details
    markup = captures.render_photo_processing_details(_record(deep_analysis=[_entry()]))
    assert SEND_ACTION in markup
    assert len(_send_forms(markup)) == 1


def test_detail_one_send_form_per_entry():
    entries = [_entry(prompt_id="damage_hazard"), _entry(prompt_id="condition_detail", result="Floor clean.")]
    markup = captures.render_deep_analysis_results(_record(deep_analysis=entries))
    forms = _send_forms(markup)
    assert len(forms) == 2
    contents = sorted(f["hidden"]["content"] for f in forms)
    assert contents == sorted(["Threshold damaged at west entrance. Needs repair.", "Floor clean."])


def test_detail_no_send_form_when_no_deep_analysis():
    markup = captures.render_photo_processing_details(_record())
    assert SEND_ACTION not in markup


# ---------------------------------------------------------------------------
# Surface B — XSS: model-generated result escaped in the hidden content value
# ---------------------------------------------------------------------------
def test_detail_hidden_content_escapes_script_and_quote():
    payload = '<script>alert("xss")</script> & "break"'
    markup = captures.render_deep_analysis_results(_record(deep_analysis=[_entry(result=payload)]))
    # No live <script> tag anywhere in the markup.
    assert "<script>" not in markup
    # The raw " inside an attribute would break the value out; the escaped &quot;
    # must be present and the raw double-quoted payload absent in the hidden input.
    assert 'value="<script>' not in markup
    assert "&lt;script&gt;" in markup
    assert "&quot;" in markup
    # Round-trip: parser unescapes, so the decoded hidden value equals the raw text
    # (proves it was escaped, not dropped/mangled).
    form = _send_forms(markup)[0]
    assert form["hidden"]["content"] == payload
    # And there is no attribute breakout: the form action is intact, single form.
    assert len(_send_forms(markup)) == 1


# ---------------------------------------------------------------------------
# Surface A — /field-photos view popup payload + JS wiring
# ---------------------------------------------------------------------------
def _decode_data_analysis(actions_html: str) -> list[dict]:
    m = re.search(r'data-analysis="([^"]*)"', actions_html)
    assert m, "data-analysis attribute not found"
    return json.loads(unescape(m.group(1)))


def test_fieldphotos_payload_carries_result_prompt_and_site():
    sidecar = {
        "capture_id": "cap-fp",
        "photo_asset_id": "fcp-fp",
        "site_id": "1200",
        "deep_analysis": [_entry()],
    }
    actions = field_photos._render_deep_analysis_actions(sidecar)
    # view button carries site_id (needed by the send context)
    assert 'data-site-id="1200"' in actions
    entries = _decode_data_analysis(actions)
    assert len(entries) == 1
    e = entries[0]
    # raw result + prompt_id + prompt label all present for the send
    assert e["result"] == "Threshold damaged at west entrance. Needs repair."
    assert e["prompt_id"] == "damage_hazard"
    assert e["prompt"] == "Damage / hazard"


def test_fieldphotos_popup_js_references_send_endpoint_and_confirm():
    js = field_photos._render_deep_analysis_dialogs("/field-photos")
    assert SEND_ACTION in js
    assert "confirm(" in js
    # field set the send needs is wired in the fetch body
    for field in ("content", "capture_id", "photo_asset_id", "site_id", "prompt_id", "prompt_label", "actor", "confirm"):
        assert f"'{field}'" in js, f"popup JS does not set field {field!r}"
    # The SEND path uses the raw entry result as `content` (result_html may still
    # be used for the existing 410 result *display* — that's untouched by 412b).
    assert "'content'" in js
    m = re.search(r"params\.set\(\s*'content'\s*,\s*([^;]+?)\)\s*;", js)
    assert m, "could not find params.set('content', ...) in popup JS"
    content_expr = m.group(1)
    assert "entry.result" in content_expr
    assert "result_html" not in content_expr, "send content must be raw result, not result_html"
    # a per-entry send affordance is rendered
    assert "Send to shift report" in js


def test_fieldphotos_payload_result_is_raw_not_html():
    # XSS-shaped result rides the escaped data-analysis JSON; raw markup must not
    # leak into the attribute, and result is the raw text (sent as a JS string).
    sidecar = {
        "capture_id": "c",
        "photo_asset_id": "f",
        "site_id": "s",
        "deep_analysis": [_entry(result='<script>x</script> & "q"')],
    }
    actions = field_photos._render_deep_analysis_actions(sidecar)
    assert "<script>x</script>" not in actions  # escaped inside the attribute
    entries = _decode_data_analysis(actions)
    assert entries[0]["result"] == '<script>x</script> & "q"'


# ---------------------------------------------------------------------------
# End-to-end: detail form's field set is accepted by the 412a endpoint
# ---------------------------------------------------------------------------
def _ctx(runtime_root: Path):
    return SimpleNamespace(runtime_root=runtime_root)


def _queue_files(runtime_root: Path) -> list[Path]:
    qdir = runtime_root / "queue"
    return list(qdir.glob("shift-report-note-*.json")) if qdir.exists() else []


def _fields_from_detail_form(entry: dict, record: dict) -> dict:
    """Reconstruct the exact name/value pairs the detail send form would POST."""
    markup = captures.render_deep_analysis_results({**record, "deep_analysis": [entry]})
    form = _send_forms(markup)[0]
    return dict(form["hidden"])  # parser already unescaped char refs -> browser-submitted values


def test_detail_field_set_stages_exactly_one_job(tmp_path):
    entry = _entry()
    record = _record()
    fields = _fields_from_detail_form(entry, record)
    ctx = _ctx(tmp_path)
    body = urllib.parse.urlencode(fields).encode()
    status, _c, _b, headers = captures.handle_send_to_shift_report(ctx, body)
    files = _queue_files(tmp_path)
    assert len(files) == 1, "detail form field set must stage exactly one shift_report_note job"
    job = json.loads(files[0].read_text())
    assert job["job_type"] == "shift_report_note"
    assert validate_job(job) is True
    payload = job["payload"]
    assert payload["content"] == entry["result"]
    assert payload["capture_id"] == "cap-77"
    assert payload["photo_asset_id"] == "fcp-77"
    assert payload["site_id"] == "7050"
    assert payload["prompt_id"] == "damage_hazard"
    assert payload["prompt_label"] == "Damage / hazard"
    assert payload["actor"]
    assert int(status) == 303
    assert "message=shift_report_note_queued" in headers["Location"]


def test_detail_field_set_with_xss_result_stages_and_preserves_content(tmp_path):
    payload_text = '<script>alert("x")</script> & done'
    fields = _fields_from_detail_form(_entry(result=payload_text), _record())
    ctx = _ctx(tmp_path)
    body = urllib.parse.urlencode(fields).encode()
    status, _c, _b, headers = captures.handle_send_to_shift_report(ctx, body)
    files = _queue_files(tmp_path)
    assert len(files) == 1
    job = json.loads(files[0].read_text())
    # content survived the escape -> submit -> parse round-trip intact
    assert job["payload"]["content"] == payload_text
    assert int(status) == 303
    assert "message=shift_report_note_queued" in headers["Location"]
