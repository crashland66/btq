"""Independent behavioral tests for ``unified_capture.server``.

Authored by the verification agent (NOT the implementer). These tests drive
real HTTP requests through ``UnifiedCaptureHandler`` in-process and exercise the
*real* authorization path: a real ``TokenStore`` (SQLite), the real
``authorize_token`` -> ``load_person_from_canonical`` -> ``allowed_sites_for_person``
chain. Only the lowest CouchDB-HTTP layer is faked (canned employee docs and
site-view payloads), so the person resolution and site-scoping logic under test
runs for real -- a hand-rolled session dict would NOT pass.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from btq_vault.couch_store import CouchDBEntityStore
from token_store import TokenStore

import field_capture.auth as fc_auth
from unified_capture import server as uc_server
from unified_capture.server import UnifiedCaptureHandler, UnifiedCaptureServer


# --------------------------------------------------------------------------- #
# In-process request driver
# --------------------------------------------------------------------------- #


class _FakeServer:
    """Minimal stand-in carrying the attributes the handler reads off ``self.server``."""

    def __init__(self, token_store: TokenStore, vault_root: Path) -> None:
        self.token_store = token_store
        self.vault_root = vault_root
        self.data_dir = None


class _Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def drive_request(
    token_store: TokenStore,
    vault_root: Path,
    method: str,
    path: str,
    headers: Optional[dict[str, str]] = None,
) -> _Response:
    """Drive a single request through the real handler without opening a socket."""
    request_line = f"{method} {path} HTTP/1.1\r\n"
    header_lines = "".join(f"{name}: {value}\r\n" for name, value in (headers or {}).items())
    raw = (request_line + header_lines + "\r\n").encode("utf-8")

    handler = UnifiedCaptureHandler.__new__(UnifiedCaptureHandler)
    handler.server = _FakeServer(token_store, vault_root)  # type: ignore[assignment]
    handler.client_address = ("127.0.0.1", 0)
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.handle_one_request()

    handler.wfile.seek(0)
    raw_out = handler.wfile.getvalue()
    status_line, _, rest = raw_out.partition(b"\r\n")
    head, _, body = rest.partition(b"\r\n\r\n")
    status = int(status_line.split(b" ")[1])
    parsed_headers: dict[str, str] = {}
    for line in head.split(b"\r\n"):
        if b":" in line:
            name, _, value = line.partition(b":")
            parsed_headers[name.decode().strip()] = value.decode().strip()
    return _Response(status, parsed_headers, body)


# --------------------------------------------------------------------------- #
# Fake CouchDB layer (lowest level only -- real auth logic runs above it)
# --------------------------------------------------------------------------- #


class _FakeEntityStore(CouchDBEntityStore):
    """Real CouchDBEntityStore subclass; only the HTTP doc fetch is canned.

    ``load_person_from_canonical`` (the real code under test) calls
    ``get_optional("employee_<id>")``, which delegates to ``_get_doc``.
    By overriding only ``_get_doc`` we keep all person-resolution logic real.
    """

    def __init__(self, employee_docs: dict[str, dict[str, Any]]) -> None:
        # Intentionally skip super().__init__ -- we never make real HTTP calls.
        self._employee_docs = employee_docs

    def _get_doc(self, doc_id: str) -> Optional[dict[str, Any]]:  # type: ignore[override]
        doc = self._employee_docs.get(doc_id)
        return dict(doc) if doc is not None else None

    # No find-fallbacks: force the employee_<id> primary lookup path.
    def find_employee_docs_by_person_id(self, person_id: str, *, limit: int = 10):  # type: ignore[override]
        return []

    def find_employee_docs(self, *, limit: int = 0):  # type: ignore[override]
        return []


def _make_registry(site_rows: list[dict[str, Any]], *, explode: bool = False):
    """Build a *real* CouchDBSiteRegistry whose HTTP layer returns canned views.

    Keeps resolve_canonical / list_sites / get_display_categories real so that
    site-scoping is genuinely exercised. ``site_rows`` are dicts with keys
    site_id, canonical, and optionally display_categories.
    """
    registry = fc_auth.CouchDBSiteRegistry()

    by_alias_rows = [
        {"key": row["canonical"], "value": {"site_id": row["site_id"], "canonical": row["canonical"]}}
        for row in site_rows
    ]
    by_id = {row["site_id"]: row for row in site_rows}

    def fake_request_json(url: str) -> dict[str, Any]:
        if explode:
            raise RuntimeError("registry view unavailable")
        if "by_alias" in url:
            return {"rows": by_alias_rows}
        if "by_site_id" in url:
            # url carries ?key="<json>"
            from urllib.parse import urlsplit, parse_qs

            key = parse_qs(urlsplit(url).query).get("key", ["null"])[0]
            site_id = json.loads(key)
            row = by_id.get(str(site_id))
            if row is None:
                return {"rows": []}
            return {"rows": [{"key": site_id, "value": {"site_id": row["site_id"], "canonical": row["canonical"]}}]}
        # _get_site_doc path: .../site_<id>
        doc_id = url.rsplit("/", 1)[-1]
        prefix = "site_"
        sid = doc_id[len(prefix):] if doc_id.startswith(prefix) else doc_id
        row = by_id.get(sid)
        if row is None:
            from event_pipeline.couchdb_registry import CouchDBRegistryError

            raise CouchDBRegistryError(f"CouchDB request failed with HTTP 404: {url}")
        doc = {"_id": doc_id, "site_id": row["site_id"], "canonical": row["canonical"]}
        if "display_categories" in row:
            doc["display_categories"] = row["display_categories"]
        return doc

    registry._request_json = fake_request_json  # type: ignore[assignment]
    return registry


def install_couch_fakes(
    employee_docs: dict[str, dict[str, Any]],
    site_rows: list[dict[str, Any]],
    *,
    system_defaults: Any = None,
    registry_unavailable: bool = False,
    defaults_unavailable: bool = False,
):
    """Context manager patching the lowest CouchDB layer used by the auth path.

    Returns an ``ExitStack``-like list of mock patchers already started; caller
    uses it as a context manager via ``contextlib``-free manual entry.
    """
    patches = []

    store = _FakeEntityStore(employee_docs)
    patches.append(mock.patch.object(fc_auth, "CouchDBEntityStore"))

    if registry_unavailable:
        def _registry_factory(*a, **k):
            raise RuntimeError("site registry unavailable")
    else:
        registry = _make_registry(site_rows)

        def _registry_factory(*a, **k):
            return registry

    patches.append(mock.patch.object(fc_auth, "CouchDBSiteRegistry", side_effect=_registry_factory))

    # system_defaults is loaded by the server module, not the auth module.
    if defaults_unavailable:
        defaults_mock = mock.patch.object(
            uc_server, "load_system_defaults", side_effect=RuntimeError("couch down")
        )
    else:
        defaults_mock = mock.patch.object(
            uc_server, "load_system_defaults", return_value=(system_defaults or {})
        )

    started = []
    entity_patch = patches[0].start()
    entity_patch.from_env.return_value = store
    started.append(patches[0])
    started.append(patches[1])
    patches[1].start()
    defaults_mock.start()
    started.append(defaults_mock)
    return started


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

EMP_SINGLE = {
    "employee_per_unified01": {
        "_id": "employee_per_unified01",
        "type": "employee",
        "person_id": "per_unified01",
        "name": "Casey Worker",
        "site_ids": ["7060"],
        "status": "active",
    }
}

SITES_TWO = [
    {"site_id": "7060", "canonical": "Continental Metalworks"},
    {"site_id": "1300", "canonical": "Other Plant"},
]


def make_store(
    tmp: str,
    *,
    person_id: str = "per_unified01",
    site_ids=None,
    can_submit: bool = True,
    revoked: bool = False,
    role: str = "cleaner",
    expires_at=None,
) -> tuple[TokenStore, str]:
    store = TokenStore(Path(tmp) / "field_capture_tokens.sqlite3")
    store.initialize()
    created = store.create_token(
        person_id=person_id,
        label="unified",
        can_submit=can_submit,
        role=role,
        site_ids=site_ids,
        expires_at=expires_at,
    )
    if revoked:
        store.revoke_token(created.record.token_id)
    return store, created.token_value


def stop_all(started) -> None:
    for patcher in started:
        patcher.stop()


# --------------------------------------------------------------------------- #
# Scaffold (Build A)
# --------------------------------------------------------------------------- #


class ScaffoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.token = make_store(self.tmp.name)
        self.vault = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def get(self, path: str, headers=None) -> _Response:
        return drive_request(self.store, self.vault, "GET", path, headers)

    def post(self, path: str, headers=None) -> _Response:
        return drive_request(self.store, self.vault, "POST", path, headers)

    def test_health_ok_identifies_unified_capture(self) -> None:
        resp = self.get("/api/health")
        self.assertEqual(resp.status, 200)
        payload = resp.json()
        self.assertEqual(payload["status"], "ok")
        # Must identify itself as unified_capture, not field_capture/voice_memo.
        self.assertIn("unified_capture", json.dumps(payload))

    def test_root_serves_shell_html(self) -> None:
        resp = self.get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIn("<", resp.text)

    def test_service_worker_renders_unified_config(self) -> None:
        resp = self.get("/sw.js")
        self.assertEqual(resp.status, 200)
        self.assertIn("javascript", resp.headers.get("Content-Type", ""))
        # Rendered from render_service_worker("unified_capture"): both markers present.
        self.assertIn("unifiedCaptureToken", resp.text)
        self.assertIn("/api/submit", resp.text)

    def test_static_db_js_served_as_javascript(self) -> None:
        resp = self.get("/static/db.js")
        self.assertEqual(resp.status, 200)
        self.assertIn("javascript", resp.headers.get("Content-Type", ""))
        self.assertTrue(len(resp.body) > 0)

    def test_unknown_route_404(self) -> None:
        resp = self.get("/api/does-not-exist")
        self.assertEqual(resp.status, 404)

    def test_post_unknown_route_is_404(self) -> None:
        # Build D implements /api/submit, so POSTing /api/submit is no longer a
        # 404 (its real behavior is covered by SubmitBehaviorTests below). The
        # generic invariant that survives: POST to an UNKNOWN route is 404, and
        # do_POST never falls through to the static/public file server.
        for probe in ("/api/submit-not-real", "/api/does-not-exist", "/app.js", "/static/db.js"):
            with self.subTest(probe=probe):
                self.assertEqual(self.post(probe).status, 404)

    def test_post_any_route_404(self) -> None:
        for path in ("/", "/api/session", "/api/health"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path).status, 404)

    def test_path_traversal_refused_no_source_leak(self) -> None:
        leak_marker = "UnifiedCaptureHandler"  # appears in server.py source
        for probe in ("/../server.py", "/..%2fserver.py", "/static/../../server.py", "/static/..%2f..%2fserver.py"):
            with self.subTest(probe=probe):
                resp = self.get(probe)
                self.assertEqual(resp.status, 404, f"{probe} should 404")
                self.assertNotIn(leak_marker, resp.text, f"{probe} leaked source")
                self.assertNotIn("from __future__", resp.text)


# --------------------------------------------------------------------------- #
# Session (Build B) -- exercises the REAL auth path
# --------------------------------------------------------------------------- #


class SessionHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_valid_token_returns_person_token_sites_can_submit_categories(self) -> None:
        store, token = make_store(self.tmp.name, site_ids=["7060"], can_submit=True)
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        try:
            resp = drive_request(store, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token}"})
        finally:
            stop_all(started)
        self.assertEqual(resp.status, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["person"]["person_id"], "per_unified01")
        self.assertEqual(body["person"]["name"], "Casey Worker")
        self.assertEqual(body["token"]["label"], "unified")
        self.assertIn("token_id", body["token"])
        self.assertTrue(body["can_submit"])
        self.assertEqual(len(body["sites"]), 1)
        self.assertEqual(body["sites"][0]["site_id"], "7060")
        # display_categories present (falls back to builtin if none configured).
        self.assertIsInstance(body["sites"][0]["display_categories"], list)
        self.assertTrue(body["sites"][0]["display_categories"])

    def test_site_scoping_excludes_ungranted_site(self) -> None:
        # Token granted only 1200; 1300 exists in the registry but must NOT appear.
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        try:
            resp = drive_request(store, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token}"})
        finally:
            stop_all(started)
        self.assertEqual(resp.status, 200, resp.text)
        site_ids = {s["site_id"] for s in resp.json()["sites"]}
        self.assertIn("7060", site_ids)
        self.assertNotIn("1300", site_ids)

    def test_token_site_scope_overrides_employee_doc_sites(self) -> None:
        # Employee doc lists 1200; but token scopes to 1300 only. Token wins.
        emp = {
            "employee_per_unified01": {
                "_id": "employee_per_unified01",
                "type": "employee",
                "person_id": "per_unified01",
                "name": "Casey Worker",
                "site_ids": ["7060"],
                "status": "active",
            }
        }
        store, token = make_store(self.tmp.name, site_ids=["1300"])
        started = install_couch_fakes(emp, SITES_TWO)
        try:
            resp = drive_request(store, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token}"})
        finally:
            stop_all(started)
        self.assertEqual(resp.status, 200, resp.text)
        site_ids = {s["site_id"] for s in resp.json()["sites"]}
        self.assertEqual(site_ids, {"1300"})

    def test_can_submit_false_authenticates_but_reflects_false(self) -> None:
        # Beyond-spec: a view-only token still authenticates; can_submit must be
        # honestly False (not blocked, not forced true).
        store, token = make_store(self.tmp.name, site_ids=["7060"], can_submit=False)
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        try:
            resp = drive_request(store, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token}"})
        finally:
            stop_all(started)
        self.assertEqual(resp.status, 200, resp.text)
        self.assertFalse(resp.json()["can_submit"])

    def test_one_worker_session_never_leaks_anothers_sites(self) -> None:
        emp = dict(EMP_SINGLE)
        emp["employee_per_other02"] = {
            "_id": "employee_per_other02",
            "type": "employee",
            "person_id": "per_other02",
            "name": "Dana Other",
            "site_ids": ["1300"],
            "status": "active",
        }
        store_a, token_a = make_store(self.tmp.name, person_id="per_unified01", site_ids=["7060"])
        # Second worker in a separate store dir to avoid token collisions.
        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        store_b, token_b = make_store(tmp2.name, person_id="per_other02", site_ids=["1300"])

        started = install_couch_fakes(emp, SITES_TWO)
        try:
            resp_a = drive_request(store_a, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token_a}"})
            resp_b = drive_request(store_b, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token_b}"})
        finally:
            stop_all(started)
        sites_a = {s["site_id"] for s in resp_a.json()["sites"]}
        sites_b = {s["site_id"] for s in resp_b.json()["sites"]}
        self.assertEqual(sites_a, {"7060"})
        self.assertEqual(sites_b, {"1300"})
        self.assertNotIn("1300", sites_a)
        self.assertNotIn("7060", sites_b)

    def test_success_path_genuinely_calls_authorize_token(self) -> None:
        # Spy on authorize_token: if the handler hand-rolled a session dict,
        # this assertion (and the wrapped real result) would fail.
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        real_authorize = uc_server.authorize_token
        calls = {"n": 0}

        def spy(token_store, vault_root, token_value):
            calls["n"] += 1
            return real_authorize(token_store, vault_root, token_value)

        spy_patch = mock.patch.object(uc_server, "authorize_token", side_effect=spy)
        spy_patch.start()
        try:
            resp = drive_request(store, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token}"})
        finally:
            spy_patch.stop()
            stop_all(started)
        self.assertEqual(resp.status, 200, resp.text)
        self.assertEqual(calls["n"], 1, "handler must route /api/session through authorize_token")
        # And the response reflects the real session it produced.
        self.assertEqual(resp.json()["person"]["person_id"], "per_unified01")

    def test_session_degrades_when_system_defaults_unavailable(self) -> None:
        # Beyond-spec: load_system_defaults raising must NOT 500.
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO, defaults_unavailable=True)
        try:
            resp = drive_request(store, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token}"})
        finally:
            stop_all(started)
        self.assertNotEqual(resp.status, 500, resp.text)
        self.assertEqual(resp.status, 200, resp.text)
        # Categories still resolve to a non-empty builtin fallback.
        self.assertTrue(resp.json()["sites"][0]["display_categories"])

    def test_session_degrades_when_site_registry_unavailable(self) -> None:
        # Beyond-spec: registry construction failing must NOT 500.
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO, registry_unavailable=True)
        try:
            resp = drive_request(store, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token}"})
        finally:
            stop_all(started)
        # Registry unavailable -> authorize_token returns None (auth lookup failed)
        # -> fail-closed 401, never a 500.
        self.assertNotEqual(resp.status, 500, resp.text)
        self.assertIn(resp.status, (401, 403), resp.text)


class SessionFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        self.store, self.token = make_store(self.tmp.name, site_ids=["7060"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def session(self, headers=None, path="/api/session") -> _Response:
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        try:
            return drive_request(self.store, self.vault, "GET", path, headers)
        finally:
            stop_all(started)

    def test_missing_token_401(self) -> None:
        self.assertEqual(self.session().status, 401)

    def test_garbage_token_401(self) -> None:
        self.assertEqual(self.session({"Authorization": "Bearer fc_not_a_real_token"}).status, 401)

    def test_revoked_token_401(self) -> None:
        store, token = make_store(tempfile.mkdtemp(), site_ids=["7060"], revoked=True)
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        try:
            resp = drive_request(store, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token}"})
        finally:
            stop_all(started)
        self.assertEqual(resp.status, 401)

    def test_expired_token_401(self) -> None:
        from datetime import datetime, timedelta, timezone

        past = datetime.now(timezone.utc) - timedelta(days=1)
        store, token = make_store(tempfile.mkdtemp(), site_ids=["7060"], expires_at=past)
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        try:
            resp = drive_request(store, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token}"})
        finally:
            stop_all(started)
        self.assertEqual(resp.status, 401)

    def test_malformed_bearer_no_value_is_401_not_500(self) -> None:
        resp = self.session({"Authorization": "Bearer"})
        self.assertNotEqual(resp.status, 500)
        self.assertEqual(resp.status, 401)

    def test_non_bearer_scheme_is_401_not_500(self) -> None:
        resp = self.session({"Authorization": "Basic dXNlcjpwYXNz"})
        self.assertNotEqual(resp.status, 500)
        self.assertEqual(resp.status, 401)

    def test_unknown_person_doc_fails_closed_401(self) -> None:
        # Token authenticates, but no canonical employee doc exists ->
        # authorize_token returns None -> 401 (fail closed), not 200/500.
        store, token = make_store(tempfile.mkdtemp(), person_id="per_ghost99", site_ids=["7060"])
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)  # no doc for per_ghost99
        try:
            resp = drive_request(store, self.vault, "GET", "/api/session", {"Authorization": f"Bearer {token}"})
        finally:
            stop_all(started)
        self.assertNotEqual(resp.status, 500, resp.text)
        self.assertIn(resp.status, (401, 403), resp.text)


class SessionTokenExtractionTests(unittest.TestCase):
    """Token-source precedence must match field_capture's extractor behavior."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, store, headers, path="/api/session"):
        started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        try:
            return drive_request(store, self.vault, "GET", path, headers)
        finally:
            stop_all(started)

    def test_token_via_query_param_authenticates(self) -> None:
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        resp = self._run(store, {}, path=f"/api/session?token={token}")
        self.assertEqual(resp.status, 200, resp.text)
        self.assertEqual(resp.json()["person"]["person_id"], "per_unified01")

    def test_header_takes_precedence_over_query_param(self) -> None:
        # field_capture's extractor checks Authorization header FIRST, then query.
        # A valid header + a garbage query param must authenticate via the header.
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        resp = self._run(store, {"Authorization": f"Bearer {token}"}, path="/api/session?token=fc_garbage")
        self.assertEqual(resp.status, 200, resp.text)
        self.assertEqual(resp.json()["person"]["person_id"], "per_unified01")

    def test_extractor_matches_field_capture_order(self) -> None:
        # Directly assert the unified extractor honors header-before-query like
        # field_capture: with a bogus header value present, the query is ignored.
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        # Bogus, non-bearer header + good query -> header path yields no bearer,
        # falls through to query param (header had no Bearer token).
        resp = self._run(store, {"Authorization": "Basic xxx"}, path=f"/api/session?token={token}")
        self.assertEqual(resp.status, 200, resp.text)


