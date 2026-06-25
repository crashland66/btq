from __future__ import annotations

import importlib
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path

import pytest

from token_store import TokenStore


REPORT_ID = "staples_usage_report_2026-01-01_2026-06-25"


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: str) -> None:
        self.status = status
        self.headers = headers
        self.body = body


def fake_reference_docs() -> list[dict[str, object]]:
    return [
        {
            "type": "staples_usage_report",
            "report_id": REPORT_ID,
            "vendor": "Staples",
            "period_start": "2026-01-01",
            "period_end": "2026-06-25",
            "source_file": "synthetic-staples.csv",
        },
        {
            "type": "staples_usage_ship_to_summary",
            "report_id": REPORT_ID,
            "ship_to_name": "ACME TEST SITE",
            "ship_to_numbers": ["ACME-100"],
            "quantity": 12,
            "estimated_monthly_quantity": 2.1,
            "adjusted_gross_sales": 125.5,
            "sku_count": 2,
            "line_count": 3,
        },
        {
            "type": "staples_usage_ship_to_summary",
            "report_id": REPORT_ID,
            "ship_to_name": "BETA TEST SITE",
            "ship_to_numbers": ["BETA-200"],
            "quantity": 5,
            "estimated_monthly_quantity": 0.86,
            "adjusted_gross_sales": 32.25,
            "sku_count": 1,
            "line_count": 1,
        },
        {
            "type": "staples_usage_ship_to_sku_summary",
            "report_id": REPORT_ID,
            "ship_to_name": "ACME TEST SITE",
            "sku_number": "SKU-001",
            "website_item_description": "Synthetic Towels, 12/Carton",
            "quantity": 10,
            "estimated_monthly_quantity": 1.72,
            "average_selling_price_values": [9.5, 10.25],
            "adjusted_gross_sales": 101.25,
            "line_count": 2,
        },
        {
            "type": "staples_usage_row",
            "report_id": REPORT_ID,
            "ship_to_name": "ACME TEST SITE",
            "row_index": 7,
            "ship_to_number": "ACME-100",
            "sku_number": "SKU-001",
            "website_item_description": "Synthetic Towels, 12/Carton",
            "quantity": 6,
            "average_selling_price": 9.5,
            "adjusted_gross_sales": 57,
        },
    ]


def build_fake_couchdb_find(calls: list[tuple[str, dict[str, object]]]):
    docs = fake_reference_docs()

    def fake_couchdb_find(database: str, mango: dict[str, object]) -> dict[str, object]:
        calls.append((database, mango))
        selector = mango.get("selector")
        assert isinstance(selector, dict)
        matches = []
        for doc in docs:
            if all(doc.get(key) == value for key, value in selector.items()):
                matches.append(doc)
        fields = mango.get("fields")
        if isinstance(fields, list):
            matches = [{key: doc[key] for key in fields if key in doc} for doc in matches]
        return {"docs": matches}

    return fake_couchdb_find


def route_get(token_store: TokenStore, path: str, couchdb_find=None, cookie: str = "") -> Response:
    server_module = importlib.import_module("admin_reporting.server")
    status, _content_type, body, headers = server_module.route_response(
        "GET",
        path,
        token_store,
        couchdb_find=couchdb_find,
        cookie_header=cookie,
    )
    return Response(status, headers, body.decode("utf-8"))


def route_post(token_store: TokenStore, path: str, couchdb_find=None) -> Response:
    server_module = importlib.import_module("admin_reporting.server")
    status, _content_type, body, headers = server_module.route_response(
        "POST",
        path,
        token_store,
        couchdb_find=couchdb_find,
    )
    return Response(status, headers, body.decode("utf-8"))


@pytest.fixture()
def token_store(tmp_path: Path) -> TokenStore:
    store = TokenStore(tmp_path / "field_capture_tokens.sqlite3")
    store.initialize()
    return store


def create_admin_token(store: TokenStore, *, expires_at: datetime | None = None) -> str:
    created = store.create_token(
        "per_admin_viewer",
        label="Admin viewer",
        expires_at=expires_at,
        can_submit=False,
        can_view_site=True,
        token_type="admin_viewer",
        site_ids=["*"],
    )
    return created.token_value


