"""545 — the Tokens table's Label column is removed; the label DATA stays.

Independent-verifier gates for the uncommitted one-line deletion in
``ops_dashboard/sections/tokens.py`` (the ``{"key": "label", "label": "Label"}``
column entry removed from ``render_list``'s column list). The operator asked
for the Label *column* to go; label data must remain fully functional:
(545 amendment: the label is optional and the label_contains filter is gone; the audit
payload and the VPS sync row still carry it, and 537's confirm-table token
list is untouched by this change (it renders its own markup, not this table).

Sandbox identities only; the only origin used is ``capture.example.com``.
"""
from __future__ import annotations

import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from field_capture.auth import TokenStore
from ops_dashboard.app import route_response_with_headers
from ops_dashboard.sections import tokens
from tests.test_ops_dashboard import request_text


SANDBOX_ROSTER: list[dict[str, object]] = [
    {"_id": "employee_sandbox_alice", "type": "employee", "status": "active", "person_id": "per_alice", "first": "Sam", "last": "Sandbox", "job": "SANDBOX"},
]


@pytest.fixture(autouse=True)
def _sandbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_TOKEN_SYNC_DISABLED", "1")
    monkeypatch.setattr(tokens, "load_employees", lambda: [dict(doc) for doc in SANDBOX_ROSTER])
    monkeypatch.setattr(tokens, "person_name_map", lambda: {"per_alice": "Sam Sandbox"})
    tokens.RAW_TOKEN_FLASH.clear()


def create_token(runtime_root: Path, **kwargs: object):
    label = str(kwargs.pop("label", "Sandy phone"))
    return TokenStore(runtime_root / "field_capture_tokens.sqlite3").create_token(
        person_id="per_alice", label=label, **kwargs
    )


def _headers(body: str) -> list[str]:
    return re.findall(r"<th[^>]*>([^<]+)</th>", body)


def _tbody(body: str) -> str:
    match = re.search(r"<tbody>(.*)</tbody>", body, re.DOTALL)
    assert match, "expected a <tbody> in the rendered table"
    return match.group(1)


# --- 1. header gates ---------------------------------------------------------


def test_compact_headers_are_exactly_the_essential_set_with_no_label(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root)

    body = request_text("GET", "/tokens", runtime_root)[2]

    assert _headers(body) == ["Token ID", "Person", "Role", "Site Scope", "Active", "Actions"]


def test_all_columns_mode_adds_detail_columns_and_still_has_no_label(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root)

    body = request_text("GET", "/tokens?columns=all", runtime_root)[2]
    headers = _headers(body)

    for header in (
        "Token ID",
        "Person",
        "Role",
        "Site Scope",
        "Active",
        "Actions",
        "Token Type",
        "Can Submit",
        "Can View Site",
        "Created At",
        "Expires At",
        "Last Used",
    ):
        assert header in headers, f"missing header {header!r} in columns=all"
    assert "Label" not in headers


# --- 2. label text not rendered as a table cell ------------------------------


def test_label_text_is_not_rendered_as_a_table_cell_in_either_mode(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Sandy phone")

    compact = request_text("GET", "/tokens", runtime_root)[2]
    full = request_text("GET", "/tokens?columns=all", runtime_root)[2]

    # sanity: the row itself is present in both renders
    assert created.record.token_id[:12] in _tbody(compact)
    assert created.record.token_id[:12] in _tbody(full)

    assert "Sandy phone" not in _tbody(compact)
    assert "Sandy phone" not in _tbody(full)


# --- 3. label_contains filter unchanged --------------------------------------


# --- 4. label still required for issuance; stored record carries it ---------


def test_new_post_with_label_succeeds_and_stores_the_label(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/tokens/new",
        runtime_root,
        b"person_id=per_alice&label=Sandy+phone&token_type=capture&site_ids=*",
    )

    assert status == HTTPStatus.SEE_OTHER
    token_id = parse_qs(urlsplit(headers["Location"]).query)["token_id"][0]
    record = TokenStore(runtime_root / "field_capture_tokens.sqlite3").get_token(token_id)
    assert record is not None
    assert record.label == "Sandy phone"


# --- 5. copy button / Set link / test link unchanged -------------------------


def _row_by_token_id(body: str, token_id: str) -> str:
    marker = f'<code title="{token_id}">'
    idx = body.index(marker)
    start = body.rfind("<tr>", 0, idx)
    end = body.index("</tr>", idx)
    return body[start:end]


def test_copy_button_still_present_with_raw_token_value(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Sandy phone")

    body = request_text("GET", "/tokens", runtime_root)[2]
    row = _row_by_token_id(body, created.record.token_id)

    assert f'data-copy-value="{created.token_value}"' in row
    assert "Copy" in row


def test_set_link_present_when_raw_token_value_missing(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    created = create_token(runtime_root, label="Sandy phone")
    store = TokenStore(runtime_root / "field_capture_tokens.sqlite3")
    with store.connect() as connection:
        connection.execute(
            "UPDATE field_capture_tokens SET token_value = NULL WHERE token_id = ?",
            (created.record.token_id,),
        )

    body = request_text("GET", "/tokens", runtime_root)[2]
    row = _row_by_token_id(body, created.record.token_id)

    assert f'href="/tokens/set-raw?token_id={created.record.token_id}"' in row
    assert "Set..." in row
    assert "data-copy-value=" not in row


def test_test_link_present_for_active_token_with_origin_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("BTQ_PUBLIC_CAPTURE_ORIGIN", "https://capture.example.com")
    created = create_token(runtime_root, label="Sandy phone")

    body = request_text("GET", "/tokens", runtime_root)[2]
    row = _row_by_token_id(body, created.record.token_id)

    assert 'class="token-test-link"' in row
    assert f"https://capture.example.com/?token={created.token_value}" in row


# --- 6. mutation-check aid: header count sanity ------------------------------


def test_all_columns_header_count_matches_visible_column_definitions(tmp_path: Path) -> None:
    # Belt-and-suspenders count check alongside the exact-list assertions
    # above: 12 columns remain (was 13 with Label). If a Label column were
    # re-inserted, this count (and the exact-list gates above) would catch it.
    runtime_root = tmp_path / "runtime"
    create_token(runtime_root)

    body = request_text("GET", "/tokens?columns=all", runtime_root)[2]

    assert len(_headers(body)) == 12
