from __future__ import annotations

from pathlib import Path

import pytest

from field_capture.auth import TokenStore


def create_store(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "tokens.sqlite3")


def test_update_token_changes_role_only(tmp_path: Path) -> None:
    store = create_store(tmp_path)
    created = store.create_token("per_alice", site_ids=["S2", "S1"], can_submit=True, can_view_site=True)
    before = store.get_token(created.record.token_id)
    assert before is not None

    updated = store.update_token(created.record.token_id, role="site_admin")

    assert updated is not None
    assert updated.role == "site_admin"
    assert updated.token_hash == before.token_hash
    assert updated.person_id == before.person_id
    assert updated.site_ids == before.site_ids
    assert updated.can_submit == before.can_submit
    assert updated.can_view_site == before.can_view_site


def test_update_token_changes_site_ids(tmp_path: Path) -> None:
    store = create_store(tmp_path)
    created = store.create_token("per_alice", site_ids=["S1"])
    before_hash = created.record.token_hash

    updated = store.update_token(created.record.token_id, site_ids=["S3", "S2"])

    assert updated is not None
    assert updated.site_ids == ("S2", "S3")
    assert updated.token_hash == before_hash


def test_update_token_can_submit_false_sets_read_only(tmp_path: Path) -> None:
    store = create_store(tmp_path)
    created = store.create_token("per_alice", can_submit=True)

    updated = store.update_token(created.record.token_id, can_submit=False)

    assert updated is not None
    assert updated.can_submit is False
    assert store.get_token(created.record.token_id).can_submit is False


def test_update_token_invalid_role_raises(tmp_path: Path) -> None:
    store = create_store(tmp_path)
    created = store.create_token("per_alice")

    with pytest.raises(ValueError):
        store.update_token(created.record.token_id, role="owner")


def test_update_token_returns_none_for_unknown_token_id(tmp_path: Path) -> None:
    store = create_store(tmp_path)

    assert store.update_token("fct_missing", role="site_admin") is None


def test_update_token_does_not_touch_token_hash(tmp_path: Path) -> None:
    store = create_store(tmp_path)
    created = store.create_token("per_alice", token_type="capture")
    before_hash = created.record.token_hash

    updated = store.update_token(created.record.token_id, token_type="admin_viewer", can_view_site=False)
    reread = store.get_token(created.record.token_id)

    assert updated is not None
    assert reread is not None
    assert updated.token_hash == before_hash
    assert reread.token_hash == before_hash


def test_update_token_returns_none_for_revoked_token(tmp_path: Path) -> None:
    store = create_store(tmp_path)
    created = store.create_token("per_alice")
    assert store.revoke_token(created.record.token_id)

    assert store.update_token(created.record.token_id, role="site_admin") is None