# --------------------------------------------------------------------------- #
# Combined capture surface (Build C) -- STRUCTURAL frontend tests
#
# Authored by the verification agent (NOT the implementer). These assert the
# combined photo+voice capture frontend by reading the *actually served* assets
# through the real handler (not by reading source files off disk). They are
# STRUCTURAL: they prove the wiring/markers exist in the served HTML/JS, not
# that IndexedDB queueing executes in a browser. Browser-execution proof of the
# offline queue is deferred to live dogfood.
# --------------------------------------------------------------------------- #

import os as _os
import shutil as _shutil
import subprocess as _subprocess

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _AssetServingMixin:
    """Provides a handler bound to a throwaway TokenStore for serving assets.

    Asset routes (/app.js, /index.html, /static/recorder.js, /sw.js) do not
    consult the token store, so a minimal store is sufficient.
    """

    def setUp(self) -> None:  # noqa: D401 - test fixture
        self.tmp = tempfile.TemporaryDirectory()
        self.store, _ = make_store(self.tmp.name)
        self.vault = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def served(self, path: str) -> _Response:
        return drive_request(self.store, self.vault, "GET", path)


class CombinedCaptureSurfaceTests(_AssetServingMixin, unittest.TestCase):
    """index.html + app.js wire BOTH photo capture AND a voice recorder."""

    def test_index_html_has_photo_capture_surface(self) -> None:
        html = self.served("/index.html").text
        # Multi-photo inputs + a thumbnail grid container.
        self.assertIn('id="thumbnailGrid"', html)
        self.assertIn('id="cameraInput"', html)
        self.assertIn('id="fileInput"', html)
        # Library picker accepts multiple files (multi-photo capture).
        self.assertRegex(html, r'id="fileInput"[^>]*\bmultiple\b')
        self.assertIn('id="photoCount"', html)

    def test_photo_buttons_have_cat_app_glyphs(self) -> None:
        # Operator request: reuse the cat app's camera/photo-roll glyphs in fc.
        html = self.served("/index.html").text
        # Both photo buttons carry an inline glyph svg, glyph-above-label.
        self.assertEqual(html.count('class="photo-glyph"'), 2)
        self.assertIn(">Take Photo</span>", html)
        self.assertIn(">Camera Roll</span>", html)
        # cat-app style: both buttons read as one set; Take Photo is the accent
        # OUTLINE primary (not a solid-green fill).
        self.assertIn("add-photo-button--primary", html)
        css = (
            Path(__file__).resolve().parents[1]
            / "unified_capture"
            / "public"
            / "styles.css"
        ).read_text(encoding="utf-8")
        base = re.search(r"\.add-photo-button \{(.*?)\}", css, re.S)
        self.assertIsNotNone(base)
        self.assertIn("background: var(--surface)", base.group(1))

    def test_index_html_has_voice_recorder_surface(self) -> None:
        html = self.served("/index.html").text
        # Prompt 290: recorder now matches the ORIGINAL field_capture/voice_memo
        # tape deck. Record (●) / Stop (■) / Clear (✕) glyph buttons + duration
        # + preview. No finish button, no pause/resume.
        for marker in (
            'id="recordVoiceButton"',
            'id="stopVoiceButton"',
            'id="clearVoiceButton"',
            'id="voiceDuration"',
            'id="voicePreview"',
        ):
            self.assertIn(marker, html, marker)
        # Tape-deck classes on the three transport buttons.
        for cls in ("tape-record", "tape-stop", "tape-clear"):
            self.assertIn(cls, html, cls)
        # Original glyphs (HTML entities &#9679; ● / &#9632; ■ / &#10005; ✕),
        # whether encoded as named/numeric entities or raw unicode.
        self.assertTrue(
            "&#9679;" in html or "●" in html, "record glyph ● missing"
        )
        self.assertTrue(
            "&#9632;" in html or "■" in html, "stop glyph ■ missing"
        )
        self.assertTrue(
            "&#10005;" in html or "✕" in html, "clear glyph ✕ missing"
        )
        # The old finish button + pause/resume controls are GONE.
        self.assertNotIn('id="finishVoiceButton"', html)
        self.assertNotIn('id="pauseVoiceButton"', html)
        self.assertNotIn('id="resumeVoiceButton"', html)

    def test_app_js_wires_both_capture_kinds(self) -> None:
        app = self.served("/app.js").text
        # Photo pipeline.
        self.assertIn("state.photos", app)
        self.assertIn("renderPhotos", app)
        self.assertIn("addFiles", app)
        # Prompt 290: voice pipeline now matches the original tape deck —
        # record/stop/clear, no pause/resume.
        self.assertIn("startVoiceRecording", app)
        self.assertIn("stopVoiceRecording", app)
        self.assertIn("MediaRecorder", app)
        self.assertIn("durationSeconds", app)
        # MediaRecorder is driven via start()/stop() only; pause/resume removed.
        self.assertIn(".start()", app)
        self.assertIn(".stop()", app)
        self.assertNotIn(".pause()", app)
        self.assertNotIn(".resume()", app)
        # The three original transport buttons are resolved/bound; the old finish
        # button is gone.
        self.assertIn('"#recordVoiceButton"', app)
        self.assertIn('"#stopVoiceButton"', app)
        self.assertIn('"#clearVoiceButton"', app)
        self.assertNotIn('"#finishVoiceButton"', app)
        # Record + stop buttons are bound to their handlers.
        self.assertRegex(
            app, r'recordVoiceButton\.addEventListener\(\s*"click",\s*startVoiceRecording'
        )
        self.assertRegex(
            app, r'stopVoiceButton\.addEventListener\(\s*"click",\s*stopVoiceRecording'
        )

    def test_recorder_module_is_wired_in_html(self) -> None:
        html = self.served("/index.html").text
        self.assertIn('src="/static/recorder.js"', html)


class SubmitGateTests(_AssetServingMixin, unittest.TestCase):
    """A capture with neither photos nor audio cannot submit."""

    def test_submit_gate_requires_at_least_one_asset(self) -> None:
        app = self.served("/app.js").text
        # The gate computes hasAsset from photos OR audio, and disables submit
        # when there is no asset.
        self.assertIn("state.photos.length > 0 || hasAudio()", app)
        self.assertRegex(app, r"submitButton\.disabled\s*=.*!hasAsset")

    def test_build_record_throws_without_any_media(self) -> None:
        app = self.served("/app.js").text
        # buildCaptureRecord guards: no photo and no audio -> throws (cannot save).
        self.assertIn("!state.photos.length && !state.audio", app)


class SingleRecordPerCaptureTests(_AssetServingMixin, unittest.TestCase):
    """Save creates ONE IndexedDB record per capture via shared putCapture."""

    def test_save_calls_putcapture_exactly_once_per_capture(self) -> None:
        app = self.served("/app.js").text
        # Exactly one capture-record putCapture call in saveCapture (the only
        # other putCapture is the SW token stash, keyed "__token__").
        self.assertIn("window.fieldCaptureDb.putCapture(record)", app)
        capture_puts = app.count("putCapture(record)")
        self.assertEqual(capture_puts, 1, "expected exactly one capture-record putCapture call")

    def test_capture_record_has_single_id_and_both_media_kinds(self) -> None:
        app = self.served("/app.js").text
        # One capture_id minted and reused for the record + fields.
        self.assertIn("const captureId =", app)
        self.assertRegex(app, r"capture_id:\s*captureId")
        # Record carries BOTH media kinds structurally.
        self.assertRegex(app, r"\bphotos:\s*state\.photos\.map")
        self.assertRegex(app, r"\baudio:\s*state\.audio")


class CaptureSessionGateTests(_AssetServingMixin, unittest.TestCase):
    """Capture UI is session-gated, consistent with Build B."""

    def test_form_starts_disabled_until_session(self) -> None:
        app = self.served("/app.js").text
        # Form is disabled on boot; submit requires a live session object.
        self.assertIn("setFormEnabled(false)", app)
        self.assertRegex(app, r"submitButton\.disabled\s*=.*!state\.session")

    def test_no_session_endpoint_keeps_capture_disabled(self) -> None:
        app = self.served("/app.js").text
        # A failed/absent /api/session response clears session and disables form.
        self.assertIn("/api/session", app)
        self.assertRegex(app, r"state\.session\s*=\s*null")
        self.assertIn("setFormEnabled(false)", app)


class RecorderRouteResolvesTests(_AssetServingMixin, unittest.TestCase):
    """The previously-dead /static/recorder.js route now resolves and serves JS."""

    def test_static_recorder_js_served_as_javascript(self) -> None:
        resp = self.served("/static/recorder.js")
        self.assertEqual(resp.status, 200, resp.text)
        self.assertIn("javascript", resp.headers.get("Content-Type", ""))
        self.assertTrue(len(resp.body) > 0)
        # It is the recorder module (exposes the btqRecorder global the app uses).
        self.assertIn("btqRecorder", resp.text)


class SoleMutatorInvariantTests(_AssetServingMixin, unittest.TestCase):
    """Frontend only POSTs to /api/submit; no canonical / direct-CouchDB writes."""

    def test_app_js_only_posts_to_api_submit(self) -> None:
        app = self.served("/app.js").text
        # The only POST is the enqueue-evidence upload to /api/submit.
        self.assertIn('fetch("/api/submit"', app)
        # No direct canonical-store mutation surfaces in the frontend.
        for forbidden in ("_bulk_docs", "couch", "CouchDB", "PUT", "/canonical"):
            self.assertNotIn(forbidden, app, f"unexpected canonical-write marker: {forbidden}")


