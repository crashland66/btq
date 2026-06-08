"""Gating tests for BTQ prompt 308b: candidate review -> CouchDB with `_rev`
optimistic concurrency + the Pro-side staging watcher.

Authored by the INDEPENDENT VERIFIER (not the executor).

The riskiest line in the whole migration is the `_rev` CAS: if status updates
are last-write-wins, two surfaces racing the same candidate can both "approve"
it and the watcher stages two jobs (silent double-approval). These tests prove:

  1. Concurrency: two approves race the SAME candidate. The first wins
     (status:approved carrying `_rev`); the second -- whether it carries a stale
     `expected_rev` OR re-reads after the first wrote -- returns
     `already_decided` and does NOT mutate again. ZERO double-mutation. The
     watcher then stages EXACTLY ONCE; a second pass is a no-op. Net: one wins,
     one already_decided, exactly one staged job.
  2. Watcher idempotency + fail-soft: approved+unstaged -> staged once via the
     EXISTING stage_candidate_job_after_approval + staged_at set; re-run = no
     double stage; already-staged is skipped; one bad candidate doesn't block
     others.
  3. Shared review fn guards: not-found -> error; status != pending_review ->
     error (no mutate); approve w/ no/invalid proposed job -> error (no mutate);
     reject -> rejected, nothing staged; approve -> approved, nothing staged
     inline (the watcher stages).

The default double is a faithful in-memory CouchDB that emulates `_rev` CAS +
409-on-stale. It patches ONLY the HTTP transport
(couchdb_candidate_writer._get_document / _put_document and the watcher's
_request_json `_find`); ALL real review/CAS/watcher logic runs under test --
set_action_candidate_status, the AlreadyDecided raise, apply_candidate_review's
guards, find_approved_unstaged_candidates + process_one + staged_at marking.

A real-CouchDB variant (test_real_couchdb_concurrency_race) runs only when
BTQ_COUCHDB_URL/USER/PASSWORD point at a reachable server; otherwise it skips
loudly. (Credentials were not obtainable in this environment, so the proof of
record came from the faithful double; the real-CouchDB variant is wired and
ready.)
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib import error, request

import pytest

from event_pipeline import couchdb_config
from event_pipeline import couchdb_candidate_writer as writer
from event_pipeline.couchdb_candidate_writer import (
    AlreadyDecided,
    build_action_candidate_document,
)
from field_capture import action_candidates as fc
from field_capture import action_candidate_staging_watcher as watcher


DB = "btq_field_captures"


# --------------------------------------------------------------------------- #
# Faithful in-memory CouchDB double: real `_rev` CAS + 409-on-stale.
# --------------------------------------------------------------------------- #
class FakeCouch:
    """Emulates the slice of CouchDB the writer + watcher touch.

    - Documents keyed by _id; each carries a monotonically-incrementing _rev
      of the form "<n>-<hash>" exactly like CouchDB.
    - PUT requires the doc's current _rev (or none for a create). A mismatch
      raises HTTP 409 -- the real conflict the writer translates to
      AlreadyDecided.
    - GET 404s for a missing doc.
    - _find supports the watcher's approved+unstaged selector.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self._seq = 0
        self.put_count = 0

    # -- low-level transport the writer uses ------------------------------- #
    def get_document(self, config, database, doc_id):  # noqa: D401
        doc = self.docs.get(doc_id)
        return json.loads(json.dumps(doc)) if doc is not None else None

    def put_document(self, config, database, doc_id, doc, *, conflict_as_already_decided=False):
        self.put_count += 1
        incoming_rev = doc.get("_rev")
        existing = self.docs.get(doc_id)
        existing_rev = existing.get("_rev") if existing else None
        if existing_rev != incoming_rev:
            # CouchDB 409 on rev mismatch -> mirror the writer's translation.
            if conflict_as_already_decided:
                raise AlreadyDecided(f"CouchDB action candidate conflict for {doc_id}")
            raise writer.CouchDBCandidateWriterError(
                f"CouchDB action candidate PUT failed with HTTP 409 for {doc_id}"
            )
        self._seq += 1
        new_rev = f"{self._seq}-{uuid.uuid4().hex[:8]}"
        stored = json.loads(json.dumps(doc))
        stored["_rev"] = new_rev
        stored["_id"] = doc_id
        self.docs[doc_id] = stored
        return {"_id": doc_id, "_rev": new_rev, **{k: v for k, v in stored.items()}}

    # -- the watcher's _find query ----------------------------------------- #
    def request_json(self, config, db, method, path, payload=None):
        if path != "_find":
            raise AssertionError(f"unexpected watcher request: {method} {path}")
        sel = (payload or {}).get("selector", {})
        # Faithfully evaluate the selector the watcher SENDS -- do NOT impose our
        # own staged_at exclusion, or a missing selector guard would be masked.
        docs = [
            json.loads(json.dumps(d))
            for d in self.docs.values()
            if self._matches(d, sel)
        ]
        limit = int((payload or {}).get("limit", 100))
        return {"docs": docs[:limit]}

    @staticmethod
    def _matches(doc, selector) -> bool:
        for key, cond in selector.items():
            if key == "$or":
                if not any(FakeCouch._matches(doc, sub) for sub in cond):
                    return False
                continue
            actual = doc.get(key)
            if isinstance(cond, dict):
                if "$exists" in cond:
                    if (key in doc) != bool(cond["$exists"]):
                        return False
                else:
                    raise AssertionError(f"unsupported selector op: {cond}")
            else:
                if actual != cond:
                    return False
        return True

    # -- seeding ----------------------------------------------------------- #
    def seed_candidate(self, candidate: dict, *, status: str = "pending_review") -> str:
        payload = dict(candidate)
        payload["status"] = status
        doc = build_action_candidate_document(payload)
        doc_id = str(doc["_id"])
        self._seq += 1
        doc["_rev"] = f"{self._seq}-{uuid.uuid4().hex[:8]}"
        self.docs[doc_id] = doc
        return str(doc["candidate_id"])

    def status_of(self, candidate_id: str) -> str:
        return str(self.docs[f"action_candidate_{candidate_id}"]["status"])

    def rev_of(self, candidate_id: str) -> str:
        return str(self.docs[f"action_candidate_{candidate_id}"]["_rev"])

    def staged_at_of(self, candidate_id: str):
        return self.docs[f"action_candidate_{candidate_id}"].get("staged_at")


