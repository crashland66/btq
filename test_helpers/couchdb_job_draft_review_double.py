"""Shared faithful in-memory CouchDB double for the job_draft REVIEW SURFACES.

Authored by the INDEPENDENT VERIFIER for prompt 337a.

Prompt 337a repoints the ops-dashboard review surfaces (swipe / candidates /
inbox) from reading ``action_candidate`` docs to reading ``job_draft`` docs and
approving/rejecting them through the 335 write path
(``job_draft_review.apply_job_draft_review`` ->
``couchdb_job_draft_writer.set_job_draft_review_status``).

The pre-337a surface tests seeded ``action_candidate`` docs into the
``couchdb_review`` double and asserted candidate cards. After 337a the surfaces
read ``job_draft`` docs through:

  * ``job_draft_review.couchdb_job_draft_payloads`` -> resolves a config via
    ``couchdb_candidate_config_or_none`` and lists drafts via
    ``couchdb_job_draft_writer.list_job_drafts`` (which calls the SHARED
    ``_request_json`` ``_find`` transport, imported into the job_draft_writer
    namespace at module load).
  * ``job_draft_review.apply_job_draft_review`` -> the 335 review write path,
    which calls ``get_job_draft`` (``_get_document``) and
    ``set_job_draft_review_status`` (``_put_document`` with
    ``conflict_as_already_decided=True``) -- all imported into the
    job_draft_writer namespace.

The ``couchdb_review`` double patches those transports on the CANDIDATE writer
module (``couchdb_candidate_writer``); the job_draft writer holds its OWN
module-level bindings of the same functions (``from ... import _request_json``),
so patching the candidate module does NOT reach the draft reader -- that is why
the draft surfaces made a real network call under the candidate double (the
308c name-resolution failure). This double patches the bindings the draft path
ACTUALLY resolves: on ``couchdb_job_draft_writer`` and on ``job_draft_review``.

Discipline (same as ``couchdb_review``): the REAL review/CAS/guard/listing/
payload-shaping logic runs under test. Only the HTTP transport + config
resolution are replaced with an in-memory store keyed by ``_id`` with real
``_rev`` optimistic concurrency (409 -> AlreadyDecided) and a minimal ``_find``.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import AlreadyDecided
from event_pipeline import couchdb_job_draft_writer as _jdw
from event_pipeline.couchdb_job_draft_writer import (
    CouchDBJobDraftWriterError,
    build_job_draft_document,
    job_draft_doc_id,
)
from field_capture import job_draft_review as _review


class FakeCouchJobDraftReview:
    """Emulates the slice of CouchDB the job_draft review surfaces touch:
    ``_get_document`` / ``_put_document`` (with real ``_rev`` CAS + 409) and a
    minimal ``_find`` selector match -- so the REAL listing + review logic runs.

    Also holds NON-draft docs (e.g. action_candidate) so a test can prove the
    draft surfaces do NOT surface a co-resident action_candidate.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self._seq = 0
        self.get_count = 0
        self.put_count = 0
        self.find_count = 0

    # -- transport (patched onto couchdb_job_draft_writer) ---------------- #
    def get_document(self, config, database, doc_id):
        self.get_count += 1
        doc = self.docs.get(doc_id)
        return json.loads(json.dumps(doc)) if doc is not None else None

    def put_document(self, config, database, doc_id, doc, *, conflict_as_already_decided=False):
        self.put_count += 1
        incoming_rev = doc.get("_rev")
        existing = self.docs.get(doc_id)
        existing_rev = existing.get("_rev") if existing else None
        if existing_rev != incoming_rev:
            if conflict_as_already_decided:
                raise AlreadyDecided(f"CouchDB job_draft conflict for {doc_id}")
            raise CouchDBJobDraftWriterError(
                f"CouchDB job_draft PUT failed with HTTP 409 for {doc_id}"
            )
        self._seq += 1
        stored = json.loads(json.dumps(doc))
        stored["_rev"] = f"{self._seq}-{uuid.uuid4().hex[:8]}"
        stored["_id"] = doc_id
        self.docs[doc_id] = stored
        return dict(stored)

    def request_json(self, config, db, method, path, payload=None):
        self.find_count += 1
        if path != "_find":
            raise AssertionError(f"unexpected draft request: {method} {path}")
        sel = (payload or {}).get("selector", {})
        docs = [
            json.loads(json.dumps(d))
            for d in self.docs.values()
            if self._matches(d, sel)
        ]
        return {"docs": docs[: int((payload or {}).get("limit", 500))]}

    @staticmethod
    def _matches(doc, selector) -> bool:
        for key, cond in selector.items():
            if key == "$or":
                if not any(FakeCouchJobDraftReview._matches(doc, sub) for sub in cond):
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

    # -- seeding ---------------------------------------------------------- #
    def seed_draft(self, draft: dict, **overrides) -> str:
        """Build the canonical job_draft doc via the real builder and store it.

        Returns the draft_id. Extra non-canonical surface fields (message,
        site_id, submitter_name, source_capture_id, confidence, created_at, ...)
        are merged onto the built doc so the review-payload projection has them.
        """
        payload = dict(draft)
        payload.update(overrides)
        doc = build_job_draft_document(payload)
        # Preserve surface/projection fields the builder may not carry verbatim.
        for key in (
            "message",
            "site_id",
            "submitter_name",
            "source_capture_id",
            "source_kind",
            "confidence",
            "created_at",
            "validation_error",
            "review_rationale",
        ):
            if key in payload and key not in doc:
                doc[key] = payload[key]
            elif key in payload and not doc.get(key):
                doc[key] = payload[key]
        self._seq += 1
        doc["_rev"] = f"{self._seq}-{uuid.uuid4().hex[:8]}"
        self.docs[str(doc["_id"])] = doc
        return str(doc["draft_id"])

    def seed_raw(self, doc: dict) -> str:
        """Store an arbitrary raw doc (e.g. an action_candidate) verbatim, so a
        test can prove the draft surfaces ignore non-draft types."""
        stored = dict(doc)
        self._seq += 1
        stored.setdefault("_rev", f"{self._seq}-{uuid.uuid4().hex[:8]}")
        doc_id = str(stored.get("_id") or "")
        if not doc_id:
            raise AssertionError("seed_raw requires an _id")
        self.docs[doc_id] = stored
        return doc_id

    # -- convenience accessors ------------------------------------------- #
    def doc(self, draft_id: str) -> dict:
        return self.docs[job_draft_doc_id(draft_id)]

    def review_status_of(self, draft_id: str) -> str:
        return str(self.doc(draft_id).get("review_status") or "")

    def rev_of(self, draft_id: str) -> str:
        return str(self.doc(draft_id).get("_rev") or "")

    def reviewer_of(self, draft_id: str) -> str:
        return str(self.doc(draft_id).get("reviewed_by") or "")


