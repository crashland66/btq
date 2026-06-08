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