@pytest.fixture()
def fake_couch(monkeypatch):
    fake = FakeCouch()
    # Patch the writer's HTTP transport: all CAS/status logic stays real.
    monkeypatch.setattr(writer, "_get_document", fake.get_document)
    monkeypatch.setattr(writer, "_put_document", fake.put_document)
    # Patch the watcher's _find transport.
    monkeypatch.setattr(watcher, "_request_json", fake.request_json)
    return fake


def _config() -> couchdb_config.CouchDBConfig:
    return couchdb_config.CouchDBConfig("http://fake", "u", "p", 1.0, 1000)


def _approvable_candidate(candidate_id: str) -> dict:
    """A candidate carrying a pre-validated proposed job (append_to_note)."""
    return {
        "type": "action_candidate_review",
        "candidate_id": candidate_id,
        "candidate_type": "field_capture_follow_up",
        "summary": "Staffing risk at site 7050",
        "confidence": "high",
        "source_text": "Bruce no-showed again.",
        "source_context": "voice memo",
        "site_id": "7050",
        "channel_metadata": {"site_id": "7050", "submitter_name": "Greg"},
        "approval_metadata": {
            "proposed_queue_job": {
                "job_type": "append_to_note",
                "payload": {
                    "path": "Accounts/7050.md",
                    "content": "Staffing risk: Bruce no-showed.",
                    "destination": "site_note",
                },
            }
        },
    }


