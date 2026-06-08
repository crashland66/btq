from __future__ import annotations

import urllib.parse
from pathlib import Path


def test_completion_dismiss_handler_writes_dismissed_with_reason(tmp_path, couchdb_review):
    # 308c: the completion-dismiss handler now routes through the shared CouchDB
    # review fn (apply_candidate_review -> reject) carrying the "completion"
    # rationale, instead of patching a filesystem candidate artifact. Seed the
    # candidate into CouchDB (the sole canonical store) and assert the CouchDB
    # doc flips to rejected with the completion marker.
    from ops_dashboard.sections import candidates
    from ops_dashboard.common import SectionContext

    candidate_id = couchdb_review.seed_doc({
        "candidate_id": "test_cand_001",
        "candidate_type": "field_capture_follow_up",
        "summary": "All done",
        "status": "pending_review",
        "channel_metadata": {"channel": "field_capture", "site_id": "7050"},
    })

    body = urllib.parse.urlencode({"candidate_id": candidate_id, "reviewer": "operator"}).encode()
    ctx = SectionContext(tmp_path, lambda: None)
    candidates._handle_completion_dismiss(ctx, body)

    doc = couchdb_review.docs[f"action_candidate_{candidate_id}"]
    assert doc["status"] == "rejected"
    assert doc["review_rationale"] == candidates.DISMISS_REASON_COMPLETION
    assert doc["reviewer"] == "operator"


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
