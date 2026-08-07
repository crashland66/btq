from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from urllib import error
from urllib.parse import parse_qs, urlsplit

import pytest

from ops_dashboard.app import route_response, route_response_with_headers
from ops_dashboard.sections import sites
from tests.test_ops_dashboard import request_text


def site_doc(site_id: str = "7050", *, active: bool = True) -> dict[str, object]:
    return {
        "_id": f"location_{site_id}",
        "_rev": "1-abc",
        "type": "location",
        "site_id": site_id,
        "location": "Summit Wire" if site_id == "7050" else "Inactive Site",
        "capture_active": active,
        "aliases": ["summit", "wire"],
        "note_path": "Accounts/Summit/about.md",
        "vision_context": "Industrial context",
        "capture_guidance": "Capture guidance",
        "display_categories": [{"label": "Restrooms", "canonical": "restrooms"}],
    }


def visit_view_row(
    site_id: str,
    visit_date: str,
    *,
    visited_by: str | None = "Jordan Avery",
    confidence: str = "high",
    evidence: str = "All done.",
) -> dict[str, object]:
    doc = {
        "type": "visit",
        "_id": f"visit_{visit_date}",
        "site_id": site_id,
        "date": visit_date,
        "visited_by": visited_by,
        "confidence": confidence,
        "evidence": evidence,
        "source": "test",
        "timestamp": "",
        "visit_key": "",
        "visit_type": "site",
    }
    return {
        "id": doc["_id"],
        "key": [site_id, visit_date],
        "value": {"site_id": site_id, "date": visit_date},
        "doc": doc,
    }


def install_visit_view(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_query_view(
        base_url: str,
        auth_headers: dict[str, str],
        database: str,
        ddoc: str,
        view: str,
        **kwargs: object,
    ) -> list[dict[str, object]]:
        calls.append(
            {
                "base_url": base_url,
                "auth_headers": auth_headers,
                "database": database,
                "ddoc": ddoc,
                "view": view,
                "kwargs": kwargs,
            }
        )
        return rows

    monkeypatch.setattr(sites, "couchdb_base_url", lambda: "http://couchdb.test")
    monkeypatch.setattr(sites, "auth_headers", lambda: {"Authorization": "test"})
    monkeypatch.setattr(sites.couchdb_config, "vault_database", lambda: "btq_vault_test")
    monkeypatch.setattr(sites.couchdb_config, "timeout", lambda: 2.5)
    monkeypatch.setattr(sites, "query_view", fake_query_view)
    return calls


def test_sites_list_renders_all_sites_including_inactive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_sites", lambda: [site_doc(), site_doc("9999", active=False)])

    body = request_text("GET", "/sites", tmp_path / "runtime")[2]

    assert "Summit Wire" in body
    assert "Inactive Site" in body


def test_sites_table_uses_data_table_class(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_sites", lambda: [site_doc()])

    body = request_text("GET", "/sites", tmp_path / "runtime")[2]

    assert 'class="data-table"' in body
    assert "<table><tr><th>site_id" not in body


def test_sites_list_filter_active_radio_works(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_sites", lambda: [site_doc(), site_doc("9999", active=False)])

    body = request_text("GET", "/sites?active=inactive", tmp_path / "runtime")[2]

    assert "Inactive Site" in body
    assert "Summit Wire" not in body


def test_sites_list_filter_site_id_contains(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_sites", lambda: [site_doc(), site_doc("9999", active=False)])

    body = request_text("GET", "/sites?site_id_contains=705", tmp_path / "runtime")[2]

    assert "7050" in body
    assert "Inactive Site" not in body


def test_site_detail_form_prepopulates_from_couchdb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))

    body = request_text("GET", "/sites?site_id=7050", tmp_path / "runtime")[2]

    assert 'value="Summit Wire"' in body
    assert "Industrial context" in body
    assert "Capture guidance" in body


def test_site_detail_form_shows_recent_visits_section(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    long_evidence = "A" * 125
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))
    calls = install_visit_view(
        monkeypatch,
        [
            visit_view_row("9999", "2026-05-15", visited_by="Wrong Site"),
            visit_view_row(
                "7050",
                "2026-05-14",
                visited_by="Jordan Avery",
                confidence="provisional",
                evidence=long_evidence,
            ),
        ],
    )

    body = request_text("GET", "/sites?site_id=7050", tmp_path / "runtime")[2]
    recent_visits = body.split("<h2>Recent Visits</h2>", 1)[1]

    assert "Recent Visits" in body
    assert "2026-05-14" in recent_visits
    assert "Jordan Avery" in recent_visits
    assert "provisional" in recent_visits
    assert f'{"A" * 120}...' in recent_visits
    assert calls == [
        {
            "base_url": "http://couchdb.test",
            "auth_headers": {"Authorization": "test"},
            "database": "btq_vault_test",
            "ddoc": sites.DDOC,
            "view": "visits_by_site_date",
            "kwargs": {"include_docs": True, "timeout": 2.5},
        }
    ]


def test_recent_visits_panel_shows_full_visited_by_from_canonical_doc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))
    install_visit_view(
        monkeypatch,
        [
            visit_view_row("7050", "2026-05-14", visited_by="Jordan Avery"),
            visit_view_row("7050", "2026-05-15", visited_by="Morgan Lee"),
        ],
    )

    body = request_text("GET", "/sites?site_id=7050", tmp_path / "runtime")[2]
    recent_visits = body.split("<h2>Recent Visits</h2>", 1)[1]

    assert ">Morgan Lee<" in recent_visits
    assert ">Jordan Avery<" in recent_visits


