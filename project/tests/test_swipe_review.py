from __future__ import annotations

import urllib.parse
from pathlib import Path
from types import SimpleNamespace


def _write_candidate(candidate_dir: Path, candidate_id: str, **overrides) -> None:
    from processing_core.artifacts import write_json_object

    payload = {
        "type": "action_candidate_review",
        "candidate_id": candidate_id,
        "candidate_type": "field_capture_follow_up",
        "status": "pending_review",
        "summary": "Staffing risk at site 7050",
        "confidence": "high",
        "source_text": "Bruce no-showed again.",
        "channel_metadata": {"site_id": "7050", "submitter_name": "Greg"},
    }
    payload.update(overrides)
    write_json_object(candidate_dir / f"{candidate_id}.json", payload)


def _approvable_candidate(candidate_dir: Path, candidate_id: str) -> None:
    # Carry a pre-validated proposed job so proposed_queue_jobs returns it
    # verbatim. append_to_note is the simplest job that passes queue_spec.
    job = {
        "job_type": "append_to_note",
        "payload": {
            "path": "Accounts/7050.md",
            "content": "Staffing risk: Bruce no-showed.",
            "destination": "site_note",
        },
    }
    _write_candidate(
        candidate_dir,
        candidate_id,
        approval_metadata={"proposed_queue_job": job},
    )


def test_swipe_payload_shape_and_approvable_flag(tmp_path, couchdb_review):
    # 308c: /swipe reads candidates from CouchDB, not the filesystem. Seed the
    # CouchDB double from the written filesystem candidate and assert the card
    # IS surfaced through the CouchDB read path (NOT weakened to expect empty).
    from ops_dashboard.sections import swipe
    from field_capture.action_candidates import default_candidate_dir

    candidate_dir = default_candidate_dir(tmp_path)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    _approvable_candidate(candidate_dir, "ac_ok")
    couchdb_review.seed_from_fs(tmp_path)

    payload = swipe.swipe_payload(tmp_path)
    assert payload["counts"]["pending_review"] == 1
    cards = {card["candidate_id"]: card for card in payload["cards"]}

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
        payload={"cards": [], "counts": {"pending_review": 3, "failed": 2, "rejected": 1}},
    )

    assert '<a class="swipe-queue" data-queue="approval" href="/field-capture/review?status=pending_review"' in body
    assert '<strong>3</strong> needs approval</a>' in body
    assert '<a class="swipe-queue" data-queue="clarify" href="/field-capture/review?status=failed"' in body
    assert '<strong>2</strong> needs clarification</a>' in body
    assert '<a class="swipe-queue" data-queue="rejected" href="/field-capture/review?status=rejected"' in body
    assert '<strong>1</strong> rejected / teachable</a>' in body


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


def test_swipe_bootstrap_is_valid_json_for_the_browser(tmp_path, couchdb_review):
    """The embedded bootstrap must parse as JSON the way a browser reads it.

    Inside a <script>, the browser does NOT decode HTML entities, so html.escape
    would turn the JSON quotes into &quot; and JSON.parse would throw -- the card
    stack then silently renders empty (the "N needs approval / Nothing waiting"
    bug). Regression for that.
    """
    import json as _json
    import re

    from ops_dashboard.sections import swipe
    from field_capture.action_candidates import default_candidate_dir

    candidate_dir = default_candidate_dir(tmp_path)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    _approvable_candidate(candidate_dir, "ac_ok")
    couchdb_review.seed_from_fs(tmp_path)

    body = swipe.render_body(
        SimpleNamespace(runtime_root=tmp_path, config=SimpleNamespace(vault_dir=tmp_path / "vault")),
    )
    m = re.search(r'id="swipe-bootstrap" type="application/json">(.*?)</script>', body, re.S)
    assert m, "bootstrap script not found"
    raw = m.group(1)
    assert "&quot;" not in raw, "bootstrap is html-escaped -> browser JSON.parse would fail"
    data = _json.loads(raw)  # browser-equivalent parse must succeed
    assert any(c["candidate_id"] == "ac_ok" for c in data["cards"])


