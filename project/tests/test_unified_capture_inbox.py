"""Independent gating tests for the VPS ``unified_capture`` approval inbox API.

Retargeted by the verification agent (NOT the implementer) for BTQ prompt 332/337c
(job-first review): the PWA inbox now reads ``job_draft`` documents
(``review_status="pending_approval"``) instead of ``action_candidate`` docs, and
mutates them through the shared ``field_capture.job_draft_review.apply_job_draft_review``
write path (``_rev`` optimistic-concurrency + status-guard).

These tests drive real HTTP requests through ``UnifiedCaptureHandler`` in-process,
exercise the *real* authorization path (real ``TokenStore``, real
``authorize_token`` -> person resolution -> site scoping) and the *real* shared
review function.

Two harnesses are used:

  * In-memory ``job_draft`` double (``FakeJobDraftCouch``) backing the writer
    functions the shared review fn + inbox reader call. The double reproduces
    CouchDB's ``_rev``-conflict and already-decided semantics faithfully so the
    real review fn's blast-shield runs for real. Used for the auth/shape/race
    gates so they stay deterministic and need no DB auth.

  * A REAL throwaway CouchDB (``RealCouchInboxTests`` / ``RealCouchApproveSetTests``),
    driven through the *same* in-process handler but with a real
    ``couchdb_config`` + a real DB seeded via ``upsert_job_draft``. This proves
    the genuine ``list_job_drafts`` ``_find`` selector, ``apply_job_draft_review``
    CAS write, and ``approve-set`` split end-to-end. These tests SKIP LOUDLY when
    no CouchDB is reachable (they must not silently pass).

Reuses the harness from ``tests.test_unified_capture`` (token store builder,
couch auth fakes, fixtures, response parser) so person resolution is genuine.
"""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any, Optional
from unittest import mock
from urllib import error, request

import pytest

from token_store import TokenStore

import field_capture.job_draft_review as job_draft_review_module
from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import AlreadyDecided
from event_pipeline.couchdb_job_draft_writer import (
    get_job_draft,
    job_draft_doc_id,
    upsert_job_draft,
)
from unified_capture import server as uc_server
from unified_capture.server import UnifiedCaptureHandler

from tests.test_unified_capture import (
    EMP_SINGLE,
    SITES_TWO,
    _FakeServer,
    _Response,
    install_couch_fakes,
    make_store,
    stop_all,
)


# --------------------------------------------------------------------------- #
# In-memory "job_draft" double
# --------------------------------------------------------------------------- #