def test_recent_visits_panel_allows_missing_visited_by(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))
    install_visit_view(monkeypatch, [visit_view_row("7050", "2026-05-16", visited_by=None)])

    body = request_text("GET", "/sites?site_id=7050", tmp_path / "runtime")[2]
    recent_visits = body.split("<h2>Recent Visits</h2>", 1)[1]

    assert "2026-05-16" in recent_visits
    assert ">None<" not in recent_visits


def test_site_detail_form_shows_empty_state_when_no_visits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))
    install_visit_view(monkeypatch, [])

    body = request_text("GET", "/sites?site_id=7050", tmp_path / "runtime")[2]

    assert "No visits recorded yet" in body


def test_site_display_categories_renders_as_row_editor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))

    body = request_text("GET", "/sites?site_id=7050", tmp_path / "runtime")[2]

    assert '<input type="text" name="display_categories_label"' in body
    assert '<input type="text" name="display_categories_canonical"' in body
    assert '<textarea name="display_categories"' not in body


def test_site_save_does_optimistic_concurrency_put(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    puts: list[dict[str, object]] = []
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))
    monkeypatch.setattr(sites, "request_json", lambda method, path, payload=None: puts.append(payload or {}) or (201, {"ok": True}))

    status, _content_type, _body, headers = route_response_with_headers("POST", "/sites/save", tmp_path / "runtime", valid_body())

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/sites?site_id=7050&message=saved"
    assert puts[0]["_rev"] == "1-abc"


def test_site_save_rev_conflict_rerenders_form_with_notice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_put(_method: str, _path: str, _payload: dict[str, object] | None = None):
        raise error.HTTPError("url", 409, "conflict", {}, None)

    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))
    monkeypatch.setattr(sites, "request_json", fail_put)

    status, _content_type, body = route_response("POST", "/sites/save", tmp_path / "runtime", valid_body())

    assert status == HTTPStatus.OK
    assert b"site was edited elsewhere" in body


def test_site_save_rejects_empty_canonical_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))

    status, _content_type, body, headers = route_response_with_headers("POST", "/sites/save", tmp_path / "runtime", valid_body(canonical_name=""))

    assert status == HTTPStatus.OK
    assert b"canonical_name_required" in body
    assert "Location" not in headers


def test_site_save_preserves_submitted_values_on_validation_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))

    status, _content_type, body, headers = route_response_with_headers("POST", "/sites/save", tmp_path / "runtime", valid_body(display_categories_label="Submitted label", display_categories_canonical=""))

    assert status == HTTPStatus.OK
    assert b'value="Submitted label"' in body
    assert "Location" not in headers


def test_site_save_audit_log_summarizes_long_vision_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))
    monkeypatch.setattr(sites, "request_json", lambda _method, _path, _payload=None: (201, {"ok": True}))

    route_response("POST", "/sites/save", tmp_path / "runtime", valid_body(vision_context_summary="x" * 300))
    payload = json.loads((tmp_path / "runtime" / "logs" / "admin_audit.log").read_text(encoding="utf-8"))

    assert len(payload["payload"]["vision_context_summary"]) == 200


def test_site_new_form_renders_empty_fields(tmp_path: Path) -> None:
    body = request_text("GET", "/sites/new", tmp_path / "runtime")[2]

    assert 'action="/sites/new"' in body
    assert 'name="site_id"' in body


def test_site_new_form_does_not_show_visits_panel(tmp_path: Path) -> None:
    body = request_text("GET", "/sites/new", tmp_path / "runtime")[2]

    assert "Recent Visits" not in body


def test_site_new_post_creates_doc_with_id_pattern(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    puts: list[dict[str, object]] = []
    monkeypatch.setattr(sites, "load_site", lambda _site_id: None)
    monkeypatch.setattr(sites, "request_json", lambda _method, _path, payload=None: puts.append(payload or {}) or (201, {"ok": True}))

    status, _content_type, _body, headers = route_response_with_headers("POST", "/sites/new", tmp_path / "runtime", valid_body(site_id="new_site", **{"_rev": ""}))

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/sites?site_id=new_site&message=created"
    assert puts[0]["_id"] == "location_new_site"


def test_site_new_post_rejects_duplicate_site_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_site", lambda _site_id: site_doc())

    _status, _content_type, _body, headers = route_response_with_headers("POST", "/sites/new", tmp_path / "runtime", valid_body())

    assert "site_id_already_exists" in headers["Location"]


def test_site_new_post_rejects_invalid_site_id_pattern(tmp_path: Path) -> None:
    _status, _content_type, _body, headers = route_response_with_headers("POST", "/sites/new", tmp_path / "runtime", valid_body(site_id="bad id"))

    assert "invalid_site_id_pattern" in headers["Location"]


def test_sites_no_delete_route_exists(tmp_path: Path) -> None:
    status, _content_type, _body = route_response("POST", "/sites/delete", tmp_path / "runtime", b"site_id=7050")

    assert status == HTTPStatus.METHOD_NOT_ALLOWED


def valid_body(**overrides: str) -> bytes:
    data = {
        "site_id": "7050",
        "canonical_name": "Summit Wire",
        "active": "1",
        "aliases": "summit\nwire",
        "note_path": "Accounts/Summit/about.md",
        "vision_context": "Industrial context",
        "capture_guidance": "Capture guidance",
        "display_categories_label": "Restrooms",
        "display_categories_canonical": "restrooms",
        "_rev": "1-abc",
    }
    data.update(overrides)
    return "&".join(f"{key}={quote_for_form(value)}" for key, value in data.items()).encode()


def quote_for_form(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)
