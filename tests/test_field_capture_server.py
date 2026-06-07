from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from event_pipeline import couchdb_config
import field_capture.auth as auth_module
import field_capture.server as server_module
from field_capture.auth import TokenStore
from field_capture.server import (
    AUDIO_ALLOWED_EXTENSIONS,
    BUILTIN_FALLBACK_CATEGORIES,
    FieldCaptureHandler,
    UploadedFile,
    build_capture_document,
    first_name_from_canonical,
)
from transcription_pipeline.main import SUPPORTED_EXTENSIONS


class FakeEmployeeStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, object]] = {}

    def get_optional(self, doc_id: str) -> dict[str, object] | None:
        doc = self.docs.get(doc_id)
        return dict(doc) if doc is not None else None


class FakeAuthSiteRegistry:
    def __init__(self) -> None:
        self.sites: dict[str, str] = {}

    def resolve_canonical(self, site_id: str) -> str | None:
        return self.sites.get(str(site_id))

    def list_sites(self) -> list[dict[str, str]]:
        return [{"site_id": site_id, "canonical": canonical} for site_id, canonical in self.sites.items()]


_employee_store: FakeEmployeeStore | None = None
_auth_site_registry: FakeAuthSiteRegistry | None = None


@pytest.fixture(autouse=True)
def patch_canonical_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    global _employee_store, _auth_site_registry
    _employee_store = FakeEmployeeStore()
    _auth_site_registry = FakeAuthSiteRegistry()
    monkeypatch.setattr(auth_module.CouchDBEntityStore, "from_env", classmethod(lambda cls: _employee_store))
    monkeypatch.setattr(auth_module, "CouchDBSiteRegistry", lambda: _auth_site_registry)
    monkeypatch.setattr(server_module, "CouchDBSiteRegistry", lambda: _auth_site_registry)


class RunningFieldCaptureServer:
    def __init__(self, server: SimpleNamespace) -> None:
        self.server = server

    def request(self, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
        handler = object.__new__(FieldCaptureHandler)
        handler.server = self.server
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 0)
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = method
        handler.path = path
        handler.close_connection = True
        handler.headers = Message()
        if method == "GET":
            handler.do_GET()
        else:
            raise AssertionError(f"unsupported test method: {method}")
        return parse_handler_response(handler.wfile.getvalue())

    def submit(self, token: str, body: bytes, content_type: str) -> tuple[int, dict[str, str], bytes]:
        handler = object.__new__(FieldCaptureHandler)
        handler.server = self.server
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 0)
        handler.requestline = "POST /api/submit HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = "POST"
        handler.path = "/api/submit"
        handler.close_connection = True
        headers = Message()
        headers["Authorization"] = f"Bearer {token}"
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        handler.headers = headers
        handler.do_POST()
        return parse_handler_response(handler.wfile.getvalue())


def parse_handler_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    header_blob, _, body = raw.partition(b"\r\n\r\n")
    lines = header_blob.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    return status, headers, body


def write_vault_site(vault_root: Path, site_id: str, name: str) -> None:
    assert _auth_site_registry is not None
    _auth_site_registry.sites[str(site_id)] = name


def write_vault_person(vault_root: Path, *, person_id: str, name: str, job: str) -> None:
    assert _employee_store is not None
    _employee_store.docs[f"employee_{person_id}"] = {
        "_id": f"employee_{person_id}",
        "type": "employee",
        "person_id": person_id,
        "name": name,
        "status": "active",
        "site_ids": [job],
        "vault_path": f"People/{person_id}.md",
    }


def start_field_capture_server(tmp_path: Path) -> tuple[RunningFieldCaptureServer, TokenStore, Path]:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    token_store = TokenStore(runtime_root / "tokens.sqlite3")
    token_store.initialize()
    server = SimpleNamespace(
        token_store=token_store,
        vault_root=vault_root,
        upload_dir=runtime_root / "uploads",
        max_images=6,
        max_upload_bytes=1024 * 1024,
        request_max_bytes=10 * 1024 * 1024,
        couchdb_config=couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000),
        couchdb_database="btq_field_captures",
        capture_reader=lambda _config, _site_id, *, database: [],
        site_lookup_reader=lambda _config, _upload_id, *, database: None,
    )
    return RunningFieldCaptureServer(server), token_store, vault_root


