from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from field_capture.auth import TokenStore
from ops_dashboard.app import route_response, route_response_with_headers
from ops_dashboard.sections import tokens
from tests.test_ops_dashboard import request_text


RAW_VALUES: list[str] = []


def setup_function() -> None:
    tokens.RAW_TOKEN_FLASH.clear()


@pytest.fixture(autouse=True)
def disable_token_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_TOKEN_SYNC_DISABLED", "1")


def create_token(runtime_root: Path, **kwargs: object):
    created = TokenStore(runtime_root / "field_capture_tokens.sqlite3").create_token(person_id="per_alice", label=str(kwargs.pop("label", "Alice phone")), **kwargs)
    RAW_VALUES.append(created.token_value)
    return created


def test_handle_new_post_calls_sync_with_upsert_payload(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.delenv("BTQ_TOKEN_SYNC_DISABLED", raising=False)
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_sync(action: str, payload: dict[str, object]) -> tuple[bool, str]:
        calls.append((action, payload))
        return True, ""

    monkeypatch.setattr(tokens, "sync_token_to_vps", fake_sync)

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/new",
        runtime_root,
        b"person_id=per_admin&label=Admin&token_type=admin_viewer&site_ids=*",
    )

    token_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    assert status == HTTPStatus.SEE_OTHER
    assert calls[0][0] == "upsert"
    assert calls[0][1]["action"] == "upsert"
    row = calls[0][1]["row"]
    assert isinstance(row, dict)
    assert row["token_id"] == token_id
    assert row["token_hash"]
    assert row["role"] == "cleaner"
    assert "token_value" not in row


def test_handle_revoke_post_calls_sync_with_revoke_payload(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("BTQ_TOKEN_SYNC_DISABLED", "1")
    created = create_token(runtime_root, label="Phone")
    monkeypatch.delenv("BTQ_TOKEN_SYNC_DISABLED", raising=False)
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_sync(action: str, payload: dict[str, object]) -> tuple[bool, str]:
        calls.append((action, payload))
        return True, ""

    monkeypatch.setattr(tokens, "sync_token_to_vps", fake_sync)

    status, _content_type, _body, _headers = route_response_with_headers("POST", "/tokens/revoke", runtime_root, f"token_id={created.record.token_id}".encode())

    assert status == HTTPStatus.SEE_OTHER
    assert calls == [("revoke", {"action": "revoke", "token_id": created.record.token_id})]


def test_tokens_list_renders_metadata_with_copyable_token_value(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root)

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert created.record.token_id[:12] in body
    assert f'data-copy-value="{created.token_value}"' in body
    assert f"<pre>{created.token_value}</pre>" not in body


def test_tokens_table_uses_data_table_class(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root)

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert 'class="data-table"' in body
    assert "<table><tr><th>token_id" not in body


def test_tokens_actions_column_is_pinned_and_present_per_row(tmp_path: Path) -> None:
    # Regression: the Revoke/Regenerate buttons exist for every row and the
    # Actions column is pinned to the right (col-sticky-right) so it stays
    # visible when the wide token table scrolls horizontally.
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root, label="One")
    create_token(runtime_root, label="Two")

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert "col-sticky-right" in body
    # both action forms render for each of the two active token rows
    assert body.count('action="/tokens/revoke"') == 2
    assert body.count('action="/tokens/regenerate"') == 2


def test_tokens_action_buttons_are_glyphs(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root)

    body = request_text("GET", "/tokens", runtime_root)[2]

    # replace = recycle glyph, revoke = red ✕ (danger icon button); no text labels
    assert 'aria-label="Replace token">↻</button>' in body
    assert 'class="icon-btn icon-btn--danger" title="Revoke this token" aria-label="Revoke token">✕</button>' in body
    assert ">Regenerate</button>" not in body
    assert ">Revoke</button>" not in body


def test_tokens_person_column_shows_id_in_compact(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root)

    compact = request_text("GET", "/tokens", runtime_root)[2]
    # a single "Person" column is in the compact default and carries the person_id
    assert ">Person</th>" in compact
    assert created.record.person_id in compact
    # the old split Person ID / Person Name columns are gone
    assert ">Person ID</th>" not in compact
    assert ">Person Name</th>" not in compact