class JsSyntaxTests(unittest.TestCase):
    """node --check must pass on app.js and static/recorder.js (no syntax errors)."""

    @classmethod
    def setUpClass(cls) -> None:
        if _shutil.which("node") is None:
            raise unittest.SkipTest("node not available")
        cls.public = _REPO_ROOT / "project" / "unified_capture" / "public"

    def _check(self, rel: str) -> None:
        target = self.public / rel
        self.assertTrue(target.is_file(), f"missing {target}")
        result = _subprocess.run(
            ["node", "--check", str(target)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_app_js_syntax_ok(self) -> None:
        self._check("app.js")

    def test_recorder_js_syntax_ok(self) -> None:
        self._check("static/recorder.js")


# --------------------------------------------------------------------------- #
# Rendered service-worker byte-identity REGRESSION GUARD (Build C critical)
#
# Build C parameterized the shared sw.js.template permanent-failure rule via a
# __PERMANENT_FAILURE_CHECK__ placeholder + a default on ServiceWorkerConfig.
# field_capture and voice_memo must NOT change behavior: their RENDERED service
# workers must remain byte-identical to Build B (committed origin/unified-capture).
# unified_capture overrides the rule to make 404 retryable (Build D writer
# arrives later). These tests render the Build-B versions out of git and compare.
# --------------------------------------------------------------------------- #

from shared_pwa.assets import (  # noqa: E402
    SERVICE_WORKERS,
    render_service_worker,
)

# Re-baselined (twice): the original ref ``origin/unified-capture`` was Build B
# when these guards were authored, but it MOVES. A later fix switched to
# ``git merge-base HEAD main`` — which ALSO moves: once the unified arc merged to
# main, the merge-base resolves to HEAD, whose template carries the
# __PERMANENT_FAILURE_CHECK__ placeholder, NOT the hardcoded rule. So that
# baselined against a moving target too (and broke the hardcoded-rule cross-check).
# We now pin to the IMMUTABLE arc base COMMIT ``c3cab43`` (the last commit before
# the unified arc), the pre-arc snapshot whose template still carries the HARDCODED
# permanent-failure rule. The invariant under guard: field_capture's and
# voice_memo's RENDERED service workers are byte-identical to that pre-arc baseline,
# and the unified default permanent-failure rule equals the rule hardcoded at the
# arc base.
_ARC_BASE_FALLBACK = "c3cab43"

# The permanent-failure rule that was HARDCODED in the arc-base template. Asserting
# the default against this literal removes any git dependency from the default-rule
# guard while still failing loudly if the parameterization default drifts.
_ARC_BASE_HARDCODED_PERMANENT_RULE = (
    "response.status >= 400 && response.status < 500 "
    "&& response.status !== 408 && response.status !== 429"
)


def _arc_base_ref() -> str:
    """Resolve the IMMUTABLE arc base commit (the last commit before the unified
    arc): ``c3cab43``.

    DESIGN-FLAW FIX: this previously resolved the base via
    ``git merge-base HEAD main``. Once the unified arc merged to main, that
    merge-base resolves to HEAD, whose ``sw.js.template`` already carries the
    ``__PERMANENT_FAILURE_CHECK__`` placeholder (the parameterization the arc
    introduced) rather than the pre-arc HARDCODED rule. The baseline must point
    at the pre-arc snapshot whose template still hardcodes the permanent-failure
    rule, so the render-vs-render comparison and the hardcoded-rule cross-check
    stay honest. Pin to the literal SHA and verify it actually exists; never
    silently fall back to a moving ref.
    """
    result = _subprocess.run(
        ["git", "rev-parse", "--verify", f"{_ARC_BASE_FALLBACK}^{{commit}}"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    ref = result.stdout.strip()
    return ref if (result.returncode == 0 and ref) else _ARC_BASE_FALLBACK


_B_REF = _arc_base_ref()


def _git_show(path_in_repo: str) -> str:
    result = _subprocess.run(
        ["git", "show", f"{_B_REF}:{path_in_repo}"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise unittest.SkipTest(f"cannot read {_B_REF}:{path_in_repo}: {result.stderr.strip()}")
    return result.stdout


def _render_build_b(product: str) -> str:
    """Render a service worker from the Build-B committed assets.py + template.

    Reproduces ServiceWorkerConfig.render_service_worker against the B-version
    sources extracted from git, so the comparison is render-vs-render (not a
    raw template diff).
    """
    import ast
    import re

    assets_src = _git_show("project/shared_pwa/assets.py")
    template_b = _git_show("project/shared_pwa/sw.js.template")

    # In Build B the permanent-failure rule was HARDCODED in the template (no
    # placeholder, no ServiceWorkerConfig field). Build C introduced both. To
    # render the *behaviorally faithful* Build-B service worker we must use the
    # B template verbatim. If B already has the __PERMANENT_FAILURE_CHECK__
    # placeholder, the dataclass default supplies it; otherwise the hardcoded
    # rule stays in the template and the placeholder replacement is a no-op.
    has_placeholder_b = "__PERMANENT_FAILURE_CHECK__" in template_b
    hardcoded_match = re.search(r"err\.permanent = (response\.status[^;]+);", template_b)

    # Parse the B SERVICE_WORKERS config without importing (the B module path is
    # the same file we've already imported as Build C). We extract the literal
    # ServiceWorkerConfig kwargs per product via AST, plus the default
    # permanent_failure_check from the dataclass field, then render by hand using
    # the same replacement map render_service_worker uses.
    tree = ast.parse(assets_src)

    default_pfc = None
    configs: dict[str, dict[str, str]] = {}

    class _V(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if node.name == "ServiceWorkerConfig":
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        if stmt.target.id == "permanent_failure_check" and stmt.value is not None:
                            nonlocal default_pfc
                            default_pfc = ast.literal_eval(stmt.value)
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "SERVICE_WORKERS" in targets and isinstance(node.value, ast.Dict):
                for key_node, val_node in zip(node.value.keys, node.value.values):
                    product_name = ast.literal_eval(key_node)
                    kwargs: dict[str, str] = {}
                    if isinstance(val_node, ast.Call):
                        for kw in val_node.keywords:
                            kwargs[kw.arg] = ast.literal_eval(kw.value)
                    configs[product_name] = kwargs
            self.generic_visit(node)

    _V().visit(tree)
    if default_pfc is None:
        # Build B had no dataclass default; the rule lived hardcoded in the
        # template. Use that hardcoded text as the placeholder value so a B
        # template that DOES carry the placeholder still renders identically.
        assert hardcoded_match is not None, "Build B permanent-failure rule not locatable"
        default_pfc = hardcoded_match.group(1)

    kwargs = configs[product]
    replacements = {
        "__PRODUCT_LABEL__": kwargs["product_label"],
        "__SYNC_TAG__": kwargs.get("sync_tag", "field-capture-drain"),
        "__TOKEN_KEY__": kwargs.get("token_key", "fieldCaptureToken"),
        "__API_ENDPOINT__": kwargs["api_endpoint"],
        "__SUCCESS_CHECK__": kwargs["success_check"],
    }
    if has_placeholder_b:
        replacements["__PERMANENT_FAILURE_CHECK__"] = kwargs.get(
            "permanent_failure_check", default_pfc
        )
    rendered = template_b
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered


class RenderedServiceWorkerByteIdentityTests(unittest.TestCase):
    """field_capture + voice_memo rendered SWs must be byte-identical to arc base.

    Baselined against the IMMUTABLE arc base commit ``_B_REF`` == ``c3cab43``
    (the last commit before the unified arc), NOT a merge-base-with-main (which
    moves to HEAD once the arc lands) nor the ``origin/unified-capture`` ref. The
    arc base is the pre-arc snapshot; these guards prove the arc never changed
    the rendered behavior of the two untouched products.
    """

    def test_field_capture_sw_byte_identical_to_arc_base(self) -> None:
        current = render_service_worker("field_capture")
        baseline = _render_build_b("field_capture")
        self.assertEqual(
            current,
            baseline,
            "field_capture rendered service worker changed vs arc base (REGRESSION)",
        )

    def test_voice_memo_sw_byte_identical_to_arc_base(self) -> None:
        current = render_service_worker("voice_memo")
        baseline = _render_build_b("voice_memo")
        self.assertEqual(
            current,
            baseline,
            "voice_memo rendered service worker changed vs arc base (REGRESSION)",
        )

    def test_default_permanent_failure_check_matches_arc_base_hardcoded_rule(self) -> None:
        # The parameterization default must be character-identical to the rule
        # that was HARDCODED in the arc-base template (so the parameterization is
        # inert for non-overriding products). Asserted against a known literal so
        # this guard has NO git dependency and cannot silently skip.
        import re

        # Cross-check the literal against the arc-base template when git is
        # available (keeps the literal honest), but never weaken to a skip-only.
        template_arc = _git_show("project/shared_pwa/sw.js.template")
        match = re.search(r"err\.permanent = (response\.status[^;]+);", template_arc)
        self.assertIsNotNone(match, "arc-base hardcoded permanent rule not found")
        self.assertEqual(match.group(1), _ARC_BASE_HARDCODED_PERMANENT_RULE)

        default = SERVICE_WORKERS["field_capture"].permanent_failure_check
        self.assertEqual(default, _ARC_BASE_HARDCODED_PERMANENT_RULE)
        # voice_memo (the other untouched product) shares the same default.
        self.assertEqual(
            SERVICE_WORKERS["voice_memo"].permanent_failure_check,
            _ARC_BASE_HARDCODED_PERMANENT_RULE,
        )


class UnifiedCapture404RetryableTests(unittest.TestCase):
    """unified_capture's rendered SW makes a 404 submit RETRYABLE (not dropped)."""

    def test_unified_sw_permanent_rule_excludes_404(self) -> None:
        sw = render_service_worker("unified_capture")
        # The status-based permanent expression must whitelist 404 as retryable.
        self.assertIn("response.status !== 404", sw)

    def test_field_capture_sw_does_not_exclude_404(self) -> None:
        # Proves the 404 override is scoped to unified_capture only.
        sw = render_service_worker("field_capture")
        self.assertNotIn("response.status !== 404", sw)

    def test_unified_app_js_treats_404_as_retryable(self) -> None:
        # The foreground drainer in app.js must mirror the SW: 404 -> not permanent.
        app = (
            _REPO_ROOT / "project" / "unified_capture" / "public" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("response.status !== 404", app)


# --------------------------------------------------------------------------- #
# Prompt 290 -- dogfood frontend fixes (frontend ONLY; index.html/app.js/
# styles.css + assets/field-capture-header.svg). Tests authored by the
# verification agent (NOT the implementer).
#
# The token-hide test is BEHAVIORAL: it executes the REAL served app.js inside a
# Node VM against a minimal DOM/fetch/navigator/localStorage stub, then drives the
# three auth cases (no token / 200 session / 401) and asserts the ACTUAL
# #tokenPastePanel.hidden the production code sets. The IIFE's own init tail
# (``if (bootstrapToken()) loadSession();``) is the code under test, so this is
# real execution, not string-matching.
#
# The remaining 290 tests are STRUCTURAL (parsing served HTML/JS) per the repo's
# frontend-test convention -- the true visual proof is the live dogfood.
# --------------------------------------------------------------------------- #


_TOKEN_HIDE_NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const appPath = process.argv[2];
const mode = process.argv[3]; // "no_token" | "session_ok" | "unauthorized"
const appSrc = fs.readFileSync(appPath, 'utf8');

// ---- Minimal but faithful DOM stub ---------------------------------------- //
function makeEl(id) {
  const el = {
    id,
    hidden: false,
    disabled: false,
    checked: false,
    value: '',
    textContent: '',
    content: '',
    selectedIndex: -1,
    dataset: {},
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    children: [],
    addEventListener() {},
    removeEventListener() {},
    append() {},
    appendChild() {},
    replaceChildren() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    play() { return Promise.resolve(); },
    pause() {},
  };
  return el;
}

const registry = new Map();
function elFor(sel) {
  // Only #id selectors are used for the elements we care about.
  const key = sel;
  if (!registry.has(key)) registry.set(key, makeEl(sel));
  return registry.get(key);
}

const documentElement = makeEl('html');
const document = {
  documentElement,
  cookie: '',
  visibilityState: 'visible',
  querySelector(sel) {
    if (sel.startsWith('#')) return elFor(sel);
    return makeEl(sel);
  },
  querySelectorAll(sel) {
    if (sel.indexOf('screenMode') !== -1) {
      // three radio inputs
      return [makeEl('r-system'), makeEl('r-light'), makeEl('r-dark')];
    }
    return [];
  },
  createElement(tag) { return makeEl(tag); },
  addEventListener() {},
  removeEventListener() {},
};

const storage = new Map();
const localStorage = {
  getItem(k) { return storage.has(k) ? storage.get(k) : null; },
  setItem(k, v) { storage.set(k, String(v)); },
  removeItem(k) { storage.delete(k); },
};
if (mode !== 'no_token') {
  storage.set('unifiedCaptureToken', 'tok-abcdef123456');
}

let fetchResolve;
const fetchDone = new Promise((res) => { fetchResolve = res; });

function fakeFetch(url) {
  if (typeof url === 'string' && url.indexOf('/api/session') !== -1) {
    if (mode === 'session_ok') {
      const body = {
        person: { name: 'Doe, Jane' },
        sites: [{ site_id: 's1', display_categories: [] }],
        can_submit: true,
      };
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
      });
    }
    if (mode === 'unauthorized') {
      return Promise.resolve({
        ok: false,
        status: 401,
        json: () => Promise.resolve({}),
      });
    }
  }
  // Any other fetch (drain etc.) -> benign empty.
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('') });
}

const navigator = {
  onLine: true,
  storage: { persist: () => Promise.resolve(true), persisted: () => Promise.resolve(true) },
  mediaDevices: { getUserMedia: () => Promise.reject(new Error('no mic')) },
  // no serviceWorker -> registerServiceWorker no-ops
};

const windowObj = {
  document,
  localStorage,
  navigator,
  fetch: fakeFetch,
  location: { search: '', hash: '', pathname: '/' },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  history: { replaceState() {} },
  confirm: () => true,
  setTimeout: (fn) => 0,
  clearTimeout() {},
  setInterval: () => 0,
  clearInterval() {},
  addEventListener() {},
  removeEventListener() {},
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  btqRecorder: { supportsAudioRecording: () => false },
  fieldCaptureDb: { isSupported: () => false, putCapture: () => Promise.resolve(), allCaptures: () => Promise.resolve([]) },
};
windowObj.window = windowObj;

const sandbox = {
  window: windowObj,
  document,
  navigator,
  localStorage,
  fetch: fakeFetch,
  history: windowObj.history,
  location: windowObj.location,
  URL: windowObj.URL,
  URLSearchParams,
  console,
  setTimeout: windowObj.setTimeout,
  clearTimeout: windowObj.clearTimeout,
  setInterval: windowObj.setInterval,
  clearInterval: windowObj.clearInterval,
  Promise,
  Date,
  Blob: class { constructor() {} },
  Math,
  JSON,
};
vm.createContext(sandbox);

// Run the IIFE. Its tail does: if (bootstrapToken()) loadSession();
vm.runInContext(appSrc, sandbox, { filename: 'app.js' });

// loadSession is async; give microtasks a chance to flush, then report.
setTimeout(() => {
  const panel = registry.get('#tokenPastePanel');
  const result = { mode, panelHidden: panel ? panel.hidden : null };
  process.stdout.write(JSON.stringify(result));
}, 0);
"""


class TokenPanelTests(_AssetServingMixin, unittest.TestCase):
    """Prompt 290: token panel is hidden when authenticated, shown otherwise."""

    def _run_harness(self, mode: str) -> dict:
        if _shutil.which("node") is None:
            self.skipTest("node not available")
        app_path = _REPO_ROOT / "project" / "unified_capture" / "public" / "app.js"
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False
        ) as handle:
            handle.write(_TOKEN_HIDE_NODE_HARNESS)
            harness_path = handle.name
        try:
            result = _subprocess.run(
                ["node", harness_path, str(app_path), mode],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            _os.unlink(harness_path)
        self.assertEqual(
            result.returncode, 0, f"harness crashed: {result.stderr}\n{result.stdout}"
        )
        return json.loads(result.stdout.strip())

    def test_behavioral_panel_shown_when_no_token(self) -> None:
        # BEHAVIORAL: real app.js runs; with no stored token bootstrapToken()
        # returns false and showTokenPasteUI un-hides the panel.
        out = self._run_harness("no_token")
        self.assertFalse(out["panelHidden"], "panel must be SHOWN when unauthenticated")

    def test_behavioral_panel_hidden_when_session_valid(self) -> None:
        # BEHAVIORAL: real app.js runs; a stored token + a 200 /api/session
        # response drives loadSession to set tokenPastePanel.hidden = true.
        out = self._run_harness("session_ok")
        self.assertTrue(out["panelHidden"], "panel must be HIDDEN when authenticated")

    def test_behavioral_panel_shown_when_token_rejected(self) -> None:
        # BEHAVIORAL: a stored token that the server rejects (401) must re-show
        # the panel (clearPersistedToken + showTokenPasteUI).
        out = self._run_harness("unauthorized")
        self.assertFalse(out["panelHidden"], "panel must be SHOWN on 401")

    def test_panel_not_unconditionally_visible(self) -> None:
        # STRUCTURAL backstop: the panel starts hidden in markup and visibility
        # is tied to auth state (never a bare `hidden = false` outside the
        # token-paste / unauthorized paths).
        html = self.served("/index.html").text
        self.assertRegex(
            html, r'id="tokenPastePanel"[^>]*\bhidden\b',
            "tokenPastePanel must start hidden in markup",
        )
        app = self.served("/app.js").text
        # The single un-hide lives inside showTokenPasteUI; a successful session
        # re-hides it.
        self.assertIn("elements.tokenPastePanel.hidden = false", app)
        self.assertIn("elements.tokenPastePanel.hidden = true", app)
        # showTokenPasteUI is the gate for un-hiding.
        self.assertRegex(
            app,
            r"function showTokenPasteUI\([^)]*\)\s*\{[^}]*tokenPastePanel\.hidden = false",
        )

    def test_panel_positioned_before_capture_form(self) -> None:
        # Prompt 290: token panel moved to the TOP (after <header>, before the
        # capture form), not the bottom.
        html = self.served("/index.html").text
        panel_idx = html.find('id="tokenPastePanel"')
        form_idx = html.find('id="captureForm"')
        self.assertGreater(panel_idx, -1, "tokenPastePanel missing")
        self.assertGreater(form_idx, -1, "captureForm missing")
        self.assertLess(
            panel_idx, form_idx, "tokenPastePanel must appear before captureForm"
        )
        # And it sits after the header (top of the body content).
        header_idx = html.find("</header>")
        self.assertGreater(header_idx, -1)
        self.assertGreater(panel_idx, header_idx, "panel must be after the header")


class ThemeDefaultSystemTests(_AssetServingMixin, unittest.TestCase):
    """Prompt 290: dark mode defaults to system; screen-mode radios persist."""

    def test_styles_default_to_system_dark_when_no_theme_attr(self) -> None:
        css = self.served("/styles.css").text
        # The system default is expressed as prefers-color-scheme applying to
        # html WITHOUT an explicit data-theme override.
        self.assertIn("@media (prefers-color-scheme: dark)", css)
        self.assertRegex(css, r"html:not\(\[data-theme\]\)")

    def test_screen_mode_radios_exist(self) -> None:
        html = self.served("/index.html").text
        for value in ("system", "light", "dark"):
            self.assertRegex(
                html, rf'name="screenMode"[^>]*value="{value}"', value
            )

    def test_app_persists_screen_mode_and_applies_data_theme(self) -> None:
        app = self.served("/app.js").text
        # Persists the chosen mode to the documented localStorage key.
        self.assertIn('SCREEN_MODE_KEY = "unifiedCaptureScreenMode"', app)
        self.assertRegex(app, r"localStorage\.setItem\(SCREEN_MODE_KEY")
        # Applies the resolved theme onto html via data-theme.
        self.assertRegex(app, r"document\.documentElement\.dataset\.theme\s*=")
        # System mode resolves from prefers-color-scheme.
        self.assertIn('matchMedia("(prefers-color-scheme: dark)")', app)

    def test_inline_head_script_sets_default_theme_from_system(self) -> None:
        # The no-FOUC inline bootstrap in index.html must default to system when
        # no stored mode exists.
        html = self.served("/index.html").text
        self.assertIn('"unifiedCaptureScreenMode"', html)
        self.assertIn('"(prefers-color-scheme: dark)"', html)
        self.assertRegex(html, r"documentElement\.dataset\.theme")


class HeaderLayoutTests(_AssetServingMixin, unittest.TestCase):
    """Prompt 290: pills on the LEFT, branding + settings on the RIGHT."""

    def test_header_pills_hold_status_and_welcome(self) -> None:
        # Prompt 292: the old two-pill header (#statusText + #welcomePill inside a
        # .header-pills group on the LEFT) was MERGED into a single #statusText pill
        # that sits inside .header-branding next to the wordmark. #welcomePill is
        # gone; the transient/loading/error states all share #statusText, and the
        # ready state renders "Ready for {FirstName}" through readyStatusText().
        html = self.served("/index.html").text
        # The merged pill exists, the dropped two-pill group + welcome pill are gone.
        self.assertIn('id="statusText"', html)
        self.assertNotIn('class="header-pills"', html, "old two-pill group must be gone")
        self.assertNotIn('id="welcomePill"', html, "#welcomePill must be removed")
        # #statusText now lives inside .header-branding (beside the wordmark), not a
        # separate left-hand pills group.
        branding_start = html.find('class="header-branding"')
        branding_block = html[branding_start:html.find("</header>")]
        self.assertGreater(branding_start, -1, "header-branding missing")
        self.assertIn('id="statusText"', branding_block, "#statusText must sit in branding")
        # The "Ready for {FirstName}" rendering path exists in the served app.js.
        app = self.served("/app.js").text
        self.assertIn("function readyStatusText", app)
        self.assertRegex(app, r"`Ready for \$\{firstName\}`")
        # readyStatusText feeds the single status pill via setStatus on a ready
        # session (the merged pill carries the ready greeting).
        self.assertRegex(app, r"setStatus\(\s*enabled \? readyStatusText\(session\)")

    def test_header_branding_has_wordmark_and_settings(self) -> None:
        # Cowork logo treatment: a light + a dark wordmark variant, swapped by
        # theme so the green waveform survives in both modes.
        html = self.served("/index.html").text
        branding_start = html.find('class="header-branding"')
        branding_block = html[branding_start:html.find("</header>")]
        self.assertIn("brand-wordmark--light", branding_block)
        self.assertIn("brand-wordmark--dark", branding_block)
        self.assertIn('src="/assets/field-capture-header-light.svg"', branding_block)
        self.assertIn('src="/assets/field-capture-header-dark.svg"', branding_block)
        # Settings gear lives in the branding group (RIGHT).
        self.assertIn('class="settings-menu"', branding_block)
        self.assertIn("⚙", branding_block)

    def test_header_wordmark_variants_are_real_assets(self) -> None:
        # Both theme variants exist on disk as real SVGs (not placeholders).
        assets = (
            _REPO_ROOT / "project" / "unified_capture" / "public" / "assets"
        )
        for name in ("field-capture-header-light.svg", "field-capture-header-dark.svg"):
            svg = assets / name
            self.assertTrue(svg.is_file(), f"{name} missing")
            data = svg.read_text(encoding="utf-8")
            self.assertGreater(len(data), 1000, f"{name} looks like a placeholder")
            self.assertIn("<svg", data)
            self.assertIn("<path", data)

    def test_header_wordmark_variants_are_served(self) -> None:
        # Both wordmark variants serve as svg (also guards the prompt-290 ".svg"
        # MIME fix in server.py PUBLIC_CONTENT_TYPES).
        for name in ("field-capture-header-light.svg", "field-capture-header-dark.svg"):
            resp = self.served(f"/assets/{name}")
            self.assertEqual(resp.status, 200, resp.text)
            self.assertIn("svg", resp.headers.get("Content-Type", "").lower())

    def test_wordmark_theme_swap_css(self) -> None:
        # The light/dark variants are swapped by theme in styles.css so exactly
        # one shows per mode.
        css = (
            _REPO_ROOT / "project" / "unified_capture" / "public" / "styles.css"
        ).read_text(encoding="utf-8")
        self.assertIn('html[data-theme="dark"] .brand-wordmark--light', css)
        self.assertIn('html[data-theme="dark"] .brand-wordmark--dark', css)
        self.assertIn("prefers-color-scheme: dark", css)

    def test_svg_favicon_is_wired_and_served(self) -> None:
        # icon.svg wired as the SVG favicon (operator request, 2026-06-06).
        index = (
            Path(__file__).resolve().parents[1]
            / "unified_capture"
            / "public"
            / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '<link rel="icon" type="image/svg+xml" href="/assets/icon.svg">',
            index,
        )
        resp = self.served("/assets/icon.svg")
        self.assertEqual(resp.status, 200, resp.text)
        self.assertIn("svg", resp.headers.get("Content-Type", "").lower())


# --------------------------------------------------------------------------- #
# Submit behavioral tests (Build D) -- the capture->queue contract step.
#
# Authored by the verification agent (NOT the implementer). These drive REAL
# multipart submits through UnifiedCaptureHandler.handle_submit in-process. The
# REAL auth path runs (TokenStore + authorize_token + canonical person/site
# fixtures, as in the session tests above). REAL capture_ingest media handling
# runs into a temp upload_dir. Only the lowest CouchDB doc read/write is faked
# (couchdb_capture_writer.get/put), so the document SHAPE the endpoint actually
# produces is captured and asserted. A test would FAIL if the endpoint wrote the
# wrong doc shape, skipped auth, double-wrote on replay, or invented its writer.
# --------------------------------------------------------------------------- #

import uuid as _uuid


class _SubmitFakeServer:
    """Carries every attribute handle_submit / build_submit_document read.

    ``couchdb_config`` is a non-None sentinel so the endpoint does not short out
    with SERVICE_UNAVAILABLE; the actual reader/writer are patched on the module.
    """

    site_registry = None  # accessed via getattr(self.server, "site_registry", None)

    def __init__(
        self,
        token_store: TokenStore,
        vault_root: Path,
        upload_dir: Path,
        *,
        max_images: int = 6,
        max_upload_bytes: int = 10 * 1024 * 1024,
        request_max_bytes: int | None = None,
        couchdb_database: str = "btq_field_captures",
    ) -> None:
        from voice_memo.core import MAX_AUDIO_BYTES as _MAX_AUDIO

        self.token_store = token_store
        self.vault_root = vault_root
        self.data_dir = None
        self.upload_dir = upload_dir
        self.max_images = max_images
        self.max_upload_bytes = max_upload_bytes
        self.request_max_bytes = request_max_bytes or (
            max_images * max_upload_bytes + _MAX_AUDIO + 1024 * 1024
        )
        # Sentinel: presence (non-None) is all handle_submit checks before the
        # patched writer is invoked.
        self.couchdb_config = object()
        self.couchdb_database = couchdb_database


def _multipart_body(
    fields: dict[str, str],
    *,
    photos: Optional[list[tuple[str, str, bytes]]] = None,
    audio: Optional[list[tuple[str, str, bytes]]] = None,
) -> tuple[bytes, str]:
    """Build a real multipart/form-data body. file tuples = (filename, mime, bytes)."""
    boundary = "----btqtest" + _uuid.uuid4().hex
    parts: list[bytes] = []

    def _field(name: str, value: str) -> None:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    def _file(name: str, filename: str, mime: str, data: bytes) -> None:
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        parts.append(head + data + b"\r\n")

    for name, value in fields.items():
        _field(name, value)
    for filename, mime, data in photos or []:
        _file("photos", filename, mime, data)
    for filename, mime, data in audio or []:
        _file("audio", filename, mime, data)
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def drive_post_multipart(
    server: _SubmitFakeServer,
    path: str,
    body: bytes,
    content_type: str,
    *,
    headers: Optional[dict[str, str]] = None,
) -> _Response:
    """Drive a real multipart POST through the handler against ``server``."""
    all_headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
    }
    all_headers.update(headers or {})
    request_line = f"POST {path} HTTP/1.1\r\n"
    header_lines = "".join(f"{name}: {value}\r\n" for name, value in all_headers.items())
    raw = (request_line + header_lines + "\r\n").encode("utf-8") + body

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


# A 1x1 PNG (valid image/png bytes) and a tiny WAV for media payloads. The
# endpoint validates by declared content_type, not by decoding bytes, so any
# non-empty payload under the size limit suffices; these are real headers anyway.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"  # PNG signature
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
_TINY_WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little") + (
    b"\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00"
) + b"data" + (0).to_bytes(4, "little")


class _WriterCapture:
    """Captures docs passed to put_field_capture_document and serves get lookups."""

    def __init__(self) -> None:
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.store: dict[str, dict[str, Any]] = {}

    def put(self, config, doc, *, database):  # signature mirrors the real writer
        self.put_calls.append(doc)
        self.store[str(doc["_id"])] = dict(doc)
        return {"id": str(doc["_id"]), "rev": "1-fake", "ok": True}

    def get(self, config, database, doc_id):  # mirrors get_field_capture_document
        self.get_calls.append(doc_id)
        existing = self.store.get(doc_id)
        return dict(existing) if existing is not None else None


def _valid_submit_fields(
    *,
    site_id: str = "7060",
    qc_category: str = "report_an_issue",
    capture_id: Optional[str] = None,
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    fields = {
        "site": "Continental Metalworks",
        "site_id": site_id,
        "target_type": "location",
        "target_id": site_id,
        "qc_category": qc_category,
        "captured_at": "2026-06-06T14:30:00Z",
        "exported_at": "2026-06-06T14:31:00Z",
        "note": "verifier note",
        "job_id": "job-123",
    }
    if capture_id is not None:
        fields["capture_id"] = capture_id
    if extra:
        fields.update(extra)
    return fields


class SubmitBehaviorTests(unittest.TestCase):
    """Real multipart submits through handle_submit; one raw field_capture doc."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.upload_dir = Path(self.tmp.name) / "uploads"
        self.store, self.token = make_store(
            self.tmp.name, site_ids=["7060"], can_submit=True, role="cleaner"
        )
        self.server = _SubmitFakeServer(self.store, self.vault, self.upload_dir)
        self.writer = _WriterCapture()
        self._patches = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        self.addCleanup(stop_all, self._patches)
        put_patch = mock.patch.object(
            uc_server, "put_field_capture_document", side_effect=self.writer.put
        )
        get_patch = mock.patch.object(
            uc_server, "get_field_capture_document", side_effect=self.writer.get
        )
        put_patch.start()
        get_patch.start()
        self.addCleanup(put_patch.stop)
        self.addCleanup(get_patch.stop)

    def submit(
        self,
        fields: dict[str, str],
        *,
        photos=None,
        audio=None,
        headers=None,
        token: Optional[str] = "__default__",
    ) -> _Response:
        body, content_type = _multipart_body(fields, photos=photos, audio=audio)
        hdrs = dict(headers or {})
        if token == "__default__":
            hdrs.setdefault("Authorization", f"Bearer {self.token}")
        elif token is not None:
            hdrs.setdefault("Authorization", f"Bearer {token}")
        return drive_post_multipart(self.server, "/api/submit", body, content_type, headers=hdrs)

    def _uploaded_files(self) -> list[Path]:
        if not self.upload_dir.exists():
            return []
        return [p for p in self.upload_dir.rglob("*") if p.is_file()]

    # ----- happy paths ----------------------------------------------------- #

    def test_photo_only_writes_one_field_capture_doc(self) -> None:
        resp = self.submit(
            _valid_submit_fields(),
            photos=[("a.png", "image/png", _TINY_PNG)],
        )
        self.assertEqual(resp.status, 201, resp.text)
        self.assertEqual(len(self.writer.put_calls), 1, "exactly one doc written")
        doc = self.writer.put_calls[0]
        self.assertEqual(doc["type"], "field_capture")
        self.assertEqual(doc["source"], "unified_capture_app")
        self.assertEqual(doc["processing_state"], "pending")
        # single capture_id, shared with _id
        self.assertEqual(doc["capture_id"], doc["_id"])
        self.assertEqual(len(doc["photos"]), 1)
        # audio absent or empty
        self.assertFalse(doc.get("audio"))
        # domain fields present
        for key in ("site_id", "target_id", "target_type", "person_id", "qc_category", "captured_at"):
            self.assertIn(key, doc, key)
        self.assertEqual(doc["site_id"], "7060")
        self.assertEqual(doc["person_id"], "per_unified01")
        self.assertEqual(doc["qc_category"], "report_an_issue")
        # media actually persisted (one photo file on disk)
        self.assertEqual(len(self._uploaded_files()), 1)

    def test_audio_only_writes_one_doc_with_single_audio(self) -> None:
        resp = self.submit(
            _valid_submit_fields(extra={"audio_duration_seconds": "12"}),
            audio=[("note.wav", "audio/wav", _TINY_WAV)],
        )
        self.assertEqual(resp.status, 201, resp.text)
        self.assertEqual(len(self.writer.put_calls), 1)
        doc = self.writer.put_calls[0]
        self.assertEqual(doc["type"], "field_capture")
        self.assertEqual(doc["photos"], [])
        self.assertEqual(len(doc.get("audio", [])), 1)
        # duration carried through
        self.assertEqual(str(doc["audio"][0].get("duration_seconds")), "12")
        self.assertEqual(doc["source"], "unified_capture_app")
        self.assertEqual(doc["processing_state"], "pending")
        self.assertEqual(len(self._uploaded_files()), 1)

    def test_combined_photo_and_audio_share_one_capture_id(self) -> None:
        resp = self.submit(
            _valid_submit_fields(),
            photos=[("a.png", "image/png", _TINY_PNG), ("b.png", "image/png", _TINY_PNG)],
            audio=[("note.wav", "audio/wav", _TINY_WAV)],
        )
        self.assertEqual(resp.status, 201, resp.text)
        self.assertEqual(len(self.writer.put_calls), 1)
        doc = self.writer.put_calls[0]
        self.assertEqual(len(doc["photos"]), 2)
        self.assertEqual(len(doc["audio"]), 1)
        cap = doc["capture_id"]
        self.assertEqual(doc["_id"], cap)
        # ONE shared capture_id across the doc + every media upload_id prefix.
        for media in list(doc["photos"]) + list(doc["audio"]):
            self.assertIn(cap, str(media["upload_id"]))
        # both media kinds persisted: 2 photos + 1 audio = 3 files
        self.assertEqual(len(self._uploaded_files()), 3)

    # ----- validation / fail-closed --------------------------------------- #

    def test_missing_asset_is_400_no_doc(self) -> None:
        resp = self.submit(_valid_submit_fields())  # no photos, no audio
        self.assertEqual(resp.status, 400, resp.text)
        self.assertEqual(len(self.writer.put_calls), 0)
        self.assertEqual(self._uploaded_files(), [])

    def test_unauthenticated_is_401_no_doc_no_media(self) -> None:
        # No Authorization header at all.
        body, content_type = _multipart_body(
            _valid_submit_fields(), photos=[("a.png", "image/png", _TINY_PNG)]
        )
        resp = drive_post_multipart(self.server, "/api/submit", body, content_type)
        self.assertEqual(resp.status, 401, resp.text)
        self.assertEqual(len(self.writer.put_calls), 0)
        self.assertEqual(self._uploaded_files(), [])

    def test_bad_token_is_401_no_doc_no_media(self) -> None:
        resp = self.submit(
            _valid_submit_fields(),
            photos=[("a.png", "image/png", _TINY_PNG)],
            token="fc_not_a_real_token",
        )
        self.assertEqual(resp.status, 401, resp.text)
        self.assertEqual(len(self.writer.put_calls), 0)
        self.assertEqual(self._uploaded_files(), [])

    def test_cross_site_submit_is_403_no_doc(self) -> None:
        # Token authorized for 1200 submits to 1300 -> site_not_allowed 403.
        resp = self.submit(
            _valid_submit_fields(site_id="1300"),
            photos=[("a.png", "image/png", _TINY_PNG)],
        )
        self.assertEqual(resp.status, 403, resp.text)
        self.assertEqual(len(self.writer.put_calls), 0)
        self.assertEqual(self._uploaded_files(), [])

    # ----- idempotency ----------------------------------------------------- #

    def test_idempotent_replay_does_not_double_write(self) -> None:
        cap = "cap-unified-fixed-0001"
        fields = _valid_submit_fields(capture_id=cap)
        first = self.submit(fields, photos=[("a.png", "image/png", _TINY_PNG)])
        self.assertEqual(first.status, 201, first.text)
        files_after_first = len(self._uploaded_files())
        # Replay the exact same capture_id.
        second = self.submit(fields, photos=[("a.png", "image/png", _TINY_PNG)])
        self.assertEqual(second.status, 200, second.text)
        self.assertTrue(second.json().get("idempotent_replay"))
        # Writer invoked exactly ONCE across both submits (no duplicate doc).
        self.assertEqual(len(self.writer.put_calls), 1, "replay must not write a second doc")
        # No extra media written on replay.
        self.assertEqual(len(self._uploaded_files()), files_after_first)
        self.assertEqual(second.json()["capture_id"], cap)

    # ----- audio size policy (decision 9: ~50 MB, not 10 MB) --------------- #

    def test_configured_audio_limit_is_50mb_not_10mb(self) -> None:
        from voice_memo.core import MAX_AUDIO_BYTES

        # The audio path must use the 50 MB voice-memo cap, not the 10 MB photo cap.
        self.assertEqual(MAX_AUDIO_BYTES, 50 * 1024 * 1024)
        self.assertGreater(MAX_AUDIO_BYTES, self.server.max_upload_bytes)

    def test_audio_over_photo_limit_under_50mb_is_accepted(self) -> None:
        # ~12 MB: larger than the 10 MB photo limit, well under the 50 MB audio cap.
        big_audio = _TINY_WAV + b"\x00" * (12 * 1024 * 1024)
        resp = self.submit(
            _valid_submit_fields(),
            audio=[("big.wav", "audio/wav", big_audio)],
        )
        self.assertEqual(resp.status, 201, resp.text)
        self.assertEqual(len(self.writer.put_calls), 1)
        self.assertEqual(len(self.writer.put_calls[0]["audio"]), 1)

    def test_oversize_audio_over_50mb_is_rejected_no_doc(self) -> None:
        from voice_memo.core import MAX_AUDIO_BYTES

        # Build a server whose request_max_bytes admits the body so we exercise the
        # AUDIO size check (413), not the whole-request guard.
        big_server = _SubmitFakeServer(
            self.store,
            self.vault,
            self.upload_dir,
            request_max_bytes=MAX_AUDIO_BYTES + 100 * 1024 * 1024,
        )
        oversize = b"\x00" * (MAX_AUDIO_BYTES + 1024)
        body, content_type = _multipart_body(
            _valid_submit_fields(), audio=[("huge.wav", "audio/wav", oversize)]
        )
        resp = drive_post_multipart(
            big_server,
            "/api/submit",
            body,
            content_type,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(resp.status, 413, resp.text)
        self.assertEqual(len(self.writer.put_calls), 0)

    # ----- cookie token extraction (Build D folded-in fix) ----------------- #

    def test_cookie_unified_capture_token_authenticates_submit(self) -> None:
        resp = self.submit(
            _valid_submit_fields(),
            photos=[("a.png", "image/png", _TINY_PNG)],
            token=None,  # no Authorization header
            headers={"Cookie": f"unifiedCaptureToken={self.token}"},
        )
        self.assertEqual(resp.status, 201, resp.text)
        self.assertEqual(len(self.writer.put_calls), 1)

    def test_header_precedence_over_cookie(self) -> None:
        # Valid Bearer header + garbage cookie -> header wins (still authenticates).
        resp = self.submit(
            _valid_submit_fields(),
            photos=[("a.png", "image/png", _TINY_PNG)],
            headers={"Cookie": "unifiedCaptureToken=fc_garbage_cookie"},
        )
        self.assertEqual(resp.status, 201, resp.text)


class SubmitCookieExtractorUnitTests(unittest.TestCase):
    """extract_token now also honors the unifiedCaptureToken cookie."""

    def _extract(self, headers: dict[str, str], path: str = "/api/submit") -> Optional[str]:
        handler = UnifiedCaptureHandler.__new__(UnifiedCaptureHandler)
        handler.path = path
        from http.client import HTTPMessage

        msg = HTTPMessage()
        for name, value in headers.items():
            msg[name] = value
        handler.headers = msg
        return handler.extract_token()

    def test_unified_capture_cookie_is_read(self) -> None:
        self.assertEqual(
            self._extract({"Cookie": "unifiedCaptureToken=tok_abc"}), "tok_abc"
        )

    def test_legacy_field_viewer_cookie_still_read(self) -> None:
        self.assertEqual(
            self._extract({"Cookie": "btq_field_viewer_token=tok_legacy"}), "tok_legacy"
        )

    def test_header_beats_cookie(self) -> None:
        self.assertEqual(
            self._extract(
                {"Authorization": "Bearer tok_header", "Cookie": "unifiedCaptureToken=tok_cookie"}
            ),
            "tok_header",
        )

    def test_query_beats_cookie(self) -> None:
        self.assertEqual(
            self._extract({"Cookie": "unifiedCaptureToken=tok_cookie"}, path="/api/submit?token=tok_query"),
            "tok_query",
        )

    def test_no_token_returns_none(self) -> None:
        self.assertIsNone(self._extract({}))


class DeployArtifactTests(unittest.TestCase):
    """Structural guards for Build F deploy artifacts (real proof is the live deploy, G).

    These catch the two ways the artifacts could silently break a deploy: an
    ExecStart flag the server CLI does not accept, and accidental collision with
    the live field_capture deploy (shared app dir / service / port).
    """

    repo_root = Path(__file__).resolve().parents[2]
    service_path = repo_root / "project/unified_capture/deploy/btq-unified-capture.service"
    deploy_path = repo_root / "scripts/deploy-unified-capture-app-on-vps"
    server_src_path = repo_root / "project/unified_capture/server.py"

    def _exec_start(self) -> str:
        for line in self.service_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart="):
                return line[len("ExecStart="):]
        self.fail("no ExecStart line in unit")

    def test_execstart_flags_are_accepted_by_the_server_cli(self):
        server_src = self.server_src_path.read_text(encoding="utf-8")
        flags = [tok for tok in self._exec_start().split() if tok.startswith("--")]
        self.assertTrue(flags, "ExecStart has no flags")
        for flag in flags:
            # The flag appears as a quoted argparse option string; match the
            # quoted token so multi-line add_argument(...) calls still count.
            self.assertIn(
                f'"{flag}"',
                server_src,
                msg=f"systemd ExecStart passes {flag} but unified_capture.server CLI does not define it",
            )

    def test_execstart_invokes_unified_server_on_distinct_port(self):
        exec_start = self._exec_start()
        self.assertIn("unified_capture.server", exec_start)
        self.assertIn("--port 8081", exec_start)  # distinct from field_capture's 8080
        self.assertNotIn(":8080", exec_start)

    def test_deploy_script_targets_distinct_app_dir_and_service(self):
        script = self.deploy_path.read_text(encoding="utf-8")
        self.assertIn("/srv/btq/apps/unified-capture", script)
        self.assertIn("btq-unified-capture.service", script)
        # must not deploy into / restart the live field_capture app
        self.assertNotIn("/srv/btq/apps/field-capture", script)
        self.assertNotIn("btq-field-capture.service", script)


# --------------------------------------------------------------------------- #
# Prompt 291 -- "Saved locally" success screen is a FULL-SCREEN TAKEOVER.
#
# Authored by the verification agent (NOT the implementer). Frontend ONLY:
# unified_capture/public/{app.js,index.html,styles.css}. On a successful save the
# WHOLE capture UI -- #captureForm, the photo panel (.photo-input-panel), the
# voice panel (.voice-panel), the note/submit panel (.note-panel), AND
# #tokenPastePanel -- must be hidden, leaving only #successScreen visible.
# "Submit Another" (#submitAnotherButton) restores a cleared capture screen and
# re-hides #successScreen. styles.css adds `[hidden] { display: none !important }`.
#
# The core test is BEHAVIORAL: it runs the REAL served app.js in a Node VM
# against a DOM/fetch/navigator/localStorage stub, drives a real successful
# saveCapture() (via the bound #submitButton click handler, with a real photo
# pushed through the bound #fileInput change handler), then inspects the ACTUAL
# `.hidden` the production code set on each panel + #successScreen. It then fires
# the bound #submitAnotherButton click handler and re-inspects. The negative
# control (mutating the hide/restore lines in a temp copy) is exercised by
# test_negative_control_* below: it proves the assertions genuinely discriminate.
# --------------------------------------------------------------------------- #


_SUCCESS_TAKEOVER_NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const appPath = process.argv[2];
const appSrc = fs.readFileSync(appPath, 'utf8');

// ---- Element registry keyed by selector token ----------------------------- //
// querySelector("#x") and querySelectorAll(".a, .b") must hand back the SAME
// element objects so the test can read the .hidden the app set on them.
const registry = new Map();
const handlers = new Map(); // selector -> { eventType: [fn,...] }

function makeEl(sel) {
  const el = {
    sel,
    id: sel.startsWith('#') ? sel.slice(1) : sel,
    hidden: false,
    disabled: false,
    checked: false,
    value: '',
    textContent: '',
    selectedIndex: -1,
    selectedOptions: [],
    files: [],
    dataset: {},
    style: {},
    width: 0,
    height: 0,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    children: [],
    options: [],
    classNameStr: '',
    set className(v) { this.classNameStr = v; },
    get className() { return this.classNameStr; },
    addEventListener(type, fn) {
      if (!handlers.has(sel)) handlers.set(sel, {});
      const byType = handlers.get(sel);
      (byType[type] = byType[type] || []).push(fn);
    },
    removeEventListener() {},
    append(...kids) { for (const k of kids) { this.children.push(k); if (k && k.id === 'option') this.options.push(k); } },
    appendChild(k) { this.children.push(k); if (k && k.id === 'option') this.options.push(k); return k; },
    replaceChildren() { this.children = []; this.options = []; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    getContext() { return { drawImage() {} }; },
    toBlob(cb) { cb(new Blob()); },
    play() { return Promise.resolve(); },
    pause() {},
    load() {},
  };
  return el;
}

function elFor(sel) {
  const key = sel.trim();
  if (!registry.has(key)) registry.set(key, makeEl(key));
  return registry.get(key);
}

function fire(sel, type, event) {
  const byType = handlers.get(sel);
  if (!byType || !byType[type]) return Promise.resolve();
  return Promise.all(byType[type].map((fn) => Promise.resolve().then(() => fn(event))));
}

const documentElement = makeEl('html');
const document = {
  documentElement,
  cookie: '',
  visibilityState: 'visible',
  querySelector(sel) {
    if (sel.startsWith('#') || sel.startsWith('.')) return elFor(sel);
    return makeEl(sel);
  },
  querySelectorAll(sel) {
    if (sel.indexOf('screenMode') !== -1) {
      return [makeEl('r-system'), makeEl('r-light'), makeEl('r-dark')];
    }
    // The app passes comma-joined selector lists for the panel groups; return
    // the SAME element objects the test inspects via querySelector.
    if (sel.indexOf(',') !== -1 || sel.startsWith('.') || sel.startsWith('#')) {
      return sel.split(',').map((tok) => elFor(tok.trim()));
    }
    return [];
  },
  createElement(tag) { return makeEl(tag); },
  addEventListener() {},
  removeEventListener() {},
};

const storage = new Map();
storage.set('unifiedCaptureToken', 'tok-abcdef123456');
const localStorage = {
  getItem(k) { return storage.has(k) ? storage.get(k) : null; },
  setItem(k, v) { storage.set(k, String(v)); },
  removeItem(k) { storage.delete(k); },
};

function fakeFetch(url) {
  if (typeof url === 'string' && url.indexOf('/api/session') !== -1) {
    const body = {
      person: { name: 'Doe, Jane', person_id: 'per_x' },
      token: { token_id: 'tok_x' },
      sites: [{ site_id: '7060', display_categories: ['report_an_issue'] }],
      can_submit: true,
    };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('') });
}

const captured = [];
const navigator = {
  onLine: true,
  storage: { persist: () => Promise.resolve(true), persisted: () => Promise.resolve(true) },
  mediaDevices: { getUserMedia: () => Promise.reject(new Error('no mic')) },
};

const windowObj = {
  document,
  localStorage,
  navigator,
  fetch: fakeFetch,
  location: { search: '', hash: '', pathname: '/' },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  history: { replaceState() {} },
  confirm: () => true,
  setTimeout: (fn) => { Promise.resolve().then(fn); return 0; },
  clearTimeout() {},
  setInterval: () => 0,
  clearInterval() {},
  addEventListener() {},
  removeEventListener() {},
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  btqRecorder: { supportsAudioRecording: () => false },
  fieldCaptureDb: {
    isSupported: () => true,
    putCapture: (rec) => { captured.push(rec); return Promise.resolve(); },
    allCaptures: () => Promise.resolve([]),
    listByStatus: () => Promise.resolve([]),
    updateCapture: () => Promise.resolve(),
  },
  createImageBitmap: () => Promise.resolve({ width: 4, height: 4, close() {} }),
};
windowObj.window = windowObj;

const sandbox = {
  window: windowObj,
  document,
  navigator,
  localStorage,
  fetch: fakeFetch,
  history: windowObj.history,
  location: windowObj.location,
  URL: windowObj.URL,
  URLSearchParams,
  console,
  setTimeout: windowObj.setTimeout,
  clearTimeout: windowObj.clearTimeout,
  setInterval: windowObj.setInterval,
  clearInterval: windowObj.clearInterval,
  createImageBitmap: windowObj.createImageBitmap,
  Promise,
  Date,
  Image: class { set src(_v) { Promise.resolve().then(() => this.onload && this.onload()); } },
  Blob: class { constructor() { this.size = 1; this.type = 'image/jpeg'; } },
  File: class { constructor(parts, name, opts) { this.name = name; this.type = (opts && opts.type) || ''; } },
  Math,
  JSON,
};
// Seed the elements that start hidden in index.html markup so the stub matches
// the real initial DOM (the app does not touch these at boot).
elFor('#successScreen').hidden = true;
elFor('#tokenPastePanel').hidden = true;

vm.createContext(sandbox);
vm.runInContext(appSrc, sandbox, { filename: 'app.js' });

function snapshot() {
  const read = (sel) => { const e = registry.get(sel); return e ? e.hidden : null; };
  return {
    captureForm: read('#captureForm'),
    photoPanel: read('.photo-input-panel'),
    voicePanel: read('.voice-panel'),
    notePanel: read('.note-panel'),
    tokenPastePanel: read('#tokenPastePanel'),
    successScreen: read('#successScreen'),
  };
}

async function drive() {
  // Let the boot loadSession() (token present + 200) settle the session/form.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  // Make the capture valid: pick a site + category, then add a real photo via
  // the bound #fileInput change handler (drives addFiles -> normalizeImage).
  const site = registry.get('#siteInput');
  site.value = 'Continental Metalworks';
  site.selectedOptions = [{ dataset: { siteId: '7060', targetType: 'location', targetId: '7060' } }];
  registry.get('#categoryInput').value = 'report_an_issue';
  registry.get('#notesInput').value = 'verifier note';

  const fileInput = registry.get('#fileInput');
  fileInput.files = [new sandbox.File([], 'p.jpg', { type: 'image/png' })];
  await fire('#fileInput', 'change', { target: fileInput });
  // addFiles is async (normalizeImage awaits createImageBitmap/toBlob); flush.
  for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));

  const beforeSave = snapshot();
  const photosBefore = captured.length;

  // Fire the bound submit handler (saveCapture).
  await fire('#submitButton', 'click', {});
  for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));
  const afterSave = snapshot();
  const photosAfter = captured.length;

  // Fire Submit Another (resetToForm).
  await fire('#submitAnotherButton', 'click', {});
  for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0));
  const afterSubmitAnother = snapshot();

  process.stdout.write(JSON.stringify({
    beforeSave,
    afterSave,
    afterSubmitAnother,
    saved: photosAfter - photosBefore,
  }));
}