def test_swipe_card_surfaces_message_and_photo_thumbnails(tmp_path, couchdb_review):
    """The card carries the worker's message + /media thumbnail URLs for its photos."""
    from ops_dashboard.sections import swipe
    from field_capture.action_candidates import default_candidate_dir

    cid = "cap-test-7050"
    photo_dir = tmp_path / "uploads" / "2026-06-10" / cid
    photo_dir.mkdir(parents=True, exist_ok=True)
    (photo_dir / "img-001.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

    candidate_dir = default_candidate_dir(tmp_path)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    _write_candidate(
        candidate_dir,
        "ac_msg",
        source_text="A new vacuum and lint brushes are needed.",
        channel_metadata={"site_id": "7050", "submitter_name": "Sandy", "upload_id": cid},
    )
    couchdb_review.seed_from_fs(tmp_path)

    card = {c["candidate_id"]: c for c in swipe.swipe_payload(tmp_path)["cards"]}["ac_msg"]
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


def test_swipe_card_carries_fallback_proposed_job(tmp_path, couchdb_review):
    # A field-capture candidate with only a summary still gets a proposed job:
    # proposed_queue_jobs falls back to a generated append_to_note. The card
    # must reflect whatever the approval path would actually stage, so this is
    # approvable rather than blocked.
    from ops_dashboard.sections import swipe
    from field_capture.action_candidates import default_candidate_dir

    candidate_dir = default_candidate_dir(tmp_path)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    _write_candidate(candidate_dir, "ac_fallback")
    couchdb_review.seed_from_fs(tmp_path)

    card = swipe.collect_cards(tmp_path)[0]
    assert card["proposed_job_type"]  # a job was generated
    assert card["approvable"] is (not card["proposed_error"])


def test_swipe_only_shows_requested_status(tmp_path, couchdb_review):
    from ops_dashboard.sections import swipe
    from field_capture.action_candidates import default_candidate_dir

    candidate_dir = default_candidate_dir(tmp_path)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    _approvable_candidate(candidate_dir, "ac_pending")
    _write_candidate(candidate_dir, "ac_done", status="approved")
    couchdb_review.seed_from_fs(tmp_path)

    cards = swipe.collect_cards(tmp_path)
    assert [card["candidate_id"] for card in cards] == ["ac_pending"]


def test_swipe_approve_posts_through_existing_pipeline(tmp_path, couchdb_review):
    # 308b: /swipe posts to the SAME shared endpoint the table review page uses
    # (one approval fn). Approve now flips the candidate's CouchDB status to
    # approved WITHOUT inline staging; the Pro-side watcher stages exactly one
    # queue job. This test proves both: the shared-fn status flip AND that
    # running the watcher then stages exactly one job through the real pipeline.
    from ops_dashboard.sections import candidates
    from ops_dashboard.common import SectionContext
    from field_capture.action_candidates import default_candidate_dir
    from processing_core.artifacts import read_json_object

    candidate_dir = default_candidate_dir(tmp_path)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    _approvable_candidate(candidate_dir, "ac_ok")
    couchdb_review.seed_from_fs(tmp_path)

    ctx = SectionContext(tmp_path, lambda: SimpleNamespace(vault_dir=tmp_path / "vault"))
    ctx.action = "approve"
    body = urllib.parse.urlencode({"candidate_id": "ac_ok", "reviewer": "Greg"}).encode()
    status, _ctype, _out, _headers = candidates.handle_review_post(ctx, body)

    assert int(status) in (200, 303)
    # Shared fn flipped CouchDB status; NO inline staging yet.
    assert couchdb_review.status_of("ac_ok") == "approved"
    assert not list((tmp_path / "queue").glob("*.json"))
    # The watcher (real staging) then stages exactly one queue job.
    couchdb_review.run_watcher(tmp_path, stub_stage=False)
    staged = list((tmp_path / "queue").glob("*.json"))
    assert len(staged) == 1
    assert read_json_object(staged[0])["job_type"] == "append_to_note"
    assert couchdb_review.staged_at_of("ac_ok")


def test_swipe_reject_marks_candidate_rejected(tmp_path, couchdb_review):
    from ops_dashboard.sections import candidates
    from ops_dashboard.common import SectionContext
    from field_capture.action_candidates import default_candidate_dir

    candidate_dir = default_candidate_dir(tmp_path)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    _write_candidate(candidate_dir, "ac_bad")
    couchdb_review.seed_from_fs(tmp_path)

    ctx = SectionContext(tmp_path, lambda: SimpleNamespace(vault_dir=tmp_path / "vault"))
    ctx.action = "reject"
    body = urllib.parse.urlencode({"candidate_id": "ac_bad", "reviewer": "Greg"}).encode()
    candidates.handle_review_post(ctx, body)

    # 308b: status now lives in CouchDB.
    assert couchdb_review.status_of("ac_bad") == "rejected"
    # Reject must not stage anything, even after a watcher pass.
    couchdb_review.run_watcher(tmp_path, stub_stage=False)
    assert not list((tmp_path / "queue").glob("*.json"))
    # Reject must not stage anything.
    assert not list((tmp_path / "queue").glob("*.json"))


def test_swipe_routes_registered_in_app():
    import inspect
    from ops_dashboard import app as app_module

    src = inspect.getsource(app_module)
    assert '"/swipe": swipe' in src
    assert "/api/swipe-queue.json" in src
    assert "swipe.swipe_payload" in src