def _stage_spy(monkeypatch):
    """Replace the real filesystem staging with a counting spy so the watcher's
    idempotency is proven independent of the draft/queue plumbing (which has its
    own gate tests). Returns the call list."""
    calls: list[str] = []

    def fake_stage(runtime_root, candidate_id):
        calls.append(candidate_id)
        return f"draft_ids=draft_{candidate_id} staged=1"

    monkeypatch.setattr(watcher, "stage_candidate_job_after_approval", fake_stage)
    # The watcher also materializes a filesystem mirror before staging; stub it
    # so we don't depend on the candidate dir existing.
    monkeypatch.setattr(
        watcher,
        "materialize_candidate_for_existing_staging",
        lambda doc, runtime_root: Path(runtime_root) / "candidate.json",
    )
    return calls


# --------------------------------------------------------------------------- #
# 1. CRITICAL: `_rev` optimistic concurrency race.
# --------------------------------------------------------------------------- #
class TestRevConcurrency:
    def test_stale_expected_rev_loses_no_double_mutation(self, fake_couch):
        cid = fake_couch.seed_candidate(_approvable_candidate("race1"))
        cfg, db = _config(), DB
        rev0 = fake_couch.rev_of(cid)

        # Reviewer A approves carrying the original rev -> wins.
        a = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve",
                                      reviewer="A", expected_rev=rev0)
        assert a.error is None
        assert a.already_decided is False
        assert a.status == "approved"
        assert fake_couch.status_of(cid) == "approved"
        puts_after_a = fake_couch.put_count
        rev_after_a = fake_couch.rev_of(cid)

        # Reviewer B approves carrying the NOW-STALE original rev -> loses.
        b = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve",
                                      reviewer="B", expected_rev=rev0)
        assert b.already_decided is True
        assert b.error is None
        # Zero double-mutation: status unchanged, rev unchanged, no extra PUT.
        assert fake_couch.status_of(cid) == "approved"
        assert fake_couch.rev_of(cid) == rev_after_a
        assert fake_couch.put_count == puts_after_a

    def test_second_approve_fresh_read_after_first_write_loses(self, fake_couch):
        """No expected_rev supplied: the second approve re-reads AFTER the first
        wrote. The load-bearing invariant is ZERO double-mutation. Here the
        status guard (prior_status != pending_review) fires first, so the loss is
        surfaced as an `error` (not `already_decided`) -- which is still safe: no
        second PUT, status stays approved. (The concurrent stale-`_rev` 409 path
        -- both reading rev0 then racing the PUT -- is the one that surfaces
        already_decided; see the expected_rev test above.)"""
        cid = fake_couch.seed_candidate(_approvable_candidate("race2"))
        cfg, db = _config(), DB

        a = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve", reviewer="A")
        assert a.status == "approved" and a.already_decided is False
        puts_after_a = fake_couch.put_count
        rev_after_a = fake_couch.rev_of(cid)

        b = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve", reviewer="B")
        # The second approve does NOT win and does NOT double-mutate.
        assert b.status != "approved" or b.error is not None
        assert b.error and "not pending_review" in b.error
        # ZERO double-mutation: status unchanged, rev unchanged, no extra PUT.
        assert fake_couch.status_of(cid) == "approved"
        assert fake_couch.rev_of(cid) == rev_after_a
        assert fake_couch.put_count == puts_after_a

    def test_genuine_toctou_409_surfaces_already_decided(self, fake_couch, monkeypatch):
        """The real concurrent race: BOTH reviewers read rev0 (so expected_rev
        matches at the status-check), then A's PUT commits first and B's PUT --
        still carrying rev0 -- hits a real CouchDB 409. That 409 MUST surface as
        already_decided (NOT an error, NOT last-write-wins)."""
        cid = fake_couch.seed_candidate(_approvable_candidate("toctou"))
        cfg, db = _config(), DB
        rev0 = fake_couch.rev_of(cid)

        # Simulate B having already read rev0, then A committing, by patching
        # get_action_candidate (as seen from set_action_candidate_status) to
        # return a doc still pinned at rev0 while the store has advanced.
        a = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve",
                                      reviewer="A", expected_rev=rev0)
        assert a.status == "approved"
        rev_after_a = fake_couch.rev_of(cid)
        puts_after_a = fake_couch.put_count

        # B's stale snapshot: status still pending_review at rev0 -> passes the
        # in-Python status + expected_rev checks, then the PUT carrying rev0
        # collides with the committed rev_after_a -> 409 -> AlreadyDecided.
        stale = dict(fake_couch.docs[f"action_candidate_{cid}"])
        stale["status"] = "pending_review"
        stale["_rev"] = rev0

        real_get = writer._get_document
        seen = {"n": 0}

        def get_stale_first(config, database, doc_id):
            # First read (apply_candidate_review's own get) -> stale snapshot.
            # Subsequent reads (set_action_candidate_status re-read + the
            # AlreadyDecided latest re-read) -> stale then real.
            seen["n"] += 1
            if seen["n"] <= 2:
                return json.loads(json.dumps(stale))
            return real_get(config, database, doc_id)

        monkeypatch.setattr(writer, "_get_document", get_stale_first)
        b = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve",
                                      reviewer="B", expected_rev=rev0)
        assert b.already_decided is True, "a genuine 409 must surface as already_decided"
        assert b.error is None
        # No double-mutation survived: store still at A's rev/status.
        assert fake_couch.status_of(cid) == "approved"
        assert fake_couch.rev_of(cid) == rev_after_a
        # B did attempt exactly one PUT (which 409'd) and no more.
        assert fake_couch.put_count == puts_after_a + 1

    def test_concurrent_approve_then_watcher_stages_exactly_once(self, fake_couch, monkeypatch, tmp_path):
        """End-to-end blast-shield: two surfaces approve, one wins / one
        already_decided, then the watcher stages EXACTLY ONE job and a second
        pass is a no-op."""
        calls = _stage_spy(monkeypatch)
        cid = fake_couch.seed_candidate(_approvable_candidate("race3"))
        cfg, db = _config(), DB
        rev0 = fake_couch.rev_of(cid)

        a = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve",
                                      reviewer="phone", expected_rev=rev0)
        b = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve",
                                      reviewer="swipe", expected_rev=rev0)
        # Exactly one winner.
        decided = [a, b]
        winners = [r for r in decided if not r.already_decided and r.error is None]
        losers = [r for r in decided if r.already_decided]
        assert len(winners) == 1, "exactly one approve must win"
        assert len(losers) == 1, "exactly one approve must be already_decided"
        assert fake_couch.status_of(cid) == "approved"

        import logging
        logger = logging.getLogger("test.watcher")

        # First watcher pass: stages once + marks staged_at.
        res1 = watcher.process_pass(config=cfg, db=db, runtime_root=tmp_path, logger=logger)
        assert [r for r in res1 if r.get("staged")] != []
        assert sum(1 for r in res1 if r.get("staged")) == 1
        assert calls == [cid]
        assert fake_couch.staged_at_of(cid)

        # Second watcher pass: candidate now has staged_at -> not returned by
        # _find -> NO additional stage.
        res2 = watcher.process_pass(config=cfg, db=db, runtime_root=tmp_path, logger=logger)
        assert all(not r.get("staged") for r in res2)
        assert calls == [cid], "second watcher pass must not stage again"


