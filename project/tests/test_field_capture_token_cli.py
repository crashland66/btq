from __future__ import annotations

from pathlib import Path

from field_capture import server
from token_store import TokenStore


def create_token(db_path: Path, *, site_ids: list[str] | None = None, can_submit: bool = True):
    return TokenStore(db_path).create_token(
        "per_cli",
        role="cleaner",
        site_ids=site_ids or ["S2"],
        can_submit=can_submit,
        can_view_site=True,
    )


def test_cli_token_edit_changes_role(tmp_path: Path) -> None:
    db_path = tmp_path / "tokens.sqlite3"
    created = create_token(db_path)

    result = server.run(["--db", str(db_path), "token", "edit", "--token-id", created.record.token_id, "--role", "site_admin"])

    assert result == 0
    record = TokenStore(db_path).get_token(created.record.token_id)
    assert record is not None
    assert record.role == "site_admin"


def test_cli_token_edit_add_and_remove_sites(tmp_path: Path) -> None:
    db_path = tmp_path / "tokens.sqlite3"
    created = create_token(db_path, site_ids=["S1", "S2"])

    result = server.run(
        [
            "--db",
            str(db_path),
            "token",
            "edit",
            "--token-id",
            created.record.token_id,
            "--add-site",
            "S3",
            "--remove-site",
            "S2",
        ]
    )

    assert result == 0
    record = TokenStore(db_path).get_token(created.record.token_id)
    assert record is not None
    assert record.site_ids == ("S1", "S3")


def test_cli_token_edit_all_sites_flag(tmp_path: Path) -> None:
    db_path = tmp_path / "tokens.sqlite3"
    created = create_token(db_path, site_ids=["S1", "S2"])

    result = server.run(["--db", str(db_path), "token", "edit", "--token-id", created.record.token_id, "--all-sites"])

    assert result == 0
    record = TokenStore(db_path).get_token(created.record.token_id)
    assert record is not None
    assert record.site_ids == ("*",)


def test_cli_token_edit_read_only_flag(tmp_path: Path) -> None:
    db_path = tmp_path / "tokens.sqlite3"
    created = create_token(db_path, can_submit=True)

    result = server.run(["--db", str(db_path), "token", "edit", "--token-id", created.record.token_id, "--read-only"])

    assert result == 0
    record = TokenStore(db_path).get_token(created.record.token_id)
    assert record is not None
    assert record.can_submit is False


def test_cli_token_edit_unknown_token_id_exits_1(tmp_path: Path) -> None:
    db_path = tmp_path / "tokens.sqlite3"

    result = server.run(["--db", str(db_path), "token", "edit", "--token-id", "fct_missing"])

    assert result == 1