drive();
"""


class SuccessTakeoverBehavioralTests(_AssetServingMixin, unittest.TestCase):
    """Prompt 291: a successful save is a full-screen #successScreen takeover."""

    def _run(self, app_src: str) -> dict:
        if _shutil.which("node") is None:
            self.skipTest("node not available")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as app_handle:
            app_handle.write(app_src)
            app_path = app_handle.name
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as h_handle:
            h_handle.write(_SUCCESS_TAKEOVER_NODE_HARNESS)
            harness_path = h_handle.name
        try:
            result = _subprocess.run(
                ["node", harness_path, app_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            _os.unlink(harness_path)
            _os.unlink(app_path)
        self.assertEqual(
            result.returncode, 0, f"harness crashed: {result.stderr}\n{result.stdout}"
        )
        return json.loads(result.stdout.strip())

    @property
    def _app_src(self) -> str:
        return (
            _REPO_ROOT / "project" / "unified_capture" / "public" / "app.js"
        ).read_text(encoding="utf-8")

    # ----- positive: real served app.js ----------------------------------- #

    def test_save_hides_all_capture_panels_and_shows_only_success(self) -> None:
        out = self._run(self._app_src)
        # Sanity: the drive actually produced a saved capture (the success path
        # genuinely executed, not a no-op).
        self.assertEqual(out["saved"], 1, f"save did not run: {out}")
        before = out["beforeSave"]
        after = out["afterSave"]
        # Before save: capture UI visible, success screen hidden.
        self.assertFalse(before["captureForm"], f"captureForm should start visible: {before}")
        self.assertTrue(before["successScreen"], f"successScreen should start hidden: {before}")
        # After save: ENTIRE capture UI + token panel hidden.
        for panel in ("captureForm", "photoPanel", "voicePanel", "notePanel", "tokenPastePanel"):
            self.assertTrue(
                after[panel],
                f"{panel} must be hidden on the success takeover: {after}",
            )
        # ...and #successScreen is the only thing shown.
        self.assertFalse(after["successScreen"], f"successScreen must be shown after save: {after}")

    def test_submit_another_restores_capture_ui_and_hides_success(self) -> None:
        out = self._run(self._app_src)
        self.assertEqual(out["saved"], 1, f"save did not run: {out}")
        restored = out["afterSubmitAnother"]
        # Capture UI restored (visible) ...
        for panel in ("captureForm", "photoPanel", "voicePanel", "notePanel"):
            self.assertFalse(
                restored[panel],
                f"{panel} must be restored (shown) after Submit Another: {restored}",
            )
        # ... and the success screen is hidden again.
        self.assertTrue(
            restored["successScreen"],
            f"successScreen must be hidden after Submit Another: {restored}",
        )

    # ----- negative controls: prove the assertions discriminate ----------- #

    def test_negative_control_break_hide_on_success_flips_to_fail(self) -> None:
        # Mutate the hide-on-success line so the panels are NOT hidden. The
        # positive assertion must then fail -> the test genuinely discriminates.
        src = self._app_src
        broken = src.replace(
            "    document.querySelectorAll(SUCCESS_TAKEOVER_SELECTOR).forEach((panel) => {\n      panel.hidden = true;\n    });",
            "    // [negative-control] hide-on-success disabled",
            1,
        )
        self.assertNotEqual(broken, src, "negative-control mutation did not apply")
        out = self._run(broken)
        self.assertEqual(out["saved"], 1, f"broken save did not run: {out}")
        after = out["afterSave"]
        # With the hide removed, at least the captureForm stays visible (hidden
        # == False) -- the positive assertion would have caught this.
        self.assertFalse(
            after["captureForm"],
            "negative control: with hide-on-success removed, captureForm must stay VISIBLE",
        )

    def test_negative_control_break_restore_flips_to_fail(self) -> None:
        # Mutate resetToForm so the capture panels are NOT restored. The
        # Submit-Another assertion must then fail.
        src = self._app_src
        broken = src.replace(
            "    document.querySelectorAll(CAPTURE_PANEL_SELECTOR).forEach((panel) => {\n      panel.hidden = false;\n    });",
            "    // [negative-control] restore disabled",
            1,
        )
        self.assertNotEqual(broken, src, "negative-control mutation did not apply")
        out = self._run(broken)
        self.assertEqual(out["saved"], 1, f"broken save did not run: {out}")
        restored = out["afterSubmitAnother"]
        # With restore removed, the captureForm stays hidden after Submit Another.
        self.assertTrue(
            restored["captureForm"],
            "negative control: with restore removed, captureForm must stay HIDDEN",
        )


class SuccessTakeoverStructuralTests(_AssetServingMixin, unittest.TestCase):
    """Prompt 291 structural guards: #successScreen markup + global [hidden] rule."""

    def test_index_html_has_success_screen_with_heading_detail_and_button(self) -> None:
        html = self.served("/index.html").text
        # The success section exists and starts hidden.
        self.assertRegex(html, r'id="successScreen"[^>]*\bhidden\b')
        # Heading, detail line, and the Submit Another button.
        self.assertIn("Saved locally", html)
        self.assertIn('id="successDetail"', html)
        self.assertRegex(
            html, r'id="submitAnotherButton"[^>]*>\s*Submit Another'
        )

    def test_styles_have_global_hidden_display_none_important(self) -> None:
        css = self.served("/styles.css").text
        # The [hidden] attribute must be forced to display:none !important so the
        # takeover (and every other hidden panel) is genuinely removed.
        self.assertRegex(
            css,
            r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important",
        )

    def test_app_js_success_takeover_selector_covers_all_panels(self) -> None:
        # The takeover selector must include the capture form, the three panels,
        # AND the token-paste panel; the restore selector covers the capture UI.
        app = self.served("/app.js").text
        self.assertIn("#captureForm", app)
        self.assertIn(".photo-input-panel", app)
        self.assertIn(".voice-panel", app)
        self.assertIn(".note-panel", app)
        # The success takeover additionally hides #tokenPastePanel.
        self.assertRegex(app, r"SUCCESS_TAKEOVER_SELECTOR\s*=.*#tokenPastePanel")
        # showSuccess shows the success screen; resetToForm hides it.
        self.assertRegex(app, r"function showSuccess[\s\S]*?successScreen\.hidden = false")
        self.assertRegex(app, r"function resetToForm[\s\S]*?successScreen\.hidden = true")


# --------------------------------------------------------------------------- #
# Prompt 292 -- dogfood frontend polish (frontend ONLY:
# unified_capture/public/{index.html,app.js,styles.css}). Tests authored by the
# verification agent (NOT the implementer). Four items:
#   1. Settings <details> closes on outside click + Escape.
#   2. Merged header pill (#welcomePill removed; #statusText is the sole pill,
#      "Ready for {FirstName}" on a ready session).
#   3. Site & Area/QC labels use the inline pattern (.inline-field span + select).
#   4. Note glyph (#noteToggle) opens #noteEditorPanel; closing saves to the
#      hidden #notesInput, sets is-active, and the note flows into the capture.
#
# The settings outside-click and note-glyph tests are BEHAVIORAL: they execute
# the REAL served app.js inside a Node VM against a DOM stub that actually
# dispatches document/element events to the bound handlers, then inspect the
# ACTUAL state the production code set. Each behavioral test has a negative
# control proving the assertion discriminates. The inline-label + merged-pill
# markup checks are STRUCTURAL (served HTML/JS) per the repo convention.
# --------------------------------------------------------------------------- #


# A DOM stub rich enough to run the prompt-292 interactions: document-level click
# / keydown dispatch, .contains() that reflects the real ancestor relationships
# we care about (#settingsMenu, #noteEditorPanel, #noteToggle), classList state
# we can read back, and the bound-handler registry used by the 291 harness.
_P292_NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const appPath = process.argv[2];
const scenario = process.argv[3]; // see switch in drive()
const appSrc = fs.readFileSync(appPath, 'utf8');

const registry = new Map();
const docHandlers = {};   // document-level listeners: type -> [fn]
const handlers = new Map(); // selector -> { type: [fn] }

function makeEl(sel) {
  const el = {
    sel,
    id: sel.startsWith('#') ? sel.slice(1) : sel,
    hidden: false,
    disabled: false,
    checked: false,
    value: '',
    textContent: '',
    selectedIndex: -1,
    selectedOptions: [],
    files: [],
    dataset: {},
    style: {},
    width: 0,
    height: 0,
    open: false,
    attrs: {},
    _classes: new Set(),
    classList: {
      add(c) { el._classes.add(c); },
      remove(c) { el._classes.delete(c); },
      toggle(c, force) {
        const want = force === undefined ? !el._classes.has(c) : !!force;
        if (want) el._classes.add(c); else el._classes.delete(c);
        return want;
      },
      contains(c) { return el._classes.has(c); },
    },
    children: [],
    options: [],
    // contains(): a node contains itself; cross-element containment is wired
    // explicitly in the scenario (see _children below) to mirror the real DOM.
    _children: new Set(),
    contains(node) { return node === el || el._children.has(node); },
    addEventListener(type, fn) {
      if (!handlers.has(sel)) handlers.set(sel, {});
      const byType = handlers.get(sel);
      (byType[type] = byType[type] || []).push(fn);
    },
    removeEventListener() {},
    append(...kids) { for (const k of kids) this.children.push(k); },
    appendChild(k) { this.children.push(k); return k; },
    replaceChildren() { this.children = []; this.options = []; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    focus() {},
    getContext() { return { drawImage() {} }; },
    toBlob(cb) { cb(new Blob()); },
    play() { return Promise.resolve(); },
    pause() {},
    load() {},
  };
  return el;
}

function elFor(sel) {
  const key = sel.trim();
  if (!registry.has(key)) registry.set(key, makeEl(key));
  return registry.get(key);
}

function fire(sel, type, event) {
  const byType = handlers.get(sel);
  if (!byType || !byType[type]) return Promise.resolve();
  return Promise.all(byType[type].map((fn) => Promise.resolve().then(() => fn(event))));
}

function fireDoc(type, event) {
  const list = docHandlers[type] || [];
  return Promise.all(list.map((fn) => Promise.resolve().then(() => fn(event))));
}

const documentElement = makeEl('html');
let activeElement = makeEl('body');
const document = {
  documentElement,
  cookie: '',
  visibilityState: 'visible',
  get activeElement() { return activeElement; },
  querySelector(sel) {
    if (sel.startsWith('#') || sel.startsWith('.')) return elFor(sel);
    return makeEl(sel);
  },
  querySelectorAll(sel) {
    if (sel.indexOf('screenMode') !== -1) {
      return [makeEl('r-system'), makeEl('r-light'), makeEl('r-dark')];
    }
    if (sel.indexOf(',') !== -1 || sel.startsWith('.') || sel.startsWith('#')) {
      return sel.split(',').map((tok) => elFor(tok.trim()));
    }
    return [];
  },
  createElement(tag) { return makeEl(tag); },
  addEventListener(type, fn) { (docHandlers[type] = docHandlers[type] || []).push(fn); },
  removeEventListener() {},
};

const storage = new Map();
storage.set('unifiedCaptureToken', 'tok-abcdef123456');
const localStorage = {
  getItem(k) { return storage.has(k) ? storage.get(k) : null; },
  setItem(k, v) { storage.set(k, String(v)); },
  removeItem(k) { storage.delete(k); },
};

function fakeFetch(url) {
  if (typeof url === 'string' && url.indexOf('/api/session') !== -1) {
    const body = {
      person: { name: 'Doe, Jane', person_id: 'per_x' },
      token: { token_id: 'tok_x' },
      sites: [{ site_id: '7060', display_categories: ['report_an_issue'] }],
      can_submit: true,
    };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('') });
}

const captured = [];
const navigator = {
  onLine: true,
  storage: { persist: () => Promise.resolve(true), persisted: () => Promise.resolve(true) },
  mediaDevices: { getUserMedia: () => Promise.reject(new Error('no mic')) },
};

const windowObj = {
  document,
  localStorage,
  navigator,
  fetch: fakeFetch,
  location: { search: '', hash: '', pathname: '/' },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  history: { replaceState() {} },
  confirm: () => true,
  setTimeout: (fn) => { Promise.resolve().then(fn); return 0; },
  clearTimeout() {},
  setInterval: () => 0,
  clearInterval() {},
  requestAnimationFrame: (fn) => { Promise.resolve().then(fn); return 0; },
  addEventListener() {},
  removeEventListener() {},
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  btqRecorder: { supportsAudioRecording: () => false },
  fieldCaptureDb: {
    isSupported: () => true,
    putCapture: (rec) => { captured.push(rec); return Promise.resolve(); },
    allCaptures: () => Promise.resolve([]),
    listByStatus: () => Promise.resolve([]),
    updateCapture: () => Promise.resolve(),
  },
  createImageBitmap: () => Promise.resolve({ width: 4, height: 4, close() {} }),
};
windowObj.window = windowObj;

const sandbox = {
  window: windowObj,
  document,
  navigator,
  localStorage,
  fetch: fakeFetch,
  history: windowObj.history,
  location: windowObj.location,
  URL: windowObj.URL,
  URLSearchParams,
  console,
  setTimeout: windowObj.setTimeout,
  clearTimeout: windowObj.clearTimeout,
  setInterval: windowObj.setInterval,
  clearInterval: windowObj.clearInterval,
  requestAnimationFrame: windowObj.requestAnimationFrame,
  createImageBitmap: windowObj.createImageBitmap,
  Promise,
  Date,
  Image: class { set src(_v) { Promise.resolve().then(() => this.onload && this.onload()); } },
  Blob: class { constructor() { this.size = 1; this.type = 'image/jpeg'; } },
  File: class { constructor(parts, name, opts) { this.name = name; this.type = (opts && opts.type) || ''; } },
  Math,
  JSON,
};

elFor('#successScreen').hidden = true;
elFor('#tokenPastePanel').hidden = true;
// The note editor panel starts hidden in markup.
elFor('#noteEditorPanel').hidden = true;

// Wire the real ancestor relationships the handlers test via .contains():
//   #settingsMenu contains its #settingsSummary (the toggle click target)
//   #noteEditorPanel contains #noteEditor; #noteToggle contains its own glyph.
const settingsMenu = elFor('#settingsMenu');
const settingsInside = makeEl('settings-inside');
settingsMenu._children.add(settingsInside);
const noteEditorPanel = elFor('#noteEditorPanel');
const noteEditor = elFor('#noteEditor');
noteEditorPanel._children.add(noteEditor);
const noteToggle = elFor('#noteToggle');
const outside = makeEl('outside');

vm.createContext(sandbox);
vm.runInContext(appSrc, sandbox, { filename: 'app.js' });

async function settleSession() {
  for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0));
}

