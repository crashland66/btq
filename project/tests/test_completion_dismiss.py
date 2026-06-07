from __future__ import annotations

import urllib.parse
from pathlib import Path


def test_completion_dismiss_handler_writes_dismissed_with_reason(tmp_path):
    from ops_dashboard.sections import candidates
    from ops_dashboard.common import SectionContext
    from processing_core.artifacts import write_json_object, read_json_object
    from field_capture.action_candidates import default_candidate_dir

    runtime_root = tmp_path
    candidate_dir = default_candidate_dir(runtime_root)
    candidate_dir.mkdir(parents=True, exist_ok=True)

    candidate_id = "test_cand_001"
    candidate_path = candidates._candidate_path_for_id(runtime_root, candidate_id)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_object(candidate_path, {
        "type": "action_candidate_review",
        "candidate_id": candidate_id,
        "status": "pending_review",
        "channel_metadata": {},
    })

    body = urllib.parse.urlencode({"candidate_id": candidate_id, "reviewer": "operator"}).encode()
    # 179: handler now takes a SectionContext (config unused here; runtime_root only).
    ctx = SectionContext(runtime_root, lambda: None)
    candidates._handle_completion_dismiss(ctx, body)

    written = read_json_object(candidate_path)
    assert written["status"] == "rejected"
    assert written["resolution"]["status"] == "dismissed"
    assert written["resolution"]["dismiss_reason"] == "completion"


def test_completion_dismiss_endpoint_registered_in_app():
    import inspect
    from ops_dashboard import app as app_module

    src = inspect.getsource(app_module)
    assert "/field-capture/review/dismiss-completion" in src
    assert "handle_completion_dismiss_post" in src


def test_home_flags_completion_row_and_shows_done_button(monkeypatch):
    from ops_dashboard.sections import home

    rows = [
        {
            "site": "S1",
            "area": "",
            "summary": "all done",
            "candidate_id": "cand_done",
            "deep_link": "/c/1",
            "visit_proposed": True,
        }
    ]
    html = home._render_card_rows(rows, home.INBOX_SHAPE_CAPTURE)
    assert "dismiss-completion" in html
    assert "✓ Done" in html or "completion" in html
