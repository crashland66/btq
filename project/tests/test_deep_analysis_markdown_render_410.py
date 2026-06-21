"""Independent verifier tests for 410 — safe deep-analysis result markdown render.

Covers the contract:
  * render_deep_analysis_markdown (common.py): escape-FIRST safe markdown subset
    emitting ONLY a controlled tag set — bold, italic, ordered list (single <ol>),
    unordered list (<ul>), paragraphs, bold-inside-list-item, mixed document.
  * XSS battery (critical): model-generated result containing <script>, a raw
    </script> breakout attempt, <img onerror=...>, <a href=javascript:...>, and a
    quote-heavy string all come out ENTITY-ESCAPED — never as a live executable
    tag. The only tags in the output are the controlled set (div/p/strong/em/ol/
    ul/li/br).
  * Integration: field_photos._deep_analysis_payload exposes a safe result_html
    (escaped for malicious input, formatted for benign markdown); the capture-detail
    captures._render_deep_analysis_result likewise escapes + formats.

The renderer is the security boundary: html.escape MUST run before any markdown
substitution so no model-supplied tag can survive. The mutation check (verifier's
notes) removes that escape-first step and confirms these XSS tests redden.

stdlib only — no live CouchDB needed for these unit/integration paths.
"""

from __future__ import annotations

import re

import pytest

from ops_dashboard.common import render_deep_analysis_markdown
from ops_dashboard.sections import captures, field_photos

# Controlled tag set the renderer is allowed to emit (opening + closing forms).
_ALLOWED_TAGS = {"div", "p", "strong", "em", "ol", "ul", "li", "br"}
_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)")


def _tags_in(html_out: str) -> set[str]:
    return {m.group(1).lower() for m in _TAG_RE.finditer(html_out)}


def _assert_only_allowed_tags(html_out: str) -> None:
    extra = _tags_in(html_out) - _ALLOWED_TAGS
    assert not extra, f"unexpected tags emitted: {extra} in {html_out!r}"


# ---------------------------------------------------------------------------
# Rendering — the markdown subset
# ---------------------------------------------------------------------------
def test_wraps_in_container_div():
    out = render_deep_analysis_markdown("hello")
    assert out.startswith('<div class="deep-analysis-md">')
    assert out.endswith("</div>")


def test_bold():
    out = render_deep_analysis_markdown("a **bold** word")
    assert "<strong>bold</strong>" in out
    assert "**" not in out
    _assert_only_allowed_tags(out)


def test_italic_asterisk():
    out = render_deep_analysis_markdown("a *italic* word")
    assert "<em>italic</em>" in out
    _assert_only_allowed_tags(out)


def test_italic_underscore():
    out = render_deep_analysis_markdown("a _italic_ word")
    assert "<em>italic</em>" in out
    _assert_only_allowed_tags(out)


def test_ordered_list_single_ol_with_items():
    out = render_deep_analysis_markdown("1. First\n2. Second\n3. Third")
    # exactly one <ol> wrapping all the items
    assert out.count("<ol>") == 1
    assert out.count("</ol>") == 1
    assert "<li>First</li>" in out
    assert "<li>Second</li>" in out
    assert "<li>Third</li>" in out
    # the "1." / "2." literal numbering markers are consumed, not echoed
    assert "1." not in out
    _assert_only_allowed_tags(out)


def test_unordered_list_dash_and_star():
    out = render_deep_analysis_markdown("- alpha\n* beta")
    assert out.count("<ul>") == 1
    assert "<li>alpha</li>" in out
    assert "<li>beta</li>" in out
    assert "<ol" not in out
    _assert_only_allowed_tags(out)


def test_paragraph_blocks_split_on_blank_line():
    out = render_deep_analysis_markdown("para one\n\npara two")
    assert "<p>para one</p>" in out
    assert "<p>para two</p>" in out


def test_single_newline_is_br_within_block():
    out = render_deep_analysis_markdown("line one\nline two")
    assert "<br>" in out
    # one paragraph, not two
    assert out.count("<p>") == 1


def test_bold_inside_list_item():
    out = render_deep_analysis_markdown("1. **Header**: detail text")
    assert "<li><strong>Header</strong>: detail text</li>" in out
    _assert_only_allowed_tags(out)


def test_bold_inside_unordered_list_item():
    out = render_deep_analysis_markdown("- **Wear**: scuffs visible")
    assert "<li><strong>Wear</strong>: scuffs visible</li>" in out
    _assert_only_allowed_tags(out)


def test_mixed_document_paragraph_plus_list():
    """Shape of a real model output: intro paragraph then a numbered findings list."""
    text = (
        "Overall the floor shows **moderate** wear.\n\n"
        "1. **Scuffing**: near the entrance\n"
        "2. **Staining**: under the table\n\n"
        "Recommend a strip and wax."
    )
    out = render_deep_analysis_markdown(text)
    assert "<p>Overall the floor shows <strong>moderate</strong> wear.</p>" in out
    assert out.count("<ol>") == 1
    assert "<li><strong>Scuffing</strong>: near the entrance</li>" in out
    assert "<li><strong>Staining</strong>: under the table</li>" in out
    assert "<p>Recommend a strip and wax.</p>" in out
    _assert_only_allowed_tags(out)


def test_dict_input_is_escaped_json_not_crash():
    out = render_deep_analysis_markdown({"key": "<b>x</b>"})
    assert "<b>" not in out
    assert "&lt;b&gt;" in out
    _assert_only_allowed_tags(out)


def test_none_and_empty_safe():
    assert render_deep_analysis_markdown(None) == '<div class="deep-analysis-md"></div>'
    assert render_deep_analysis_markdown("") == '<div class="deep-analysis-md"></div>'


