"""Independent gating tests for the VPS ``unified_capture`` approval inbox API.

Authored by the verification agent (NOT the implementer) for BTQ prompt 309
(Phase 3). These tests drive real HTTP requests through ``UnifiedCaptureHandler``
in-process, exercise the *real* authorization path (real ``TokenStore``, real
``authorize_token`` -> person resolution -> site scoping) and the *real* shared
review function (``field_capture.action_candidates.apply_candidate_review``,
including its ``_rev`` optimistic-concurrency + status-guard logic).

Only the lowest CouchDB candidate-store HTTP layer is faked, via an in-memory
"couchdb_review" double that backs the writer functions
(``get_action_candidate`` / ``list_action_candidates`` /
``set_action_candidate_status``). The double reproduces CouchDB's
revision-conflict and already-decided semantics faithfully so the real review
fn's blast-shield runs for real. A real CouchDB on the box would require auth
this verifier does not hold, so the double is used (documented in the report).

Reuses the harness from ``tests.test_unified_capture`` (token store builder,
couch auth fakes, fixtures, response parser) so person resolution is genuine.
"""

from __future__ import annotations

import copy
import io
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from token_store import TokenStore

import field_capture.action_candidates as action_candidates_module
from event_pipeline.couchdb_candidate_writer import AlreadyDecided
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
# In-memory "couchdb_review" double
# --------------------------------------------------------------------------- #


