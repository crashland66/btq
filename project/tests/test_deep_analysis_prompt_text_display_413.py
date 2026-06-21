"""413: the actual deep-analysis prompt text (incl. custom) is surfaced + escaped."""
from __future__ import annotations

from ops_dashboard.sections.captures import _render_deep_analysis_prompt_text
from ops_dashboard.sections.field_photos import _deep_analysis_payload


def test_detail_shows_custom_prompt_text():
    out = _render_deep_analysis_prompt_text({"prompt_id": "custom", "prompt_text": "advise on the lab sink please"})
    assert "Prompt:" in out and "advise on the lab sink please" in out


def test_detail_prompt_text_is_escaped():
    out = _render_deep_analysis_prompt_text({"prompt_id": "custom", "prompt_text": "<script>alert(1)</script>"})
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_detail_empty_prompt_text_renders_nothing():
    assert _render_deep_analysis_prompt_text({"prompt_id": "custom", "prompt_text": ""}) == ""


def test_field_photos_payload_carries_prompt_text():
    payload = _deep_analysis_payload([{"prompt_id": "custom", "prompt_text": "my custom q", "result": "r", "status": "completed"}])
    assert payload and payload[0]["prompt_text"] == "my custom q"
