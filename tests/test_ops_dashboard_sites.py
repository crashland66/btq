from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib import error
from urllib.parse import parse_qs, urlsplit

import pytest

import ops_dashboard.app as ops_app
from ops_dashboard.app import route_response, route_response_with_headers
from ops_dashboard.sections import sites
from tests.test_ops_dashboard import request_text
from tests.test_site_visits import write_visit


def site_doc(site_id: str = "7050", *, active: bool = True) -> dict[str, object]:
    return {
        "_id": f"site_{site_id}",
        "_rev": "1-abc",
        "type": "site",
        "site_id": site_id,
        "canonical_name": "Summit Wire" if site_id == "7050" else "Inactive Site",
        "active": active,
        "aliases": ["summit", "wire"],
        "note_path": "Accounts/Summit/about.md",
        "vision_context": "Industrial context",
        "capture_guidance": "Capture guidance",
        "display_categories": [{"label": "Restrooms", "canonical": "restrooms"}],
    }


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
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))
    monkeypatch.setattr(
        sites,
        "discover_site_visits",
        lambda _vault_dir, *, site_id, limit: {
            "visits": [
                {
                    "date": "2026-05-14",
                    "confidence": "provisional",
                    "evidence": "All done.",
                    "site_id": "7050",
                    "timestamp": "",
                    "source": "",
                    "visit_key": "",
                    "path": "",
                }
            ]
        },
    )

    body = request_text("GET", "/sites?site_id=7050", tmp_path / "runtime")[2]

    assert "Recent Visits" in body
    assert "2026-05-14" in body


def test_recent_visits_panel_shows_first_name_when_resolved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    people_dir = vault_root / "People"
    people_dir.mkdir(parents=True)
    (people_dir / "jordan-avery.md").write_text(
        """---
person_id: per_test003
name: "Avery, Jordan"
---
""",
        encoding="utf-8",
    )
    write_visit(vault_root, file_date="2026-05-14", site_id="7050", visited_by="per_test003")
    write_visit(vault_root, file_date="2026-05-15", site_id="7050", visited_by="per_unknown")
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))
    monkeypatch.setattr(ops_app, "get_config", lambda: SimpleNamespace(vault_dir=vault_root))

    body = request_text("GET", "/sites?site_id=7050", tmp_path / "runtime")[2]
    recent_visits = body.split("<h2>Recent Visits</h2>", 1)[1]

    assert ">Jordan<" in recent_visits
    assert ">per_unknown<" in recent_visits


def test_site_detail_form_shows_empty_state_when_no_visits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "load_site", lambda site_id: site_doc(site_id))
    monkeypatch.setattr(sites, "discover_site_visits", lambda _vault_dir, *, site_id, limit: {"visits": []})

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

    route_response("POST", "/sites/save", tmp_path / "runtime", valid_body(vision_context="x" * 300))
    payload = json.loads((tmp_path / "runtime" / "logs" / "admin_audit.log").read_text(encoding="utf-8"))

    assert len(payload["payload"]["vision_context"]) == 200


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
    assert puts[0]["_id"] == "site_new_site"


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
