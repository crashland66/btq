from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from admin_reporting.server import authenticate_request
from token_store import TokenStore


def create_token(tmp_path: Path, *, role: str, can_view_site: bool = True) -> str:
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery", role=role, can_view_site=can_view_site)
    return created.token_value


def test_read_only_token_can_view_reporting_when_site_view_allowed(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")
    token = create_token(tmp_path, role="read_only", can_view_site=True)

    auth = authenticate_request("", f"token={token}", store)

    assert auth.status == HTTPStatus.OK
    assert auth.record is not None
    assert auth.record.role == "read_only"


def test_read_only_reporting_still_requires_can_view_site(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")
    token = create_token(tmp_path, role="read_only", can_view_site=False)

    auth = authenticate_request("", f"token={token}", store)

    assert auth.status == HTTPStatus.FORBIDDEN
    assert auth.record is None


def test_cleaner_token_still_cannot_view_reporting(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")
    token = create_token(tmp_path, role="cleaner", can_view_site=True)

    auth = authenticate_request("", f"token={token}", store)

    assert auth.status == HTTPStatus.FORBIDDEN
    assert auth.record is None