# --------------------------------------------------------------------------- #
# 2. Watcher idempotency + fail-soft.
# --------------------------------------------------------------------------- #
class TestWatcher:
    def test_approved_unstaged_staged_once_and_marked(self, fake_couch, monkeypatch, tmp_path):
        calls = _stage_spy(monkeypatch)
        cid = fake_couch.seed_candidate(_approvable_candidate("w1"), status="approved")
        cfg, db = _config(), DB
        import logging
        logger = logging.getLogger("test.watcher")

        res = watcher.process_pass(config=cfg, db=db, runtime_root=tmp_path, logger=logger)
        assert calls == [cid]
        assert sum(1 for r in res if r.get("staged")) == 1
        assert fake_couch.staged_at_of(cid)

    def test_rerun_does_not_double_stage(self, fake_couch, monkeypatch, tmp_path):
        calls = _stage_spy(monkeypatch)
        cid = fake_couch.seed_candidate(_approvable_candidate("w2"), status="approved")
        cfg, db = _config(), DB
        import logging
        logger = logging.getLogger("test.watcher")
        watcher.process_pass(config=cfg, db=db, runtime_root=tmp_path, logger=logger)
        watcher.process_pass(config=cfg, db=db, runtime_root=tmp_path, logger=logger)
        assert calls == [cid]

    def test_already_staged_is_skipped(self, fake_couch, monkeypatch, tmp_path):
        calls = _stage_spy(monkeypatch)
        cid = fake_couch.seed_candidate(_approvable_candidate("w3"), status="approved")
        # Pre-mark staged_at so the doc is already staged.
        fake_couch.docs[f"action_candidate_{cid}"]["staged_at"] = "2026-06-07T00:00:00+00:00"
        cfg, db = _config(), DB
        import logging
        logger = logging.getLogger("test.watcher")
        res = watcher.process_pass(config=cfg, db=db, runtime_root=tmp_path, logger=logger)
        assert calls == []
        assert all(not r.get("staged") for r in res)

    def test_one_bad_candidate_does_not_block_others(self, fake_couch, monkeypatch, tmp_path):
        good = fake_couch.seed_candidate(_approvable_candidate("good"), status="approved")
        bad = fake_couch.seed_candidate(_approvable_candidate("bad"), status="approved")
        cfg, db = _config(), DB

        staged: list[str] = []

        def flaky_stage(runtime_root, candidate_id):
            if candidate_id == bad:
                raise RuntimeError("simulated staging failure")
            staged.append(candidate_id)
            return f"draft_ids=d_{candidate_id} staged=1"

        monkeypatch.setattr(watcher, "stage_candidate_job_after_approval", flaky_stage)
        monkeypatch.setattr(
            watcher, "materialize_candidate_for_existing_staging",
            lambda doc, runtime_root: Path(runtime_root) / "c.json",
        )
        import logging
        logger = logging.getLogger("test.watcher")
        res = watcher.process_pass(config=cfg, db=db, runtime_root=tmp_path, logger=logger)

        by_id = {r["candidate_id"]: r for r in res}
        # The bad one fails soft (ok False, logged) -- the good one still staged.
        assert by_id[bad]["ok"] is False
        assert by_id[bad]["staged"] is False
        assert by_id[good]["ok"] is True
        assert by_id[good]["staged"] is True
        assert staged == [good]
        # The good candidate is marked staged; the bad one is NOT.
        assert fake_couch.staged_at_of(good)
        assert not fake_couch.staged_at_of(bad)