class FakeCandidateCouch:
    """Faithful in-memory stand-in for the CouchDB candidate store.

    Backs the writer functions that the shared review fn + inbox reader call:
      * ``get_action_candidate(config, db, candidate_id)`` -> doc-with-_rev | None
      * ``list_action_candidates(config, db, status=..)`` -> [docs-with-_rev]
      * ``set_action_candidate_status(...)`` -> mutate status, bump _rev,
        reproducing CouchDB's optimistic-concurrency + already-decided rules.

    Records every status mutation so tests can assert no second status codepath
    and exactly-one mutation per candidate.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.status_writes: list[dict[str, Any]] = []
        self._rev_counter = 0

    # ---- seeding ---------------------------------------------------------- #
    def seed(self, doc: dict[str, Any]) -> None:
        self._rev_counter += 1
        stored = copy.deepcopy(doc)
        stored["_rev"] = stored.get("_rev") or f"1-{self._rev_counter:08d}"
        stored.setdefault("_id", f"action_candidate_{stored['candidate_id']}")
        self.docs[str(stored["candidate_id"])] = stored

    # ---- writer-layer doubles -------------------------------------------- #
    def get_action_candidate(self, config: Any, db: str, candidate_id: str) -> dict[str, Any] | None:
        doc = self.docs.get(str(candidate_id))
        return copy.deepcopy(doc) if doc is not None else None

    def list_action_candidates(
        self,
        config: Any,
        db: str,
        *,
        status: str | None = None,
        person_id: str | None = None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for doc in self.docs.values():
            if str(doc.get("type") or "") != "action_candidate":
                continue
            if status is not None and str(doc.get("status") or "") != str(status):
                continue
            if person_id is not None and str(doc.get("person_id") or "") != str(person_id):
                continue
            out.append(copy.deepcopy(doc))
        out.sort(key=lambda d: (str(d.get("created_at") or ""), str(d.get("candidate_id") or "")))
        return out

    def set_action_candidate_status(
        self,
        config: Any,
        db: str,
        candidate_id: str,
        *,
        status: str,
        reviewed_by: str,
        rationale: str,
        expected_rev: str | None = None,
    ) -> dict[str, Any]:
        doc = self.docs.get(str(candidate_id))
        if doc is None:
            raise RuntimeError(f"candidate not found: {candidate_id}")
        current_rev = str(doc.get("_rev") or "")
        # Reproduce the real writer's two AlreadyDecided gates.
        if expected_rev is not None and str(expected_rev) != current_rev:
            raise AlreadyDecided(f"candidate _rev changed: {candidate_id}")
        if str(doc.get("status") or "") != "pending_review":
            raise AlreadyDecided(f"candidate already decided: {candidate_id}")
        self._rev_counter += 1
        doc["status"] = status
        doc["reviewed_by"] = reviewed_by
        doc["reviewer"] = reviewed_by
        doc["review_rationale"] = rationale
        doc["_rev"] = f"2-{self._rev_counter:08d}"
        self.status_writes.append(
            {"candidate_id": str(candidate_id), "status": status, "expected_rev": expected_rev}
        )
        return copy.deepcopy(doc)


def install_candidate_double(double: FakeCandidateCouch) -> list[Any]:
    """Patch the writer functions where they are looked up at call time.

    ``apply_candidate_review`` (the real shared fn) resolves
    ``get_action_candidate`` / ``set_action_candidate_status`` from the
    ``field_capture.action_candidates`` module namespace; the server's inbox
    reader resolves ``list_action_candidates`` from ``unified_capture.server``.
    """
    patchers = [
        mock.patch.object(action_candidates_module, "get_action_candidate", double.get_action_candidate),
        mock.patch.object(action_candidates_module, "set_action_candidate_status", double.set_action_candidate_status),
        mock.patch.object(uc_server, "list_action_candidates", double.list_action_candidates),
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


def pending_candidate(
    candidate_id: str = "cand_001",
    *,
    status: str = "pending_review",
    site_id: str = "7060",
    proposed: Any = "valid",
    created_at: str = "2026-06-01T12:00:00+00:00",
    person_id: str = "per_unified01",
) -> dict[str, Any]:
    """Build a CouchDB action_candidate doc that the inbox should surface."""
    if proposed == "valid":
        proposed_job: Any = copy.deepcopy(VALID_JOB)
    else:
        proposed_job = proposed
    doc: dict[str, Any] = {
        "_id": f"action_candidate_{candidate_id}",
        "type": "action_candidate",
        "candidate_id": candidate_id,
        "capture_id": f"cap_{candidate_id}",
        "status": status,
        "candidate_type": "field_capture_follow_up",
        "source_kind": "voice",
        "summary": f"Supply need at site for {candidate_id}",
        "evidence": {"source_text": f"We are out of trash bags ({candidate_id})."},
        "site_id": site_id,
        "proposed_queue_job": proposed_job,
        "person_id": person_id,
        "created_at": created_at,
        "source": "field_capture_pipeline",
        "source_detail": {
            "channel_metadata": {"action_key": "log_supply_need"},
            "approval_metadata": {"proposed_queue_job": proposed_job if isinstance(proposed_job, dict) else None},
        },
    }
    return doc


# --------------------------------------------------------------------------- #
# 1. Auth tripwire (fail-closed)
# --------------------------------------------------------------------------- #


class InboxAuthTripwireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.double = FakeCandidateCouch()
        self.double.seed(pending_candidate())
        self._dbl = install_candidate_double(self.double)
        self.addCleanup(lambda: stop_all(self._dbl))

    def _server(self, store: TokenStore) -> _InboxFakeServer:
        return _InboxFakeServer(store, self.vault, site_registry=None)

    def _admin_store(self):
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        store.create_token  # noqa: B018  (token already created above is 'capture')
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
                body=b'{"candidate_id":"cand_001","_rev":"%s"}' % rev.encode(),
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
                body=b'{"candidate_id":"cand_001","_rev":"%s","reason":"dup"}' % rev.encode(),
            )
            self.assertEqual(r_rej.status, 200, r_rej.text)
            self.assertEqual(r_rej.json().get("status"), "rejected")
        finally:
            stop_all(started)

    def test_non_admin_tokens_get_403_and_never_reach_review_fn(self) -> None:
        # Tripwire on the shared review fn: a non-admin must NEVER reach it.
        tripwire = mock.patch.object(
            uc_server, "apply_candidate_review",
            side_effect=AssertionError("apply_candidate_review reached by non-admin token"),
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
                        body=b'{"candidate_id":"cand_001","_rev":"x"}',
                    )
                    self.assertEqual(r_app.status, 403, r_app.text)
                    self.assertEqual(r_app.json().get("error"), "not_authorized")
                    r_rej = drive_inbox_request(
                        srv, "POST", "/api/inbox/reject",
                        headers={"Authorization": f"Bearer {token}"},
                        body=b'{"candidate_id":"cand_001","_rev":"x"}',
                    )
                    self.assertEqual(r_rej.status, 403, r_rej.text)
                    self.assertEqual(r_rej.json().get("error"), "not_authorized")
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
        self.double = FakeCandidateCouch()
        self._dbl = install_candidate_double(self.double)
        self.addCleanup(lambda: stop_all(self._dbl))

    def _server(self, *, site_registry: Any = None) -> _InboxFakeServer:
        return _InboxFakeServer(self.store, self.vault, site_registry=site_registry)

    def _list(self, srv: _InboxFakeServer) -> _Response:
        return drive_inbox_request(srv, "GET", "/api/inbox", headers={"Authorization": f"Bearer {self.token}"})

    def _approve(self, srv: _InboxFakeServer, candidate_id: str, rev: str) -> _Response:
        body = ('{"candidate_id":"%s","_rev":"%s"}' % (candidate_id, rev)).encode()
        return drive_inbox_request(srv, "POST", "/api/inbox/approve", headers={"Authorization": f"Bearer {self.token}"}, body=body)

    def _reject(self, srv: _InboxFakeServer, candidate_id: str, rev: str, reason: str = "dup") -> _Response:
        body = ('{"candidate_id":"%s","_rev":"%s","reason":"%s"}' % (candidate_id, rev, reason)).encode()
        return drive_inbox_request(srv, "POST", "/api/inbox/reject", headers={"Authorization": f"Bearer {self.token}"}, body=body)


# --------------------------------------------------------------------------- #
# 2. Inbox list = CouchDB + correct shape (field-for-field)
# --------------------------------------------------------------------------- #


class InboxListShapeTests(_AdminInboxMixin, unittest.TestCase):
    def test_inbox_returns_pending_candidates_with_full_contract_shape(self) -> None:
        self.double.seed(pending_candidate("cand_001", created_at="2026-06-01T10:00:00+00:00"))
        self.double.seed(pending_candidate("cand_002", created_at="2026-06-01T11:00:00+00:00"))
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
        # Field-for-field contract (Cowork's PWA mock is built against this).
        self.assertEqual(
            set(item.keys()),
            {"candidate_id", "_rev", "capture_id", "source", "summary", "evidence", "site", "created_at", "proposed_action"},
        )
        self.assertEqual(item["candidate_id"], "cand_001")
        self.assertEqual(item["capture_id"], "cap_cand_001")
        self.assertEqual(item["source"], "voice")
        self.assertEqual(item["summary"], "Supply need at site for cand_001")
        self.assertEqual(item["evidence"], "We are out of trash bags (cand_001).")
        self.assertEqual(item["created_at"], "2026-06-01T10:00:00+00:00")

        # _rev MUST be the live doc rev (so concurrent mutate is gated).
        live_rev = self.double.docs["cand_001"]["_rev"]
        self.assertEqual(item["_rev"], live_rev)
        self.assertTrue(item["_rev"])

        pa = item["proposed_action"]
        self.assertEqual(set(pa.keys()), {"action_key", "title", "job_type", "payload"})
        self.assertEqual(pa["job_type"], "log_supply_need")
        self.assertEqual(pa["action_key"], "log_supply_need")
        self.assertEqual(pa["payload"], VALID_JOB["payload"])

    def test_candidate_without_valid_proposed_job_is_excluded(self) -> None:
        self.double.seed(pending_candidate("cand_ok"))
        # No proposed job at all.
        self.double.seed(pending_candidate("cand_nojob", proposed=None))
        # Proposed job that fails validate_job (missing required fields).
        self.double.seed(pending_candidate("cand_bad", proposed={"job_type": "log_supply_need", "payload": {"site_id": "7060"}}))
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._list(srv)
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        ids = {it["candidate_id"] for it in r.json()["items"]}
        self.assertEqual(ids, {"cand_ok"})

    def test_only_pending_review_candidates_appear(self) -> None:
        # MUTATION-CHECK ANCHOR (task 7b): this asserts status filtering.
        self.double.seed(pending_candidate("cand_pending", status="pending_review"))
        self.double.seed(pending_candidate("cand_approved", status="approved"))
        self.double.seed(pending_candidate("cand_rejected", status="rejected"))
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._list(srv)
        finally:
            stop_all(started)
        ids = {it["candidate_id"] for it in r.json()["items"]}
        self.assertEqual(ids, {"cand_pending"})

    def test_filesystem_only_candidate_does_not_appear(self) -> None:
        # CouchDB-canonical read: a candidate that exists only on the filesystem
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
        self.double.seed(pending_candidate("cand_001"))
        live_rev = self.double.docs["cand_001"]["_rev"]
        spy = mock.patch.object(
            uc_server, "apply_candidate_review", wraps=uc_server.apply_candidate_review
        )
        # Tripwire: VPS endpoint must NOT stage anything.
        import field_capture.candidate_staging as staging_module
        stage_tripwire = mock.patch.object(
            staging_module, "stage_candidate_job_after_approval",
            side_effect=AssertionError("VPS endpoint staged a job (staging is the Pro watcher's job)"),
        )
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        spy_mock = spy.start()
        stage_tripwire.start()
        try:
            r = self._approve(srv, "cand_001", live_rev)
        finally:
            spy.stop()
            stage_tripwire.stop()
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "candidate_id": "cand_001", "status": "approved"})
        # Shared fn called once, with expected_rev = the passed _rev.
        self.assertEqual(spy_mock.call_count, 1)
        _, kwargs = spy_mock.call_args
        self.assertEqual(kwargs.get("expected_rev"), live_rev)
        self.assertEqual(kwargs.get("action"), "approve")
        # CouchDB status set to approved; exactly one status write; nothing staged.
        self.assertEqual(self.double.docs["cand_001"]["status"], "approved")
        self.assertEqual([w["status"] for w in self.double.status_writes], ["approved"])

    def test_reject_sets_rejected_and_stages_nothing(self) -> None:
        self.double.seed(pending_candidate("cand_001"))
        live_rev = self.double.docs["cand_001"]["_rev"]
        import field_capture.candidate_staging as staging_module
        stage_tripwire = mock.patch.object(
            staging_module, "stage_candidate_job_after_approval",
            side_effect=AssertionError("VPS reject staged a job"),
        )
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        stage_tripwire.start()
        try:
            r = self._reject(srv, "cand_001", live_rev)
        finally:
            stage_tripwire.stop()
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "candidate_id": "cand_001", "status": "rejected"})
        self.assertEqual(self.double.docs["cand_001"]["status"], "rejected")
        self.assertEqual([w["status"] for w in self.double.status_writes], ["rejected"])

    def test_server_has_no_parallel_approval_path_or_ops_dashboard_import(self) -> None:
        import inspect
        src = inspect.getsource(uc_server)
        # No second status machine / parallel approval semantics in the server.
        self.assertNotIn("set_action_candidate_status", src,
                         "server must not call the status writer directly; go through apply_candidate_review")
        self.assertNotIn("stage_candidate_job_after_approval", src,
                         "server must not stage (staging is the Pro watcher's job)")
        self.assertNotIn("ops_dashboard", src, "server must not import ops_dashboard approval code")
        # Approval/reject both route through the one shared fn.
        self.assertIn("apply_candidate_review", src)


# --------------------------------------------------------------------------- #
# 4. _rev race (the critical UX): stale _rev -> 200 already_decided
# --------------------------------------------------------------------------- #


class InboxRevRaceTests(_AdminInboxMixin, unittest.TestCase):
    def test_stale_rev_approve_returns_200_already_decided_no_remutation(self) -> None:
        self.double.seed(pending_candidate("cand_001"))
        stale_rev = self.double.docs["cand_001"]["_rev"]
        # Simulate the phone-vs-/swipe race: another reviewer already approved,
        # bumping the rev. The PWA still holds the stale rev.
        self.double.set_action_candidate_status(
            None, "db", "cand_001", status="approved", reviewed_by="other", rationale="r", expected_rev=stale_rev
        )
        new_rev = self.double.docs["cand_001"]["_rev"]
        self.assertNotEqual(new_rev, stale_rev)
        writes_before = len(self.double.status_writes)

        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._approve(srv, "cand_001", stale_rev)
        finally:
            stop_all(started)
        # Critical: 200 (reads as 'already handled' in the PWA), NOT a 4xx/5xx.
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json(), {"error": "already_decided"})
        # Not re-mutated: no new status write, rev unchanged.
        self.assertEqual(len(self.double.status_writes), writes_before)
        self.assertEqual(self.double.docs["cand_001"]["_rev"], new_rev)

    def test_stale_rev_reject_returns_200_already_decided_no_remutation(self) -> None:
        self.double.seed(pending_candidate("cand_001"))
        stale_rev = self.double.docs["cand_001"]["_rev"]
        self.double.set_action_candidate_status(
            None, "db", "cand_001", status="rejected", reviewed_by="other", rationale="r", expected_rev=stale_rev
        )
        new_rev = self.double.docs["cand_001"]["_rev"]
        writes_before = len(self.double.status_writes)

        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        srv = self._server()
        try:
            r = self._reject(srv, "cand_001", stale_rev)
        finally:
            stop_all(started)
        self.assertEqual(r.status, 200, r.text)
        self.assertEqual(r.json(), {"error": "already_decided"})
        self.assertEqual(len(self.double.status_writes), writes_before)
        self.assertEqual(self.double.docs["cand_001"]["_rev"], new_rev)


# --------------------------------------------------------------------------- #
# 5. inbox_count on /api/session
# --------------------------------------------------------------------------- #


class InboxCountTests(_AdminInboxMixin, unittest.TestCase):
    def test_session_inbox_count_matches_pending_approvable_count_for_admin(self) -> None:
        self.double.seed(pending_candidate("cand_001"))
        self.double.seed(pending_candidate("cand_002"))
        self.double.seed(pending_candidate("cand_nojob", proposed=None))  # not approvable
        self.double.seed(pending_candidate("cand_done", status="approved"))  # not pending
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
        self.double.seed(pending_candidate("cand_001"))
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


# --------------------------------------------------------------------------- #
# 6. Graceful degrade when site-label lookup fails
# --------------------------------------------------------------------------- #


class InboxSiteLabelDegradeTests(_AdminInboxMixin, unittest.TestCase):
    def test_site_label_lookup_throwing_falls_back_to_raw_site_id_and_returns_200(self) -> None:
        self.double.seed(pending_candidate("cand_001", site_id="7060"))

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


if __name__ == "__main__":
    unittest.main()