class FakeJobDraftCouch:
    """Faithful in-memory stand-in for the CouchDB job_draft store.

    Backs the writer functions that the shared review fn + inbox reader call:
      * ``get_job_draft(config, db, draft_id)`` -> doc-with-_rev | None
      * ``list_job_drafts(config, db, review_status=..)`` -> [docs-with-_rev]
      * ``set_job_draft_review_status(...)`` -> mutate review_status, bump _rev,
        reproducing CouchDB's optimistic-concurrency + already-decided rules.

    Records every status mutation so tests can assert no second status codepath
    and exactly-one mutation per draft.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.status_writes: list[dict[str, Any]] = []
        self.route_writes: list[dict[str, Any]] = []
        self._rev_counter = 0

    # ---- seeding ---------------------------------------------------------- #
    def seed(self, doc: dict[str, Any]) -> None:
        self._rev_counter += 1
        stored = copy.deepcopy(doc)
        stored["_rev"] = stored.get("_rev") or f"1-{self._rev_counter:08d}"
        stored.setdefault("_id", f"job_draft_{stored['draft_id']}")
        self.docs[str(stored["draft_id"])] = stored

    # ---- writer-layer doubles -------------------------------------------- #
    def get_job_draft(self, config: Any, db: str, draft_id: str) -> dict[str, Any] | None:
        doc = self.docs.get(str(draft_id))
        return copy.deepcopy(doc) if doc is not None else None

    def list_job_drafts(
        self,
        config: Any,
        db: str,
        *,
        review_status: str | None = None,
        group_id: str | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for doc in self.docs.values():
            if str(doc.get("type") or "") != "job_draft":
                continue
            if review_status is not None and str(doc.get("review_status") or "") != str(review_status):
                continue
            if group_id is not None and str(doc.get("group_id") or "") != str(group_id):
                continue
            out.append(copy.deepcopy(doc))
        out.sort(key=lambda d: (str(d.get("created_at") or ""), str(d.get("draft_id") or "")))
        return out

    def set_job_draft_review_status(
        self,
        config: Any,
        db: str,
        draft_id: str,
        *,
        review_status: str,
        reviewed_by: str,
        rationale: str,
        expected_rev: str | None = None,
        expected_prior_statuses: Any = ("pending_approval",),
        reason: str | None = None,
    ) -> dict[str, Any]:
        doc = self.docs.get(str(draft_id))
        if doc is None:
            raise RuntimeError(f"job_draft not found: {draft_id}")
        current_rev = str(doc.get("_rev") or "")
        # Reproduce the real writer's two AlreadyDecided gates.
        if expected_rev is not None and str(expected_rev) != current_rev:
            raise AlreadyDecided(f"job_draft _rev changed: {draft_id}")
        allowed = {str(item) for item in expected_prior_statuses}
        if allowed and str(doc.get("review_status") or "") not in allowed:
            raise AlreadyDecided(f"job_draft already decided: {draft_id}")
        self._rev_counter += 1
        doc["review_status"] = review_status
        doc["reviewed_by"] = reviewed_by
        doc["reviewer"] = reviewed_by
        doc["review_rationale"] = rationale
        doc["_rev"] = f"2-{self._rev_counter:08d}"
        self.status_writes.append(
            {"draft_id": str(draft_id), "review_status": review_status, "expected_rev": expected_rev}
        )
        return copy.deepcopy(doc)

    def set_job_draft_approval_route_and_review_status(
        self,
        config: Any,
        db: str,
        draft_id: str,
        *,
        route: str,
        job_type: str,
        payload: dict[str, Any],
        reviewed_by: str,
        rationale: str,
        route_rationale: str,
        expected_rev: str | None = None,
        expected_prior_statuses: Any = ("pending_approval",),
    ) -> dict[str, Any]:
        doc = self.docs.get(str(draft_id))
        if doc is None:
            raise RuntimeError(f"job_draft not found: {draft_id}")
        current_rev = str(doc.get("_rev") or "")
        if expected_rev is not None and str(expected_rev) != current_rev:
            raise AlreadyDecided(f"job_draft _rev changed: {draft_id}")
        allowed = {str(item) for item in expected_prior_statuses}
        if allowed and str(doc.get("review_status") or "") not in allowed:
            raise AlreadyDecided(f"job_draft already decided: {draft_id}")
        self._rev_counter += 1
        original_job_type = str(doc.get("job_type") or "")
        original_payload = doc.get("payload") if isinstance(doc.get("payload"), dict) else {}
        routed_job_type = str(job_type or "")
        routed_payload = copy.deepcopy(payload)
        if routed_job_type:
            doc["job_type"] = routed_job_type
            doc["payload"] = routed_payload
        doc.update(
            {
                "original_job_type": original_job_type,
                "original_payload": copy.deepcopy(original_payload),
                "routed_job_type": routed_job_type,
                "routed_payload": routed_payload,
                "route": route,
                "route_chosen_by": reviewed_by,
                "route_chosen_at": "2026-07-02T00:00:00+00:00",
                "route_rationale": route_rationale,
                "review_status": "approved",
                "reviewed_by": reviewed_by,
                "reviewer": reviewed_by,
                "review_rationale": rationale,
                "_rev": f"2-{self._rev_counter:08d}",
            }
        )
        self.route_writes.append({"draft_id": str(draft_id), "route": route, "expected_rev": expected_rev})
        return copy.deepcopy(doc)


def install_job_draft_double(double: FakeJobDraftCouch) -> list[Any]:
    """Patch the writer functions where they are looked up at call time.

    ``apply_job_draft_review`` (the real shared fn) resolves ``get_job_draft`` /
    ``set_job_draft_review_status`` from the ``field_capture.job_draft_review``
    module namespace; the server's inbox reader resolves ``list_job_drafts``
    from ``unified_capture.server``.
    """
    patchers = [
        mock.patch.object(job_draft_review_module, "get_job_draft", double.get_job_draft),
        mock.patch.object(job_draft_review_module, "set_job_draft_review_status", double.set_job_draft_review_status),
        mock.patch.object(
            job_draft_review_module,
            "set_job_draft_approval_route_and_review_status",
            double.set_job_draft_approval_route_and_review_status,
        ),
        mock.patch.object(uc_server, "list_job_drafts", double.list_job_drafts),
    ]
    started = []
    for p in patchers:
        p.start()
        started.append(p)
    return started


# --------------------------------------------------------------------------- #
# Inbox server fake + request driver
# --------------------------------------------------------------------------- #

_COUCH_SENTINEL = object()


class _InboxFakeServer(_FakeServer):
    """Fake server carrying the couchdb + registry attrs the inbox handler reads."""

    def __init__(
        self,
        token_store: TokenStore,
        vault_root: Path,
        *,
        couchdb_config: Any = _COUCH_SENTINEL,
        couchdb_database: str = "btq_field_captures",
        site_registry: Any = None,
    ) -> None:
        super().__init__(token_store, vault_root)
        self.couchdb_config = couchdb_config
        self.couchdb_database = couchdb_database
        self.site_registry = site_registry


def drive_inbox_request(
    server: _InboxFakeServer,
    method: str,
    path: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: Optional[bytes] = None,
) -> _Response:
    """Drive one request through the real handler without opening a socket."""
    hdrs = dict(headers or {})
    payload = body or b""
    if method == "POST":
        hdrs.setdefault("Content-Type", "application/json")
        hdrs["Content-Length"] = str(len(payload))
    request_line = f"{method} {path} HTTP/1.1\r\n"
    header_lines = "".join(f"{name}: {value}\r\n" for name, value in hdrs.items())
    raw = (request_line + header_lines + "\r\n").encode("utf-8") + payload

    handler = UnifiedCaptureHandler.__new__(UnifiedCaptureHandler)
    handler.server = server  # type: ignore[assignment]
    handler.client_address = ("127.0.0.1", 0)
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.handle_one_request()

    handler.wfile.seek(0)
    raw_out = handler.wfile.getvalue()
    status_line, _, rest = raw_out.partition(b"\r\n")
    head, _, resp_body = rest.partition(b"\r\n\r\n")
    status = int(status_line.split(b" ")[1])
    parsed_headers: dict[str, str] = {}
    for line in head.split(b"\r\n"):
        if b":" in line:
            name, _, value = line.partition(b":")
            parsed_headers[name.decode().strip()] = value.decode().strip()
    return _Response(status, parsed_headers, resp_body)


# --------------------------------------------------------------------------- #
# Seed-doc builders
# --------------------------------------------------------------------------- #

VALID_JOB = {
    "job_type": "log_supply_need",
    "payload": {"site_id": "7060", "item_name": "trash bags", "requested_by": "Casey Worker"},
}


def pending_draft(
    draft_id: str = "draft_001",
    *,
    review_status: str = "pending_approval",
    site_id: str = "7060",
    job_type: Any = "valid",
    payload: Any = "valid",
    created_at: str = "2026-06-01T12:00:00+00:00",
    group_id: str | None = None,
    person_id: str = "per_unified01",
    submitter_name: str = "Casey Worker",
) -> dict[str, Any]:
    """Build a CouchDB job_draft doc that the inbox should surface."""
    if job_type == "valid":
        job_type_value = VALID_JOB["job_type"]
    else:
        job_type_value = job_type
    if payload == "valid":
        payload_value: Any = copy.deepcopy(VALID_JOB["payload"])
    else:
        payload_value = payload
    doc: dict[str, Any] = {
        "_id": f"job_draft_{draft_id}",
        "type": "job_draft",
        "draft_id": draft_id,
        "source_capture_id": f"cap_{draft_id}",
        "review_status": review_status,
        "source_kind": "voice",
        "message": f"Supply need at site for {draft_id}",
        "evidence": {"source_text": f"We are out of trash bags ({draft_id})."},
        "site_id": site_id,
        "job_type": job_type_value,
        "payload": payload_value if isinstance(payload_value, dict) else payload_value,
        "group_id": group_id if group_id is not None else draft_id,
        "submitter_name": submitter_name,
        "person_id": person_id,
        "created_at": created_at,
        "source": "field_capture_pipeline",
    }
    return doc


# --------------------------------------------------------------------------- #
# 1. Auth tripwire (fail-closed) -- invariant #6, all three operator routes
# --------------------------------------------------------------------------- #


class InboxAuthTripwireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.double = FakeJobDraftCouch()
        self.double.seed(pending_draft())
        self._dbl = install_job_draft_double(self.double)
        self.addCleanup(lambda: stop_all(self._dbl))

    def _server(self, store: TokenStore) -> _InboxFakeServer:
        return _InboxFakeServer(store, self.vault, site_registry=None)

    def _admin_store(self):
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        # Re-issue an admin_viewer token for the same person in the same store.
        created = store.create_token(
            person_id="per_unified01", label="admin", role="cleaner", token_type="admin_viewer", site_ids=["7060"]
        )
        return store, created.token_value

    def _worker_store(self, token_type: str):
        store = TokenStore(Path(tempfile.mkdtemp(dir=self.tmp.name)) / "tok.sqlite3")
        store.initialize()
        created = store.create_token(
            person_id="per_unified01", label=token_type, role="cleaner", token_type=token_type, site_ids=["7060"]
        )
        return store, created.token_value

    def test_admin_viewer_can_list_approve_reject(self) -> None:
        store, token = self._admin_store()
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server(store)
        try:
            r_list = drive_inbox_request(srv, "GET", "/api/inbox", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(r_list.status, 200, r_list.text)
            rev = r_list.json()["items"][0]["_rev"]
            r_app = drive_inbox_request(
                srv, "POST", "/api/inbox/approve",
                headers={"Authorization": f"Bearer {token}"},
                body=b'{"draft_id":"draft_001","_rev":"%s"}' % rev.encode(),
            )
            self.assertEqual(r_app.status, 200, r_app.text)
            self.assertEqual(r_app.json().get("status"), "approved")
        finally:
            stop_all(started)

    def test_admin_viewer_can_reject(self) -> None:
        store, token = self._admin_store()
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server(store)
        try:
            r_list = drive_inbox_request(srv, "GET", "/api/inbox", headers={"Authorization": f"Bearer {token}"})
            rev = r_list.json()["items"][0]["_rev"]
            r_rej = drive_inbox_request(
                srv, "POST", "/api/inbox/reject",
                headers={"Authorization": f"Bearer {token}"},
                body=b'{"draft_id":"draft_001","_rev":"%s","reason":"dup"}' % rev.encode(),
            )
            self.assertEqual(r_rej.status, 200, r_rej.text)
            self.assertEqual(r_rej.json().get("status"), "rejected")
        finally:
            stop_all(started)

    def test_admin_viewer_can_approve_set(self) -> None:
        # New operator route must be allowed for admin_viewer.
        store, token = self._admin_store()
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server(store)
        try:
            r_list = drive_inbox_request(srv, "GET", "/api/inbox", headers={"Authorization": f"Bearer {token}"})
            rev = r_list.json()["items"][0]["_rev"]
            body = json.dumps({"drafts": [{"draft_id": "draft_001", "_rev": rev, "checked": True}]}).encode()
            r_set = drive_inbox_request(
                srv, "POST", "/api/inbox/approve-set",
                headers={"Authorization": f"Bearer {token}"},
                body=body,
            )
            self.assertEqual(r_set.status, 200, r_set.text)
            self.assertEqual(r_set.json().get("approved"), 1)
        finally:
            stop_all(started)

    def test_non_admin_tokens_get_403_on_all_three_routes_and_never_reach_review_fn(self) -> None:
        # Tripwire on the shared review fn: a non-admin must NEVER reach it,
        # on inbox, approve, reject, OR approve-set (invariant #6).
        tripwire = mock.patch.object(
            uc_server, "apply_job_draft_review",
            side_effect=AssertionError("apply_job_draft_review reached by non-admin token"),
        )
        for token_type in ("capture", "viewer", "client_viewer", "import"):
            with self.subTest(token_type=token_type):
                store, token = self._worker_store(token_type)
                started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
                srv = self._server(store)
                tripwire.start()
                try:
                    r_list = drive_inbox_request(srv, "GET", "/api/inbox", headers={"Authorization": f"Bearer {token}"})
                    self.assertEqual(r_list.status, 403, r_list.text)
                    self.assertEqual(r_list.json().get("error"), "not_authorized")
                    r_app = drive_inbox_request(
                        srv, "POST", "/api/inbox/approve",
                        headers={"Authorization": f"Bearer {token}"},
                        body=b'{"draft_id":"draft_001","_rev":"x"}',
                    )
                    self.assertEqual(r_app.status, 403, r_app.text)
                    self.assertEqual(r_app.json().get("error"), "not_authorized")
                    r_rej = drive_inbox_request(
                        srv, "POST", "/api/inbox/reject",
                        headers={"Authorization": f"Bearer {token}"},
                        body=b'{"draft_id":"draft_001","_rev":"x"}',
                    )
                    self.assertEqual(r_rej.status, 403, r_rej.text)
                    self.assertEqual(r_rej.json().get("error"), "not_authorized")
                    r_set = drive_inbox_request(
                        srv, "POST", "/api/inbox/approve-set",
                        headers={"Authorization": f"Bearer {token}"},
                        body=b'{"drafts":[{"draft_id":"draft_001","_rev":"x","checked":true}]}',
                    )
                    self.assertEqual(r_set.status, 403, r_set.text)
                    self.assertEqual(r_set.json().get("error"), "not_authorized")
                finally:
                    tripwire.stop()
                    stop_all(started)
        # Nothing was mutated by any non-admin path.
        self.assertEqual(self.double.status_writes, [])

    def test_missing_token_unauthorized(self) -> None:
        store, _ = self._admin_store()
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server(store)
        try:
            r = drive_inbox_request(srv, "GET", "/api/inbox")
            self.assertIn(r.status, (401, 403), r.text)
        finally:
            stop_all(started)

    def test_invalid_token_unauthorized(self) -> None:
        store, _ = self._admin_store()
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server(store)
        try:
            r = drive_inbox_request(srv, "GET", "/api/inbox", headers={"Authorization": "Bearer fc_not_a_real_token"})
            self.assertIn(r.status, (401, 403), r.text)
        finally:
            stop_all(started)


# --------------------------------------------------------------------------- #
# Shared mixin for admin-token + couch-double setup
# --------------------------------------------------------------------------- #


class _AdminInboxMixin:
    def setUp(self) -> None:  # type: ignore[override]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.store = TokenStore(Path(self.tmp.name) / "tok.sqlite3")
        self.store.initialize()
        created = self.store.create_token(
            person_id="per_unified01", label="admin", role="cleaner", token_type="admin_viewer", site_ids=["7060"]
        )
        self.token = created.token_value
        self.double = FakeJobDraftCouch()
        self._dbl = install_job_draft_double(self.double)
        self.addCleanup(lambda: stop_all(self._dbl))

    def _server(self, *, site_registry: Any = None) -> _InboxFakeServer:
        return _InboxFakeServer(self.store, self.vault, site_registry=site_registry)

    def _list(self, srv: _InboxFakeServer) -> _Response:
        return drive_inbox_request(srv, "GET", "/api/inbox", headers={"Authorization": f"Bearer {self.token}"})

    def _approve(self, srv: _InboxFakeServer, draft_id: str, rev: str) -> _Response:
        body = ('{"draft_id":"%s","_rev":"%s"}' % (draft_id, rev)).encode()
        return drive_inbox_request(srv, "POST", "/api/inbox/approve", headers={"Authorization": f"Bearer {self.token}"}, body=body)

    def _reject(self, srv: _InboxFakeServer, draft_id: str, rev: str, reason: str = "dup") -> _Response:
        body = ('{"draft_id":"%s","_rev":"%s","reason":"%s"}' % (draft_id, rev, reason)).encode()
        return drive_inbox_request(srv, "POST", "/api/inbox/reject", headers={"Authorization": f"Bearer {self.token}"}, body=body)

    def _approve_set(self, srv: _InboxFakeServer, drafts: list[dict[str, Any]]) -> _Response:
        body = json.dumps({"drafts": drafts}).encode()
        return drive_inbox_request(srv, "POST", "/api/inbox/approve-set", headers={"Authorization": f"Bearer {self.token}"}, body=body)


# --------------------------------------------------------------------------- #
# 2. Inbox list = CouchDB job_drafts + correct shape (field-for-field)
# --------------------------------------------------------------------------- #


class InboxListShapeTests(_AdminInboxMixin, unittest.TestCase):
    def test_inbox_returns_pending_drafts_with_full_contract_shape(self) -> None:
        self.double.seed(pending_draft("draft_001", created_at="2026-06-01T10:00:00+00:00"))
        self.double.seed(pending_draft("draft_002", created_at="2026-06-01T11:00:00+00:00"))
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._list(srv)
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        body = r.json()
        self.assertEqual(set(body.keys()), {"count", "items"})
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["items"]), 2)

        item = body["items"][0]
        # Field-for-field contract (inbox.js renders against this draft shape).
        self.assertEqual(
            set(item.keys()),
            {
                "draft_id", "_rev", "source_capture_id", "source", "message",
                "evidence", "site", "site_id", "group_id", "submitter_name",
                "created_at", "job_type", "payload",
            },
        )
        self.assertEqual(item["draft_id"], "draft_001")
        self.assertEqual(item["source_capture_id"], "cap_draft_001")
        self.assertEqual(item["source"], "voice")
        self.assertEqual(item["message"], "Supply need at site for draft_001")
        self.assertEqual(item["evidence"], "We are out of trash bags (draft_001).")
        self.assertEqual(item["created_at"], "2026-06-01T10:00:00+00:00")
        self.assertEqual(item["group_id"], "draft_001")
        self.assertEqual(item["job_type"], "log_supply_need")
        self.assertEqual(item["payload"], VALID_JOB["payload"])

        # _rev MUST be the live doc rev (so concurrent mutate is gated).
        live_rev = self.double.docs["draft_001"]["_rev"]
        self.assertEqual(item["_rev"], live_rev)
        self.assertTrue(item["_rev"])

    def test_draft_without_valid_job_is_excluded(self) -> None:
        self.double.seed(pending_draft("draft_ok"))
        # No payload at all (not a dict).
        self.double.seed(pending_draft("draft_nojob", payload=None))
        # Payload that fails validate_job (missing required fields).
        self.double.seed(pending_draft("draft_bad", payload={"site_id": "7060"}))
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._list(srv)
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        ids = {it["draft_id"] for it in r.json()["items"]}
        self.assertEqual(ids, {"draft_ok"})

    def test_only_pending_approval_drafts_appear(self) -> None:
        # MUTATION-CHECK ANCHOR: this asserts review_status filtering.
        self.double.seed(pending_draft("draft_pending", review_status="pending_approval"))
        self.double.seed(pending_draft("draft_approved", review_status="approved"))
        self.double.seed(pending_draft("draft_rejected", review_status="rejected"))
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._list(srv)
        finally:
            stop_all(started)
        ids = {it["draft_id"] for it in r.json()["items"]}
        self.assertEqual(ids, {"draft_pending"})

    def test_filesystem_only_draft_does_not_appear(self) -> None:
        # CouchDB-canonical read: a draft that exists only on the filesystem
        # (i.e. never seeded into the couch double) must NOT surface.
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._list(srv)
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json(), {"count": 0, "items": []})


# --------------------------------------------------------------------------- #
# 3. One blast shield + no VPS staging
# --------------------------------------------------------------------------- #


class InboxBlastShieldTests(_AdminInboxMixin, unittest.TestCase):
    def test_approve_drives_shared_fn_with_rev_and_stages_nothing(self) -> None:
        self.double.seed(pending_draft("draft_001"))
        live_rev = self.double.docs["draft_001"]["_rev"]
        spy = mock.patch.object(
            uc_server, "apply_job_draft_review", wraps=uc_server.apply_job_draft_review
        )
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        spy_mock = spy.start()
        try:
            r = self._approve(srv, "draft_001", live_rev)
        finally:
            spy.stop()
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "draft_id": "draft_001", "status": "approved"})
        # Shared fn called once, with expected_rev = the passed _rev.
        self.assertEqual(spy_mock.call_count, 1)
        _, kwargs = spy_mock.call_args
        self.assertEqual(kwargs.get("expected_rev"), live_rev)
        self.assertEqual(kwargs.get("action"), "approve")
        # CouchDB review_status set to approved; exactly one status write.
        self.assertEqual(self.double.docs["draft_001"]["review_status"], "approved")
        self.assertEqual([w["review_status"] for w in self.double.status_writes], ["approved"])

    def test_no_route_approve_preserves_legacy_job_payload_shape(self) -> None:
        original_payload = {"site_id": "7060", "item_name": "trash bags", "requested_by": "Casey Worker"}
        self.double.seed(pending_draft("draft_001", payload=original_payload))
        live_rev = self.double.docs["draft_001"]["_rev"]
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._approve(srv, "draft_001", live_rev)
        finally:
            stop_all(started)

        self.assertEqual(r.status, 200, r.text)
        doc = self.double.docs["draft_001"]
        self.assertEqual(doc["review_status"], "approved")
        self.assertEqual(doc["job_type"], "log_supply_need")
        self.assertEqual(doc["payload"], original_payload)
        for field in ("route", "original_job_type", "original_payload", "routed_job_type", "routed_payload"):
            self.assertNotIn(field, doc)
        self.assertEqual(self.double.route_writes, [])

    def test_route_open_issue_on_access_origin_overrides_to_site_issue(self) -> None:
        doc = pending_draft(
            "draft_access_issue",
            job_type="flag_access_constraint",
            payload={"site": "7060", "details": "No access, crew locked out by the gate."},
            submitter_name="Casey Worker",
        )
        doc["message"] = "No access, crew locked out by the gate."
        self.double.seed(doc)
        live_rev = self.double.docs["draft_access_issue"]["_rev"]
        body = json.dumps(
            {
                "draft_id": "draft_access_issue",
                "_rev": live_rev,
                "route": "open_issue",
                "reason": "operator selected Open issue",
            }
        ).encode("utf-8")
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = drive_inbox_request(
                srv,
                "POST",
                "/api/inbox/approve",
                headers={"Authorization": f"Bearer {self.token}"},
                body=body,
            )
        finally:
            stop_all(started)

        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json()["route"], "open_issue")
        routed = self.double.docs["draft_access_issue"]
        self.assertEqual(routed["job_type"], "log_site_issue")
        self.assertEqual(routed["original_job_type"], "flag_access_constraint")
        self.assertEqual(routed["original_payload"], {"site": "7060", "details": "No access, crew locked out by the gate."})
        self.assertEqual(routed["route_chosen_by"], "per_unified01")
        self.assertEqual(routed["route_rationale"], "operator selected Open issue")
        payload = routed["payload"]
        self.assertEqual(payload["category"], "access")
        self.assertEqual(payload["reported_by"], "Casey Worker")
        self.assertEqual(payload["client_notified"], False)
        self.assertEqual(payload["priority"], "high")
        self.assertEqual(payload["related_capture_ids"], ["cap_draft_access_issue"])
        self.assertEqual(payload["related_candidate_ids"], ["draft_access_issue"])

    def test_route_access_constraint_preserves_access_bucket(self) -> None:
        doc = pending_draft(
            "draft_access_constraint",
            job_type="log_site_issue",
            payload={"site_id": "7060", "title": "Access", "reported_by": "Casey Worker", "client_notified": False, "resolution_trigger": "done", "summary": "Door code is 1234."},
        )
        doc["message"] = "Door code is 1234 and lockbox is behind the office."
        self.double.seed(doc)
        live_rev = self.double.docs["draft_access_constraint"]["_rev"]
        body = json.dumps({"draft_id": "draft_access_constraint", "_rev": live_rev, "route": "access_constraint"}).encode("utf-8")
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = drive_inbox_request(
                srv,
                "POST",
                "/api/inbox/approve",
                headers={"Authorization": f"Bearer {self.token}"},
                body=body,
            )
        finally:
            stop_all(started)

        self.assertEqual(r.status, 200, r.text)
        routed = self.double.docs["draft_access_constraint"]
        self.assertEqual(routed["job_type"], "flag_access_constraint")
        self.assertEqual(routed["payload"], {"site": "7060", "details": "Door code is 1234 and lockbox is behind the office."})

    def test_route_no_action_approves_without_overwriting_job_payload(self) -> None:
        original_payload = {"site_id": "7060", "item_name": "trash bags", "requested_by": "Casey Worker"}
        self.double.seed(pending_draft("draft_no_action", payload=original_payload))
        live_rev = self.double.docs["draft_no_action"]["_rev"]
        body = json.dumps({"draft_id": "draft_no_action", "_rev": live_rev, "route": "no_action"}).encode("utf-8")
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = drive_inbox_request(
                srv,
                "POST",
                "/api/inbox/approve",
                headers={"Authorization": f"Bearer {self.token}"},
                body=body,
            )
        finally:
            stop_all(started)

        self.assertEqual(r.status, 200, r.text)
        doc = self.double.docs["draft_no_action"]
        self.assertEqual(doc["review_status"], "approved")
        self.assertEqual(doc["route"], "no_action")
        self.assertEqual(doc["job_type"], "log_supply_need")
        self.assertEqual(doc["payload"], original_payload)
        self.assertEqual(doc["routed_job_type"], "")
        self.assertEqual(doc["routed_payload"], {})

    def test_invalid_route_returns_400_and_leaves_pending(self) -> None:
        self.double.seed(pending_draft("draft_invalid_route"))
        live_rev = self.double.docs["draft_invalid_route"]["_rev"]
        body = json.dumps({"draft_id": "draft_invalid_route", "_rev": live_rev, "route": "mystery"}).encode("utf-8")
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = drive_inbox_request(
                srv,
                "POST",
                "/api/inbox/approve",
                headers={"Authorization": f"Bearer {self.token}"},
                body=body,
            )
        finally:
            stop_all(started)

        self.assertEqual(r.status, 400, r.text)
        self.assertEqual(r.json(), {"error": "invalid_route"})
        self.assertEqual(self.double.docs["draft_invalid_route"]["review_status"], "pending_approval")
        self.assertEqual(self.double.status_writes, [])
        self.assertEqual(self.double.route_writes, [])

    def test_reject_sets_rejected(self) -> None:
        self.double.seed(pending_draft("draft_001"))
        live_rev = self.double.docs["draft_001"]["_rev"]
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._reject(srv, "draft_001", live_rev)
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "draft_id": "draft_001", "status": "rejected"})
        self.assertEqual(self.double.docs["draft_001"]["review_status"], "rejected")
        self.assertEqual([w["review_status"] for w in self.double.status_writes], ["rejected"])

    def test_server_has_no_parallel_approval_path_or_candidate_reader(self) -> None:
        import inspect
        src = inspect.getsource(uc_server)
        # No second status machine / parallel approval semantics in the server.
        self.assertNotIn("set_job_draft_review_status", src,
                         "server must not call the status writer directly; go through apply_job_draft_review")
        self.assertNotIn("ops_dashboard", src, "server must not import ops_dashboard approval code")
        # The PWA inbox must NOT read action_candidate docs any more (337c).
        self.assertNotIn("list_action_candidates", src,
                         "PWA inbox must read job_drafts, never action_candidates")
        self.assertNotIn("apply_candidate_review", src,
                         "PWA inbox must mutate via apply_job_draft_review, not the candidate path")
        # Approval/reject/approve-set route through the one shared draft fn.
        self.assertIn("apply_job_draft_review", src)


# --------------------------------------------------------------------------- #
# 4. _rev race (the critical UX): stale _rev -> 200 already_decided
# --------------------------------------------------------------------------- #


class InboxRevRaceTests(_AdminInboxMixin, unittest.TestCase):
    def test_stale_rev_approve_returns_200_already_decided_no_remutation(self) -> None:
        self.double.seed(pending_draft("draft_001"))
        stale_rev = self.double.docs["draft_001"]["_rev"]
        # Simulate the phone-vs-/swipe race: another reviewer already approved,
        # bumping the rev. The PWA still holds the stale rev.
        self.double.set_job_draft_review_status(
            None, "db", "draft_001", review_status="approved", reviewed_by="other", rationale="r", expected_rev=stale_rev
        )
        new_rev = self.double.docs["draft_001"]["_rev"]
        self.assertNotEqual(new_rev, stale_rev)
        writes_before = len(self.double.status_writes)

        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._approve(srv, "draft_001", stale_rev)
        finally:
            stop_all(started)
        # Critical: 200 (reads as 'already handled' in the PWA), NOT a 4xx/5xx.
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json(), {"error": "already_decided"})
        # Not re-mutated: no new status write, rev unchanged.
        self.assertEqual(len(self.double.status_writes), writes_before)
        self.assertEqual(self.double.docs["draft_001"]["_rev"], new_rev)

    def test_stale_rev_reject_returns_200_already_decided_no_remutation(self) -> None:
        self.double.seed(pending_draft("draft_001"))
        stale_rev = self.double.docs["draft_001"]["_rev"]
        self.double.set_job_draft_review_status(
            None, "db", "draft_001", review_status="rejected", reviewed_by="other", rationale="r", expected_rev=stale_rev
        )
        new_rev = self.double.docs["draft_001"]["_rev"]
        writes_before = len(self.double.status_writes)

        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._reject(srv, "draft_001", stale_rev)
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json(), {"error": "already_decided"})
        self.assertEqual(len(self.double.status_writes), writes_before)
        self.assertEqual(self.double.docs["draft_001"]["_rev"], new_rev)


# --------------------------------------------------------------------------- #
# 5. inbox_count on /api/session
# --------------------------------------------------------------------------- #


class InboxCountTests(_AdminInboxMixin, unittest.TestCase):
    def test_session_inbox_count_matches_pending_approvable_count_for_admin(self) -> None:
        self.double.seed(pending_draft("draft_001"))
        self.double.seed(pending_draft("draft_002"))
        self.double.seed(pending_draft("draft_nojob", payload=None))  # not approvable
        self.double.seed(pending_draft("draft_done", review_status="approved"))  # not pending
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r_sess = drive_inbox_request(srv, "GET", "/api/session", headers={"Authorization": f"Bearer {self.token}"})
            r_inbox = self._list(srv)
        finally:
            stop_all(started)
        self.assertEqual(r_sess.status, 200, r_sess.text)
        self.assertIn("inbox_count", r_sess.json())
        self.assertEqual(r_sess.json()["inbox_count"], 2)
        # Matches /api/inbox count.
        self.assertEqual(r_sess.json()["inbox_count"], r_inbox.json()["count"])

    def test_session_inbox_count_zero_for_non_admin(self) -> None:
        self.double.seed(pending_draft("draft_001"))
        # Issue a capture token for the same person in a fresh store.
        worker_store = TokenStore(Path(tempfile.mkdtemp(dir=self.tmp.name)) / "w.sqlite3")
        worker_store.initialize()
        created = worker_store.create_token(
            person_id="per_unified01", label="w", role="cleaner", token_type="capture", site_ids=["7060"]
        )
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = _InboxFakeServer(worker_store, self.vault, site_registry=None)
        try:
            r = drive_inbox_request(srv, "GET", "/api/session", headers={"Authorization": f"Bearer {created.token_value}"})
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json().get("inbox_count"), 0)

    # ----- can_review gating on /api/session ------------------------------- #

    def test_session_can_review_true_for_admin_viewer(self) -> None:
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = drive_inbox_request(srv, "GET", "/api/session", headers={"Authorization": f"Bearer {self.token}"})
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        body = r.json()
        self.assertIn("can_review", body)
        self.assertIs(body["can_review"], True)
        # Raw token_type must NOT be exposed.
        self.assertNotIn("token_type", body)

    def test_session_can_review_false_for_capture_token(self) -> None:
        worker_store = TokenStore(Path(tempfile.mkdtemp(dir=self.tmp.name)) / "w_canreview.sqlite3")
        worker_store.initialize()
        created = worker_store.create_token(
            person_id="per_unified01", label="w", role="cleaner", token_type="capture", site_ids=["7060"]
        )
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = _InboxFakeServer(worker_store, self.vault, site_registry=None)
        try:
            r = drive_inbox_request(srv, "GET", "/api/session", headers={"Authorization": f"Bearer {created.token_value}"})
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        body = r.json()
        self.assertIn("can_review", body)
        self.assertIs(body["can_review"], False)
        self.assertEqual(body.get("inbox_count"), 0)
        self.assertNotIn("token_type", body)


# --------------------------------------------------------------------------- #
# 6. Graceful degrade when site-label lookup fails
# --------------------------------------------------------------------------- #


class InboxSiteLabelDegradeTests(_AdminInboxMixin, unittest.TestCase):
    def test_site_label_lookup_throwing_falls_back_to_raw_site_id_and_returns_200(self) -> None:
        self.double.seed(pending_draft("draft_001", site_id="7060"))

        class ExplodingRegistry:
            def resolve_canonical(self, site_id):  # noqa: ARG002
                raise RuntimeError("registry view unavailable")

        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server(site_registry=ExplodingRegistry())
        try:
            r = self._list(srv)
        finally:
            stop_all(started)
        # Never hard-fails: still 200, item falls back to raw site_id.
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json()["count"], 1)
        self.assertEqual(r.json()["items"][0]["site"], "7060")


# --------------------------------------------------------------------------- #
# 7. approve-set checklist (double harness) -- split + mid-set already_decided
# --------------------------------------------------------------------------- #


class InboxApproveSetTests(_AdminInboxMixin, unittest.TestCase):
    def test_approve_set_applies_per_item_routes(self) -> None:
        self.double.seed(pending_draft("draft_supply", group_id="grp1", payload={"site_id": "7060", "item_name": "liners", "requested_by": "Casey Worker"}))
        self.double.seed(pending_draft("draft_equipment", group_id="grp1", payload={"site_id": "7060", "equipment_name": "vacuum", "requested_by": "Casey Worker"}))
        rev_supply = self.double.docs["draft_supply"]["_rev"]
        rev_equipment = self.double.docs["draft_equipment"]["_rev"]
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._approve_set(srv, [
                {"draft_id": "draft_supply", "_rev": rev_supply, "checked": True, "route": "supply_need"},
                {"draft_id": "draft_equipment", "_rev": rev_equipment, "checked": True, "route": "equipment_request"},
            ])
        finally:
            stop_all(started)

        self.assertEqual(r.status, 200, r.text)
        body = r.json()
        self.assertEqual(body.get("approved"), 2)
        self.assertEqual(self.double.docs["draft_supply"]["job_type"], "log_supply_need")
        self.assertEqual(self.double.docs["draft_supply"]["payload"]["item_name"], "liners")
        self.assertEqual(self.double.docs["draft_supply"]["route"], "supply_need")
        self.assertEqual(self.double.docs["draft_equipment"]["job_type"], "log_equipment_request")
        self.assertEqual(self.double.docs["draft_equipment"]["payload"]["equipment_name"], "vacuum")
        self.assertEqual(self.double.docs["draft_equipment"]["route"], "equipment_request")
        by_id = {res["draft_id"]: res for res in body["results"]}
        self.assertEqual(by_id["draft_supply"]["route"], "supply_need")
        self.assertEqual(by_id["draft_equipment"]["route"], "equipment_request")

    def test_approve_set_invalid_route_returns_400_without_partial_mutation(self) -> None:
        self.double.seed(pending_draft("draft_a", group_id="grp1"))
        self.double.seed(pending_draft("draft_b", group_id="grp1"))
        rev_a = self.double.docs["draft_a"]["_rev"]
        rev_b = self.double.docs["draft_b"]["_rev"]
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._approve_set(srv, [
                {"draft_id": "draft_a", "_rev": rev_a, "checked": True, "route": "supply_need"},
                {"draft_id": "draft_b", "_rev": rev_b, "checked": True, "route": "bad_route"},
            ])
        finally:
            stop_all(started)

        self.assertEqual(r.status, 400, r.text)
        self.assertEqual(r.json(), {"error": "invalid_route"})
        self.assertEqual(self.double.docs["draft_a"]["review_status"], "pending_approval")
        self.assertEqual(self.double.docs["draft_b"]["review_status"], "pending_approval")
        self.assertEqual(self.double.status_writes, [])
        self.assertEqual(self.double.route_writes, [])

    def test_approve_set_splits_checked_and_unchecked(self) -> None:
        # 3 drafts in one group: 2 checked -> approved, 1 unchecked -> rejected.
        self.double.seed(pending_draft("draft_a", group_id="grp1"))
        self.double.seed(pending_draft("draft_b", group_id="grp1"))
        self.double.seed(pending_draft("draft_c", group_id="grp1"))
        rev_a = self.double.docs["draft_a"]["_rev"]
        rev_b = self.double.docs["draft_b"]["_rev"]
        rev_c = self.double.docs["draft_c"]["_rev"]
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._approve_set(srv, [
                {"draft_id": "draft_a", "_rev": rev_a, "checked": True},
                {"draft_id": "draft_b", "_rev": rev_b, "checked": True},
                {"draft_id": "draft_c", "_rev": rev_c, "checked": False},
            ])
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        body = r.json()
        self.assertEqual(body.get("approved"), 2)
        self.assertEqual(body.get("rejected"), 1)
        self.assertEqual(body.get("already_decided"), 0)
        # Persisted state proves the split.
        self.assertEqual(self.double.docs["draft_a"]["review_status"], "approved")
        self.assertEqual(self.double.docs["draft_b"]["review_status"], "approved")
        self.assertEqual(self.double.docs["draft_c"]["review_status"], "rejected")
        # Per-draft summary present.
        by_id = {res["draft_id"]: res for res in body["results"]}
        self.assertEqual(by_id["draft_a"]["status"], "approved")
        self.assertEqual(by_id["draft_c"]["status"], "rejected")

    def test_approve_set_predecided_member_reported_rest_still_decided(self) -> None:
        # One draft is pre-decided before the set call; it must be reported
        # already_decided WITHOUT aborting the rest of the set.
        self.double.seed(pending_draft("draft_a", group_id="grp1"))
        self.double.seed(pending_draft("draft_b", group_id="grp1"))
        self.double.seed(pending_draft("draft_c", group_id="grp1"))
        rev_a = self.double.docs["draft_a"]["_rev"]
        rev_b = self.double.docs["draft_b"]["_rev"]
        rev_c = self.double.docs["draft_c"]["_rev"]
        # Pre-decide draft_b out from under the operator (stale rev held below).
        self.double.set_job_draft_review_status(
            None, "db", "draft_b", review_status="approved", reviewed_by="other", rationale="r", expected_rev=rev_b
        )
        writes_after_predecide = len(self.double.status_writes)
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._approve_set(srv, [
                {"draft_id": "draft_a", "_rev": rev_a, "checked": True},
                {"draft_id": "draft_b", "_rev": rev_b, "checked": True},  # stale rev
                {"draft_id": "draft_c", "_rev": rev_c, "checked": False},
            ])
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        body = r.json()
        # b reported already_decided; a/c still decided (no abort).
        self.assertEqual(body.get("already_decided"), 1)
        self.assertEqual(body.get("approved"), 1)
        self.assertEqual(body.get("rejected"), 1)
        self.assertEqual(self.double.docs["draft_a"]["review_status"], "approved")
        self.assertEqual(self.double.docs["draft_c"]["review_status"], "rejected")
        by_id = {res["draft_id"]: res for res in body["results"]}
        self.assertEqual(by_id["draft_b"].get("error"), "already_decided")
        # draft_b was NOT re-mutated by the set call (still 'other's approval).
        self.assertEqual(len(self.double.status_writes), writes_after_predecide + 2)  # only a + c


# --------------------------------------------------------------------------- #
# 8. REAL throwaway CouchDB: genuine list_job_drafts + apply_job_draft_review
#    round-trip through the in-process handler. Skips LOUDLY if no DB.
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


DESIGN_DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "event_pipeline"
    / "couchdb"
    / "design_btq_field_captures.json"
)


def _seed_real_draft(cfg, db, draft_id, *, job_type="log_supply_need", payload=None,
                     review_status="pending_approval", group_id=None, site_id="7060",
                     source_kind="voice", message="m", created_at="2026-06-01T10:00:00+00:00"):
    if payload is None:
        payload = {"site_id": "7060", "item_name": "trash bags", "requested_by": "Casey Worker"}
    upsert_job_draft(cfg, db, {
        "draft_id": draft_id,
        "job_type": job_type,
        "payload": payload,
        "review_status": review_status,
        "group_id": group_id or draft_id,
        "site_id": site_id,
        "source_kind": source_kind,
        "message": message,
        "submitter_name": "Casey Worker",
        "created_at": created_at,
    })
    return get_job_draft(cfg, db, draft_id)


@pytest.mark.real_couchdb
class RealCouchInboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = _live_config()
        if cls.cfg is None:
            raise unittest.SkipTest(
                "no reachable CouchDB (set BTQ_COUCHDB_URL/USER/PASSWORD or "
                "populate ~/.config/btq/config.json) -- real_couchdb gate skipped LOUDLY"
            )
        cls.db = f"btq_verify_337c_{uuid.uuid4().hex[:12]}"
        _http(cls.cfg, "PUT", cls.db)
        if DESIGN_DOC_PATH.exists():
            design = json.loads(DESIGN_DOC_PATH.read_text(encoding="utf-8"))
            _http(cls.cfg, "PUT", f"{cls.db}/_design/btq_field_captures",
                  {k: v for k, v in design.items() if k != "_rev"})

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "cfg", None) is not None and getattr(cls, "db", None):
            try:
                _http(cls.cfg, "DELETE", cls.db)
            except Exception:
                pass

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.store = TokenStore(Path(self.tmp.name) / "tok.sqlite3")
        self.store.initialize()
        created = self.store.create_token(
            person_id="per_unified01", label="admin", role="cleaner", token_type="admin_viewer", site_ids=["7060"]
        )
        self.token = created.token_value

    def _server(self) -> _InboxFakeServer:
        return _InboxFakeServer(self.store, self.vault, couchdb_config=self.cfg,
                                couchdb_database=self.db, site_registry=None)

    def _list(self, srv):
        return drive_inbox_request(srv, "GET", "/api/inbox", headers={"Authorization": f"Bearer {self.token}"})

    def test_real_round_trip_approve_reject_and_reads_zero_candidates(self) -> None:
        # Seed a pending draft AND a stray action_candidate doc; the PWA must
        # surface ONLY the draft and NEVER the candidate.
        did = f"d_{uuid.uuid4().hex[:8]}"
        _seed_real_draft(self.cfg, self.db, did, group_id=f"grp_{did}")
        # Stray action_candidate that the OLD code would have surfaced.
        cand_id = f"action_candidate_{uuid.uuid4().hex[:8]}"
        _http(self.cfg, "PUT", f"{self.db}/{cand_id}", {
            "_id": cand_id, "type": "action_candidate", "candidate_id": cand_id,
            "status": "pending_review", "site_id": "7060",
            "proposed_queue_job": {"job_type": "log_supply_need",
                                   "payload": {"site_id": "7060", "item_name": "x", "requested_by": "y"}},
            "summary": "stray candidate", "created_at": "2026-06-01T09:00:00+00:00",
        })

        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._list(srv)
            self.assertEqual(r.status, 200, r.text)
            items = r.json()["items"]
            ids = {it["draft_id"] for it in items}
            # The draft is present; the action_candidate is invisible.
            self.assertIn(did, ids)
            self.assertNotIn(cand_id, ids)
            for it in items:
                self.assertNotIn("candidate_id", it)
                # Draft contract shape, not candidate shape.
                self.assertIn("job_type", it)
                self.assertIn("group_id", it)
            mine = next(it for it in items if it["draft_id"] == did)
            self.assertEqual(mine["job_type"], "log_supply_need")
            self.assertEqual(mine["message"], "m")
            rev = mine["_rev"]

            # Approve -> draft flips to approved in real CouchDB.
            r_app = drive_inbox_request(
                srv, "POST", "/api/inbox/approve",
                headers={"Authorization": f"Bearer {self.token}"},
                body=json.dumps({"draft_id": did, "_rev": rev}).encode(),
            )
            self.assertEqual(r_app.status, 200, r_app.text)
            self.assertEqual(r_app.json().get("status"), "approved")
            persisted = get_job_draft(self.cfg, self.db, did)
            self.assertEqual(persisted["review_status"], "approved")

            # Stale-rev re-approve -> 200 already_decided, no overwrite.
            r_stale = drive_inbox_request(
                srv, "POST", "/api/inbox/approve",
                headers={"Authorization": f"Bearer {self.token}"},
                body=json.dumps({"draft_id": did, "_rev": rev}).encode(),
            )
            self.assertEqual(r_stale.status, 200, r_stale.text)
            self.assertEqual(r_stale.json(), {"error": "already_decided"})
            self.assertEqual(get_job_draft(self.cfg, self.db, did)["review_status"], "approved")
        finally:
            stop_all(started)

    def test_real_reject_round_trip(self) -> None:
        did = f"d_{uuid.uuid4().hex[:8]}"
        _seed_real_draft(self.cfg, self.db, did, group_id=f"grp_{did}")
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._list(srv)
            mine = next(it for it in r.json()["items"] if it["draft_id"] == did)
            r_rej = drive_inbox_request(
                srv, "POST", "/api/inbox/reject",
                headers={"Authorization": f"Bearer {self.token}"},
                body=json.dumps({"draft_id": did, "_rev": mine["_rev"], "reason": "dup"}).encode(),
            )
            self.assertEqual(r_rej.status, 200, r_rej.text)
            self.assertEqual(r_rej.json().get("status"), "rejected")
            self.assertEqual(get_job_draft(self.cfg, self.db, did)["review_status"], "rejected")
        finally:
            stop_all(started)

    def test_real_approve_set_split(self) -> None:
        grp = f"grp_{uuid.uuid4().hex[:8]}"
        ids = [f"d_{uuid.uuid4().hex[:8]}" for _ in range(3)]
        for did in ids:
            _seed_real_draft(self.cfg, self.db, did, group_id=grp)
        revs = {did: get_job_draft(self.cfg, self.db, did)["_rev"] for did in ids}
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            body = json.dumps({"drafts": [
                {"draft_id": ids[0], "_rev": revs[ids[0]], "checked": True},
                {"draft_id": ids[1], "_rev": revs[ids[1]], "checked": True},
                {"draft_id": ids[2], "_rev": revs[ids[2]], "checked": False},
            ]}).encode()
            r = drive_inbox_request(
                srv, "POST", "/api/inbox/approve-set",
                headers={"Authorization": f"Bearer {self.token}"}, body=body,
            )
            self.assertEqual(r.status, 200, r.text)
            j = r.json()
            self.assertEqual(j.get("approved"), 2)
            self.assertEqual(j.get("rejected"), 1)
            self.assertEqual(get_job_draft(self.cfg, self.db, ids[0])["review_status"], "approved")
            self.assertEqual(get_job_draft(self.cfg, self.db, ids[1])["review_status"], "approved")
            self.assertEqual(get_job_draft(self.cfg, self.db, ids[2])["review_status"], "rejected")
        finally:
            stop_all(started)


if __name__ == "__main__":
    unittest.main()