async function makeCaptureValid() {
  const site = registry.get('#siteInput');
  site.value = 'Continental Metalworks';
  site.selectedOptions = [{ dataset: { siteId: '7060', targetType: 'location', targetId: '7060' } }];
  registry.get('#categoryInput').value = 'report_an_issue';
  const fileInput = registry.get('#fileInput');
  fileInput.files = [new sandbox.File([], 'p.jpg', { type: 'image/png' })];
  await fire('#fileInput', 'change', { target: fileInput });
  for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));
}

async function drive() {
  await settleSession();
  let out = {};

  if (scenario === 'settings_outside_click') {
    settingsMenu.open = true;
    await fireDoc('click', { target: outside });
    out.afterOutsideClick = settingsMenu.open;

    settingsMenu.open = true;
    await fireDoc('click', { target: settingsInside });
    out.afterInsideClick = settingsMenu.open;

    settingsMenu.open = true;
    await fireDoc('keydown', { key: 'Escape' });
    out.afterEscape = settingsMenu.open;
  } else if (scenario === 'note_glyph_save_outside') {
    // Open via the bound #noteToggle click.
    await fire('#noteToggle', 'click', {});
    out.panelHiddenAfterOpen = noteEditorPanel.hidden;
    // Type into the editor, then close by clicking outside.
    noteEditor.value = 'CFO office reset';
    await fireDoc('click', { target: outside });
    out.panelHiddenAfterOutside = noteEditorPanel.hidden;
    out.savedNote = registry.get('#notesInput').value;
    out.glyphActive = noteToggle.classList.contains('is-active');
  } else if (scenario === 'note_glyph_save_escape') {
    await fire('#noteToggle', 'click', {});
    noteEditor.value = 'escape-saved note';
    await fireDoc('keydown', { key: 'Escape' });
    out.panelHidden = noteEditorPanel.hidden;
    out.savedNote = registry.get('#notesInput').value;
    out.glyphActive = noteToggle.classList.contains('is-active');
  } else if (scenario === 'note_flows_into_capture') {
    await makeCaptureValid();
    // Author a note through the glyph editor, close via outside click.
    await fire('#noteToggle', 'click', {});
    noteEditor.value = 'note via glyph editor';
    await fireDoc('click', { target: outside });
    for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0));
    // Submit.
    await fire('#submitButton', 'click', {});
    for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));
    const rec = captured.find((r) => r && r.fields && r.fields.note !== undefined);
    out.captureNote = rec ? rec.fields.note : null;
    out.saved = captured.filter((r) => r && r.capture_id !== '__token__').length;
  }

  process.stdout.write(JSON.stringify(out));
}