def session_for_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    site_registry: object | None = None,
    system_defaults: dict[str, object] | None = None,
    role: str = "cleaner",
) -> dict[str, object]:
    app, store, vault_root = start_field_capture_server(tmp_path)
    if site_registry is not None:
        app.server.site_registry = site_registry
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"], role=role)
    monkeypatch.setattr("field_capture.server.load_system_defaults", lambda: system_defaults or {})

    status, _headers, body = app.request("GET", f"/api/session?token={token.token_value}")

    assert status == 200
    return json.loads(body)


def multipart_body(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----btq-field-capture-server-test"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    for name, filename, content_type, content in files:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8"))
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def valid_submit_fields(qc_category: str = "Restrooms") -> dict[str, str]:
    return {
        "job_id": "2026-05-02T00-00-00-04-00__photo-capture-summit-wire",
        "capture_id": "cap-photo-server",
        "site": "Summit Wire",
        "site_id": "7050",
        "qc_category": qc_category,
        "note": "Sink checked.",
        "captured_at": "2026-05-02T00:00:00-04:00",
        "exported_at": "2026-05-02T00:01:00-04:00",
    }


class FakeSiteRegistry:
    def __init__(
        self,
        *,
        capture_guidance: str | None = None,
        display_categories: list[dict[str, str]] | None = None,
        guidance_error: Exception | None = None,
        categories_error: Exception | None = None,
    ) -> None:
        self.capture_guidance = capture_guidance
        self.display_categories = display_categories
        self.guidance_error = guidance_error
        self.categories_error = categories_error

    def get_capture_guidance(self, site_id: str) -> str | None:
        assert site_id == "7050"
        if self.guidance_error is not None:
            raise self.guidance_error
        return self.capture_guidance

    def get_display_categories(self, site_id: str) -> list[dict[str, str]] | None:
        assert site_id == "7050"
        if self.categories_error is not None:
            raise self.categories_error
        return self.display_categories


def test_session_response_includes_first_name_field(tmp_path: Path) -> None:
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"])

    status, _headers, body = app.request("GET", f"/api/session?token={token.token_value}")

    assert status == 200
    session = json.loads(body)
    assert session["person"]["person_id"] == "jordan-avery"
    assert session["person"]["name"] == "Avery, Jordan"
    assert session["person"]["first"] == "Jordan"


def test_session_site_includes_capture_guidance_from_site_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = session_for_site(
        tmp_path,
        monkeypatch,
        site_registry=FakeSiteRegistry(capture_guidance="Focus on north dock"),
        system_defaults={"default_capture_guidance": "Default guidance"},
    )

    assert session["sites"][0]["capture_guidance"] == "Focus on north dock"


def test_session_site_falls_back_to_system_default_guidance_when_site_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = session_for_site(
        tmp_path,
        monkeypatch,
        site_registry=FakeSiteRegistry(capture_guidance=None),
        system_defaults={"default_capture_guidance": "Default guidance"},
    )

    assert session["sites"][0]["capture_guidance"] == "Default guidance"


def test_session_site_falls_back_to_empty_string_when_no_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = session_for_site(tmp_path, monkeypatch, site_registry=FakeSiteRegistry(capture_guidance=None), system_defaults={})

    assert session["sites"][0]["capture_guidance"] == ""


def test_session_site_includes_display_categories_from_site_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    categories = [{"label": "Mill floor", "canonical": "Common / Open Areas"}]
    session = session_for_site(
        tmp_path,
        monkeypatch,
        site_registry=FakeSiteRegistry(display_categories=categories),
        system_defaults={"default_display_categories": [{"label": "Default", "canonical": "Other"}]},
    )

    assert session["sites"][0]["display_categories"] == categories


def test_session_site_falls_back_to_system_default_categories_when_site_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    categories = [{"label": "Default restroom", "canonical": "Restrooms"}]
    session = session_for_site(
        tmp_path,
        monkeypatch,
        site_registry=FakeSiteRegistry(display_categories=None),
        system_defaults={"default_display_categories": categories},
    )

    assert session["sites"][0]["display_categories"] == categories


