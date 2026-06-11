"""Shared faithful in-memory CouchDB double for the job_draft emission tests.

Authored by the INDEPENDENT VERIFIER for prompt 334b.

Prompt 334b repoints the three live pipeline emission call sites
(``pipeline_watcher.run_cycle``, ``voice_memo.semantics.run_semantic_pass`` and
the ``collect-action-candidates`` CLI non-dry-run path) from
``action_candidates.collect_action_candidates`` to
``field_capture.job_draft_emission.collect_job_drafts``. The repointed sites now
emit ``job_draft`` docs (review_status ``pending_approval``) instead of
``action_candidate`` review docs.

The pre-334b tests asserted the OLD candidate-emission behaviour at those sites.
This double lets the retargeted tests drive the NEW (job_draft) contract without
a live CouchDB, mirroring ``couchdb_review_double`` exactly:

  * It marks the candidate store CONFIGURED via the explicit-write switch so
    ``couchdb_candidate_config_or_none()`` returns a config (no real CouchDB env
    needed), which is what makes ``collect_job_drafts`` take the write branch.
  * It patches the job_draft writer transport that
    ``field_capture.job_draft_emission`` imported at module load
    (``get_job_draft`` / ``upsert_job_draft``) onto an in-memory store with the
    real ``draft_id``-keyed upsert + existence semantics. The REAL
    ``collect_job_drafts`` logic -- including the 334b exists->skip guard
    (``couchdb_job_draft_exists``) -- runs unchanged against the double, so a
    re-walk genuinely exercises the no-clobber path.
  * It builds the canonical doc through the real
    ``build_job_draft_document`` so the stored shape is the production
    ``job_draft`` doc (``_id == job_draft_<draft_id>``, ``type == job_draft``,
    validated ``review_status``), never a hand-rolled stand-in.

A test asserts against ``fake.drafts`` (the stored docs keyed by ``_id``) or the
convenience accessors below, the same way the candidate tests assert against
``couchdb_review.docs``.
"""
from __future__ import annotations

import json

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb_job_draft_writer import (
    CouchDBJobDraftWriterError,
    build_job_draft_document,
    job_draft_doc_id,
)


class FakeCouchJobDrafts:
    """In-memory slice of CouchDB the job_draft writer touches: a draft_id-keyed
    upsert that preserves an existing review_status on re-emit ONLY because the
    real ``collect_job_drafts`` exists->skip guard never re-writes an existing
    draft -- exactly the production no-clobber behaviour under test."""

    def __init__(self) -> None:
        self.drafts: dict[str, dict] = {}
        self._seq = 0
        self.get_count = 0
        self.put_count = 0

    # -- transport (patched onto job_draft_emission) ---------------------- #
    def get_job_draft(self, config, db, draft_id):
        self.get_count += 1
        doc = self.drafts.get(job_draft_doc_id(draft_id))
        return json.loads(json.dumps(doc)) if doc is not None else None

    def upsert_job_draft(self, config, db, draft, *, expected_rev=None):
        self.put_count += 1
        doc = build_job_draft_document(draft)
        doc_id = str(doc["_id"])
        existing = self.drafts.get(doc_id)
        if existing is not None and existing.get("_rev"):
            doc["_rev"] = existing["_rev"]
        self._seq += 1
        stored = json.loads(json.dumps(doc))
        stored["_rev"] = f"{self._seq}-fake"
        self.drafts[doc_id] = stored
        return dict(stored)

    # -- out-of-band review mutation (simulates an approver) -------------- #
    def set_review_status(self, draft_id: str, review_status: str) -> None:
        """Set review_status on a stored draft out of band, as a reviewer/Pro
        watcher would (the watcher re-walk must NOT clobber this)."""
        doc_id = job_draft_doc_id(draft_id)
        doc = self.drafts[doc_id]
        doc["review_status"] = review_status
        self._seq += 1
        doc["_rev"] = f"{self._seq}-fake"

    # -- convenience accessors ------------------------------------------- #
    def ids(self) -> list[str]:
        return sorted(self.drafts)

    def draft_ids(self) -> list[str]:
        return sorted(str(d["draft_id"]) for d in self.drafts.values())

    def doc(self, draft_id: str) -> dict:
        return self.drafts[job_draft_doc_id(draft_id)]

    def review_status_of(self, draft_id: str) -> str:
        return str(self.doc(draft_id)["review_status"])

    def for_group(self, group_id: str) -> list[dict]:
        return [d for d in self.drafts.values() if str(d.get("group_id")) == group_id]


@pytest.fixture
def couchdb_job_drafts(monkeypatch):
    """Install the faithful in-memory job_draft writer for a test.

    Marks the candidate store configured (so ``collect_job_drafts`` writes) and
    patches the writer transport the emission module imported. Returns the
    ``FakeCouchJobDrafts`` instance.
    """
    fake = FakeCouchJobDrafts()
    import field_capture.job_draft_emission as jde

    # Make couchdb_candidate_config_or_none() return a (fake-targeted) config so
    # collect_job_drafts takes the write branch -- same switch the candidate
    # double uses, so no real CouchDB env/creds are needed.
    monkeypatch.setenv("BTQ_COUCHDB_ACTION_CANDIDATE_WRITE", "1")
    cfg = couchdb_config.CouchDBConfig("http://fake-job-draft", "u", "p", 1.0, 1000)
    monkeypatch.setattr(jde, "couchdb_candidate_config_or_none", lambda: cfg)
    monkeypatch.setattr(jde.couchdb_config, "field_captures_database", lambda: "btq_field_captures")

    # Patch the writer symbols job_draft_emission imported at module load. The
    # 334b exists->skip guard (couchdb_job_draft_exists) calls jde.get_job_draft;
    # the write path calls jde.upsert_job_draft. Both resolve to the double.
    monkeypatch.setattr(jde, "get_job_draft", fake.get_job_draft)
    monkeypatch.setattr(jde, "upsert_job_draft", fake.upsert_job_draft)
    return fake
