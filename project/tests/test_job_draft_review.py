"""Behavioral gate for prompt 335: the shared job_draft review WRITE path.

Authored by the INDEPENDENT VERIFIER (not the executor). These tests EXECUTE the
real write path -- ``apply_job_draft_review`` / ``edit_job_draft_payload``
(``project/field_capture/job_draft_review.py``) on top of the ``_rev``-CAS
mutators ``set_job_draft_review_status`` / ``set_job_draft_payload``
(``project/event_pipeline/couchdb_job_draft_writer.py``) -- against a throwaway
CouchDB, with the real 333 design doc pushed so ``validate_doc_update`` is live.
No mock of the database; every assertion is a real read-back of the persisted
doc bytes after a real HTTP round-trip.

The two safety gates are proven by reading the doc BEFORE and AFTER the refused
operation and asserting the persisted state did not change:
  * approve-refuses-invalid: a draft whose current payload fails ``validate_job``
    cannot be approved -- ``review_status`` stays ``pending_approval``;
  * edit-invalid-not-persisted: an edit whose new payload fails ``validate_job``
    is refused AND the stored ``payload`` is the PRIOR one, byte-for-byte.

If ``node`` were needed it isn't here. If no CouchDB is reachable the
``real_couchdb`` tests skip LOUDLY (they must not silently pass). Creds come from
the env (BTQ_TEST_COUCHDB_* / BTQ_COUCHDB_*) and, failing that, from
``~/.config/btq/config.json`` so the integration tests actually RUN on dev boxes.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib import error, request

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb_job_draft_writer import (
    get_job_draft,
    job_draft_doc_id,
    upsert_job_draft,
)
from field_capture.job_draft_review import (
    JobDraftReviewResult,
    apply_job_draft_review,
    edit_job_draft_payload,
)


# --------------------------------------------------------------------------- #
# Real design doc (so validate_doc_update runs inside CouchDB).
# --------------------------------------------------------------------------- #
DESIGN_DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "event_pipeline"
    / "couchdb"
    / "design_btq_field_captures.json"
)


def _design_doc() -> dict:
    return json.loads(DESIGN_DOC_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Live-CouchDB config discovery (env first, then on-box config.json).
# --------------------------------------------------------------------------- #
def _config_file_creds():
    path = Path(os.path.expanduser("~/.config/btq/config.json"))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = data.get("couchdb_url")
    user = data.get("couchdb_user")
    pwd = data.get("couchdb_password")
    if url and user and pwd:
        return url, user, pwd
    return None


def _live_config():
    url = os.environ.get("BTQ_TEST_COUCHDB_URL") or os.environ.get("BTQ_COUCHDB_URL")
    user = os.environ.get("BTQ_TEST_COUCHDB_USER") or os.environ.get("BTQ_COUCHDB_USER")
    pwd = os.environ.get("BTQ_TEST_COUCHDB_PASSWORD") or os.environ.get("BTQ_COUCHDB_PASSWORD")
    if not (url and user and pwd):
        creds = _config_file_creds()
        if creds is None:
            return None
        url, user, pwd = creds
    cfg = couchdb_config.from_env(
        base_url_override=url,
        username_override=user,
        password_override=pwd,
        timeout_override=10.0,
    )
    req = request.Request(
        f"{cfg.base_url}/_session",
        headers={**cfg.auth_header(), "Accept": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=5) as resp:
            if not 200 <= getattr(resp, "status", 200) < 300:
                return None
    except (error.URLError, OSError):
        return None
    return cfg


def _http(cfg, method, path, body=None):
    url = f"{cfg.base_url}/{path}"
    headers = {"Accept": "application/json", **cfg.auth_header()}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=10) as resp:
        return int(getattr(resp, "status", 200)), json.loads(resp.read() or b"{}")


@pytest.fixture()
def live_db():
    cfg = _live_config()
    if cfg is None:
        pytest.skip(
            "no reachable CouchDB (set BTQ_COUCHDB_URL/USER/PASSWORD or "
            "populate ~/.config/btq/config.json)"
        )
    db = f"btq_verify_335_{uuid.uuid4().hex[:12]}"
    _http(cfg, "PUT", db)
    design = _design_doc()
    _http(
        cfg,
        "PUT",
        f"{db}/_design/btq_field_captures",
        {k: v for k, v in design.items() if k != "_rev"},
    )
    try:
        yield cfg, db
    finally:
        try:
            request.urlopen(
                request.Request(
                    f"{cfg.base_url}/{db}",
                    headers={**cfg.auth_header(), "Accept": "application/json"},
                    method="DELETE",
                ),
                timeout=10,
            )
        except (error.URLError, OSError):
            pass


# --------------------------------------------------------------------------- #
# Payload fixtures: a queue_spec-VALID and an INVALID log_supply_need payload.
# (log_supply_need required = site_id / item_name / requested_by, all non-empty
# strings; an empty payload {} fails validate_job -- confirmed by direct probe.)
# --------------------------------------------------------------------------- #
VALID_PAYLOAD = {"site_id": "SANDBOX", "item_name": "towels", "requested_by": "op_jordan"}
VALID_PAYLOAD_2 = {"site_id": "SANDBOX", "item_name": "trash bags", "requested_by": "op_jordan"}
# Missing requested_by -> fails validate_job, but is still a structurally valid
# job_draft doc (the server gate only checks draft_id/job_type/review_status).
INVALID_PAYLOAD = {"site_id": "SANDBOX", "item_name": "towels"}


def _seed_draft(cfg, db, draft_id, *, payload=None, validation_error=None):
    """Seed a pending_approval job_draft and return its read-back doc."""
    draft = {
        "draft_id": draft_id,
        "job_type": "log_supply_need",
        "payload": VALID_PAYLOAD if payload is None else payload,
        "message": "operator note",
        "created_at": "2026-06-11T00:00:00Z",
    }
    if validation_error is not None:
        draft["validation_error"] = validation_error
    upsert_job_draft(cfg, db, draft)
    _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id(draft_id)}")
    assert doc["review_status"] == "pending_approval"
    return doc


# --------------------------------------------------------------------------- #
# Result-shape sanity (the dataclass is dict-ish per the executor's design).
# --------------------------------------------------------------------------- #
def test_result_shape_is_dataclass_and_dictlike() -> None:
    r = JobDraftReviewResult("d", "approved", False, None, "2-abc")
    assert r.review_status == "approved"
    assert r["already_decided"] is False
    assert set(r) == {"draft_id", "review_status", "already_decided", "error", "new_rev"}


@pytest.mark.real_couchdb
class TestApproveReject:
    # ---- approve happy ---------------------------------------------------- #
    def test_approve_happy_audits_and_advances_rev(self, live_db) -> None:
        cfg, db = live_db
        seeded = _seed_draft(cfg, db, "draft-approve-1")
        before_rev = seeded["_rev"]

        result = apply_job_draft_review(
            cfg, db, "draft-approve-1", "approve",
            reviewer="op_jordan", rationale="looks good",
        )
        assert result.error is None, result.error
        assert result.already_decided is False
        assert result.review_status == "approved"
        assert result.new_rev and result.new_rev != before_rev

        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-approve-1')}")
        assert doc["review_status"] == "approved"
        assert doc["reviewed_by"] == "op_jordan"
        assert doc["reviewed_at"]
        assert doc["review_rationale"] == "looks good"
        # review_history grew exactly one approve entry.
        history = doc["review_history"]
        assert isinstance(history, list) and len(history) == 1
        entry = history[-1]
        assert entry["review_status"] == "approved"
        assert entry["reviewer"] == "op_jordan"
        assert entry["prior_status"] == "pending_approval"
        # The doc still validates as an executable job (payload untouched).
        assert doc["payload"] == VALID_PAYLOAD

    # ---- reject happy: doc KEPT, audited --------------------------------- #
    def test_reject_keeps_doc_and_audits(self, live_db) -> None:
        cfg, db = live_db
        _seed_draft(cfg, db, "draft-reject-1")

        result = apply_job_draft_review(
            cfg, db, "draft-reject-1", "reject",
            reviewer="op_jordan", rationale="duplicate",
        )
        assert result.error is None, result.error
        assert result.already_decided is False
        assert result.review_status == "rejected"

        # Doc still EXISTS (rejected, not deleted) and is audited.
        status, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-reject-1')}")
        assert status == 200
        assert doc["review_status"] == "rejected"
        assert doc["reviewed_by"] == "op_jordan"
        assert doc["review_rationale"] == "duplicate"
        assert doc["review_history"][-1]["review_status"] == "rejected"

    # ---- result-shape: a known job_type round-trips through approve ------- #
    def test_approve_then_reread_via_get_job_draft(self, live_db) -> None:
        cfg, db = live_db
        _seed_draft(cfg, db, "draft-approve-getter")
        apply_job_draft_review(cfg, db, "draft-approve-getter", "approve", reviewer="op_jordan")
        doc = get_job_draft(cfg, db, "draft-approve-getter")
        assert doc is not None and doc["review_status"] == "approved"


@pytest.mark.real_couchdb
class TestConcurrencyAndAlreadyDecided:
    # ---- (a) stale expected_rev -> already_decided, doc UNCHANGED --------- #
    def test_stale_rev_is_rejected_without_mutation(self, live_db) -> None:
        cfg, db = live_db
        seeded = _seed_draft(cfg, db, "draft-stale-1")
        good_rev = seeded["_rev"]
        stale_rev = "999-deadbeefdeadbeefdeadbeefdeadbeef"

        result = apply_job_draft_review(
            cfg, db, "draft-stale-1", "approve",
            reviewer="op_jordan", expected_rev=stale_rev,
        )
        assert result.already_decided is True
        assert result.error is None

        # The doc was NOT touched: still pending_approval, still the seed _rev.
        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-stale-1')}")
        assert doc["review_status"] == "pending_approval"
        assert doc["_rev"] == good_rev
        assert "reviewed_by" not in doc or not doc.get("reviewed_by")

    # ---- (b) double-approve -> second call no-ops, no double-mutation ----- #
    def test_double_approve_is_idempotent_no_clobber(self, live_db) -> None:
        cfg, db = live_db
        _seed_draft(cfg, db, "draft-double-1")

        first = apply_job_draft_review(cfg, db, "draft-double-1", "approve", reviewer="op_jordan")
        assert first.review_status == "approved"
        assert first.already_decided is False
        _, after_first = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-double-1')}")
        rev_after_first = after_first["_rev"]
        history_len_after_first = len(after_first["review_history"])

        # Second approve: already-decided, status stays approved, NOT clobbered.
        second = apply_job_draft_review(cfg, db, "draft-double-1", "approve", reviewer="op_someone_else")
        assert second.already_decided is True
        assert second.review_status == "approved"
        assert second.error is None

        _, after_second = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-double-1')}")
        # Bytes/_rev did not change: no double mutation.
        assert after_second["_rev"] == rev_after_first
        assert after_second["review_status"] == "approved"
        assert after_second["reviewed_by"] == "op_jordan"  # the first reviewer, not the second
        assert len(after_second["review_history"]) == history_len_after_first

    # ---- reject-after-approve also refused -------------------------------- #
    def test_reject_after_approve_is_refused(self, live_db) -> None:
        cfg, db = live_db
        _seed_draft(cfg, db, "draft-flip-1")
        apply_job_draft_review(cfg, db, "draft-flip-1", "approve", reviewer="op_jordan")
        _, after_approve = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-flip-1')}")

        flip = apply_job_draft_review(cfg, db, "draft-flip-1", "reject", reviewer="op_jordan")
        assert flip.already_decided is True
        assert flip.review_status == "approved"  # did NOT become rejected
        _, after_flip = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-flip-1')}")
        assert after_flip["_rev"] == after_approve["_rev"]
        assert after_flip["review_status"] == "approved"


@pytest.mark.real_couchdb
class TestApproveRefusesInvalid:
    # ---- THE safety gate: cannot approve an unexecutable job -------------- #
    def test_approve_refuses_invalid_payload(self, live_db) -> None:
        cfg, db = live_db
        # Seed a draft whose CURRENT payload fails validate_job.
        seeded = _seed_draft(cfg, db, "draft-badjob-1", payload=INVALID_PAYLOAD)
        before_rev = seeded["_rev"]
        assert seeded["payload"] == INVALID_PAYLOAD
        assert seeded["review_status"] == "pending_approval"  # STATUS BEFORE

        result = apply_job_draft_review(cfg, db, "draft-badjob-1", "approve", reviewer="op_jordan")
        assert result.error, "approve of an invalid job must return an error"
        assert result.review_status == "pending_approval"  # not advanced
        assert result.already_decided is False

        # Persisted state unchanged: STILL pending_approval, STILL same _rev.
        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-badjob-1')}")
        assert doc["review_status"] == "pending_approval"  # STATUS AFTER
        assert doc["_rev"] == before_rev
        assert doc["payload"] == INVALID_PAYLOAD

    # ---- also refused when validation_error is pre-stamped on the doc ----- #
    def test_approve_refuses_when_validation_error_set(self, live_db) -> None:
        cfg, db = live_db
        # Payload is queue_spec-VALID, but the draft carries a validation_error
        # stamp (the emission layer's "could not derive" marker). Approve must
        # still refuse -- an operator cannot approve a flagged draft.
        _seed_draft(cfg, db, "draft-flagged-1", validation_error="queue_spec: missing field")
        result = apply_job_draft_review(cfg, db, "draft-flagged-1", "approve", reviewer="op_jordan")
        assert result.error
        assert result.review_status == "pending_approval"
        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-flagged-1')}")
        assert doc["review_status"] == "pending_approval"


@pytest.mark.real_couchdb
class TestEditPayload:
    # ---- edit valid: persisted, stays pending, audited ------------------- #
    def test_edit_valid_persists_and_keeps_pending(self, live_db) -> None:
        cfg, db = live_db
        seeded = _seed_draft(cfg, db, "draft-edit-1")
        before_rev = seeded["_rev"]

        result = edit_job_draft_payload(
            cfg, db, "draft-edit-1", VALID_PAYLOAD_2, editor="op_jordan",
        )
        assert result.error is None, result.error
        assert result.review_status == "pending_approval"  # edit does NOT decide
        assert result.new_rev and result.new_rev != before_rev

        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-edit-1')}")
        assert doc["payload"] == VALID_PAYLOAD_2  # new payload persisted
        assert doc["validation_error"] is None
        assert doc["review_status"] == "pending_approval"
        assert doc["edited_by"] == "op_jordan"
        # edit audited in review_history.
        assert any(e.get("action") == "edit_payload" for e in doc["review_history"])

    # ---- edit invalid: THE critical safety gate -------------------------- #
    def test_edit_invalid_payload_not_persisted(self, live_db) -> None:
        cfg, db = live_db
        seeded = _seed_draft(cfg, db, "draft-editbad-1")  # starts with VALID_PAYLOAD
        before_rev = seeded["_rev"]
        assert seeded["payload"] == VALID_PAYLOAD  # PAYLOAD BEFORE

        result = edit_job_draft_payload(
            cfg, db, "draft-editbad-1", INVALID_PAYLOAD, editor="op_jordan",
        )
        assert result.error, "an edit to an invalid payload must be refused with a reason"
        assert result.review_status == "pending_approval"

        # The stored payload is the PRIOR (valid) one, NOT the invalid edit.
        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-editbad-1')}")
        assert doc["payload"] == VALID_PAYLOAD  # PAYLOAD AFTER -- unchanged
        assert doc["payload"] != INVALID_PAYLOAD
        assert doc["_rev"] == before_rev  # doc not even rewritten

    # ---- edit refused on stale rev (no clobber) -------------------------- #
    def test_edit_stale_rev_refused(self, live_db) -> None:
        cfg, db = live_db
        seeded = _seed_draft(cfg, db, "draft-editstale-1")
        good_rev = seeded["_rev"]
        result = edit_job_draft_payload(
            cfg, db, "draft-editstale-1", VALID_PAYLOAD_2, editor="op_jordan",
            expected_rev="999-deadbeefdeadbeefdeadbeefdeadbeef",
        )
        assert result.already_decided is True
        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-editstale-1')}")
        assert doc["payload"] == VALID_PAYLOAD  # original
        assert doc["_rev"] == good_rev

    # ---- not editable when decided (approved AND rejected) --------------- #
    def test_edit_refused_when_approved(self, live_db) -> None:
        cfg, db = live_db
        _seed_draft(cfg, db, "draft-editapproved-1")
        apply_job_draft_review(cfg, db, "draft-editapproved-1", "approve", reviewer="op_jordan")
        _, after_approve = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-editapproved-1')}")

        result = edit_job_draft_payload(
            cfg, db, "draft-editapproved-1", VALID_PAYLOAD_2, editor="op_jordan",
        )
        assert result.already_decided is True or result.error
        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-editapproved-1')}")
        assert doc["payload"] == VALID_PAYLOAD  # unchanged from seed
        assert doc["review_status"] == "approved"
        assert doc["_rev"] == after_approve["_rev"]  # not touched

    def test_edit_refused_when_rejected(self, live_db) -> None:
        cfg, db = live_db
        _seed_draft(cfg, db, "draft-editrejected-1")
        apply_job_draft_review(cfg, db, "draft-editrejected-1", "reject", reviewer="op_jordan")
        _, after_reject = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-editrejected-1')}")

        result = edit_job_draft_payload(
            cfg, db, "draft-editrejected-1", VALID_PAYLOAD_2, editor="op_jordan",
        )
        assert result.already_decided is True or result.error
        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-editrejected-1')}")
        assert doc["payload"] == VALID_PAYLOAD
        assert doc["review_status"] == "rejected"
        assert doc["_rev"] == after_reject["_rev"]


@pytest.mark.real_couchdb
class TestInputGuards:
    def test_missing_draft_id_errors(self, live_db) -> None:
        cfg, db = live_db
        r = apply_job_draft_review(cfg, db, "", "approve", reviewer="op_jordan")
        assert r.error and "draft_id" in r.error

    def test_missing_reviewer_errors(self, live_db) -> None:
        cfg, db = live_db
        _seed_draft(cfg, db, "draft-noreviewer-1")
        r = apply_job_draft_review(cfg, db, "draft-noreviewer-1", "approve", reviewer="  ")
        assert r.error and "reviewer" in r.error

    def test_unknown_action_errors(self, live_db) -> None:
        cfg, db = live_db
        _seed_draft(cfg, db, "draft-badaction-1")
        r = apply_job_draft_review(cfg, db, "draft-badaction-1", "frobnicate", reviewer="op_jordan")
        assert r.error
        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-badaction-1')}")
        assert doc["review_status"] == "pending_approval"

    def test_review_missing_draft_returns_not_found(self, live_db) -> None:
        cfg, db = live_db
        r = apply_job_draft_review(cfg, db, "draft-ghost-1", "approve", reviewer="op_jordan")
        assert r.error and "not found" in r.error