# ---------------------------------------------------------------------------
# XSS battery (critical) — escape-first is the security boundary.
# For each malicious input: assert NO live executable tag, the payload is
# entity-escaped, and ONLY the controlled tag set appears.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "before </script> after",  # script-context breakout attempt
        "<img src=x onerror=alert(1)>",
        '<a href="javascript:alert(1)">click</a>',
        "<svg/onload=alert(1)>",
        "<iframe src=javascript:alert(1)></iframe>",
        '"><script>alert(document.cookie)</script>',
    ],
)
def test_xss_payload_never_yields_live_tag(payload):
    out = render_deep_analysis_markdown(payload)
    low = out.lower()
    # No live tags from the model payload survive.
    assert "<script" not in low
    assert "</script>" not in low
    assert "<img" not in low
    assert "<svg" not in low
    assert "<iframe" not in low
    assert "<a " not in low and "<a>" not in low
    # The dangerous chars came out as entities.
    assert "&lt;" in out
    assert "&gt;" in out
    # Only our controlled tags exist in the output.
    _assert_only_allowed_tags(out)


def test_xss_script_is_entity_escaped():
    out = render_deep_analysis_markdown("<script>alert(1)</script>")
    assert "<script" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;/script&gt;" in out


def test_xss_img_onerror_inert():
    out = render_deep_analysis_markdown("<img src=x onerror=alert(1)>")
    # The <img tag must not survive; onerror text may remain but only as inert
    # content inside the escaped &lt;img ...&gt; — there is no live <img to hang it on.
    assert "<img" not in out.lower()
    assert "&lt;img" in out


def test_xss_javascript_href_no_anchor_tag():
    out = render_deep_analysis_markdown('<a href="javascript:alert(1)">x</a>')
    assert "<a " not in out.lower()
    assert "&lt;a href=" in out


def test_xss_quote_heavy_string():
    out = render_deep_analysis_markdown("she said \"hi\" and 'bye' & <ok>")
    assert "&quot;" in out
    assert "&#x27;" in out
    assert "&amp;" in out
    assert "&lt;ok&gt;" in out
    assert "<ok>" not in out
    _assert_only_allowed_tags(out)


def test_xss_markdown_cannot_smuggle_tag_via_bold():
    """A model could try **<script>** — the script must still be escaped, only
    the <strong> wrapper is our own controlled tag."""
    out = render_deep_analysis_markdown("**<script>alert(1)</script>**")
    assert "<script" not in out.lower()
    assert "<strong>" in out
    assert "&lt;script&gt;" in out
    _assert_only_allowed_tags(out)


# ---------------------------------------------------------------------------
# Integration — field_photos view popup payload
# ---------------------------------------------------------------------------
def _entry(result):
    return {
        "prompt_id": "wear_tear",
        "label": "Wear & tear",
        "status": "succeeded",
        "model_provider": "mlx",
        "model_name": "vlm",
        "actor": "greg",
        "generated_at": "2026-06-21T10:00:00Z",
        "result": result,
    }


def test_field_photos_payload_has_safe_result_html_for_malicious():
    payload = field_photos._deep_analysis_payload(
        [_entry("<script>alert(1)</script>")]
    )
    assert len(payload) == 1
    html_out = payload[0]["result_html"]
    assert "<script" not in html_out.lower()
    assert "&lt;script&gt;" in html_out
    _assert_only_allowed_tags(html_out)
    # raw result is still carried (the JS uses result_html for display)
    assert payload[0]["result"] == "<script>alert(1)</script>"


def test_field_photos_payload_formats_benign_markdown():
    payload = field_photos._deep_analysis_payload(
        [_entry("Intro.\n\n1. **One**: a\n2. **Two**: b")]
    )
    html_out = payload[0]["result_html"]
    assert "<strong>One</strong>" in html_out
    assert html_out.count("<ol>") == 1
    assert "<li><strong>One</strong>: a</li>" in html_out
    _assert_only_allowed_tags(html_out)


def test_field_photos_other_fields_remain_plain_text():
    """Only the result becomes rich; other fields stay raw strings (escaped as
    textContent client-side, not via result_html)."""
    payload = field_photos._deep_analysis_payload(
        [_entry("ok")]
    )[0]
    assert payload["prompt"] == "Wear & tear"
    assert payload["model"] == "mlx / vlm"
    assert payload["actor"] == "greg"
    assert payload["status"] == "succeeded"
    assert payload["generated_at"] == "2026-06-21T10:00:00Z"


# ---------------------------------------------------------------------------
# Integration — capture-detail block
# ---------------------------------------------------------------------------
def test_capture_detail_escapes_malicious_result():
    out = captures._render_deep_analysis_result("<script>alert(1)</script>")
    assert "<script" not in out.lower()
    assert "&lt;script&gt;" in out
    _assert_only_allowed_tags(out)


def test_capture_detail_formats_benign_markdown():
    out = captures._render_deep_analysis_result(
        "1. **First**: a\n2. **Second**: b"
    )
    assert out.count("<ol>") == 1
    assert "<li><strong>First</strong>: a</li>" in out
    _assert_only_allowed_tags(out)


def test_capture_detail_empty_result_message():
    assert "No result text." in captures._render_deep_analysis_result("")
    assert "No result text." in captures._render_deep_analysis_result(None)


def test_capture_detail_dict_result_escaped():
    out = captures._render_deep_analysis_result({"note": "<b>x</b>"})
    assert "<b>" not in out
    assert "&lt;b&gt;" in out
    _assert_only_allowed_tags(out)
