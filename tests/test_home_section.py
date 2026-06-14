from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.common import SectionContext
from ops_dashboard.sections import home


def section_ctx(runtime_root: Path) -> SectionContext:
    vault = runtime_root / "vault"
    return SectionContext(runtime_root, lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))


def install_fake_home_views(monkeypatch: pytest.MonkeyPatch, *, by_type: list[dict] | None = None) -> None:
    view_rows: dict[str, list[dict]] = {
        "by_type": by_type or [],
        "locations_all": [],
        "employees_by_site": [],
        "opportunities_by_site_status": [],
        "visits_by_site_date": [],
    }

    def fake_query_view(
        _base_url: str,
        _auth_headers: dict,
        _database: str,
        _ddoc: str,
        view: str,
        **_kwargs: object,
    ) -> list[dict]:
        return view_rows[view]

    monkeypatch.setattr(home, "query_view", fake_query_view)
    monkeypatch.setattr(home, "inbox_cards", lambda rt: [])


def test_home_section_renders_account_grouped_site_table(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_home_views(
        monkeypatch,
        by_type=[
            {
                "key": ["opportunity", "opp_x"],
                "value": None,
                "doc": {
                    "type": "opportunity",
                    "_id": "opp_x",
                    "site_id": "42",
                    "location": "Test Site",
                    "account": "TestCo",
                },
            }
        ],
    )
    ctx = section_ctx(Path("."))
    html = home.render(ctx)
    assert "TestCo" in html
    assert "/sites/42" in html


def test_home_section_renders_operational_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    # Redesign (372): the two-up "needs attention" lists are now sentence-case
    # named panels ("No visit in 30 days · N" / "Open opportunities · N"), not
    # the old title-case "Sites With ..." headings. Behavioral intent — both
    # operational lists are present on the landing — is unchanged.
    install_fake_home_views(monkeypatch)
    ctx = section_ctx(Path("."))
    html = home.render(ctx)
    assert "No visit in 30 days" in html
    assert "Open opportunities" in html


# NOTE (320/A): the home page is now the tabbed console. The old inline candidate/capture preview
# cards (pending_candidates, captures_with_note) were superseded — quick approve/reject/unknown is the
# Review tab (swipe), and full candidate management (Open Issue, Done, filters) lives at /candidates.
# Their old per-card-render tests were removed; the console + candidates routes cover the new surface.


def test_home_does_not_render_admin_cards(monkeypatch):
    import types
    from pathlib import Path
    from ops_dashboard.sections import home

    fake_cards = [
        {"id": "failed_photo_vision_sidecars", "title": "Failed photo vision sidecars",
         "count": 3, "top": [], "see_all": "/failed", "shape": "capture"},
        {"id": "captures_with_note", "title": "Captures with a note",
         "count": 0, "top": [], "see_all": "/candidates", "shape": "capture"},
    ]
    monkeypatch.setattr(home, "inbox_cards", lambda rt: fake_cards)
    monkeypatch.setattr(home, "query_view", lambda *a, **kw: [])

    ctx = section_ctx(Path("."))
    html = home.render(ctx)
    assert "Failed photo vision sidecars" not in html


# NOTE (320/A): the candidate action-form / icon-button rendering (approve/reject/Open-Issue/Dismiss/
# Done) is no longer an inline home card — it lives on the /candidates route (candidates.py, its own
# tests) and the Review tab (swipe approve/reject/unknown). These home-card-render tests were removed.


def test_voice_memo_card_renders_site_options():
    from ops_dashboard.sections.home import _render_voice_card
    from btq_vault.projector import _SiteRecord
    site_records = {"42": _SiteRecord(site_id="42", name="Test Site", account="TestCo")}
    html = _render_voice_card(site_records)
    assert 'value="42"' in html
    assert "Test Site" in html


def test_voice_memo_card_appears_in_home_render(monkeypatch):
    import types
    from pathlib import Path
    from ops_dashboard.sections import home
    monkeypatch.setattr(home, "inbox_cards", lambda rt: [])
    monkeypatch.setattr(home, "query_view", lambda *a, **kw: [])
    ctx = section_ctx(Path("."))
    html = home.render(ctx)
    # Redesign (372): the capture quick-action heading is sentence-case
    # ("Capture observation") and the voice card lives behind the promoted
    # Capture button / quick-capture <details>; the form still posts to the
    # same action (don't-lose-info).
    assert "Capture observation" in html
    assert 'action="/vault-home/voice-memo"' in html


def test_handle_voice_memo_post_text_only_runs_text_semantics(monkeypatch, tmp_path):
    import types
    from ops_dashboard.sections import home

    calls = []

    def fake_record_upload(**kwargs):
        from voice_memo.core import UploadResult
        return UploadResult(record={})

    def fake_text_pipeline(text, *, site_id="", upload_id="", area="", person_id="", **_kwargs):
        calls.append(text)
        return {"type": "field_text_semantic_summary", "status": "complete"}

    monkeypatch.setattr(home, "record_upload", fake_record_upload)
    monkeypatch.setattr(home, "run_text_semantic_pipeline", fake_text_pipeline)

    boundary = "testboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="note"\r\n\r\n'
        "soap is low\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="site_id"\r\n\r\n'
        "42\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    content_type = f"multipart/form-data; boundary={boundary}"

    ctx = types.SimpleNamespace(runtime_root=tmp_path)
    result = home.handle_voice_memo_post(ctx, body, content_type=content_type)
    assert calls, "run_text_semantic_pipeline was not called"
    assert "soap" in calls[0]


def test_handle_voice_memo_post_text_only_persists_semantic_artifact_without_sync_candidate(monkeypatch, tmp_path):
    import types
    from ops_dashboard.sections import home

    def fake_text_pipeline(text, *, site_id="", upload_id="", area="", person_id="", **_kwargs):
        return {"type": "field_text_semantic_summary", "status": "complete", "text": text, "upload_id": upload_id}

    monkeypatch.setattr(home, "run_text_semantic_pipeline", fake_text_pipeline)

    boundary = "testboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="note"\r\n\r\n'
        "Need recruiting coverage for the evening cleaner opening.\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="site_id"\r\n\r\n'
        "603\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="capture_id"\r\n\r\n'
        "text-command-1\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    content_type = f"multipart/form-data; boundary={boundary}"

    ctx = types.SimpleNamespace(runtime_root=tmp_path)
    home.handle_voice_memo_post(ctx, body, content_type=content_type)

    semantic_path = tmp_path / "field_capture" / "semantics" / "text-command-1.json"
    semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
    assert semantic["type"] == "field_text_semantic_summary"
    assert semantic["text"] == "Need recruiting coverage for the evening cleaner opening."
    assert not (tmp_path / "reviews" / "action_candidates" / "field_capture").exists()
    assert not (tmp_path / "reviews" / "approved_job_drafts").exists()


def test_help_popup_renders_without_error():
    from ops_dashboard.sections.home import _render_help_popup
    html = _render_help_popup()
    assert "help-popup" in html
    assert len(html) > 100


def test_help_popup_covers_visit_completion_terms():
    from ops_dashboard.sections.home import _render_help_popup
    import yaml
    from pathlib import Path
    yaml_path = Path("project/event_pipeline/extraction_terms.yaml")
    data = yaml.safe_load(yaml_path.read_text())
    global_phrases = data.get("global", data).get("visit_completion", [])
    popup_html = _render_help_popup()
    assert any(phrase.lower() in popup_html.lower() for phrase in global_phrases), \
        "help popup must include at least one visit_completion phrase from extraction_terms.yaml"


def test_help_popup_mentions_issue_types():
    from ops_dashboard.sections.home import _render_help_popup
    from field_capture.audio_semantics import ISSUE_TYPES
    html = _render_help_popup()
    for issue_type in ISSUE_TYPES:
        if issue_type == "other":
            continue
        assert issue_type in html.lower() or issue_type.replace("_", " ") in html.lower(), \
            f"issue type '{issue_type}' not reachable from help popup"