@pytest.fixture
def couchdb_job_draft_review(monkeypatch):
    """Install the faithful in-memory job_draft REVIEW double for a test.

    Patches the transport bindings the draft path resolves (on
    ``couchdb_job_draft_writer``), the config resolution
    (``job_draft_review.couchdb_candidate_config_or_none`` and
    ``couchdb_config.from_env``), and the field-captures DB name. Returns the
    ``FakeCouchJobDraftReview`` instance.
    """
    fake = FakeCouchJobDraftReview()

    cfg = couchdb_config.CouchDBConfig("http://fake-job-draft-review", "u", "p", 1.0, 1000)

    # The draft listing reader + the 335 write path resolve these symbols on the
    # couchdb_job_draft_writer module (imported there at module load). Patch them
    # onto the in-memory store so the REAL list/CAS/guard logic runs.
    monkeypatch.setattr(_jdw, "_request_json", fake.request_json)
    monkeypatch.setattr(_jdw, "_get_document", fake.get_document)
    monkeypatch.setattr(_jdw, "_put_document", fake.put_document)

    # couchdb_job_draft_payloads() gates on a non-None config and on
    # field_captures_database(); from_env() is what _handle_review_post resolves.
    monkeypatch.setattr(_review, "couchdb_candidate_config_or_none", lambda: cfg)
    monkeypatch.setattr(couchdb_config, "from_env", lambda *a, **k: cfg)
    monkeypatch.setattr(couchdb_config, "field_captures_database", lambda *a, **k: "btq_field_captures")
    monkeypatch.setattr(_review.couchdb_config, "field_captures_database", lambda *a, **k: "btq_field_captures", raising=False)

    return fake
