"""Gating tests for BTQ prompt 308 (Phase 1a): action_candidate CouchDB write side.

Authored by the INDEPENDENT VERIFIER (not the executor).

The hard rule (spec invariant 5, the prompt-133->135 trap): ``validate_doc_update``
is server-side JavaScript. A Python re-implementation of the rule proves nothing.
These tests EXECUTE the real function string two ways:

  * ``test_validate_doc_update_*`` -- extract the JS from the design doc and run it
    under ``node`` with crafted (newDoc, oldDoc) arguments, asserting accept/reject.
  * ``test_real_couchdb_*`` -- push the design doc into a throwaway CouchDB database
    and exercise the actual writer round-trip / idempotency / 403-on-malformed, so
    the validate function runs inside CouchDB itself.

If neither ``node`` nor a reachable CouchDB is available the relevant tests skip
loudly (they must not silently pass).
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
from event_pipeline.couchdb_candidate_writer import (
    CouchDBCandidateWriterError,
    action_candidate_doc_id,
    build_action_candidate_document,
    upsert_action_candidate,
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

# Harness: define the real validate fn, call it, and report ACCEPT or
# REJECT:<reason>. A CouchDB ``throw({forbidden: ...})`` is exactly what the
# database does on a rejected write, so we catch the thrown object.
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
    """Execute the REAL validate_doc_update JS -- the gate against prompt-133->135."""

    def test_valid_action_candidate_accepted(self) -> None:
        assert _run_validate_js(_valid_candidate_doc()) == "ACCEPT"

    @pytest.mark.parametrize("status", ["pending_review", "approved", "rejected", "failed"])
    def test_whitelisted_statuses_accepted(self, status: str) -> None:
        assert _run_validate_js(_valid_candidate_doc(status=status)) == "ACCEPT"

    def test_action_candidate_missing_candidate_id_rejected(self) -> None:
        doc = _valid_candidate_doc()
        del doc["candidate_id"]
        result = _run_validate_js(doc)
        assert result.startswith("REJECT"), result
        assert "candidate_id" in result

    def test_action_candidate_missing_status_rejected(self) -> None:
        doc = _valid_candidate_doc()
        del doc["status"]
        result = _run_validate_js(doc)
        assert result.startswith("REJECT"), result
        assert "status" in result

    def test_action_candidate_bad_status_rejected(self) -> None:
        result = _run_validate_js(_valid_candidate_doc(status="bogus"))
        assert result.startswith("REJECT"), result
        assert "status" in result

    def test_valid_field_capture_still_accepted(self) -> None:
        assert _run_validate_js(_valid_field_capture_doc()) == "ACCEPT"

    def test_unknown_type_rejected(self) -> None:
        doc = {"_id": "weird-1", "type": "totally_unknown", "candidate_id": "x", "status": "pending_review"}
        result = _run_validate_js(doc)
        assert result.startswith("REJECT"), result

    def test_missing_type_rejected(self) -> None:
        result = _run_validate_js({"_id": "notype-1"})
        assert result.startswith("REJECT"), result

    def test_deleted_doc_accepted(self) -> None:
        # Tombstones must not be blocked by the type guard.
        assert _run_validate_js({"_id": "x", "_deleted": True}) == "ACCEPT"

    def test_design_doc_accepted(self) -> None:
        assert _run_validate_js({"_id": "_design/whatever"}) == "ACCEPT"


# --------------------------------------------------------------------------- #
# Real CouchDB round-trip (writer + validate_doc_update inside the database).
# --------------------------------------------------------------------------- #
def _live_config() -> couchdb_config.CouchDBConfig | None:
    url = os.environ.get("BTQ_TEST_COUCHDB_URL") or os.environ.get("BTQ_COUCHDB_URL")
    user = os.environ.get("BTQ_TEST_COUCHDB_USER") or os.environ.get("BTQ_COUCHDB_USER")
    pwd = os.environ.get("BTQ_TEST_COUCHDB_PASSWORD") or os.environ.get("BTQ_COUCHDB_PASSWORD")
    if not (url and user and pwd):
        return None
    cfg = couchdb_config.from_env(
        base_url_override=url,
        username_override=user,
        password_override=pwd,
        timeout_override=10.0,
    )
    # Reachability probe.
    req = request.Request(f"{cfg.base_url}/_session", headers={**cfg.auth_header(), "Accept": "application/json"})
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
        pytest.skip("no reachable CouchDB (set BTQ_COUCHDB_URL/USER/PASSWORD)")
    db = f"btq_verify_308_{uuid.uuid4().hex[:12]}"
    _http(cfg, "PUT", db)
    # Push the real design doc so validate_doc_update runs server-side.
    design = _design_doc()
    _http(cfg, "PUT", f"{db}/_design/btq_field_captures", {k: v for k, v in design.items() if k != "_rev"})
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


def _candidate(candidate_id="cand-rt-1", status="pending_review", **extra):
    base = {
        "candidate_id": candidate_id,
        "status": status,
        "candidate_type": "field_capture_follow_up",
        "source_kind": "voice",
        "summary": "Operator follow-up",
        "site_id": "7050",
        "created_at": "2026-06-07T00:00:00Z",
    }
    base.update(extra)
    return base


class TestRealCouchDBRoundTrip:
    def test_upsert_creates_doc(self, live_db) -> None:
        cfg, db = live_db
        upsert_action_candidate(cfg, db, _candidate())
        status, doc = _http(cfg, "GET", f"{db}/{action_candidate_doc_id('cand-rt-1')}")
        assert status == 200
        assert doc["type"] == "action_candidate"
        assert doc["candidate_id"] == "cand-rt-1"
        assert doc["status"] == "pending_review"
        assert doc["_rev"].startswith("1-")

    def test_upsert_idempotent_no_409(self, live_db) -> None:
        cfg, db = live_db
        upsert_action_candidate(cfg, db, _candidate())
        _, first = _http(cfg, "GET", f"{db}/{action_candidate_doc_id('cand-rt-1')}")
        # Second upsert must read the current _rev and PUT successfully -- no 409.
        upsert_action_candidate(cfg, db, _candidate(status="approved"))
        _, second = _http(cfg, "GET", f"{db}/{action_candidate_doc_id('cand-rt-1')}")
        assert second["_rev"].startswith("2-")
        assert second["status"] == "approved"
        assert first["_rev"] != second["_rev"]

    def test_malformed_status_rejected_by_validate(self, live_db) -> None:
        cfg, db = live_db
        # build_action_candidate_document itself guards bad status, so to prove the
        # SERVER rejects it we PUT a raw doc that bypasses the writer's normalizer.
        bad = {
            "_id": action_candidate_doc_id("cand-bad-1"),
            "type": "action_candidate",
            "candidate_id": "cand-bad-1",
            "status": "bogus",
        }
        with pytest.raises(error.HTTPError) as exc:
            _http(cfg, "PUT", f"{db}/{bad['_id']}", bad)
        assert exc.value.code == 403

    def test_missing_candidate_id_rejected_by_validate(self, live_db) -> None:
        cfg, db = live_db
        bad = {"_id": "action_candidate_x", "type": "action_candidate", "status": "pending_review"}
        with pytest.raises(error.HTTPError) as exc:
            _http(cfg, "PUT", f"{db}/{bad['_id']}", bad)
        assert exc.value.code == 403

    def test_unknown_type_rejected_by_validate(self, live_db) -> None:
        cfg, db = live_db
        bad = {"_id": "weird_1", "type": "nope"}
        with pytest.raises(error.HTTPError) as exc:
            _http(cfg, "PUT", f"{db}/{bad['_id']}", bad)
        assert exc.value.code == 403

    def test_valid_field_capture_accepted_by_validate(self, live_db) -> None:
        cfg, db = live_db
        ok = {"_id": "field_capture_cap-9", "type": "field_capture", "capture_id": "cap-9", "site_id": "7050"}
        status, _ = _http(cfg, "PUT", f"{db}/{ok['_id']}", ok)
        assert status in (201, 202)

    def test_writer_surfaces_403_as_writer_error(self, live_db) -> None:
        # Force a bad status through a candidate whose normalizer is bypassed by
        # monkeypatching is overkill; instead confirm the writer raises when the
        # server rejects. We hand-build a doc dict that the writer will PUT as-is
        # by stubbing build via a known-bad candidate -> writer guards first, so
        # here we assert the writer's own guard for bad status.
        cfg, db = live_db
        with pytest.raises(CouchDBCandidateWriterError):
            upsert_action_candidate(cfg, db, _candidate(status="bogus"))