def test_token_gate_rejects_missing_unknown_expired_revoked_and_wrong_scope(token_store: TokenStore) -> None:
    valid_token = create_admin_token(token_store)
    expired_token = create_admin_token(token_store, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    revoked = token_store.create_token(
        "per_revoked",
        can_submit=False,
        can_view_site=True,
        token_type="admin_viewer",
    )
    token_store.revoke_token(revoked.record.token_id)
    capture = token_store.create_token("per_capture", can_submit=True, can_view_site=True, token_type="capture")
    no_view = token_store.create_token("per_no_view", can_submit=False, can_view_site=False, token_type="admin_viewer")
    calls: list[tuple[str, dict[str, object]]] = []

    couchdb_find = build_fake_couchdb_find(calls)
    assert route_get(token_store, "/", couchdb_find).status == HTTPStatus.UNAUTHORIZED
    assert route_get(token_store, "/?token=unknown", couchdb_find).status == HTTPStatus.UNAUTHORIZED
    assert route_get(token_store, f"/?token={expired_token}", couchdb_find).status == HTTPStatus.UNAUTHORIZED
    assert route_get(token_store, f"/?token={revoked.token_value}", couchdb_find).status == HTTPStatus.UNAUTHORIZED
    assert route_get(token_store, f"/?token={capture.token_value}", couchdb_find).status == HTTPStatus.FORBIDDEN
    assert route_get(token_store, f"/?token={no_view.token_value}", couchdb_find).status == HTTPStatus.FORBIDDEN
    accepted = route_get(token_store, f"/?token={valid_token}", couchdb_find)

    assert accepted.status == HTTPStatus.OK
    assert "Staples Site Orders" in accepted.body
    assert "ACME TEST SITE" in accepted.body


def test_token_type_allowlist_rejects_otherwise_valid_read_token(token_store: TokenStore) -> None:
    # Isolates the token_type allowlist from the can_submit/can_view_site checks:
    # this token is read-only (can_submit=False, can_view_site=True) yet its
    # token_type is not in the admin_viewer allowlist, so it must be 403.
    wrong_type = token_store.create_token(
        "per_other_reader",
        can_submit=False,
        can_view_site=True,
        token_type="site_viewer",
    )
    calls: list[tuple[str, dict[str, object]]] = []

    response = route_get(token_store, f"/?token={wrong_type.token_value}", build_fake_couchdb_find(calls))

    assert response.status == HTTPStatus.FORBIDDEN
    # No data route work happened for a rejected token.
    assert calls == []
    assert "ACME" not in response.body


def test_cookie_is_httponly_and_no_couch_credentials_leak(token_store: TokenStore, monkeypatch) -> None:
    # Plant fake CouchDB credentials in the environment the way the real
    # deployment would, then confirm they never surface in any rendered HTML
    # nor in the auth cookie handed back to the browser.
    secret_user = "couch_admin_SECRET_USER"
    secret_pass = "couch_p455word_SECRET"
    for key in ("COUCHDB_USER", "COUCHDB_PASSWORD", "BTQ_COUCHDB_URL"):
        monkeypatch.setenv(key, f"{secret_user}:{secret_pass}@db")
    token = create_admin_token(token_store)
    calls: list[tuple[str, dict[str, object]]] = []
    couchdb_find = build_fake_couchdb_find(calls)

    landing = route_get(token_store, f"/?token={token}", couchdb_find)
    set_cookie = landing.headers.get("Set-Cookie", "")
    report = route_get(token_store, "/site-orders?ship_to_name=ACME%20TEST%20SITE", couchdb_find, cookie=set_cookie)

    assert landing.status == HTTPStatus.OK
    assert report.status == HTTPStatus.OK
    # HttpOnly cookie -> no token exposed to JS; SameSite hardening present.
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie
    for leak in (secret_user, secret_pass):
        assert leak not in landing.body
        assert leak not in report.body
        assert leak not in set_cookie


def test_landing_and_site_order_report_render_from_synthetic_find(token_store: TokenStore) -> None:
    token = create_admin_token(token_store)
    calls: list[tuple[str, dict[str, object]]] = []

    couchdb_find = build_fake_couchdb_find(calls)
    landing = route_get(token_store, f"/?token={token}", couchdb_find)
    cookie = landing.headers.get("Set-Cookie", "")
    report = route_get(token_store, "/site-orders?ship_to_name=ACME%20TEST%20SITE", couchdb_find, cookie=cookie)

    assert landing.status == HTTPStatus.OK
    assert "Site-level usage imported from CouchDB references." in landing.body
    assert "ACME TEST SITE" in landing.body
    assert "BETA TEST SITE" in landing.body
    assert "2026-01-01 to 2026-06-25" in landing.body
    assert report.status == HTTPStatus.OK
    assert "SKU-001" in report.body
    assert "Synthetic Towels, 12/Carton" in report.body
    assert "$101.25" in report.body
    assert "Order-Line Detail" in report.body
    assert "ACME-100" in report.body
    assert all(database == "btq_references" for database, _mango in calls)


def test_shared_query_token_link_renders_without_cookie(token_store: TokenStore) -> None:
    token = create_admin_token(token_store)
    calls: list[tuple[str, dict[str, object]]] = []

    response = route_get(token_store, f"/site-orders?ship_to_name=ACME%20TEST%20SITE&token={token}", build_fake_couchdb_find(calls))

    assert response.status == HTTPStatus.OK
    assert "ACME TEST SITE" in response.body
    assert "SKU-001" in response.body


def test_no_write_route_or_couchdb_mutation_path(token_store: TokenStore) -> None:
    token = create_admin_token(token_store)
    calls: list[tuple[str, dict[str, object]]] = []

    response = route_post(token_store, f"/site-orders?token={token}", build_fake_couchdb_find(calls))

    assert response.status == HTTPStatus.METHOD_NOT_ALLOWED
    assert "read_only" in response.body
    assert calls == []


def test_api_health_is_unauthenticated_and_contains_no_report_data(token_store: TokenStore) -> None:
    response = route_get(token_store, "/api/health", build_fake_couchdb_find([]))

    assert response.status == HTTPStatus.OK
    assert response.body == '{"status":"ok"}\n'
    assert "ACME" not in response.body
    assert "Staples" not in response.body


def test_admin_reporting_server_import_does_not_import_ops_dashboard() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "project") + os.pathsep + env.get("PYTHONPATH", "")
    script = """
import importlib
import sys
importlib.import_module("admin_reporting.server")
print("\\n".join(name for name in sys.modules if name == "ops_dashboard" or name.startswith("ops_dashboard.")))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""
