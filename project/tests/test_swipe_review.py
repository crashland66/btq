from __future__ import annotations

import urllib.parse
from pathlib import Path
from types import SimpleNamespace


# 337a: the swipe surface now reads job_draft docs (review_status
# pending_approval) and approves/rejects via the 335 write path. The seed
# helpers build job_draft fixtures into the in-memory couchdb_job_draft_review
# double; the surface lists + shapes them through the REAL draft reader.
def _seed_draft(couchdb_job_draft_review, draft_id: str, **overrides) -> None:
    draft = {
        "draft_id": draft_id,
        "job_type": "append_to_note",
        "payload": {
            "path": "Accounts/7050.md",
            "content": "Staffing risk: Bruce no-showed.",
            "destination": "site_note",
        },
        "message": "Bruce no-showed again.",
        "site_id": "7050",
        "submitter_name": "Greg",
        "source_capture_id": f"cap-{draft_id}",
        "confidence": "high",
        "created_at": "2026-06-11T00:00:00Z",
    }
    draft.update(overrides)
    couchdb_job_draft_review.seed_draft(draft)


def _seed_approvable_draft(couchdb_job_draft_review, draft_id: str) -> None:
    # append_to_note is the simplest job that passes queue_spec, so the draft is
    # approvable (review_status pending_approval + a valid job).
    _seed_draft(couchdb_job_draft_review, draft_id)


def test_swipe_payload_shape_and_approvable_flag(tmp_path, couchdb_job_draft_review):
    # 337a: /swipe reads drafts from CouchDB. Seed a pending_approval draft and
    # assert the card IS surfaced through the draft read path (NOT weakened to
    # expect empty).
    from ops_dashboard.sections import swipe

    _seed_approvable_draft(couchdb_job_draft_review, "ac_ok")

    payload = swipe.swipe_payload(tmp_path)
    assert payload["counts"]["pending_approval"] == 1
    cards = {card["draft_id"]: card for card in payload["cards"]}

    ok = cards["ac_ok"]
    assert ok["approvable"] is True
    assert ok["proposed_job_type"] == "append_to_note"
    assert ok["site_id"] == "7050"
    assert ok["evidence"] == "Bruce no-showed again."
    assert ok["confidence"] == "high"


def test_swipe_review_counts_are_filter_links(tmp_path):
    from ops_dashboard.sections import swipe

    body = swipe.render_body(
        SimpleNamespace(runtime_root=tmp_path, config=SimpleNamespace(vault_dir=tmp_path / "vault")),
        payload={"cards": [], "counts": {"pending_approval": 3, "rejected": 1}},
    )

    # 337a: the swipe queue links target the draft review statuses. The
    # candidate-only "needs clarification"/failed queue is retired (no failed
    # review_status in the draft model).
    assert '<a class="swipe-queue" data-queue="approval" href="/field-capture/review?status=pending_approval"' in body
    assert '<strong>3</strong> needs approval</a>' in body
    assert '<a class="swipe-queue" data-queue="rejected" href="/field-capture/review?status=rejected"' in body
    assert '<strong>1</strong> rejected</a>' in body


def test_swipe_has_skip_control_that_advances_without_acting(tmp_path):
    """A reviewer can move to the next card without approving/rejecting."""
    from ops_dashboard.sections import swipe

    body = swipe.render_body(
        SimpleNamespace(runtime_root=tmp_path, config=SimpleNamespace(vault_dir=tmp_path / "vault")),
        payload={"cards": [], "counts": {}},
    )

    # The Skip button is rendered in the action row...
    assert 'data-act="skip"' in body
    assert ">Skip<" in body
    # ...wired to advance the index without any network write...
    assert "if (action === 'skip') { index += 1; render(); return; }" in body
    # ...bound to S / Down, and documented in the keyboard help.
    assert "e.key === 's' || e.key === 'S' || e.key === 'ArrowDown'" in body
    assert "<kbd>S</kbd>" in body and "skip" in body


def test_swipe_reviewer_name_has_no_blocking_prompt_loop(tmp_path):
    """Reviewer name comes from an inline field, never a window.prompt loop.

    A `while (!name) { window.prompt(...) }` hard-freezes the tab once the user
    checks "Don't ask again" (prompt returns null forever). Regression for that.
    """
    from ops_dashboard.sections import swipe

    body = swipe.render_body(
        SimpleNamespace(runtime_root=tmp_path, config=SimpleNamespace(vault_dir=tmp_path / "vault")),
        payload={"cards": [], "counts": {}},
    )
    assert 'id="swipe-reviewer-input"' in body  # inline field present
    assert "window.prompt" not in body  # no suppressible prompt
    assert "while (!name)" not in body  # no infinite loop


def test_plain_site_label_strips_html_markup():
    from ops_dashboard.sections import swipe

    label = '<span class="site-label">Liberty Wire <span class="site-id">(1337)</span></span>'
    assert swipe._plain_site_label(label) == "Liberty Wire (1337)"
    assert swipe._plain_site_label("B&amp;T") == "B&T"
    assert swipe._plain_site_label("") == ""