def test_session_site_falls_back_to_builtin_categories_when_no_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = session_for_site(tmp_path, monkeypatch, site_registry=FakeSiteRegistry(display_categories=None), system_defaults={})

    assert session["sites"][0]["display_categories"] == BUILTIN_FALLBACK_CATEGORIES
    assert session["sites"][0]["display_categories"][0] == {
        "label": "Report an Issue",
        "canonical": "report_an_issue",
    }
    assert session["sites"][0]["display_categories"][-1] == {"label": "Other", "canonical": "Other"}


def test_session_payload_includes_report_an_issue_in_default_categories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"label": "Report an Issue", "canonical": "report_an_issue"}

    cleaner_session = session_for_site(
        tmp_path,
        monkeypatch,
        site_registry=FakeSiteRegistry(display_categories=None),
        system_defaults={},
        role="cleaner",
    )
    site_admin_session = session_for_site(
        tmp_path,
        monkeypatch,
        site_registry=FakeSiteRegistry(display_categories=None),
        system_defaults={},
        role="site_admin",
    )

    assert cleaner_session["sites"][0]["display_categories"][0] == expected
    assert expected in cleaner_session["sites"][0]["display_categories"]
    assert site_admin_session["sites"][0]["display_categories"][0] == expected
    assert expected in site_admin_session["sites"][0]["display_categories"]


def test_session_payload_omits_operator_categories_for_cleaner_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = session_for_site(tmp_path, monkeypatch, site_registry=FakeSiteRegistry(display_categories=None), system_defaults={})

    canonicals = {category["canonical"] for category in session["sites"][0]["display_categories"]}
    assert "baseline" not in canonicals
    assert "pre_engagement" not in canonicals


def test_session_payload_includes_operator_categories_for_site_admin_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = session_for_site(
        tmp_path,
        monkeypatch,
        site_registry=FakeSiteRegistry(display_categories=None),
        system_defaults={},
        role="site_admin",
    )

    assert {"label": "Baseline", "canonical": "baseline"} in session["sites"][0]["display_categories"]
    assert {"label": "Pre-Engagement", "canonical": "pre_engagement"} in session["sites"][0]["display_categories"]


def test_session_payload_includes_prospects_for_site_admin_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "field_capture.server.list_active_prospects",
        lambda _config: [{"prospect_id": "kmf-birch-1", "name": "KMF Birch Ave", "address": "1836 Birch", "account": "KMF"}],
    )
    session = session_for_site(tmp_path, monkeypatch, site_registry=FakeSiteRegistry(), system_defaults={}, role="site_admin")

    assert session["prospects"] == [
        {
            "prospect_id": "kmf-birch-1",
            "name": "KMF Birch Ave",
            "address": "1836 Birch",
            "account": "KMF",
            "viewer_url": session["prospects"][0]["viewer_url"],
        }
    ]
    assert session["prospects"][0]["viewer_url"].startswith("/prospect/kmf-birch-1?token=")


def test_session_payload_empty_prospects_for_cleaner_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_list(_config: object) -> list[dict[str, object]]:
        raise AssertionError("cleaner tokens must not query prospects")

    monkeypatch.setattr("field_capture.server.list_active_prospects", fail_list)
    session = session_for_site(tmp_path, monkeypatch, site_registry=FakeSiteRegistry(), system_defaults={})

    assert session["prospects"] == []


def test_session_payload_does_not_duplicate_operator_categories_when_site_already_has_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = session_for_site(
        tmp_path,
        monkeypatch,
        site_registry=FakeSiteRegistry(
            display_categories=[
                {"label": "Baseline", "canonical": "baseline"},
                {"label": "Restrooms", "canonical": "Restrooms"},
            ]
        ),
        system_defaults={},
        role="site_admin",
    )

    categories = session["sites"][0]["display_categories"]
    assert [category["canonical"] for category in categories].count("baseline") == 1
    assert {"label": "Pre-Engagement", "canonical": "pre_engagement"} in categories


def test_session_resolution_chain_loads_system_defaults_once_per_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_load_system_defaults() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"default_capture_guidance": "Default guidance"}

    app, store, vault_root = start_field_capture_server(tmp_path)
    app.server.site_registry = FakeSiteRegistry(capture_guidance=None)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"])
    monkeypatch.setattr("field_capture.server.load_system_defaults", fake_load_system_defaults)

    status, _headers, body = app.request("GET", f"/api/session?token={token.token_value}")

    assert status == 200
    assert json.loads(body)["sites"][0]["capture_guidance"] == "Default guidance"
    assert calls == 1