def test_tokens_compact_default_hides_detail_columns_and_toggle_shows_all(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root)

    compact = request_text("GET", "/tokens", runtime_root)[2]
    # essential columns (incl. Person + pinned Actions) always present
    for header in ("Token ID", "Person", "Label", "Token Type", "Site Scope", "Active", "Last Used", "Actions"):
        assert f">{header}</th>" in compact
    # noisy detail columns hidden in the compact default
    for header in ("Role", "Can Submit", "Can View Site", "Created At", "Expires At"):
        assert f">{header}</th>" not in compact

    full = request_text("GET", "/tokens?columns=all", runtime_root)[2]
    for header in ("Role", "Can Submit", "Created At", "Expires At"):
        assert f">{header}</th>" in full


def test_build_person_name_map_resolves_token_slug_from_employee_doc() -> None:
    from ops_dashboard.sections.tokens import _build_person_name_map

    docs = [
        {"type": "employee", "_id": "employee_mercer_glen", "first": "Glen", "last": "Mercer"},
        {"type": "employee", "_id": "employee_reyes_castillo_jose", "name": "José Reyes Castillo"},
        {"type": "site_issue", "_id": "iss_1"},
    ]
    names = _build_person_name_map(docs)

    # token person_id is the de-prefixed dash slug of the employee doc id
    assert names["mercer-glen"] == "Glen Mercer"
    assert names["reyes-castillo-jose"] == "José Reyes Castillo"
    # raw doc id also resolves; non-person docs are ignored
    assert names["employee_mercer_glen"] == "Glen Mercer"
    assert "iss_1" not in names


def test_build_person_name_map_resolves_underscore_slug_form() -> None:
    # New behavior under verification: the de-prefixed slug in UNDERSCORE form
    # must resolve (e.g. token person_id stored as `greenwood_megan`).
    from ops_dashboard.sections.tokens import _build_person_name_map

    docs = [
        {"type": "employee", "_id": "employee_greenwood_megan", "name": "Megan Greenwood"},
    ]
    names = _build_person_name_map(docs)

    assert names["greenwood_megan"] == "Megan Greenwood"  # underscore form
    assert names["greenwood-megan"] == "Megan Greenwood"  # dash form still works
    assert names["employee_greenwood_megan"] == "Megan Greenwood"  # raw _id


def test_build_person_name_map_resolves_dash_slug_regression_guard() -> None:
    # Regression guard: a doc whose _id already uses a dash inside the slug
    # (employee_mccartney-michael) must still resolve via the dash form.
    from ops_dashboard.sections.tokens import _build_person_name_map

    docs = [
        {"type": "employee", "_id": "employee_mccartney-michael", "first": "Michael", "last": "McCartney"},
    ]
    names = _build_person_name_map(docs)

    assert names["mccartney-michael"] == "Michael McCartney"
    assert names["employee_mccartney-michael"] == "Michael McCartney"


def test_build_person_name_map_resolves_explicit_person_id_field() -> None:
    from ops_dashboard.sections.tokens import _build_person_name_map

    docs = [
        {"type": "employee", "_id": "employee_dalton_eric", "name": "Eric Dalton", "person_id": "per_eric_d"},
    ]
    names = _build_person_name_map(docs)

    assert names["per_eric_d"] == "Eric Dalton"  # explicit person_id field
    assert names["dalton_eric"] == "Eric Dalton"  # underscore slug
    assert names["dalton-eric"] == "Eric Dalton"  # dash slug


def test_build_person_name_map_keys_both_slug_forms_for_person_and_operator() -> None:
    from ops_dashboard.sections.tokens import _build_person_name_map

    docs = [
        {"type": "person", "_id": "person_smith_jane", "name": "Jane Smith"},
        {"type": "operator", "_id": "operator_jones_bob", "name": "Bob Jones"},
    ]
    names = _build_person_name_map(docs)

    assert names["smith_jane"] == "Jane Smith"
    assert names["smith-jane"] == "Jane Smith"
    assert names["jones_bob"] == "Bob Jones"
    assert names["jones-bob"] == "Bob Jones"


