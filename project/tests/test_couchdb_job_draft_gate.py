"""Gating tests for BTQ prompt 333: job_draft CouchDB write side.

Authored by the INDEPENDENT VERIFIER (not the executor).

Mirrors the precedent ``test_couchdb_action_candidate_gate.py``: the hard rule
(``validate_doc_update`` is server-side JavaScript) means a Python
re-implementation proves nothing. These tests EXECUTE the real artefacts two
ways:

  * ``TestValidateDocUpdateRealJS`` -- extract the JS from the design doc and run
    it under ``node`` with crafted (newDoc, oldDoc) arguments, asserting
    accept/reject. The action_candidate + field_capture blocks are re-checked so
    a regression in the SIBLING types is caught here too.
  * ``TestRealCouchDBRoundTrip`` (``@pytest.mark.real_couchdb``) -- push the real
    design doc into a throwaway CouchDB database and exercise the actual
    ``upsert_job_draft`` round-trip / idempotency / optimistic-concurrency /
    403-on-malformed, so the validate function runs inside CouchDB itself.

If ``node`` is absent the JS tests skip loudly; if no CouchDB is reachable the
real_couchdb tests skip loudly (they must not silently pass). Creds are read
from the env (BTQ_TEST_COUCHDB_* / BTQ_COUCHDB_*) and, failing that, from
``~/.config/btq/config.json`` so the integration tests actually RUN on dev boxes
where the CouchDB creds live only in the config file.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib import error, request

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb_job_draft_writer import (
    CouchDBJobDraftWriterError,
    JOB_DRAFT_REVIEW_STATUSES,
    JOB_DRAFT_TYPE,
    build_job_draft_document,
    get_job_draft,
    job_draft_doc_id,
    upsert_job_draft,
)


# --------------------------------------------------------------------------- #
# Locate the real design doc + its validate_doc_update string.
# --------------------------------------------------------------------------- #
DESIGN_DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "event_pipeline"
    / "couchdb"
    / "design_btq_field_captures.json"
)


def _design_doc() -> dict:
    return json.loads(DESIGN_DOC_PATH.read_text(encoding="utf-8"))


def _validate_fn_source() -> str:
    fn = _design_doc().get("validate_doc_update")
    assert isinstance(fn, str) and fn.strip(), "design doc has no validate_doc_update"
    return fn


# --------------------------------------------------------------------------- #
# Real JS execution of validate_doc_update via node.
# --------------------------------------------------------------------------- #
NODE = shutil.which("node")

_JS_HARNESS = """
const validate = %(fn)s;
const args = JSON.parse(process.argv[1]);
const userCtx = {name: "tester", roles: ["_admin"]};
const secObj = {admins: {names: [], roles: []}, members: {names: [], roles: []}};
try {
  validate(args.newDoc, args.oldDoc || null, userCtx, secObj);
  console.log("ACCEPT");
} catch (e) {
  if (e && (e.forbidden || e.unauthorized)) {
    console.log("REJECT:" + (e.forbidden || e.unauthorized));
  } else {
    console.log("ERROR:" + (e && e.message ? e.message : String(e)));
  }
}
"""


def _run_validate_js(new_doc: dict, old_doc: dict | None = None) -> str:
    fn = _validate_fn_source()
    script = _JS_HARNESS % {"fn": fn}
    payload = json.dumps({"newDoc": new_doc, "oldDoc": old_doc})
    proc = subprocess.run(
        [NODE, "-e", script, payload],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node harness crashed: {proc.stderr}"
    return proc.stdout.strip()


def _valid_job_draft_doc(**overrides) -> dict:
    doc = {
        "_id": "job_draft_draft-js-1",
        "type": "job_draft",
        "draft_id": "draft-js-1",
        "job_type": "create_supply_order",
        "review_status": "pending_approval",
        "payload": {"site_id": "7050"},
    }
    doc.update(overrides)
    return doc


def _valid_candidate_doc(**overrides) -> dict:
    doc = {
        "_id": "action_candidate_cand-js-1",
        "type": "action_candidate",
        "candidate_id": "cand-js-1",
        "status": "pending_review",
    }
    doc.update(overrides)
    return doc


def _valid_field_capture_doc() -> dict:
    return {
        "_id": "field_capture_cap-1",
        "type": "field_capture",
        "capture_id": "cap-1",
        "site_id": "7050",
    }


@pytest.mark.skipif(NODE is None, reason="node not available to execute validate_doc_update as real JS")
class TestValidateDocUpdateRealJS:
    """Execute the REAL validate_doc_update JS for the new job_draft type."""

    # ---- job_draft ACCEPT path --------------------------------------------- #
    def test_valid_job_draft_accepted(self) -> None:
        assert _run_validate_js(_valid_job_draft_doc()) == "ACCEPT"

    @pytest.mark.parametrize("status", ["pending_approval", "approved", "rejected"])
    def test_whitelisted_review_statuses_accepted(self, status: str) -> None:
        assert _run_validate_js(_valid_job_draft_doc(review_status=status)) == "ACCEPT"

    def test_empty_payload_object_accepted(self) -> None:
        # A draft whose job failed queue_spec is stored with payload={} +
        # validation_error; an empty payload must NOT trigger rejection.
        doc = _valid_job_draft_doc(payload={}, validation_error="queue_spec: missing field")
        assert _run_validate_js(doc) == "ACCEPT"

    # ---- job_draft REJECT path: draft_id ----------------------------------- #
    def test_job_draft_missing_draft_id_rejected(self) -> None:
        doc = _valid_job_draft_doc()
        del doc["draft_id"]
        result = _run_validate_js(doc)
        assert result.startswith("REJECT"), result
        assert "draft_id" in result

    def test_job_draft_empty_draft_id_rejected(self) -> None:
        result = _run_validate_js(_valid_job_draft_doc(draft_id=""))
        assert result.startswith("REJECT"), result
        assert "draft_id" in result

    def test_job_draft_whitespace_draft_id_rejected(self) -> None:
        # Probe beyond the stated contract: a whitespace-only draft_id is "empty".
        result = _run_validate_js(_valid_job_draft_doc(draft_id="   "))
        assert result.startswith("REJECT"), result
        assert "draft_id" in result

    # ---- job_draft REJECT path: job_type ----------------------------------- #
    def test_job_draft_missing_job_type_rejected(self) -> None:
        doc = _valid_job_draft_doc()
        del doc["job_type"]
        result = _run_validate_js(doc)
        assert result.startswith("REJECT"), result
        assert "job_type" in result

    def test_job_draft_empty_job_type_rejected(self) -> None:
        result = _run_validate_js(_valid_job_draft_doc(job_type=""))
        assert result.startswith("REJECT"), result
        assert "job_type" in result

    def test_job_draft_whitespace_job_type_rejected(self) -> None:
        result = _run_validate_js(_valid_job_draft_doc(job_type="  \t "))
        assert result.startswith("REJECT"), result
        assert "job_type" in result

    # ---- job_draft REJECT path: review_status ------------------------------ #
    def test_job_draft_missing_review_status_rejected(self) -> None:
        doc = _valid_job_draft_doc()
        del doc["review_status"]
        result = _run_validate_js(doc)
        assert result.startswith("REJECT"), result
        assert "review_status" in result

    @pytest.mark.parametrize(
        "bad_status",
        ["pending_review", "failed", "approve", "", "PENDING_APPROVAL", "Approved"],
    )
    def test_job_draft_bad_review_status_rejected(self, bad_status: str) -> None:
        # Includes the action_candidate-only values (pending_review/failed) to
        # prove the two whitelists are NOT cross-contaminated, plus near-misses.
        result = _run_validate_js(_valid_job_draft_doc(review_status=bad_status))
        assert result.startswith("REJECT"), result
        assert "review_status" in result

    # ---- sibling types unchanged ------------------------------------------- #
    def test_valid_action_candidate_still_accepted(self) -> None:
        assert _run_validate_js(_valid_candidate_doc()) == "ACCEPT"

    def test_action_candidate_bad_status_still_rejected(self) -> None:
        result = _run_validate_js(_valid_candidate_doc(status="bogus"))
        assert result.startswith("REJECT"), result
        assert "status" in result

    def test_action_candidate_accepts_failed_but_job_draft_does_not(self) -> None:
        # "failed" is valid for action_candidate, invalid for job_draft: the two
        # status whitelists must stay distinct.
        assert _run_validate_js(_valid_candidate_doc(status="failed")) == "ACCEPT"
        rejected = _run_validate_js(_valid_job_draft_doc(review_status="failed"))
        assert rejected.startswith("REJECT"), rejected

    def test_valid_field_capture_still_accepted(self) -> None:
        assert _run_validate_js(_valid_field_capture_doc()) == "ACCEPT"

    def test_unknown_type_rejected(self) -> None:
        doc = {"_id": "weird-1", "type": "totally_unknown"}
        assert _run_validate_js(doc).startswith("REJECT")

    def test_job_draft_tombstone_accepted(self) -> None:
        # A delete of a job_draft must not be blocked by the field guards.
        assert _run_validate_js({"_id": "job_draft_x", "_deleted": True}) == "ACCEPT"


# --------------------------------------------------------------------------- #
# Python/JS parity: the Python whitelist must equal the JS-enforced set.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(NODE is None, reason="node not available")
def test_python_js_review_status_parity() -> None:
    """Every status in JOB_DRAFT_REVIEW_STATUSES is accepted by the JS, and the
    JS rejects every status NOT in it (probed over a representative sample)."""
    for status in JOB_DRAFT_REVIEW_STATUSES:
        assert _run_validate_js(_valid_job_draft_doc(review_status=status)) == "ACCEPT", status
    for status in ["pending_review", "failed", "draft", "approve", "done", ""]:
        if status in JOB_DRAFT_REVIEW_STATUSES:
            continue
        assert _run_validate_js(
            _valid_job_draft_doc(review_status=status)
        ).startswith("REJECT"), status
    # Exact-set assertion against the literal JS whitelist (defends parity even
    # if node were unavailable to enumerate): the three canonical values.
    assert JOB_DRAFT_REVIEW_STATUSES == frozenset(
        {"pending_approval", "approved", "rejected"}
    )


# --------------------------------------------------------------------------- #
# build_job_draft_document: Option-A shape + defaults + guards (pure Python).
# --------------------------------------------------------------------------- #
class TestBuildJobDraftDocument:
    def test_option_a_shape_and_id(self) -> None:
        doc = build_job_draft_document(
            {"draft_id": "d-1", "job_type": "create_supply_order"}
        )
        assert doc["_id"] == "job_draft_d-1" == job_draft_doc_id("d-1")
        assert doc["type"] == JOB_DRAFT_TYPE == "job_draft"
        assert doc["draft_id"] == "d-1"
        assert doc["job_type"] == "create_supply_order"

    def test_defaults_applied(self) -> None:
        doc = build_job_draft_document({"draft_id": "d-2", "job_type": "jt"})
        assert doc["review_status"] == "pending_approval"
        assert doc["group_id"] == "d-2"  # defaults to draft_id when absent
        assert doc["reviewed_by"] is None
        assert doc["reviewed_at"] is None

    def test_payload_non_dict_coerced_to_empty(self) -> None:
        for bad in [None, "x", 5, ["a"], 0, False]:
            doc = build_job_draft_document(
                {"draft_id": "d-3", "job_type": "jt", "payload": bad}
            )
            assert doc["payload"] == {}, bad

    def test_payload_dict_preserved(self) -> None:
        doc = build_job_draft_document(
            {"draft_id": "d-4", "job_type": "jt", "payload": {"k": "v"}}
        )
        assert doc["payload"] == {"k": "v"}

    def test_explicit_group_id_preserved(self) -> None:
        doc = build_job_draft_document(
            {"draft_id": "d-5", "job_type": "jt", "group_id": "g-9"}
        )
        assert doc["group_id"] == "g-9"

    def test_missing_draft_id_raises(self) -> None:
        with pytest.raises(CouchDBJobDraftWriterError):
            build_job_draft_document({"job_type": "jt"})
        with pytest.raises(CouchDBJobDraftWriterError):
            build_job_draft_document({"draft_id": "  ", "job_type": "jt"})

    def test_missing_job_type_raises(self) -> None:
        with pytest.raises(CouchDBJobDraftWriterError):
            build_job_draft_document({"draft_id": "d-6"})
        with pytest.raises(CouchDBJobDraftWriterError):
            build_job_draft_document({"draft_id": "d-6", "job_type": "   "})

    def test_bad_review_status_raises(self) -> None:
        with pytest.raises(CouchDBJobDraftWriterError):
            build_job_draft_document(
                {"draft_id": "d-7", "job_type": "jt", "review_status": "pending_review"}
            )

    def test_doc_id_guard(self) -> None:
        with pytest.raises(CouchDBJobDraftWriterError):
            job_draft_doc_id("")


# --------------------------------------------------------------------------- #
# Real CouchDB round-trip (writer + validate_doc_update inside the database).
# --------------------------------------------------------------------------- #
def _config_file_creds() -> tuple[str, str, str] | None:
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


def _live_config() -> couchdb_config.CouchDBConfig | None:
    url = os.environ.get("BTQ_TEST_COUCHDB_URL") or os.environ.get("BTQ_COUCHDB_URL")
    user = os.environ.get("BTQ_TEST_COUCHDB_USER") or os.environ.get("BTQ_COUCHDB_USER")
    pwd = os.environ.get("BTQ_TEST_COUCHDB_PASSWORD") or os.environ.get("BTQ_COUCHDB_PASSWORD")
    if not (url and user and pwd):
        # Fall back to the on-box config file so this integration test actually
        # RUNS on dev boxes (Dell) where creds live only in config.json.
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
    db = f"btq_verify_333_{uuid.uuid4().hex[:12]}"
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


def _draft(draft_id="draft-rt-1", **extra):
    base = {
        "draft_id": draft_id,
        "job_type": "create_supply_order",
        "payload": {"site_id": "7050", "items": ["towels"]},
        "message": "operator note",
        "created_at": "2026-06-11T00:00:00Z",
    }
    base.update(extra)
    return base


@pytest.mark.real_couchdb
class TestRealCouchDBRoundTrip:
    def test_upsert_creates_doc_with_default_status(self, live_db) -> None:
        cfg, db = live_db
        # review_status omitted -> default pending_approval applied by builder,
        # accepted by the server-side validate fn.
        upsert_job_draft(cfg, db, _draft())
        status, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-rt-1')}")
        assert status == 200
        assert doc["type"] == "job_draft"
        assert doc["draft_id"] == "draft-rt-1"
        assert doc["review_status"] == "pending_approval"
        assert doc["_rev"].startswith("1-")

    def test_get_job_draft_reads_back(self, live_db) -> None:
        cfg, db = live_db
        upsert_job_draft(cfg, db, _draft())
        got = get_job_draft(cfg, db, "draft-rt-1")
        assert got is not None
        assert got["draft_id"] == "draft-rt-1"
        assert get_job_draft(cfg, db, "does-not-exist") is None

    def test_idempotent_same_draft_id_one_doc(self, live_db) -> None:
        cfg, db = live_db
        upsert_job_draft(cfg, db, _draft())
        upsert_job_draft(cfg, db, _draft(review_status="approved"))
        # exactly one doc (not two): the _id is deterministic from draft_id.
        _, alldocs = _http(
            cfg, "GET", f"{db}/_all_docs?startkey=%22job_draft_%22&endkey=%22job_draft_%5Cufff0%22"
        )
        assert alldocs["total_rows"] >= 1
        matching = [r for r in alldocs["rows"] if r["id"] == job_draft_doc_id("draft-rt-1")]
        assert len(matching) == 1
        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-rt-1')}")
        assert doc["_rev"].startswith("2-")
        assert doc["review_status"] == "approved"

    def test_optimistic_concurrency_stale_rev_raises(self, live_db) -> None:
        cfg, db = live_db
        upsert_job_draft(cfg, db, _draft())
        _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-rt-1')}")
        stale_rev = doc["_rev"]
        # advance the doc so stale_rev is no longer current
        upsert_job_draft(cfg, db, _draft(review_status="approved"))
        with pytest.raises(CouchDBJobDraftWriterError):
            upsert_job_draft(cfg, db, _draft(review_status="rejected"), expected_rev=stale_rev)
        # the blind overwrite did NOT happen
        _, after = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-rt-1')}")
        assert after["review_status"] == "approved"

    def test_malformed_draft_raw_put_rejected_403(self, live_db) -> None:
        cfg, db = live_db
        # Bypass the Python builder's guards with a raw PUT: missing job_type.
        bad = {
            "_id": job_draft_doc_id("draft-bad-1"),
            "type": "job_draft",
            "draft_id": "draft-bad-1",
            "review_status": "pending_approval",
        }
        with pytest.raises(error.HTTPError) as exc:
            _http(cfg, "PUT", f"{db}/{bad['_id']}", bad)
        assert exc.value.code == 403

    def test_malformed_bad_review_status_raw_put_rejected_403(self, live_db) -> None:
        cfg, db = live_db
        bad = {
            "_id": job_draft_doc_id("draft-bad-2"),
            "type": "job_draft",
            "draft_id": "draft-bad-2",
            "job_type": "jt",
            "review_status": "pending_review",  # action_candidate-only value
        }
        with pytest.raises(error.HTTPError) as exc:
            _http(cfg, "PUT", f"{db}/{bad['_id']}", bad)
        assert exc.value.code == 403

    def test_empty_payload_object_accepted_by_server(self, live_db) -> None:
        cfg, db = live_db
        # payload={} (failed-queue_spec draft) must be accepted server-side.
        upsert_job_draft(
            cfg, db, _draft(draft_id="draft-empty", payload={}, validation_error="bad")
        )
        status, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id('draft-empty')}")
        assert status == 200
        assert doc["payload"] == {}
        assert doc["validation_error"] == "bad"

    def test_writer_surfaces_builder_guard(self, live_db) -> None:
        cfg, db = live_db
        with pytest.raises(CouchDBJobDraftWriterError):
            upsert_job_draft(cfg, db, _draft(job_type=""))