def test_session_logs_warning_and_falls_back_on_system_defaults_load_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_load_system_defaults() -> dict[str, object]:
        raise RuntimeError("no couch")

    app, store, vault_root = start_field_capture_server(tmp_path)
    app.server.site_registry = FakeSiteRegistry(capture_guidance=None, display_categories=None)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"])
    monkeypatch.setattr("field_capture.server.load_system_defaults", fake_load_system_defaults)

    status, _headers, body = app.request("GET", f"/api/session?token={token.token_value}")

    assert status == 200
    assert json.loads(body)["sites"][0]["display_categories"] == BUILTIN_FALLBACK_CATEGORIES
    assert "WARNING: system_defaults unavailable" in capsys.readouterr().err


def test_session_falls_back_when_couchdb_url_not_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load_system_defaults() -> dict[str, object]:
        raise RuntimeError("BTQ_COUCHDB_URL must be set")

    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"])
    monkeypatch.setattr("field_capture.server.load_system_defaults", fake_load_system_defaults)

    status, _headers, body = app.request("GET", f"/api/session?token={token.token_value}")

    assert status == 200
    assert json.loads(body)["sites"][0]["capture_guidance"] == ""
    assert json.loads(body)["sites"][0]["display_categories"] == BUILTIN_FALLBACK_CATEGORIES


def test_session_isolates_per_site_lookup_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = session_for_site(
        tmp_path,
        monkeypatch,
        site_registry=FakeSiteRegistry(guidance_error=RuntimeError("bad guidance"), categories_error=RuntimeError("bad categories")),
        system_defaults={
            "default_capture_guidance": "Default guidance",
            "default_display_categories": [{"label": "Default", "canonical": "Other"}],
        },
    )

    assert session["sites"][0]["capture_guidance"] == "Default guidance"
    assert session["sites"][0]["display_categories"] == [{"label": "Default", "canonical": "Other"}]


def test_session_response_shape_unchanged_for_existing_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = session_for_site(tmp_path, monkeypatch, site_registry=FakeSiteRegistry(), system_defaults={})
    site = session["sites"][0]

    assert session["person"] == {"person_id": "jordan-avery", "name": "Avery, Jordan", "first": "Jordan"}
    assert {"token_id", "label", "expires_at", "token_type", "can_submit", "can_view_site"} <= set(session["token"])
    assert site["site_id"] == "7050"
    assert site["name"] == "Summit Wire"
    assert site["account"] is None
    assert site["viewer_url"].startswith("/site/7050?token=")


def test_root_with_token_single_site_redirects_303_to_site(tmp_path: Path) -> None:
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"])

    status, headers, body = app.request("GET", f"/?token={token.token_value}")

    assert status == 303
    assert headers["location"] == f"/site/7050?token={token.token_value}"
    assert body == b"Redirecting to your site..."


def test_root_with_token_multi_site_renders_picker_html(tmp_path: Path) -> None:
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_site(vault_root, "7060", "Apex Powdered Metals")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050", "7060"])

    status, headers, body = app.request("GET", f"/?token={token.token_value}")

    decoded = body.decode("utf-8")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert f'<a href="/site/7050?token={token.token_value}">' in decoded
    assert f'<a href="/site/7060?token={token.token_value}">' in decoded


def test_root_without_token_returns_404_html(tmp_path: Path) -> None:
    app, _store, _vault_root = start_field_capture_server(tmp_path)

    status, headers, body = app.request("GET", "/")

    assert status == 404
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert "site url required" in body.decode("utf-8").lower()


def test_root_invalid_token_returns_401_or_403(tmp_path: Path) -> None:
    app, _store, _vault_root = start_field_capture_server(tmp_path)

    status, _headers, _body = app.request("GET", "/?token=bogus-token")

    assert status in {401, 403}


def test_root_token_with_zero_sites_returns_404_html(tmp_path: Path) -> None:
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="missing-site")
    token = store.create_token("jordan-avery", site_ids=["missing-site"])

    status, headers, body = app.request("GET", f"/?token={token.token_value}")

    assert status == 404
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert "no sites assigned" in body.decode("utf-8").lower()


def test_site_view_found_site_uses_canonical_registry_then_requires_access(tmp_path: Path) -> None:
    app, _store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")

    status, _headers, body = app.request("GET", "/site/7050?format=json")

    assert status == 403
    assert json.loads(body)["error"] == "access_required"