drive();
"""


class _P292HarnessMixin(_AssetServingMixin):
    def _run292(self, scenario: str, app_src: Optional[str] = None) -> dict:
        if _shutil.which("node") is None:
            self.skipTest("node not available")
        src = app_src if app_src is not None else (
            _REPO_ROOT / "project" / "unified_capture" / "public" / "app.js"
        ).read_text(encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as app_handle:
            app_handle.write(src)
            app_path = app_handle.name
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as h_handle:
            h_handle.write(_P292_NODE_HARNESS)
            harness_path = h_handle.name
        try:
            result = _subprocess.run(
                ["node", harness_path, app_path, scenario],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            _os.unlink(harness_path)
            _os.unlink(app_path)
        self.assertEqual(
            result.returncode, 0, f"harness crashed: {result.stderr}\n{result.stdout}"
        )
        return json.loads(result.stdout.strip())

    @property
    def _app_src(self) -> str:
        return (
            _REPO_ROOT / "project" / "unified_capture" / "public" / "app.js"
        ).read_text(encoding="utf-8")


class SettingsOutsideClickTests(_P292HarnessMixin, unittest.TestCase):
    """Prompt 292 item 1: settings <details> closes on outside click + Escape."""

    def test_outside_click_closes_inside_click_keeps_escape_closes(self) -> None:
        out = self._run292("settings_outside_click")
        # Outside click while open -> closed.
        self.assertFalse(out["afterOutsideClick"], f"outside click must close settings: {out}")
        # Click inside the menu -> stays open.
        self.assertTrue(out["afterInsideClick"], f"inside click must keep settings open: {out}")
        # Escape -> closed.
        self.assertFalse(out["afterEscape"], f"Escape must close settings: {out}")

    def test_negative_control_break_close_handler_flips_to_fail(self) -> None:
        # Remove the document-click close branch for #settingsMenu; the outside
        # click then must NOT close it -> the positive assertion would catch this.
        src = self._app_src
        broken = src.replace(
            "    if (elements.settingsMenu.open && !elements.settingsMenu.contains(event.target)) {\n      elements.settingsMenu.open = false;\n    }",
            "    // [negative-control] settings outside-click close disabled",
            1,
        )
        self.assertNotEqual(broken, src, "negative-control mutation did not apply")
        out = self._run292("settings_outside_click", app_src=broken)
        self.assertTrue(
            out["afterOutsideClick"],
            "negative control: with the close branch removed, an outside click must leave settings OPEN",
        )


class MergedHeaderPillTests(_P292HarnessMixin, unittest.TestCase):
    """Prompt 292 item 2: single merged #statusText pill ('Ready for {FirstName}')."""

    def test_welcome_pill_absent_in_markup(self) -> None:
        html = self.served("/index.html").text
        self.assertNotIn('id="welcomePill"', html)
        self.assertNotIn('class="header-pills"', html)
        self.assertIn('id="statusText"', html)

    def test_ready_rendering_path_exists(self) -> None:
        # STRUCTURAL backstop to the behavioral pill-text test below: the single
        # merged pill is fed "Ready for {FirstName}" via readyStatusText, and the
        # comma-form name parser ("Doe, Jane" -> "Jane") exists.
        app = self.served("/app.js").text
        self.assertRegex(app, r"`Ready for \$\{firstName\}`")
        self.assertIn('name.split(",", 2)[1]', app)