def test_build_person_name_map_ignores_other_types_and_skips_nameless() -> None:
    from ops_dashboard.sections.tokens import _build_person_name_map

    docs = [
        {"type": "site_issue", "_id": "employee_should_be_ignored", "name": "Ignore Me"},
        {"type": "employee", "_id": "employee_no_name"},  # no name/first/last
    ]
    names = _build_person_name_map(docs)

    assert names == {}


def test_render_person_cell_shows_name_when_row_has_person_name_else_id_only() -> None:
    from ops_dashboard.sections.tokens import render_person_cell

    with_name = render_person_cell("greenwood_megan", {"person_name": "Megan Greenwood"})
    assert "Megan Greenwood" in with_name
    assert "greenwood_megan" in with_name

    id_only = render_person_cell("greenwood_megan", {})
    assert "Megan Greenwood" not in id_only
    assert "greenwood_megan" in id_only


def test_tokens_list_filter_by_token_type(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root, label="Worker", token_type="capture")
    create_token(runtime_root, label="Admin", token_type="admin_viewer")

    body = request_text("GET", "/tokens?token_type=admin_viewer", runtime_root)[2]

    assert "Admin" in body
    assert "Worker" not in body


def test_tokens_list_filter_revoked_radio(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    active = create_token(runtime_root, label="Active")
    revoked = create_token(runtime_root, label="Revoked")
    TokenStore(runtime_root / "field_capture_tokens.sqlite3").revoke_token(revoked.record.token_id)

    body = request_text("GET", "/tokens?revoked=revoked", runtime_root)[2]

    assert revoked.record.token_id[:12] in body
    assert active.record.token_id[:12] not in body


def test_tokens_list_last_used_warning_pill_after_90_days(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root)
    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat().replace("+00:00", "Z")
    with TokenStore(runtime_root / "field_capture_tokens.sqlite3").connect() as connection:
        connection.execute("UPDATE field_capture_tokens SET last_used_at = ? WHERE token_id = ?", (old, created.record.token_id))

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert 'class="pill warning"' in body
    assert old in body


def test_token_create_admin_viewer_with_universal_site_scope(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/new",
        runtime_root,
        b"person_id=per_admin&label=Admin&token_type=admin_viewer&can_view_site=1&site_ids=*",
    )

    token_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    record = TokenStore(runtime_root / "field_capture_tokens.sqlite3").get_token(token_id)
    assert status == HTTPStatus.SEE_OTHER
    assert record is not None
    assert record.token_type == "admin_viewer"
    assert record.site_ids == ("*",)


def test_token_create_site_admin_role_from_form(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/new",
        runtime_root,
        b"person_id=per_admin&label=Admin&token_type=capture&role=site_admin&can_submit=1&can_view_site=1&site_ids=*",
    )

    token_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    record = TokenStore(runtime_root / "field_capture_tokens.sqlite3").get_token(token_id)
    assert status == HTTPStatus.SEE_OTHER
    assert record is not None
    assert record.role == "site_admin"


def test_token_new_form_renders_issue_fields(tmp_path: Path) -> None:
    body = request_text("GET", "/tokens/new", tmp_path / "runtime")[2]

    assert 'action="/tokens/new"' in body
    assert 'name="person_id"' in body
    assert 'name="role"' in body
    assert 'value="site_admin"' in body
    assert 'value="admin_viewer"' in body


def test_tokens_set_raw_route_returns_200(tmp_path: Path) -> None:
    """Regression: /tokens/set-raw GET must route to the form, not 404.

    The render_set_raw_form helper has always existed and tokens.render
    dispatches to it on `route_path == "/tokens/set-raw"`, but the path
    was missing from SECTION_ROUTES in app.py, so the link from the
    tokens list page 404'd.
    """
    from http import HTTPStatus

    status, _content_type, body = request_text(
        "GET", "/tokens/set-raw?token_id=fct_test_route", tmp_path / "runtime"
    )

    assert status == HTTPStatus.OK
    assert "Set raw value" in body
    assert "fct_test_route" in body


def test_render_set_raw_form_includes_token_id_and_password_input() -> None:
    body = tokens.render_set_raw_form({"token_id": ["fct_abc"]})

    assert 'name="token_id" value="fct_abc"' in body
    assert 'type="password"' in body
    assert 'name="raw_value"' in body


def test_token_create_returns_raw_value_once_on_reveal_page(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _status, _content_type, _body, headers = route_response_with_headers("POST", "/tokens/new", runtime_root, b"person_id=per_admin&label=Admin&token_type=admin_viewer&site_ids=*")
    token_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    raw = tokens.RAW_TOKEN_FLASH[token_id][0]
    RAW_VALUES.append(raw)

    body = request_text("GET", headers["Location"], runtime_root)[2]

    assert raw in body
    assert "This token will not be shown again. Copy it now." in body


def test_token_reveal_page_clears_flash_after_render(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _status, _content_type, _body, headers = route_response_with_headers("POST", "/tokens/new", runtime_root, b"person_id=per_admin&label=Admin&token_type=admin_viewer&site_ids=*")
    token_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    raw = tokens.RAW_TOKEN_FLASH[token_id][0]
    RAW_VALUES.append(raw)

    first = request_text("GET", headers["Location"], runtime_root)[2]
    second = request_text("GET", headers["Location"], runtime_root)[2]

    assert raw in first
    assert raw not in second


def test_token_create_audit_payload_excludes_raw_token_value(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _status, _content_type, _body, headers = route_response_with_headers("POST", "/tokens/new", runtime_root, b"person_id=per_admin&label=Admin&token_type=admin_viewer&site_ids=*")
    token_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    raw = tokens.RAW_TOKEN_FLASH[token_id][0]
    RAW_VALUES.append(raw)

    audit_text = (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8")
    assert raw not in audit_text


def test_token_create_audit_payload_includes_label_and_person_id(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    route_response("POST", "/tokens/new", runtime_root, b"person_id=per_admin&label=Admin&token_type=admin_viewer&site_ids=*")

    payload = json.loads((runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()[0])
    assert payload["payload"]["label"] == "Admin"
    assert payload["payload"]["person_id"] == "per_admin"


def test_token_revoke_marks_token_revoked_and_audits(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Phone")

    status, _content_type, _body, _headers = route_response_with_headers("POST", "/tokens/revoke", runtime_root, f"token_id={created.record.token_id}".encode())

    record = TokenStore(runtime_root / "field_capture_tokens.sqlite3").get_token(created.record.token_id)
    assert status == HTTPStatus.SEE_OTHER
    assert record is not None and record.revoked


def test_token_revoke_audit_payload_includes_label_and_person_id(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Phone")

    route_response("POST", "/tokens/revoke", runtime_root, f"token_id={created.record.token_id}".encode())
    payload = json.loads((runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()[0])

    assert payload["payload"]["label"] == "Phone"
    assert payload["payload"]["person_id"] == "per_alice"


def test_regenerate_creates_replacement_and_revokes_source(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    expires_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    created = create_token(
        runtime_root,
        label="Continental field capture - Cody",
        expires_at=expires_at,
        can_submit=False,
        can_view_site=True,
        role="site_admin",
        token_type="client_viewer",
        site_ids=("site_continental", "site_north"),
    )
    source_before = created.record
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_sync(action: str, payload: dict[str, object]) -> tuple[bool, str]:
        calls.append((action, payload))
        return True, ""

    monkeypatch.setattr(tokens, "sync_token_to_vps", fake_sync)

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/regenerate",
        runtime_root,
        f"token_id={source_before.token_id}".encode(),
    )

    query = parse_qs(urlsplit(headers["Location"]).query)
    new_id = query["token_id"][0]
    store = TokenStore(runtime_root / "field_capture_tokens.sqlite3")
    old_record = store.get_token(source_before.token_id)
    new_record = store.get_token(new_id)
    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"].startswith("/tokens?issued=1&")
    assert query["old_token_id"] == [source_before.token_id]
    assert old_record is not None and old_record.revoked is True
    assert new_record is not None and new_record.revoked is False
    assert new_record.person_id == source_before.person_id
    assert new_record.label == source_before.label
    assert new_record.token_type == source_before.token_type
    assert new_record.role == source_before.role
    assert new_record.site_ids == source_before.site_ids
    assert new_record.can_submit == source_before.can_submit
    assert new_record.can_view_site == source_before.can_view_site
    assert new_record.expires_at == source_before.expires_at
    assert calls[0][0] == "upsert"
    assert calls[1] == ("revoke", {"action": "revoke", "token_id": source_before.token_id})


def test_regenerate_returns_error_for_unknown_token(tmp_path: Path) -> None:
    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/regenerate",
        tmp_path / "runtime",
        b"token_id=fct_missing",
    )

    assert status == HTTPStatus.SEE_OTHER
    assert parse_qs(urlsplit(headers["Location"]).query)["error"] == ["unknown_token"]


def test_regenerate_returns_error_for_already_revoked_token(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Revoked phone")
    TokenStore(runtime_root / "field_capture_tokens.sqlite3").revoke_token(created.record.token_id)

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/regenerate",
        runtime_root,
        f"token_id={created.record.token_id}".encode(),
    )

    assert status == HTTPStatus.SEE_OTHER
    assert parse_qs(urlsplit(headers["Location"]).query)["error"] == ["token_already_revoked"]


def test_regenerate_audit_log_records_both_actions(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Phone")

    _status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/regenerate",
        runtime_root,
        f"token_id={created.record.token_id}".encode(),
    )

    new_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    entries = [
        json.loads(line)
        for line in (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()
    ]
    regenerate_entries = [entry for entry in entries if entry["route"] == "/tokens/regenerate"]
    revoke_entries = [entry for entry in entries if entry["route"] == "/tokens/revoke"]
    assert regenerate_entries
    assert f"replacement token_id={new_id}" in regenerate_entries[0]["result_summary"]
    assert f"old token_id={created.record.token_id}" in regenerate_entries[0]["result_summary"]
    assert revoke_entries
    assert revoke_entries[0]["payload"]["via"] == "regenerate"
    assert f"revoked token_id={created.record.token_id}" in revoke_entries[0]["result_summary"]


def test_regenerate_audit_log_never_logs_raw_value(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Phone")

    _status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/regenerate",
        runtime_root,
        f"token_id={created.record.token_id}".encode(),
    )

    new_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    raw = tokens.RAW_TOKEN_FLASH[new_id][0]
    audit_text = (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8")
    assert raw.startswith("fc_")
    assert raw not in audit_text


def test_regenerate_button_present_for_active_token_row(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root, label="Active phone")

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert 'action="/tokens/regenerate"' in body


def test_regenerate_button_absent_for_revoked_token_row(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Revoked phone")
    TokenStore(runtime_root / "field_capture_tokens.sqlite3").revoke_token(created.record.token_id)

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert 'action="/tokens/regenerate"' not in body
    assert 'action="/tokens/revoke"' in body


def test_regenerate_redirect_includes_old_token_id_for_reveal_screen(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Phone")

    _status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/regenerate",
        runtime_root,
        f"token_id={created.record.token_id}".encode(),
    )

    body = request_text("GET", headers["Location"], runtime_root)[2]
    assert f"Regenerated from <code>{created.record.token_id}</code>" in body
    assert "The previous token is now revoked. Send the new link to the recipient." in body


def test_handle_set_raw_post_succeeds_and_redirects_with_set_raw_flag(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Legacy phone")
    store = TokenStore(runtime_root / "field_capture_tokens.sqlite3")
    with store.connect() as connection:
        connection.execute("UPDATE field_capture_tokens SET token_value = NULL WHERE token_id = ?", (created.record.token_id,))

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/set-raw",
        runtime_root,
        f"token_id={created.record.token_id}&raw_value={created.token_value}".encode(),
    )

    record = store.get_token(created.record.token_id)
    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/tokens?set_raw=1"
    assert record is not None
    assert record.token_value == created.token_value


def test_handle_set_raw_post_redirects_with_error_on_mismatch(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Legacy phone")

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/set-raw",
        runtime_root,
        f"token_id={created.record.token_id}&raw_value=fc_wrong_value".encode(),
    )

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"].startswith(f"/tokens/set-raw?token_id={created.record.token_id}&error=")


def test_set_raw_handler_never_logs_raw_value_to_audit(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Legacy phone")
    store = TokenStore(runtime_root / "field_capture_tokens.sqlite3")
    with store.connect() as connection:
        connection.execute("UPDATE field_capture_tokens SET token_value = NULL WHERE token_id = ?", (created.record.token_id,))

    route_response_with_headers(
        "POST",
        "/tokens/set-raw",
        runtime_root,
        f"token_id={created.record.token_id}&raw_value={created.token_value}".encode(),
    )

    audit_text = (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8")
    assert created.token_value not in audit_text


def test_audit_log_never_contains_raw_token_value_across_full_test_run(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _status, _content_type, _body, headers = route_response_with_headers("POST", "/tokens/new", runtime_root, b"person_id=per_admin&label=Admin&token_type=admin_viewer&site_ids=*")
    token_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    raw = tokens.RAW_TOKEN_FLASH[token_id][0]
    RAW_VALUES.append(raw)

    audit_text = (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8")
    assert all(value not in audit_text for value in RAW_VALUES)


def test_active_column_renders_green_yes_for_unrevoked_token(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root, label="Active phone")

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert "<th data-priority=\"2\">Active</th>" in body
    assert '<span class="pill success">Yes</span>' in body


def test_active_column_renders_warning_no_for_revoked_token(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Revoked phone")
    TokenStore(runtime_root / "field_capture_tokens.sqlite3").revoke_token(created.record.token_id)

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert "<th data-priority=\"2\">Active</th>" in body
    assert '<span class="pill warning">No</span>' in body


def test_copy_button_data_copy_value_is_raw_token_for_new_token(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="New phone")
    store = TokenStore(runtime_root / "field_capture_tokens.sqlite3")
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO field_capture_tokens
              (token_id, token_hash, person_id, created_at, expires_at, revoked, label, last_used_at,
               can_submit, can_view_site, token_type, site_ids)
            VALUES (?, ?, ?, ?, NULL, 0, ?, NULL, 1, 1, 'capture', '[]')
            """,
            ("fct_legacy", "legacy_hash", "per_alice", created.record.created_at, "Legacy phone"),
        )

    body = request_text("GET", "/tokens", runtime_root)[2]
    legacy_row = body[body.index("fct_legacy") : body.index("Legacy phone")]

    assert f'data-copy-value="{created.token_value}"' in body
    assert f'data-copy-value="{created.record.token_id}"' not in body
    assert 'data-copy-value="fc_' not in legacy_row
    assert 'href="/tokens/set-raw?token_id=fct_legacy"' in legacy_row
    assert "Set..." in legacy_row


def test_new_form_uses_admin_form_layout_and_person_dropdown(monkeypatch) -> None:
    # Layout fix: the issue-token form is width-constrained like sites/employees.
    # Typo guard: person_id is a <select> of known people, not a free-text box.
    monkeypatch.setattr(
        tokens,
        "load_employees",
        lambda: [
            {"person_id": "dalton_eric", "first": "Eric", "last": "Dalton", "type": "employee"},
            {"person_id": "reed_taylor", "first": "Taylor", "last": "Reed", "type": "employee"},
        ],
    )
    out = tokens.render_new_form({})
    assert 'action="/tokens/new" class="admin-form"' in out
    assert '<select name="person_id" required>' in out
    assert '<option value="dalton_eric">Dalton, Eric (dalton_eric)</option>' in out
    assert '<input name="person_id"' not in out  # no free-text fallback when roster loads


def test_new_form_falls_back_to_text_input_when_roster_unavailable(monkeypatch) -> None:
    def boom() -> list:
        raise RuntimeError("couchdb unreachable")

    monkeypatch.setattr(tokens, "load_employees", boom)
    out = tokens.render_new_form({})
    # Issuance must not hard-fail on a roster hiccup — degrade to free text.
    assert '<input name="person_id" required' in out
    assert 'class="admin-form"' in out