def test_site_view_unknown_site_uses_existing_not_found_fallback(tmp_path: Path) -> None:
    app, _store, _vault_root = start_field_capture_server(tmp_path)

    status, _headers, body = app.request("GET", "/site/missing?format=json")

    assert status == 404
    assert json.loads(body)["error"] == "site_uploads_not_found"


def test_submit_rejects_baseline_category_from_cleaner_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"])
    monkeypatch.setattr("field_capture.server.load_system_defaults", lambda: {})
    body, content_type = multipart_body(
        valid_submit_fields("baseline"),
        [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")],
    )

    status, _headers, response_body = app.submit(token.token_value, body, content_type)

    assert status == 403
    assert json.loads(response_body)["error"] == "category_not_allowed"


def test_server_audio_extensions_are_subset_of_worker_supported() -> None:
    accepted = {ext for exts in AUDIO_ALLOWED_EXTENSIONS.values() for ext in exts}
    missing = accepted - SUPPORTED_EXTENSIONS
    assert not missing, (
        f"server accepts audio extensions the transcription worker will "
        f"silently skip: {sorted(missing)}"
    )


def test_submit_rejects_audio_mp4_file_extension(tmp_path: Path) -> None:
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"])
    body, content_type = multipart_body(
        valid_submit_fields("Restrooms"),
        [
            ("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo"),
            ("audio", "voice.mp4", "audio/mp4", b"audio bytes"),
        ],
    )

    status, _headers, response_body = app.submit(token.token_value, body, content_type)

    response = json.loads(response_body)
    assert status == 400
    assert response["error"] == "unsupported_audio_extension"
    assert response["message"] == "Unsupported audio extension: .mp4"


def test_build_capture_document_defaults_target_to_location(tmp_path: Path) -> None:
    session = SimpleNamespace(
        person=SimpleNamespace(person_id="jordan-avery", canonical_name="Avery, Jordan"),
        record=SimpleNamespace(token_id="tok1", label="Jordan phone"),
    )
    doc = build_capture_document(
        valid_submit_fields("Restrooms"),
        [UploadedFile("photos", "sink.jpg", "image/jpeg", b"photo")],
        [],
        session,
        tmp_path / "uploads",
    )

    assert doc["target_type"] == "location"
    assert doc["target_id"] == "7050"
    assert doc["site_id"] == "7050"


def test_build_capture_document_accepts_prospect_target(tmp_path: Path) -> None:
    session = SimpleNamespace(
        person=SimpleNamespace(person_id="jordan-avery", canonical_name="Avery, Jordan"),
        record=SimpleNamespace(token_id="tok1", label="Jordan phone"),
    )
    fields = valid_submit_fields("baseline")
    fields.update({"site": "KMF Birch Ave", "site_id": "", "target_type": "prospect", "target_id": "kmf-birch-1"})
    doc = build_capture_document(
        fields,
        [UploadedFile("photos", "sink.jpg", "image/jpeg", b"photo")],
        [],
        session,
        tmp_path / "uploads",
    )

    assert doc["site"] == "KMF Birch Ave"
    assert doc["site_id"] == ""
    assert doc["target_type"] == "prospect"
    assert doc["target_id"] == "kmf-birch-1"


def test_intentional_fallback_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = SimpleNamespace(sites=())
    capture = {
        "target_type": "prospect",
        "target_id": "kmf-birch-1",
        "site_name": "KMF Birch Ave",
    }

    def fail_load_prospect(_config: object, _prospect_id: str) -> None:
        raise RuntimeError("prospect lookup unavailable")

    monkeypatch.setattr(server_module, "load_prospect", fail_load_prospect)

    assert server_module._current_target_summary(capture, session, object()) == {
        "target_type": "prospect",
        "target_id": "kmf-birch-1",
        "label": "KMF Birch Ave",
    }


def test_submit_rejects_prospect_target_from_cleaner_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"])
    fields = valid_submit_fields("baseline")
    fields.update({"site": "KMF Birch Ave", "site_id": "", "target_type": "prospect", "target_id": "kmf-birch-1"})
    body, content_type = multipart_body(fields, [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")])

    status, _headers, response_body = app.submit(token.token_value, body, content_type)

    assert status == 403
    assert json.loads(response_body)["error"] == "prospect_not_allowed"


def test_submit_rejects_prospect_target_for_won_prospect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"], role="site_admin")
    monkeypatch.setattr("field_capture.server.load_prospect", lambda _config, _prospect_id: {"prospect_id": "kmf-birch-1", "status": "won"})
    fields = valid_submit_fields("baseline")
    fields.update({"site": "KMF Birch Ave", "site_id": "", "target_type": "prospect", "target_id": "kmf-birch-1"})
    body, content_type = multipart_body(fields, [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")])

    status, _headers, response_body = app.submit(token.token_value, body, content_type)

    assert status == 403
    assert json.loads(response_body)["error"] == "prospect_not_allowed"


def test_submit_accepts_baseline_category_from_site_admin_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"], role="site_admin")
    monkeypatch.setattr("field_capture.server.load_system_defaults", lambda: {})
    put_calls: list[dict[str, object]] = []

    def fake_put(_config: object, doc: dict[str, object], *, database: str) -> dict[str, object]:
        put_calls.append(doc)
        return {"ok": True, "id": doc["_id"], "rev": "1-test"}

    monkeypatch.setattr("field_capture.server.get_field_capture_document", lambda *_args: None)
    monkeypatch.setattr("field_capture.server.put_field_capture_document", fake_put)
    body, content_type = multipart_body(
        valid_submit_fields("baseline"),
        [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")],
    )

    status, _headers, response_body = app.submit(token.token_value, body, content_type)

    response = json.loads(response_body)
    assert status == 201
    assert response["status"] == "submitted"
    assert put_calls[0]["qc_category"] == "baseline"
    assert (app.server.upload_dir / "2026-05-02" / "cap-photo-server" / "sink.jpg").read_bytes() == b"\xff\xd8photo"


def test_submit_accepts_existing_category_from_cleaner_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    token = store.create_token("jordan-avery", site_ids=["7050"])
    monkeypatch.setattr("field_capture.server.load_system_defaults", lambda: {})
    put_calls: list[dict[str, object]] = []

    def fake_put(_config: object, doc: dict[str, object], *, database: str) -> dict[str, object]:
        put_calls.append(doc)
        return {"ok": True, "id": doc["_id"], "rev": "1-test"}

    monkeypatch.setattr("field_capture.server.get_field_capture_document", lambda *_args: None)
    monkeypatch.setattr("field_capture.server.put_field_capture_document", fake_put)
    body, content_type = multipart_body(
        valid_submit_fields("Restrooms"),
        [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")],
    )

    status, _headers, response_body = app.submit(token.token_value, body, content_type)

    response = json.loads(response_body)
    assert status == 201
    assert response["status"] == "submitted"
    assert put_calls[0]["qc_category"] == "Restrooms"
    assert (app.server.upload_dir / "2026-05-02" / "cap-photo-server" / "sink.jpg").read_bytes() == b"\xff\xd8photo"


def test_first_name_from_canonical_handles_last_first_format() -> None:
    assert first_name_from_canonical("Avery, Jordan David") == "Jordan"
    assert first_name_from_canonical("Avery,") == ""


def test_first_name_from_canonical_handles_first_last_format() -> None:
    assert first_name_from_canonical("Jordan Avery") == "Jordan"
    assert first_name_from_canonical("Jordan") == "Jordan"


def test_first_name_from_canonical_handles_empty_and_whitespace() -> None:
    assert first_name_from_canonical("") == ""
    assert first_name_from_canonical("   ") == ""


def test_styles_css_status_banner_visual_weight_relaxed() -> None:
    css = Path("project/field_capture/public/styles.css").read_text(encoding="utf-8")

    assert "font-size: clamp(14px, 3vw, 18px)" in css
    assert "font-size: clamp(18px, 4vw, 26px)" not in css


def test_styles_css_app_shell_respects_safe_area_top() -> None:
    css = Path("project/field_capture/public/styles.css").read_text(encoding="utf-8")

    assert "calc(22px + env(safe-area-inset-top))" in css
    assert "calc(14px + env(safe-area-inset-top))" in css


def test_styles_css_error_status_visual_weight_unchanged() -> None:
    css = Path("project/field_capture/public/styles.css").read_text(encoding="utf-8")

    assert '.status-text.hero-status[data-tone="error"]' in css