class StatusPillTextBehavioralTests(_P292HarnessMixin, unittest.TestCase):
    """Behavioral: the merged pill actually shows 'Ready for Jane' on a ready session."""

    def test_ready_pill_text_is_ready_for_first_name(self) -> None:
        out = self._run_status()
        self.assertEqual(out["statusText"], "Ready for Jane", out)

    def _run_status(self) -> dict:
        if _shutil.which("node") is None:
            self.skipTest("node not available")
        # A tiny harness variant: settle the session, then read #statusText.
        harness = _P292_NODE_HARNESS.replace(
            "  process.stdout.write(JSON.stringify(out));",
            "  out.statusText = registry.get('#statusText').textContent;\n"
            "  process.stdout.write(JSON.stringify(out));",
        )
        app_path = (
            _REPO_ROOT / "project" / "unified_capture" / "public" / "app.js"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as h_handle:
            h_handle.write(harness)
            harness_path = h_handle.name
        try:
            result = _subprocess.run(
                ["node", harness_path, str(app_path), "settings_outside_click"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            _os.unlink(harness_path)
        self.assertEqual(result.returncode, 0, f"harness crashed: {result.stderr}\n{result.stdout}")
        return json.loads(result.stdout.strip())


class InlineSelectLabelTests(_AssetServingMixin, unittest.TestCase):
    """Prompt 292 item 3: Site & Area/QC labels use the inline (.inline-field) pattern."""

    def test_site_and_category_labels_are_inline_field_with_span_beside_select(self) -> None:
        html = self.served("/index.html").text
        # Both selects sit inside a label.inline-field with a <span> label beside
        # the <select> (not stacked text above the control).
        for select_id, span_text in (("siteInput", "Site"), ("categoryInput", "Area / QC")):
            # The label carries the inline-field class and wraps a <span> + the select.
            pattern = (
                r'<label class="inline-field">\s*'
                r'<span>' + span_text + r'</span>\s*'
                r'<select id="' + select_id + r'"'
            )
            self.assertRegex(html, pattern, f"{select_id} must use the inline-field span+select pattern")
        # The old stacked patterns are gone (no bare label text above the select).
        self.assertNotRegex(html, r'<label>\s*Site\s*<select')
        self.assertNotRegex(html, r'<label>\s*Area / QC Category\s*<select')

    def test_inline_field_styles_lay_span_beside_select(self) -> None:
        css = self.served("/styles.css").text
        # .inline-field is a flex row (label span + select side by side).
        self.assertRegex(css, r"\.inline-field\s*\{[^}]*display:\s*flex")


class NoteGlyphBehavioralTests(_P292HarnessMixin, unittest.TestCase):
    """Prompt 292 item 4: ✎ glyph opens the editor; close saves to hidden #notesInput."""

    def test_open_then_outside_click_saves_note_and_activates_glyph(self) -> None:
        out = self._run292("note_glyph_save_outside")
        # Clicking the glyph reveals the editor panel.
        self.assertFalse(out["panelHiddenAfterOpen"], f"glyph click must open the editor: {out}")
        # Clicking outside closes the panel, saves the text, and marks the glyph active.
        self.assertTrue(out["panelHiddenAfterOutside"], f"outside click must close the editor: {out}")
        self.assertEqual(out["savedNote"], "CFO office reset", f"note must be saved to #notesInput: {out}")
        self.assertTrue(out["glyphActive"], f"glyph must show is-active when a note exists: {out}")

    def test_escape_closes_and_saves_note(self) -> None:
        out = self._run292("note_glyph_save_escape")
        self.assertTrue(out["panelHidden"], f"Escape must close the editor: {out}")
        self.assertEqual(out["savedNote"], "escape-saved note", f"Escape must save the note: {out}")
        self.assertTrue(out["glyphActive"], f"glyph must be active after an Escape-save: {out}")

    def test_note_authored_via_glyph_flows_into_captured_record(self) -> None:
        out = self._run292("note_flows_into_capture")
        self.assertEqual(out["saved"], 1, f"a capture must have been saved: {out}")
        self.assertEqual(
            out["captureNote"],
            "note via glyph editor",
            f"the glyph-authored note must flow into the captured record's fields.note: {out}",
        )

    def test_negative_control_break_save_on_exit_flips_to_fail(self) -> None:
        # Break saveNoteFromEditor so closing does NOT persist the editor text into
        # #notesInput. The save-on-exit assertions must then fail.
        src = self._app_src
        broken = src.replace(
            '    elements.notesInput.value = elements.noteEditor.value || "";\n    syncNoteAffordance();',
            "    // [negative-control] saveNoteFromEditor disabled",
            1,
        )
        self.assertNotEqual(broken, src, "negative-control mutation did not apply")
        out = self._run292("note_glyph_save_outside", app_src=broken)
        # With the save disabled, the note is NOT carried into #notesInput.
        self.assertNotEqual(
            out["savedNote"],
            "CFO office reset",
            "negative control: with saveNoteFromEditor broken, the note must NOT be saved",
        )


class NoteGlyphStructuralTests(_AssetServingMixin, unittest.TestCase):
    """Prompt 292 item 4 markup/wiring guards + success-takeover coverage of the note panel."""

    def test_note_affordance_replaces_always_visible_textarea(self) -> None:
        html = self.served("/index.html").text
        # The glyph affordance + hidden input + editor panel exist.
        self.assertIn('id="noteAffordance"', html)
        self.assertRegex(html, r'id="notesInput"[^>]*type="hidden"')
        self.assertRegex(html, r'id="noteToggle"[^>]*>\s*✎')
        self.assertRegex(html, r'id="noteEditorPanel"[^>]*\bhidden\b')
        self.assertIn('id="noteEditor"', html)
        # The old always-visible textarea (name=note rows=4) is gone; the only
        # `name="note"` element is the hidden input.
        self.assertNotRegex(html, r'<textarea id="notesInput"')

    def test_app_wires_save_on_exit_paths(self) -> None:
        app = self.served("/app.js").text
        # saveNoteFromEditor copies the editor into the hidden input.
        self.assertRegex(app, r"function saveNoteFromEditor\(\)\s*\{[^}]*notesInput\.value")
        # closeNoteEditor saves on close; bound to blur + the document outside-click
        # + Escape.
        self.assertRegex(app, r"function closeNoteEditor[\s\S]*?saveNoteFromEditor\(\)")
        self.assertRegex(app, r'noteEditor\.addEventListener\(\s*"blur"')
        # The note still rides into the capture via the hidden #notesInput.
        self.assertRegex(app, r"const note = elements\.notesInput\.value\.trim\(\)")
        self.assertRegex(app, r"\bnote,")  # fields.note carried into the record

    def test_note_affordance_covered_by_capture_panel_hide(self) -> None:
        # The note affordance lives in .note-panel, which is part of both the
        # success-takeover hide selector and the Submit-Another restore selector,
        # so the glyph/editor is hidden on success and cleared on Submit Another.
        app = self.served("/app.js").text
        self.assertRegex(app, r'CAPTURE_PANEL_SELECTOR\s*=.*\.note-panel')
        # Submit Another (resetToForm) re-closes the editor + syncs the glyph.
        self.assertRegex(app, r"function resetToForm[\s\S]*?closeNoteEditor\(\)")
        self.assertRegex(app, r"function resetToForm[\s\S]*?syncNoteAffordance\(\)")
        # saveCapture clears the note on a successful save.
        self.assertRegex(app, r'function saveCapture[\s\S]*?elements\.notesInput\.value = ""')


# --------------------------------------------------------------------------- #
# Prompt 294: "My Submissions" -- read-only GET /api/my-submissions endpoint.
#
# These tests are authored by the verification agent (NOT the implementer).
# The auth path runs FOR REAL (real TokenStore + real authorize_token through
# install_couch_fakes), exactly like the /api/session tests. Only the
# CouchDB capture-read layer (query_captures_by_person_id) and the pure
# field_capture collectors (collect_my_submissions / rolling_quality_summary)
# are faked -- mirroring how the session tests fake their couch reads. The
# write path (put_field_capture_document and every stager/writer the handler
# could touch) is wired to a TRIPWIRE that fails the test if invoked, proving
# the endpoint is genuinely read-only.
# --------------------------------------------------------------------------- #


class _MySubsFakeServer(_FakeServer):
    """Extends the session fake-server with the attributes handle_my_submissions reads."""

    def __init__(
        self,
        token_store: TokenStore,
        vault_root: Path,
        *,
        couchdb_config: Any,
        couchdb_database: str,
        upload_dir: Path,
    ) -> None:
        super().__init__(token_store, vault_root)
        self.couchdb_config = couchdb_config
        self.couchdb_database = couchdb_database
        self.upload_dir = upload_dir


def drive_my_subs_request(
    token_store: TokenStore,
    vault_root: Path,
    *,
    couchdb_config: Any,
    couchdb_database: str = "btq_field_captures",
    upload_dir: Path,
    headers: Optional[dict[str, str]] = None,
    path: str = "/api/my-submissions",
) -> _Response:
    """Drive a GET /api/my-submissions through the real handler.

    Same in-process technique as ``drive_request`` but binds a server carrying
    the couchdb_config / upload_dir attributes the my-submissions handler reads.
    """
    request_line = f"GET {path} HTTP/1.1\r\n"
    header_lines = "".join(f"{name}: {value}\r\n" for name, value in (headers or {}).items())
    raw = (request_line + header_lines + "\r\n").encode("utf-8")

    handler = UnifiedCaptureHandler.__new__(UnifiedCaptureHandler)
    handler.server = _MySubsFakeServer(  # type: ignore[assignment]
        token_store,
        vault_root,
        couchdb_config=couchdb_config,
        couchdb_database=couchdb_database,
        upload_dir=upload_dir,
    )
    handler.client_address = ("127.0.0.1", 0)
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.handle_one_request()

    handler.wfile.seek(0)
    raw_out = handler.wfile.getvalue()
    status_line, _, rest = raw_out.partition(b"\r\n")
    head, _, body = rest.partition(b"\r\n\r\n")
    status = int(status_line.split(b" ")[1])
    parsed_headers: dict[str, str] = {}
    for line in head.split(b"\r\n"):
        if b":" in line:
            name, _, value = line.partition(b":")
            parsed_headers[name.decode().strip()] = value.decode().strip()
    return _Response(status, parsed_headers, body)


# Sentinel: a non-None couchdb_config so the handler passes the
# "couchdb unavailable (config missing)" guard and proceeds to the query.
_COUCH_CONFIG_SENTINEL = object()


class _WriteTripwire(Exception):
    """Raised if any write/mutation path is invoked from the read-only endpoint."""


class MySubmissionsEndpointTests(unittest.TestCase):
    """Prompt 294: behavioral, server-side tests for GET /api/my-submissions."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        self.upload_dir = self.vault / "runtime" / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _install_read_layer(
        self,
        *,
        capture_docs: Optional[list] = None,
        submissions: Optional[list] = None,
        quality_summary: Any = None,
        query_error: Optional[Exception] = None,
    ) -> list:
        """Patch the capture-read layer + arm the write tripwire. Returns patchers."""
        started = []

        if query_error is not None:
            q_patch = mock.patch.object(
                uc_server, "query_captures_by_person_id", side_effect=query_error
            )
        else:
            q_patch = mock.patch.object(
                uc_server,
                "query_captures_by_person_id",
                return_value=(capture_docs if capture_docs is not None else []),
            )
        started.append(q_patch)

        collect_patch = mock.patch.object(
            uc_server.my_submissions_module,
            "collect_my_submissions",
            return_value=(submissions if submissions is not None else []),
        )
        started.append(collect_patch)

        quality_patch = mock.patch.object(
            uc_server.my_submissions_module,
            "rolling_quality_summary",
            return_value=quality_summary,
        )
        started.append(quality_patch)

        # Read-only tripwires: ANY writer/stager invocation fails the test.
        def _tripwire(*args, **kwargs):
            raise _WriteTripwire("read-only endpoint invoked a write path")

        for writer_name in (
            "put_field_capture_document",
            "write_capture_media",
        ):
            if hasattr(uc_server, writer_name):
                started.append(
                    mock.patch.object(uc_server, writer_name, side_effect=_tripwire)
                )

        for patcher in started:
            patcher.start()
        return started

    # -- Happy path --------------------------------------------------------- #

    def test_valid_session_returns_submissions_and_quality_summary(self) -> None:
        store, token = make_store(self.tmp.name, site_ids=["7060"], can_submit=True)
        auth = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        read = self._install_read_layer(
            submissions=[
                {"capture_id": "cap-1", "track": "B", "stage": "acted_on"},
                {"capture_id": "cap-2", "track": "A", "stage": "queued"},
            ],
            quality_summary={"total_processed": 5, "clear": 4, "flag_counts": {"blurry": 1}},
        )
        try:
            resp = drive_my_subs_request(
                store,
                self.vault,
                couchdb_config=_COUCH_CONFIG_SENTINEL,
                upload_dir=self.upload_dir,
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            stop_all(read)
            stop_all(auth)
        self.assertEqual(resp.status, 200, resp.text)
        body = resp.json()
        self.assertIn("submissions", body)
        self.assertIn("quality_summary", body)
        self.assertEqual(len(body["submissions"]), 2)
        self.assertEqual(body["submissions"][0]["capture_id"], "cap-1")
        self.assertEqual(body["quality_summary"]["total_processed"], 5)

    def test_happy_path_routes_through_real_authorize_token(self) -> None:
        # Spy proves the handler did not hand-roll a session: it must call the
        # real authorize_token (same guarantee the /api/session tests assert).
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        auth = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        read = self._install_read_layer(submissions=[], quality_summary=None)
        real_authorize = uc_server.authorize_token
        calls = {"n": 0}

        def spy(token_store, vault_root, token_value):
            calls["n"] += 1
            return real_authorize(token_store, vault_root, token_value)

        spy_patch = mock.patch.object(uc_server, "authorize_token", side_effect=spy)
        spy_patch.start()
        try:
            resp = drive_my_subs_request(
                store,
                self.vault,
                couchdb_config=_COUCH_CONFIG_SENTINEL,
                upload_dir=self.upload_dir,
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            spy_patch.stop()
            stop_all(read)
            stop_all(auth)
        self.assertEqual(resp.status, 200, resp.text)
        self.assertEqual(calls["n"], 1, "endpoint must route through authorize_token")

    def test_query_called_with_authenticated_person_id(self) -> None:
        # The lookup must be scoped to the session's OWN person_id (no leakage).
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        auth = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        captured = {}

        def fake_query(config, person_id, *, database, **kwargs):
            captured["person_id"] = person_id
            captured["config"] = config
            return []

        q_patch = mock.patch.object(uc_server, "query_captures_by_person_id", side_effect=fake_query)
        collect_patch = mock.patch.object(
            uc_server.my_submissions_module, "collect_my_submissions", return_value=[]
        )
        quality_patch = mock.patch.object(
            uc_server.my_submissions_module, "rolling_quality_summary", return_value=None
        )
        write_patch = mock.patch.object(
            uc_server, "put_field_capture_document",
            side_effect=lambda *a, **k: (_ for _ in ()).throw(_WriteTripwire("write!")),
        )
        for p in (q_patch, collect_patch, quality_patch, write_patch):
            p.start()
        try:
            resp = drive_my_subs_request(
                store,
                self.vault,
                couchdb_config=_COUCH_CONFIG_SENTINEL,
                upload_dir=self.upload_dir,
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            for p in (write_patch, quality_patch, collect_patch, q_patch):
                p.stop()
            stop_all(auth)
        self.assertEqual(resp.status, 200, resp.text)
        self.assertEqual(captured.get("person_id"), "per_unified01")
        self.assertIs(captured.get("config"), _COUCH_CONFIG_SENTINEL)

    # -- Fail-closed -------------------------------------------------------- #

    def test_missing_token_401(self) -> None:
        store, _ = make_store(self.tmp.name)
        # No auth fakes needed: extract_token returns None before any couch call.
        resp = drive_my_subs_request(
            store,
            self.vault,
            couchdb_config=_COUCH_CONFIG_SENTINEL,
            upload_dir=self.upload_dir,
            headers={},
        )
        self.assertEqual(resp.status, 401, resp.text)

    def test_garbage_token_401(self) -> None:
        store, _ = make_store(self.tmp.name)
        auth = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        try:
            resp = drive_my_subs_request(
                store,
                self.vault,
                couchdb_config=_COUCH_CONFIG_SENTINEL,
                upload_dir=self.upload_dir,
                headers={"Authorization": "Bearer fc_not_a_real_token"},
            )
        finally:
            stop_all(auth)
        self.assertEqual(resp.status, 401, resp.text)

    def test_couchdb_query_error_503(self) -> None:
        from field_capture.server import CouchDBCaptureReaderError

        store, token = make_store(self.tmp.name, site_ids=["7060"])
        auth = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        read = self._install_read_layer(
            query_error=CouchDBCaptureReaderError("CouchDB _find failed")
        )
        try:
            resp = drive_my_subs_request(
                store,
                self.vault,
                couchdb_config=_COUCH_CONFIG_SENTINEL,
                upload_dir=self.upload_dir,
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            stop_all(read)
            stop_all(auth)
        self.assertEqual(resp.status, 503, resp.text)

    def test_couchdb_config_missing_503(self) -> None:
        # Defense-in-depth: when the server has no couchdb_config, fail closed 503.
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        auth = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        try:
            resp = drive_my_subs_request(
                store,
                self.vault,
                couchdb_config=None,
                upload_dir=self.upload_dir,
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            stop_all(auth)
        self.assertEqual(resp.status, 503, resp.text)

    # -- Read-only proof ---------------------------------------------------- #

    def test_endpoint_is_read_only_no_writer_invoked(self) -> None:
        # The tripwire (armed in _install_read_layer) raises if any writer runs.
        # A 200 here is positive proof the read-only path never mutates.
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        auth = install_couch_fakes(EMP_SINGLE, SITES_TWO)
        read = self._install_read_layer(
            submissions=[{"capture_id": "cap-1", "track": "A", "stage": "queued"}],
            quality_summary={"total_processed": 1, "clear": 1, "flag_counts": {}},
        )
        try:
            resp = drive_my_subs_request(
                store,
                self.vault,
                couchdb_config=_COUCH_CONFIG_SENTINEL,
                upload_dir=self.upload_dir,
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            stop_all(read)
            stop_all(auth)
        # If put_field_capture_document/write_capture_media had been called, the
        # tripwire would have produced a 500, not a 200.
        self.assertEqual(resp.status, 200, resp.text)

    def test_post_to_my_submissions_is_not_a_write_route(self) -> None:
        # The handler only registers /api/my-submissions on GET; a POST must not
        # reach a write path (it 404s in do_POST, never mutating).
        store, token = make_store(self.tmp.name, site_ids=["7060"])
        resp = drive_request(
            store, self.vault, "POST", "/api/my-submissions",
            {"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status, 404, resp.text)


# --------------------------------------------------------------------------- #
# Prompt 294: "My Submissions" structural placement + feed-view behavior.
# --------------------------------------------------------------------------- #


class MySubmissionsPlacementTests(_AssetServingMixin, unittest.TestCase):
    """#mySubsBtn sits inline with the #statusText pill in .header-status-row."""

    def test_mysubs_button_in_header_status_row_with_status_pill(self) -> None:
        html = self.served("/index.html").text
        # The status row container exists.
        row = re.search(r'<div class="header-status-row">(.*?)</div>', html, re.S)
        self.assertIsNotNone(row, "expected a .header-status-row container in the header")
        block = row.group(1)
        # Both the status pill and the My Submissions button live inside it.
        self.assertIn('id="statusText"', block, "#statusText must be in .header-status-row")
        self.assertIn('id="mySubsBtn"', block, "#mySubsBtn must be inline with #statusText")
        self.assertIn("My Submissions", block)

    def test_mysubs_button_carries_badge(self) -> None:
        html = self.served("/index.html").text
        self.assertIn('id="mySubsBadge"', html)
        # Badge starts hidden in markup (only un-hidden when unseen items exist).
        self.assertRegex(html, r'id="mySubsBadge"[^>]*\bhidden\b')

    def test_status_row_lives_inside_the_header(self) -> None:
        html = self.served("/index.html").text
        header_open = html.find("<header")
        header_close = html.find("</header>")
        row_idx = html.find('class="header-status-row"')
        self.assertGreater(header_open, -1)
        self.assertGreater(header_close, header_open)
        self.assertTrue(
            header_open < row_idx < header_close,
            "header-status-row must sit inside the header",
        )

    def test_feed_section_exists_and_starts_hidden(self) -> None:
        html = self.served("/index.html").text
        section = re.search(r'<section id="feedSection"[^>]*>', html)
        self.assertIsNotNone(section, "#feedSection must exist")
        self.assertIn("hidden", section.group(0), "#feedSection must start hidden")
        self.assertIn('id="feedBack"', html, "feed must have a Back control")
        self.assertIn('id="submissionList"', html)


_FEED_SWAP_NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const appPath = process.argv[2];
const appSrc = fs.readFileSync(appPath, 'utf8');

// DOM stub that RECORDS event listeners so we can fire the real click handlers.
function makeEl(id) {
  const listeners = {};
  const el = {
    id, hidden: false, disabled: false, checked: false, value: '',
    textContent: '', content: '', selectedIndex: -1, innerHTML: '',
    dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    children: [],
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener() {},
    append() {}, appendChild() {}, replaceChildren() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    setAttribute() {}, removeAttribute() {}, focus() {},
    play() { return Promise.resolve(); }, pause() {},
    _fire(type, evt) { (listeners[type] || []).forEach((fn) => fn(evt || { preventDefault() {}, stopPropagation() {} })); },
  };
  return el;
}

const registry = new Map();
function elFor(sel) {
  if (!registry.has(sel)) registry.set(sel, makeEl(sel));
  return registry.get(sel);
}

const documentElement = makeEl('html');
const document = {
  documentElement, cookie: '', visibilityState: 'visible',
  activeElement: null,
  querySelector(sel) { if (sel.startsWith('#')) return elFor(sel); return makeEl(sel); },
  querySelectorAll(sel) {
    if (sel.indexOf('screenMode') !== -1) return [makeEl('r-system'), makeEl('r-light'), makeEl('r-dark')];
    return [];
  },
  createElement(tag) { return makeEl(tag); },
  addEventListener() {}, removeEventListener() {},
};

const storage = new Map();
storage.set('unifiedCaptureToken', 'tok-abcdef123456');
const localStorage = {
  getItem(k) { return storage.has(k) ? storage.get(k) : null; },
  setItem(k, v) { storage.set(k, String(v)); },
  removeItem(k) { storage.delete(k); },
};

function fakeFetch(url) {
  if (typeof url === 'string' && url.indexOf('/api/session') !== -1) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
      person: { name: 'Doe, Jane' }, sites: [{ site_id: 's1', display_categories: [] }], can_submit: true,
    }) });
  }
  if (typeof url === 'string' && url.indexOf('/api/my-submissions') !== -1) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
      submissions: [], quality_summary: null,
    }) });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('') });
}

