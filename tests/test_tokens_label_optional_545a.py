"""545a — verifier gates for the uncommitted diff on top of 545 (Label column
removal, already verified) that makes the Label field genuinely OPTIONAL:

  1. The Issue Token form's ``label`` input drops ``required`` and is
     relabeled "Label (optional)".
  2. ``handle_new_post`` no longer raises ``label_required`` for a blank
     label.
  3. ``render_list`` no longer reads ``label_contains`` from the query and
     the filter rail no longer renders that input.

Drives the real routes via ``route_response_with_headers`` / ``request_text``
(no internals monkeypatched except the roster and, where noted, the sync
transport). Sandbox identities only.
"""
from __future__ import annotations

import json
import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from field_capture.auth import TokenStore
from ops_dashboard.app import route_response_with_headers
from ops_dashboard.sections import tokens
from tests.test_ops_dashboard import request_text


SANDBOX_DOC: dict[str, object] = {
    "_id": "employee_sandbox_user",
    "type": "employee",
    "status": "active",
    "person_id": "per_sandbox",
    "first": "Sandy",
    "last": "Sandbox",
    "job": "SANDBOX",
}


@pytest.fixture(autouse=True)
def _sandbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_TOKEN_SYNC_DISABLED", "1")
    monkeypatch.setattr(tokens, "load_employees", lambda: [dict(SANDBOX_DOC)])
    tokens.RAW_TOKEN_FLASH.clear()


def create_token(runtime_root: Path, **kwargs: object):
    person_id = str(kwargs.pop("person_id", "per_sandbox"))
    label = str(kwargs.pop("label", ""))
    return TokenStore(runtime_root / "field_capture_tokens.sqlite3").create_token(
        person_id=person_id, label=label, **kwargs
    )


def _audit_entries(runtime_root: Path, route: str) -> list[dict[str, object]]:
    log_path = runtime_root / "logs" / "admin_audit.log"
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    return [entry for entry in entries if entry["route"] == route]


def _row_token_ids(body: str) -> set[str]:
    return set(re.findall(r'<code title="([^"]+)">', body))


# --- 1. Issue Token form: label optional, person picker unaffected ----------


def test_new_form_label_input_has_no_required_attribute(tmp_path: Path) -> None:
    body = request_text("GET", "/tokens/new", tmp_path / "runtime")[2]

    match = re.search(r'<input name="label"[^>]*>', body)
    assert match is not None, "expected a label <input> in the Issue Token form"
    assert "required" not in match.group(0)


def test_new_form_shows_label_optional_wording(tmp_path: Path) -> None:
    body = request_text("GET", "/tokens/new", tmp_path / "runtime")[2]

    assert "Label (optional)" in body


def test_new_form_person_id_select_still_present_with_active_roster(tmp_path: Path) -> None:
    body = request_text("GET", "/tokens/new", tmp_path / "runtime")[2]

    assert '<select name="person_id" required>' in body
    assert '<option value="per_sandbox">Sandbox, Sandy (per_sandbox)</option>' in body


# --- 2. issuance no longer requires a label ----------------------------------


def test_new_post_with_empty_label_issues_a_token_with_empty_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        b"person_id=per_sandbox&label=&token_type=capture&site_ids=SANDBOX",
    )

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"].startswith("/tokens?issued=1")
    token_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]

    store = TokenStore(runtime_root / "field_capture_tokens.sqlite3")
    records = store.list_tokens()
    assert len(records) == 1
    assert records[0].token_id == token_id
    assert records[0].label == ""

    upserts = [payload for action, payload in calls if action == "upsert"]
    assert len(upserts) == 1
    row = upserts[0]["row"]
    assert isinstance(row, dict)
    assert row["label"] == ""

    new_entries = _audit_entries(runtime_root, "/tokens/new")
    assert new_entries
    assert new_entries[0]["result_summary"].startswith("success:")


def test_new_post_with_nonempty_label_still_stores_the_label(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/new",
        runtime_root,
        b"person_id=per_sandbox&label=Sandy+phone&token_type=capture&site_ids=SANDBOX",
    )

    assert status == HTTPStatus.SEE_OTHER
    token_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    record = TokenStore(runtime_root / "field_capture_tokens.sqlite3").get_token(token_id)
    assert record is not None
    assert record.label == "Sandy phone"


def test_new_post_with_empty_person_id_fails_before_any_roster_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_root = tmp_path / "runtime"
    calls = {"count": 0}

    def counting_loader() -> list[dict[str, object]]:
        calls["count"] += 1
        return [dict(SANDBOX_DOC)]

    monkeypatch.setattr(tokens, "load_employees", counting_loader)

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/new",
        runtime_root,
        b"person_id=&label=&token_type=capture&site_ids=SANDBOX",
    )

    assert status == HTTPStatus.SEE_OTHER
    assert parse_qs(urlsplit(headers["Location"]).query)["error"] == ["person_id_required"]
    assert calls["count"] == 0, "person_id_required must fail before load_employees() is ever called"

    store = TokenStore(runtime_root / "field_capture_tokens.sqlite3")
    assert store.list_tokens() == []


# --- 3. label_contains filter is gone ----------------------------------------


def test_label_contains_query_param_no_longer_filters_the_list(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root, label="Alice phone")
    create_token(runtime_root, label="Bob tablet")

    unfiltered = request_text("GET", "/tokens?revoked=all", runtime_root)[2]
    filtered = request_text("GET", "/tokens?revoked=all&label_contains=zzz", runtime_root)[2]

    unfiltered_ids = _row_token_ids(unfiltered)
    filtered_ids = _row_token_ids(filtered)
    assert len(unfiltered_ids) == 2
    assert filtered_ids == unfiltered_ids


def test_filter_rail_has_no_label_contains_input_but_keeps_other_controls(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root, label="Alice phone")

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert 'name="label_contains"' not in body
    assert "Label contains" not in body
    assert 'name="token_type"' in body
    assert 'name="revoked"' in body
    assert 'name="columns"' in body


# --- 4. Tokens table still has no Label header (both column modes) ----------


def test_compact_table_has_no_label_header(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root, label="Alice phone")

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert ">Label</th>" not in body


def test_all_columns_table_has_no_label_header(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root, label="Alice phone")

    body = request_text("GET", "/tokens?columns=all", runtime_root)[2]

    assert ">Label</th>" not in body


# --- 5. regenerate works on a token whose label is already empty ------------


def test_regenerate_a_token_with_empty_label_creates_replacement_with_empty_label(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="")

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/regenerate",
        runtime_root,
        f"token_id={created.record.token_id}".encode(),
    )

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"].startswith("/tokens?issued=1&")
    new_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    store = TokenStore(runtime_root / "field_capture_tokens.sqlite3")
    old_record = store.get_token(created.record.token_id)
    new_record = store.get_token(new_id)
    assert old_record is not None and old_record.revoked is True
    assert new_record is not None
    assert new_record.label == ""