def test_swipe_bootstrap_is_valid_json_for_the_browser(tmp_path, couchdb_job_draft_review):
    """The embedded bootstrap must parse as JSON the way a browser reads it.

    Inside a <script>, the browser does NOT decode HTML entities, so html.escape
    would turn the JSON quotes into &quot; and JSON.parse would throw -- the card
    stack then silently renders empty (the "N needs approval / Nothing waiting"
    bug). Regression for that.
    """
    import json as _json
    import re

    from ops_dashboard.sections import swipe

    _seed_approvable_draft(couchdb_job_draft_review, "ac_ok")

    body = swipe.render_body(
        SimpleNamespace(runtime_root=tmp_path, config=SimpleNamespace(vault_dir=tmp_path / "vault")),
    )
    m = re.search(r'id="swipe-bootstrap" type="application/json">(.*?)</script>', body, re.S)
    assert m, "bootstrap script not found"
    raw = m.group(1)
    assert "&quot;" not in raw, "bootstrap is html-escaped -> browser JSON.parse would fail"
    data = _json.loads(raw)  # browser-equivalent parse must succeed
    assert any(c["draft_id"] == "ac_ok" for c in data["cards"])


def test_swipe_card_surfaces_message_and_photo_thumbnails(tmp_path, couchdb_job_draft_review):
    """The card carries the worker's message + /media thumbnail URLs for its photos."""
    from ops_dashboard.sections import swipe

    cid = "cap-test-7050"
    photo_dir = tmp_path / "uploads" / "2026-06-10" / cid
    photo_dir.mkdir(parents=True, exist_ok=True)
    (photo_dir / "img-001.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

    _seed_draft(
        couchdb_job_draft_review,
        "ac_msg",
        message="A new vacuum and lint brushes are needed.",
        submitter_name="Sandy",
        site_id="7050",
        source_capture_id=cid,
    )

    card = {c["draft_id"]: c for c in swipe.swipe_payload(tmp_path)["cards"]}["ac_msg"]
    assert card["message"] == "A new vacuum and lint brushes are needed."
    assert card["submitter_name"] == "Sandy"
    assert card["site_id"] == "7050"
    assert card["photos"] == [f"/media/2026-06-10/{cid}/img-001.jpg"]


def test_swipe_card_render_leads_with_message_not_mutation(tmp_path):
    from ops_dashboard.sections import swipe

    body = swipe.render_body(
        SimpleNamespace(runtime_root=tmp_path, config=SimpleNamespace(vault_dir=tmp_path / "vault")),
        payload={"cards": [], "counts": {}},
    )
    assert "swipe-message" in body
    assert "swipe-thumbs" in body
    # The technical proposed-mutation block is gone from the card.
    assert "Proposed mutation" not in body


def test_swipe_card_carries_proposed_job(tmp_path, couchdb_job_draft_review):
    # 337a: the card's proposed job is the draft's own job_type+payload (no
    # second extraction path). A pending_approval draft with a valid job is
    # approvable; the card mirrors exactly what approval would commit.
    from ops_dashboard.sections import swipe

    _seed_draft(couchdb_job_draft_review, "ac_fallback")

    card = swipe.collect_cards(tmp_path)[0]
    assert card["proposed_job_type"]  # the draft carries a job
    assert card["approvable"] is (not card["proposed_error"])


def test_swipe_only_shows_requested_status(tmp_path, couchdb_job_draft_review):
    from ops_dashboard.sections import swipe

    _seed_approvable_draft(couchdb_job_draft_review, "ac_pending")
    # A decided (approved) draft must NOT appear in the pending_approval queue.
    _seed_draft(couchdb_job_draft_review, "ac_done", review_status="approved")

    cards = swipe.collect_cards(tmp_path)
    assert [card["draft_id"] for card in cards] == ["ac_pending"]


def test_swipe_approve_posts_through_existing_pipeline(tmp_path, couchdb_job_draft_review):
    # 337a: /swipe posts to the SAME shared endpoint the table review page uses
    # (one approval fn -> apply_job_draft_review -> 335 write path). Approve flips
    # the draft's CouchDB review_status pending_approval -> approved.
    from ops_dashboard.sections import candidates
    from ops_dashboard.common import SectionContext

    _seed_approvable_draft(couchdb_job_draft_review, "ac_ok")
    rev = couchdb_job_draft_review.rev_of("ac_ok")

    ctx = SectionContext(tmp_path, lambda: SimpleNamespace(vault_dir=tmp_path / "vault"))
    ctx.action = "approve"
    body = urllib.parse.urlencode(
        {"draft_id": "ac_ok", "_rev": rev, "reviewer": "Greg"}
    ).encode()
    status, _ctype, _out, _headers = candidates.handle_review_post(ctx, body)

    assert int(status) in (200, 303)
    # Shared fn flipped the draft's review_status to approved.
    assert couchdb_job_draft_review.review_status_of("ac_ok") == "approved"
    assert couchdb_job_draft_review.reviewer_of("ac_ok") == "Greg"


def test_swipe_reject_marks_draft_rejected(tmp_path, couchdb_job_draft_review):
    from ops_dashboard.sections import candidates
    from ops_dashboard.common import SectionContext

    _seed_draft(couchdb_job_draft_review, "ac_bad")
    rev = couchdb_job_draft_review.rev_of("ac_bad")

    ctx = SectionContext(tmp_path, lambda: SimpleNamespace(vault_dir=tmp_path / "vault"))
    ctx.action = "reject"
    body = urllib.parse.urlencode(
        {"draft_id": "ac_bad", "_rev": rev, "reviewer": "Greg"}
    ).encode()
    candidates.handle_review_post(ctx, body)

    # 337a: review_status now lives on the job_draft doc in CouchDB; rejected is
    # KEPT (not deleted).
    assert couchdb_job_draft_review.review_status_of("ac_bad") == "rejected"


def test_swipe_routes_registered_in_app():
    import inspect
    from ops_dashboard import app as app_module

    src = inspect.getsource(app_module)
    assert '"/swipe": swipe' in src
    assert "/api/swipe-queue.json" in src
    assert "swipe.swipe_payload" in src