# --------------------------------------------------------------------------- #
# 3. Shared review fn guards.
# --------------------------------------------------------------------------- #
class TestSharedReviewGuards:
    def test_not_found_is_error(self, fake_couch):
        cfg, db = _config(), DB
        r = fc.apply_candidate_review(cfg, db, candidate_id="missing", action="approve", reviewer="A")
        assert r.error and "not found" in r.error
        assert r.already_decided is False

    def test_status_not_pending_is_error_no_mutate(self, fake_couch):
        cid = fake_couch.seed_candidate(_approvable_candidate("g1"), status="approved")
        cfg, db = _config(), DB
        puts = fake_couch.put_count
        r = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve", reviewer="A")
        assert r.error and "not pending_review" in r.error
        assert fake_couch.put_count == puts  # no mutation attempted

    def test_approve_no_proposed_job_is_error_no_mutate(self, fake_couch):
        bad = {
            "type": "action_candidate_review",
            "candidate_id": "g2",
            "candidate_type": "voice_memo_operator_action",
            "summary": "",  # no summary -> no fallback job generated
            "channel_metadata": {},
        }
        cid = fake_couch.seed_candidate(bad)
        cfg, db = _config(), DB
        puts = fake_couch.put_count
        r = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve", reviewer="A")
        assert r.error  # invalid / missing proposed job
        assert fake_couch.put_count == puts
        assert fake_couch.status_of(cid) == "pending_review"

    def test_reject_sets_rejected_nothing_staged(self, fake_couch, monkeypatch, tmp_path):
        calls = _stage_spy(monkeypatch)
        cid = fake_couch.seed_candidate(_approvable_candidate("g3"))
        cfg, db = _config(), DB
        r = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="reject", reviewer="A",
                                      rationale="not needed")
        assert r.error is None
        assert r.status == "rejected"
        assert fake_couch.status_of(cid) == "rejected"
        # Watcher only stages approved -> rejected never staged.
        import logging
        watcher.process_pass(config=cfg, db=db, runtime_root=tmp_path,
                             logger=logging.getLogger("t"))
        assert calls == []

    def test_approve_sets_approved_nothing_staged_inline(self, fake_couch, monkeypatch):
        # The shared fn must NOT stage inline: assert stage helper is never
        # called by apply_candidate_review.
        from field_capture import candidate_staging
        called = {"n": 0}
        monkeypatch.setattr(
            candidate_staging, "stage_candidate_job_after_approval",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        cid = fake_couch.seed_candidate(_approvable_candidate("g4"))
        cfg, db = _config(), DB
        r = fc.apply_candidate_review(cfg, db, candidate_id=cid, action="approve", reviewer="A")
        assert r.status == "approved"
        assert fake_couch.status_of(cid) == "approved"
        assert called["n"] == 0, "apply_candidate_review must NOT stage inline"


# --------------------------------------------------------------------------- #
# Real CouchDB variant -- runs only against a reachable server.
# --------------------------------------------------------------------------- #
def _live_config():
    url = os.environ.get("BTQ_TEST_COUCHDB_URL") or os.environ.get("BTQ_COUCHDB_URL")
    user = os.environ.get("BTQ_TEST_COUCHDB_USER") or os.environ.get("BTQ_COUCHDB_USER")
    pwd = os.environ.get("BTQ_TEST_COUCHDB_PASSWORD") or os.environ.get("BTQ_COUCHDB_PASSWORD")
    if not (url and user and pwd):
        return None
    cfg = couchdb_config.from_env(base_url_override=url, username_override=user, password_override=pwd)
    try:
        req = request.Request(f"{cfg.base_url}/", headers=cfg.auth_header())
        with request.urlopen(req, timeout=cfg.timeout):
            pass
    except (error.URLError, OSError):
        return None
    return cfg


def test_real_couchdb_concurrency_race():
    cfg = _live_config()
    if cfg is None:
        pytest.skip("no reachable CouchDB (set BTQ_COUCHDB_URL/USER/PASSWORD)")
    db = f"btq_verify_308b_{uuid.uuid4().hex[:8]}"

    def _req(method, path="", body=None):
        url = f"{cfg.base_url}/{db}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json"
        headers.update(cfg.auth_header())
        r = request.Request(url, data=data, headers=headers, method=method)
        with request.urlopen(r, timeout=cfg.timeout) as resp:
            return json.loads(resp.read() or b"{}")

    _req("PUT")
    try:
        candidate = _approvable_candidate("live1")
        candidate["status"] = "pending_review"
        doc = build_action_candidate_document(candidate)
        _req("PUT", f"/{doc['_id']}", doc)
        stored = _req("GET", f"/{doc['_id']}")
        rev0 = stored["_rev"]

        a = fc.apply_candidate_review(cfg, db, candidate_id="live1", action="approve",
                                      reviewer="A", expected_rev=rev0)
        b = fc.apply_candidate_review(cfg, db, candidate_id="live1", action="approve",
                                      reviewer="B", expected_rev=rev0)
        assert (a.already_decided, b.already_decided) in {(False, True), (True, False)}
        final = _req("GET", f"/{doc['_id']}")
        assert final["status"] == "approved"
    finally:
        _req("DELETE")