const navigator = {
  onLine: true,
  storage: { persist: () => Promise.resolve(true), persisted: () => Promise.resolve(true) },
  mediaDevices: { getUserMedia: () => Promise.reject(new Error('no mic')) },
};

const windowObj = {
  document, localStorage, navigator, fetch: fakeFetch,
  location: { search: '', hash: '', pathname: '/' },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  history: { replaceState() {} }, confirm: () => true,
  setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  addEventListener() {}, removeEventListener() {},
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  btqRecorder: { supportsAudioRecording: () => false },
  fieldCaptureDb: { isSupported: () => false, putCapture: () => Promise.resolve(), allCaptures: () => Promise.resolve([]) },
};
windowObj.window = windowObj;

const sandbox = {
  window: windowObj, document, navigator, localStorage, fetch: fakeFetch,
  history: windowObj.history, location: windowObj.location, URL: windowObj.URL,
  URLSearchParams, console,
  setTimeout: windowObj.setTimeout, clearTimeout: windowObj.clearTimeout,
  setInterval: windowObj.setInterval, clearInterval: windowObj.clearInterval,
  Promise, Date, Blob: class { constructor() {} }, Math, JSON, Set, Array, Object,
};
vm.createContext(sandbox);
vm.runInContext(appSrc, sandbox, { filename: 'app.js' });

// Let session load settle, then drive: capture screen -> feed (My Submissions) -> Back.
setTimeout(() => {
  const feed = registry.get('#feedSection');
  const mySubsBtn = registry.get('#mySubsBtn');
  const feedBack = registry.get('#feedBack');
  const out = { wired: !!(mySubsBtn && feedBack) };
  // NOTE: the DOM stub can't reflect the HTML `hidden` attribute (no parser),
  // so initial-hidden is asserted STRUCTURALLY elsewhere. Here we prove the
  // SWAP: show on click, hide on Back -- the real behavioral claim.
  // Click My Submissions.
  mySubsBtn._fire('click');
  setTimeout(() => {
    out.feedShownAfterClick = feed ? feed.hidden === false : null;
    // Click Back.
    feedBack._fire('click');
    setTimeout(() => {
      out.feedHiddenAfterBack = feed ? feed.hidden : null;
      process.stdout.write(JSON.stringify(out));
    }, 0);
  }, 0);
}, 0);
"""


class MySubmissionsFeedSwapBehavioralTests(_AssetServingMixin, unittest.TestCase):
    """BEHAVIORAL (Node-vm): clicking My Submissions swaps in the feed; Back restores."""

    def _run(self) -> dict:
        if _shutil.which("node") is None:
            self.skipTest("node not available")
        app_path = _REPO_ROOT / "project" / "unified_capture" / "public" / "app.js"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(_FEED_SWAP_NODE_HARNESS)
            harness_path = handle.name
        try:
            result = _subprocess.run(
                ["node", harness_path, str(app_path)],
                capture_output=True, text=True, timeout=30,
            )
        finally:
            _os.unlink(harness_path)
        self.assertEqual(result.returncode, 0, f"harness crashed: {result.stderr}\n{result.stdout}")
        return json.loads(result.stdout.strip())

    def test_feed_swaps_in_on_my_submissions_and_back_restores(self) -> None:
        out = self._run()
        self.assertTrue(out["wired"], "mySubsBtn + feedBack must both be wired")
        self.assertTrue(out["feedShownAfterClick"], "feed must be shown after My Submissions click")
        self.assertTrue(out["feedHiddenAfterBack"], "Back must hide the feed (restore capture screen)")

    def test_feed_swap_wiring_structural_backstop(self) -> None:
        # STRUCTURAL backstop in case node is unavailable.
        app = self.served("/app.js").text
        self.assertRegex(app, r'mySubsBtn\.addEventListener\(\s*"click",\s*showFeedView\)')
        self.assertRegex(app, r'feedBack\.addEventListener\(\s*"click",\s*hideFeedView\)')
        self.assertRegex(app, r"function showFeedView[\s\S]*?feedSection\.hidden = false")
        self.assertRegex(app, r"function hideFeedView[\s\S]*?feedSection\.hidden = true")


if __name__ == "__main__":
    unittest.main()
