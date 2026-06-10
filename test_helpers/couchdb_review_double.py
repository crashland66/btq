"""Shared faithful in-memory CouchDB double for the 308b candidate-review tests.

Authored by the INDEPENDENT VERIFIER for prompt 308b.

Prompt 308b migrates candidate review from a filesystem status mutation to a
CouchDB doc mutation guarded by `_rev` optimistic concurrency, with Pro-side
watcher staging. The pre-308b ops-dashboard / swipe tests asserted the OLD
contract (filesystem status flip + inline "approved and staged"). This helper
lets those tests drive the NEW contract without a live CouchDB:

  * It patches ONLY the HTTP transport in
    ``event_pipeline.couchdb_candidate_writer`` (``_get_document`` /
    ``_put_document``) and the watcher's ``_request_json`` (`_find`), so ALL of
    the real review/CAS/guard/watcher/payload-patch logic runs under test --
    including the real ``_rev`` 409 -> AlreadyDecided translation.
  * It patches ``couchdb_config.from_env`` so the dashboard's
    ``_handle_review_post`` resolves a config without real env creds.
  * ``seed_from_fs`` mirrors the filesystem candidate artifacts the tests
    already write into the CouchDB store (the migration's backfill, in miniature)
    so the review path finds the doc.

The re-aligned tests assert the candidate's CouchDB status flips and -- where the
old test asserted staging -- that running the watcher then stages exactly once.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from event_pipeline import couchdb_config
from event_pipeline import couchdb_candidate_writer as _writer
from event_pipeline.couchdb_candidate_writer import (
    AlreadyDecided,
    build_action_candidate_document,
)
from field_capture import action_candidate_staging_watcher as _watcher


class FakeCouchReview:
    """Emulates the slice of CouchDB the writer + watcher touch, with real
    `_rev` CAS + 409-on-stale and a minimal `_find`."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self._seq = 0
        self.put_count = 0
        self.stage_calls: list[str] = []

    # -- transport -------------------------------------------------------- #
    def get_document(self, config, database, doc_id):
        doc = self.docs.get(doc_id)
        return json.loads(json.dumps(doc)) if doc is not None else None

    def put_document(self, config, database, doc_id, doc, *, conflict_as_already_decided=False):
        self.put_count += 1
        incoming_rev = doc.get("_rev")
        existing = self.docs.get(doc_id)
        existing_rev = existing.get("_rev") if existing else None
        if existing_rev != incoming_rev:
            if conflict_as_already_decided:
                raise AlreadyDecided(f"CouchDB action candidate conflict for {doc_id}")
            raise _writer.CouchDBCandidateWriterError(
                f"CouchDB action candidate PUT failed with HTTP 409 for {doc_id}"
            )
        self._seq += 1
        stored = json.loads(json.dumps(doc))
        stored["_rev"] = f"{self._seq}-{uuid.uuid4().hex[:8]}"
        stored["_id"] = doc_id
        self.docs[doc_id] = stored
        return dict(stored)

    def request_json(self, config, db, method, path, payload=None):
        if path != "_find":
            raise AssertionError(f"unexpected watcher request: {method} {path}")
        sel = (payload or {}).get("selector", {})
        docs = [json.loads(json.dumps(d)) for d in self.docs.values() if self._matches(d, sel)]
        return {"docs": docs[: int((payload or {}).get("limit", 100))]}

    @staticmethod
    def _matches(doc, selector) -> bool:
        for key, cond in selector.items():
            if key == "$or":
                if not any(FakeCouchReview._matches(doc, sub) for sub in cond):
                    return False
                continue
            actual = doc.get(key)
            if isinstance(cond, dict):
                if "$exists" in cond:
                    if (key in doc) != bool(cond["$exists"]):
                        return False
                else:
                    raise AssertionError(f"unsupported selector op: {cond}")
            elif actual != cond:
                return False
        return True

    # -- seeding / queries ------------------------------------------------ #
    def seed_doc(self, candidate: dict, *, status: str = "pending_review") -> str:
        payload = dict(candidate)
        payload.setdefault("status", status)
        doc = build_action_candidate_document(payload)
        self._seq += 1
        doc["_rev"] = f"{self._seq}-{uuid.uuid4().hex[:8]}"
        self.docs[str(doc["_id"])] = doc
        return str(doc["candidate_id"])

    def seed_from_fs(self, runtime_root: Path) -> list[str]:
        """Mirror every filesystem candidate artifact into the CouchDB store."""
        cand_dir = (
            Path(runtime_root)
            / "reviews"
            / "action_candidates"
            / "field_capture"
        )
        seeded: list[str] = []
        if not cand_dir.exists():
            return seeded
        for path in sorted(cand_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not payload.get("candidate_id"):
                continue
            seeded.append(self.seed_doc(payload, status=str(payload.get("status") or "pending_review")))
        return seeded

    def upsert(self, candidate) -> None:
        """Collector write path: build the canonical doc and store it in-memory."""
        if not isinstance(candidate, dict):
            return
        doc = build_action_candidate_document(dict(candidate))
        doc_id = str(doc["_id"])
        self._seq += 1
        doc["_rev"] = str(self._seq) + "-" + uuid.uuid4().hex[:8]
        self.docs[doc_id] = doc

    def mirror_write(self, candidate_dir, payload) -> None:
        """Mirror a single filesystem candidate write into the CouchDB store.

        This is the 308c migration backfill-in-miniature applied per write: a
        test that writes a candidate to the filesystem (via any shared helper)
        has that same candidate land in CouchDB, so the reader path surfaces it.
        Idempotent on candidate_id (re-write keeps one doc).
        """
        if not isinstance(payload, dict):
            return
        candidate_id = str(payload.get("candidate_id") or "")
        if not candidate_id:
            return
        doc_id = "action_candidate_" + candidate_id
        doc = build_action_candidate_document(dict(payload))
        self._seq += 1
        doc["_rev"] = str(self._seq) + "-" + uuid.uuid4().hex[:8]
        doc["_id"] = doc_id
        self.docs[doc_id] = doc

    def project_to_fs(self, candidate_dir, only_ids=None) -> None:
        """Write the canonical CouchDB candidates out to the filesystem review
        dir as a faithful read-projection.

        308c makes CouchDB the SOLE canonical candidate store; the collector no
        longer writes the filesystem artifact. Legacy collector/CLI tests assert
        candidate *content/shape* via the filesystem review artifacts. We let the
        REAL collector run (so the CouchDB write + counts + dedup are genuinely
        exercised and proven in self.docs), then project each canonical CouchDB
        doc back to the filesystem as the review payload those assertions read.
        FS is thus strictly downstream of CouchDB: a file exists ONLY because the
        candidate was written to CouchDB first -- so the legacy assertions now
        transitively prove the CouchDB write, never mask an empty reader.
        """
        from pathlib import Path as _Path
        import field_capture.action_candidates as _fc
        from processing_core.action_candidates import (
            action_candidate_review_path as _review_path,
            write_action_candidate_review as _orig_fs_write,
        )

        cand_dir = _Path(candidate_dir)
        cand_dir.mkdir(parents=True, exist_ok=True)
        for doc_id, doc in self.docs.items():
            if only_ids is not None and doc_id not in only_ids:
                continue
            if str(doc.get("type") or "") != "action_candidate":
                continue
            payload = _fc.couchdb_action_candidate_to_review_payload(doc)
            if not str(payload.get("candidate_id") or ""):
                continue
            _orig_fs_write(cand_dir, payload)

    def review_payload_for(self, candidate_path) -> dict:
        """Resolve a candidate pseudo-path (couchdb/<db>/action_candidate_<id>)
        to the review-shaped payload from the in-memory CouchDB store.

        308c: collect/run_semantic_pass now report each candidate's location as a
        CouchDB pseudo-path, not a filesystem file. Tests that previously did
        ``json.loads(candidate_path.read_text())`` read the candidate from the
        sole canonical store (CouchDB) instead -- so they still prove the
        candidate was written, never an empty/absent reader.
        """
        import field_capture.action_candidates as _fc
        from pathlib import Path as _Path
        doc_id = _Path(str(candidate_path)).name
        doc = self.docs[doc_id]
        return _fc.couchdb_action_candidate_to_review_payload(doc)

    def status_of(self, candidate_id: str) -> str:
        return str(self.docs[f"action_candidate_{candidate_id}"]["status"])

    def staged_at_of(self, candidate_id: str):
        return self.docs[f"action_candidate_{candidate_id}"].get("staged_at")

    def archived_of(self, candidate_id: str) -> bool:
        return bool(self.docs[f"action_candidate_{candidate_id}"].get("archived") is True)

    def channel_metadata_of(self, candidate_id: str) -> dict:
        source_detail = self.docs[f"action_candidate_{candidate_id}"].get("source_detail")
        if not isinstance(source_detail, dict):
            return {}
        metadata = source_detail.get("channel_metadata")
        return dict(metadata) if isinstance(metadata, dict) else {}

    def run_watcher(self, runtime_root: Path, *, stub_stage: bool = True):
        """Run one watcher pass. By default the actual filesystem staging is
        replaced by a counting spy (recorded in ``stage_calls``) and the
        candidate-materialization is stubbed, so the test depends only on the
        watcher's idempotency, not on the draft/queue plumbing (which has its own
        gate tests)."""
        import logging

        cfg = couchdb_config.from_env()
        db = couchdb_config.field_captures_database()
        logger = logging.getLogger("test.308b.watcher")
        import unittest.mock as _mock

        ctx = []
        if stub_stage:
            def fake_stage(_rt, cid):
                self.stage_calls.append(cid)
                return f"draft_ids=draft_{cid} staged=1"

            ctx.append(_mock.patch.object(_watcher, "stage_candidate_job_after_approval", fake_stage))
            ctx.append(
                _mock.patch.object(
                    _watcher,
                    "materialize_candidate_for_existing_staging",
                    lambda doc, rt: Path(rt) / "candidate.json",
                )
            )
        for c in ctx:
            c.start()
        try:
            return _watcher.process_pass(
                config=cfg, db=db, runtime_root=Path(runtime_root), logger=logger
            )
        finally:
            for c in ctx:
                c.stop()


@pytest.fixture
def couchdb_review(monkeypatch):
    """Install the faithful CouchDB review double for a test.

    Patches the writer HTTP transport, the watcher `_find`, and
    ``couchdb_config.from_env`` so ``_handle_review_post`` reaches the in-memory
    store. Returns the ``FakeCouchReview`` instance.
    """
    fake = FakeCouchReview()
    import field_capture.action_candidates as _pc_fc
    import processing_core.action_candidates as _pc
    # Mark the candidate store CONFIGURED so the reader path
    # (couchdb_candidate_store_configured) engages against the double instead of
    # short-circuiting to an empty list. Uses the explicit-write switch so no
    # real CouchDB env (URL/user/pass) is needed.
    monkeypatch.setenv("BTQ_COUCHDB_ACTION_CANDIDATE_WRITE", "1")
    monkeypatch.setattr(_writer, "_get_document", fake.get_document)
    # upsert_action_candidate does its OWN inline urlopen PUT (not _put_document),
    # so patch it directly onto the store -- this is the collector's write path.
    monkeypatch.setattr(_writer, "upsert_action_candidate", lambda config, db, candidate: fake.upsert(candidate))
    monkeypatch.setattr(_pc_fc, "upsert_action_candidate", lambda config, db, candidate: fake.upsert(candidate), raising=False)
    monkeypatch.setattr(_writer, "_put_document", fake.put_document)
    # The watcher and the candidate LISTING reader (list_action_candidates) each
    # have their own _request_json (`_find`). Patch BOTH onto the in-memory store
    # so reader surfaces (swipe/dashboard/inbox/site-status/retarget) resolve
    # seeded candidates through the real CouchDB read path -- not an empty list.
    monkeypatch.setattr(_watcher, "_request_json", fake.request_json)
    monkeypatch.setattr(_writer, "_request_json", fake.request_json)

    cfg = couchdb_config.CouchDBConfig("http://fake-review", "u", "p", 1.0, 1000)
    monkeypatch.setattr(couchdb_config, "from_env", lambda *a, **k: cfg)
    # The dashboard imports couchdb_config as a module attr; patching the module
    # function above covers ops_dashboard.sections.candidates.couchdb_config too.

    # 308c: candidate READERS now read CouchDB, not the filesystem. Auto-mirror
    # every filesystem candidate write into the double so reader tests that seed
    # via the shared FS helpers still surface their candidate through the CouchDB
    # read path (seed-and-assert-surfaced; never weakened to expect empty).
    import sys as _sys
    _orig_write = _pc.write_action_candidate_review

    def _mirroring_write(candidate_dir, payload):
        result = _orig_write(candidate_dir, payload)
        fake.mirror_write(candidate_dir, payload)
        return result

    monkeypatch.setattr(_pc, "write_action_candidate_review", _mirroring_write)
    # Re-bind the imported references in EVERY already-loaded test module that
    # pulled the symbol in at import time, so helpers calling the module-local
    # name also mirror. pytest's default prepend import mode loads these test
    # modules under their BARE name (no ``tests.`` package prefix, since there is
    # no tests/__init__.py); iterate sys.modules so we re-bind whatever object
    # pytest actually executes against -- not a separate dotted copy.
    for _mod in list(_sys.modules.values()):
        if _mod is None:
            continue
        try:
            current = getattr(_mod, "write_action_candidate_review", None)
        except Exception:
            continue
        if current is _orig_write:
            monkeypatch.setattr(_mod, "write_action_candidate_review", _mirroring_write, raising=False)
    # 308c: the collector writes candidates to CouchDB and NO LONGER writes the
    # filesystem artifact. Legacy collector/CLI tests assert candidate content
    # via the filesystem review dir. Wrap collect_action_candidates so the REAL
    # collector runs (genuinely writing to the double + computing counts/dedup),
    # then project the canonical CouchDB docs back to the filesystem review dir
    # the test passed in -- FS strictly downstream of CouchDB (no projected file
    # unless the candidate reached CouchDB first).
    _orig_collect = _pc_fc.collect_action_candidates

    def _collect_then_project(semantic_dirs, candidate_dir, *args, **kwargs):
        before = set(fake.docs)
        counts = _orig_collect(semantic_dirs, candidate_dir, *args, **kwargs)
        # Project ONLY the candidates this collect call wrote into CouchDB, so a
        # test that runs the collector multiple times against DIFFERENT candidate
        # dirs does not have the accumulated store dumped into each dir (the
        # double persists across calls within one test). FS stays strictly
        # downstream of -- and scoped to -- the candidates this call produced.
        new_ids = set(fake.docs) - before
        try:
            fake.project_to_fs(candidate_dir, only_ids=new_ids)
        except Exception:
            pass
        return counts

    monkeypatch.setattr(_pc_fc, "collect_action_candidates", _collect_then_project)
    return fake
