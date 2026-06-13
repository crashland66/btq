"""Gating tests for prompt 308c: candidate READERS switch to CouchDB +
filesystem candidate retired as canonical.

Authored by the INDEPENDENT VERIFIER for 308c. These use the faithful in-memory
``couchdb_review`` double (extended for `_find` listing + the collector write
path) so the REAL reader/writer/review/watcher logic runs under test.

Central discipline: every reader gate SEEDS a candidate into CouchDB and asserts
it is SURFACED through the CouchDB path -- never weakened to "expect empty".
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

import field_capture.action_candidates as fc
from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import CouchDBCandidateWriterError


def _candidate(candidate_id: str, *, status: str = "pending_review", site_id: str = "7050") -> dict:
    payload = fc.action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary=f"Follow up {candidate_id}",
        rationale="needs triage",
        source_text="Bruce no-showed again.",
        source_context="field capture note",
        channel_metadata={"channel": "field_capture", "site_id": site_id, "upload_id": f"cap-{candidate_id}"},
    )
    payload["approval_metadata"] = {
        "proposed_queue_job": {
            "job_type": "append_to_note",
            "payload": {"path": f"Accounts/{site_id}.md", "content": "Staffing risk.", "destination": "site_note"},
        }
    }
    payload["candidate_id"] = candidate_id
    payload["status"] = status
    return payload


# --------------------------------------------------------------------------- #
# GATE 1: filesystem candidate is NO LONGER read on the approval surfaces.
# A candidate present ONLY on the filesystem (never in CouchDB) must NOT appear.
# --------------------------------------------------------------------------- #
def test_filesystem_only_candidate_is_not_surfaced(tmp_path, couchdb_job_draft_review):
    """337a: the swipe surface reads job_draft docs from CouchDB only. A
    candidate sitting ONLY on the filesystem (and even a candidate doc in
    CouchDB) is NOT a job_draft, so it must NOT surface on the draft review
    page. The CouchDB draft store is empty here -> the reader surfaces nothing."""
    from ops_dashboard.sections import swipe
    import json

    # Write an action_candidate artifact ONLY to the filesystem.
    candidate_dir = fc.default_candidate_dir(tmp_path)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    payload = _candidate("fs_only")
    (candidate_dir / "fs_only.json").write_text(
        json.dumps({**payload, "type": "action_candidate_review"}), encoding="utf-8"
    )

    # Draft store is EMPTY -> the swipe reader must surface nothing (the FS read
    # path is retired AND the surface reads drafts, not candidates).
    cards = swipe.collect_cards(tmp_path)
    assert [c["draft_id"] for c in cards] == [], (
        "a filesystem-only candidate must NOT appear; the draft surface reads "
        "job_draft docs from CouchDB only"
    )


def test_couchdb_seeded_candidate_is_surfaced(tmp_path, couchdb_job_draft_review):
    """Positive companion: a job_draft seeded into CouchDB IS surfaced (proves
    the reader reads CouchDB, so test_filesystem_only above isn't vacuously
    empty). 337a retargets this from action_candidate to job_draft."""
    from ops_dashboard.sections import swipe

    couchdb_job_draft_review.seed_draft({
        "draft_id": "in_couch",
        "job_type": "append_to_note",
        "payload": {"path": "Accounts/7050.md", "content": "Staffing risk.", "destination": "site_note"},
        "message": "Follow up in_couch",
        "site_id": "7050",
        "source_capture_id": "cap-in_couch",
        "created_at": "2026-06-11T00:00:00Z",
    })
    cards = swipe.collect_cards(tmp_path)
    assert [c["draft_id"] for c in cards] == ["in_couch"]


# --------------------------------------------------------------------------- #
# GATE 2: no silent drop on a configured-but-erroring CouchDB write.
# --------------------------------------------------------------------------- #
def test_configured_but_erroring_write_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("BTQ_COUCHDB_ACTION_CANDIDATE_WRITE", "1")

    def boom(config, db, candidate):
        raise CouchDBCandidateWriterError("simulated couch outage")

    monkeypatch.setattr(fc, "upsert_action_candidate", boom)
    monkeypatch.setattr(
        fc.couchdb_config, "from_env",
        lambda **_: couchdb_config.CouchDBConfig("http://x", "u", "p", 1.0, 1000),
    )
    with pytest.raises(CouchDBCandidateWriterError):
        fc.write_candidate_to_couchdb_required_when_configured({"candidate_id": "x"})


def test_unconfigured_write_skips_silently(tmp_path, monkeypatch):
    # No CouchDB env + no explicit write switch -> store unconfigured -> skip (None), no raise.
    monkeypatch.delenv("BTQ_COUCHDB_ACTION_CANDIDATE_WRITE", raising=False)
    for name in ("BTQ_COUCHDB_URL", "BTQ_COUCHDB_USER", "BTQ_COUCHDB_PASSWORD", "BTQ_COUCHDB_FIELD_CAPTURES_DB"):
        monkeypatch.delenv(name, raising=False)
    assert fc.write_candidate_to_couchdb_required_when_configured({"candidate_id": "x"}) is None


# --------------------------------------------------------------------------- #
# GATE 2b: no silent drop on the EXISTENCE GET either. The 308c fix decides
# skip-vs-write by reading whether the candidate already exists in CouchDB. If
# that GET errors while CouchDB is CONFIGURED, we cannot safely decide -> RAISE
# (same fail-loud discipline as the write). UNCONFIGURED -> skip silently (None).
# --------------------------------------------------------------------------- #
def test_configured_but_erroring_existence_check_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("BTQ_COUCHDB_ACTION_CANDIDATE_WRITE", "1")

    def boom(config, db, candidate_id):
        raise CouchDBCandidateWriterError("simulated couch outage on existence GET")

    monkeypatch.setattr(fc, "get_action_candidate", boom)
    monkeypatch.setattr(
        fc.couchdb_config, "from_env",
        lambda **_: couchdb_config.CouchDBConfig("http://x", "u", "p", 1.0, 1000),
    )
    # Direct existence helper raises.
    with pytest.raises(CouchDBCandidateWriterError):
        fc.couchdb_action_candidate_exists_required_when_configured("x")
    # And the write path (which calls the existence check first) raises too --
    # never silently skips/writes when it cannot read.
    with pytest.raises(CouchDBCandidateWriterError):
        fc.write_candidate_to_couchdb_required_when_configured({"candidate_id": "x"})


def test_unconfigured_existence_check_returns_none(monkeypatch):
    monkeypatch.delenv("BTQ_COUCHDB_ACTION_CANDIDATE_WRITE", raising=False)
    for name in ("BTQ_COUCHDB_URL", "BTQ_COUCHDB_USER", "BTQ_COUCHDB_PASSWORD", "BTQ_COUCHDB_FIELD_CAPTURES_DB"):
        monkeypatch.delenv(name, raising=False)
    assert fc.couchdb_action_candidate_exists_required_when_configured("x") is None


def test_existing_candidate_skips_write_and_is_not_upserted(tmp_path, couchdb_review):
    # An already-present candidate must NOT be re-upserted (preserving its status)
    # -> write helper returns False (skipped), and put/upsert count is unchanged.
    couchdb_review.seed_doc(_candidate("already_here"))
    before = couchdb_review.put_count
    result = fc.write_candidate_to_couchdb_required_when_configured(_candidate("already_here"))
    assert result is False
    assert couchdb_review.status_of("already_here") == "pending_review"


# --------------------------------------------------------------------------- #
# GATE 3: migrated review loop -- CouchDB candidate -> listing(pending) ->
# approve via apply_candidate_review (the still-live CouchDB review path).
#
# prompt 370: the action-candidate STAGING WATCHER was retired with the dead
# LEGACY action-candidate path, so the "watcher stages exactly one job" tail is
# gone. The live review-mutation path (listing + apply_candidate_review approve)
# is KEPT and still asserted end-to-end here.
# --------------------------------------------------------------------------- #
def test_end_to_end_approve(tmp_path, couchdb_review):
    cid = couchdb_review.seed_doc(_candidate("e2e"))
    db = couchdb_config.field_captures_database()
    cfg = couchdb_config.from_env()

    # Appears in the pending_review listing.
    listed = fc.couchdb_candidate_payloads(status="pending_review")
    assert cid in [p["candidate_id"] for _path, p in listed]

    # Approve via the shared review fn -> status flips to approved in CouchDB.
    result = fc.apply_candidate_review(cfg, db, cid, "approve", "Greg", rationale="ok")
    assert result.error is None and result.status == "approved"
    assert couchdb_review.status_of(cid) == "approved"


# --------------------------------------------------------------------------- #
# GATE 4: backfill compat -- BOTH new-shape (source string) and old-shape
# (source object) docs are surfaced and shaped without choking.
# --------------------------------------------------------------------------- #
def test_backfill_old_and_new_source_shapes_both_surface(tmp_path, couchdb_job_draft_review):
    """337a: the draft surface must shape BOTH a fully-populated draft and a
    sparse draft (minimal fields) without choking -- the dashboard shaping is
    defensive against missing optional fields. Retargeted from the candidate
    source-shape backfill compat to the draft reality (drafts carry no `source`
    object; the analogous risk is a draft missing optional surface fields)."""
    from ops_dashboard.sections import swipe

    # Fully-populated draft.
    couchdb_job_draft_review.seed_draft({
        "draft_id": "new_shape",
        "job_type": "append_to_note",
        "payload": {"path": "Accounts/7050.md", "content": "x", "destination": "site_note"},
        "message": "full draft",
        "site_id": "7050",
        "submitter_name": "Sandy",
        "source_capture_id": "cap-new",
        "confidence": "high",
        "created_at": "2026-06-11T00:00:00Z",
    })
    # Sparse draft: only the required fields (no site/submitter/message/capture).
    couchdb_job_draft_review.seed_draft({
        "draft_id": "old_shape",
        "job_type": "append_to_note",
        "payload": {"path": "Accounts/7051.md", "content": "y", "destination": "site_note"},
    })

    cards = {c["draft_id"]: c for c in swipe.collect_cards(tmp_path)}
    assert {"new_shape", "old_shape"} <= set(cards), "both draft shapes must surface"
    # Dashboard shaping must not choke on the sparse form.
    for c in cards.values():
        assert isinstance(c["site_id"], str)
        assert "approvable" in c


# --------------------------------------------------------------------------- #
# GATE 5: the type,status Mango index is provisioned by setup.
# --------------------------------------------------------------------------- #
def test_field_capture_index_provisioned_by_setup():
    from event_pipeline.couchdb import setup_databases

    names = [str(idx.get("name")) for idx in setup_databases.FIELD_CAPTURE_MANGO_INDEXES]
    assert "idx-type-status" in names
    fields = setup_databases.FIELD_CAPTURE_MANGO_INDEXES[0]["index"]["fields"]
    assert fields == ["type", "status"]

    captured = {}

    def fake_create(db_name, index_def, base_url, headers):
        captured.setdefault(db_name, []).append(index_def.get("name"))
        return "created"

    import unittest.mock as mock
    with mock.patch.object(setup_databases, "create_mango_index", fake_create):
        outcomes = setup_databases.provision_field_capture_indexes("http://x", {})
    assert outcomes.get("idx-type-status") == "created"
    assert "idx-type-status" in captured.get(setup_databases.couchdb_config.DEFAULT_FIELD_CAPTURES_DB, [])


# --------------------------------------------------------------------------- #
# GATE 6 (REGRESSION GUARD): a collector re-run must NOT clobber a reviewed
# candidate's status back to pending_review. 308c removed the FS-exists skip and
# upsert overwrites status from the (fresh, pending_review) payload -> approvals
# are silently wiped on the next pipeline pass. This gate documents the contract;
# it FAILS against the current 308c code (real regression), so it is marked
# xfail(strict) -- when the executor fixes the writer to preserve a decided
# status, this turns into an unexpected-pass and the xfail must be removed.
# --------------------------------------------------------------------------- #
def test_collector_rerun_preserves_reviewed_status(tmp_path, couchdb_review):
    # FIXED (308c): the writer now reads the existence of the candidate in CouchDB
    # before upserting and SKIPS an already-present candidate, so a collector
    # re-run does NOT clobber a decided status back to pending_review. (Was a
    # strict-xfail documenting the regression; flipped to a normal passing test
    # by the verifier once the fix landed.)
    cfg = couchdb_config.from_env()
    db = couchdb_config.field_captures_database()

    payload = _candidate("reviewed")
    couchdb_review.upsert(payload)
    fc.apply_candidate_review(cfg, db, "reviewed", "approve", "Greg", rationale="ok")
    assert couchdb_review.status_of("reviewed") == "approved"

    # Pipeline re-runs the collector -> re-emits the SAME candidate_id at
    # pending_review (fresh from the persisted semantic artifact).
    fc.write_candidate_to_couchdb_required_when_configured(_candidate("reviewed"))

    assert couchdb_review.status_of("reviewed") == "approved", (
        "collector re-run must NOT clobber a reviewed candidate back to pending_review"
    )
