from __future__ import annotations

from pathlib import Path

import pytest

from queue_processor import main as qp
from queue_processor.handlers import _shared as shared


def context(tmp_path: Path) -> qp.RunContext:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    return qp.RunContext(
        project_root=tmp_path,
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=False,
        valid_site_ids={"7050"},
        site_id_to_opportunities_dir={},
    )


def test_promote_prospect_retargets_captures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captures = [
        {"_id": f"cap-{idx}", "type": "field_capture", "target_type": "prospect", "target_id": "x", "site_id": ""}
        for idx in range(3)
    ]
    written: list[dict[str, object]] = []
    prospect_writes: list[dict[str, object]] = []
    monkeypatch.setattr(shared, "_field_capture_config", lambda: object())
    monkeypatch.setattr(shared, "_field_capture_database", lambda: "btq_field_captures")
    monkeypatch.setattr(shared, "load_prospect", lambda _config, prospect_id: {"_id": f"prospect_{prospect_id}", "prospect_id": prospect_id, "status": "open"})
    monkeypatch.setattr(shared, "_site_id_registered", lambda site_id: site_id == "7050")
    monkeypatch.setattr(shared, "_find_prospect_captures", lambda _config, prospect_id, *, database: captures)
    monkeypatch.setattr(shared, "put_field_capture_document", lambda _config, doc, *, database: written.append(dict(doc)) or {"ok": True})
    monkeypatch.setattr(shared, "write_prospect", lambda _config, *, prospect: prospect_writes.append(dict(prospect)) or {"ok": True})

    result = qp.handle_promote_prospect({"prospect_id": "x", "site_id": "7050", "actor": "Jordan"}, context(tmp_path))

    assert result["status"] == "promoted"
    assert len(written) == 3
    assert all(doc["target_type"] == "location" for doc in written)
    assert all(doc["target_id"] == "7050" for doc in written)
    assert all(doc["site_id"] == "7050" for doc in written)
    assert all(doc["promoted_from_prospect_id"] == "x" for doc in written)
    assert prospect_writes[0]["status"] == "won"
    assert prospect_writes[0]["promoted_to_site_id"] == "7050"


def test_promote_prospect_fails_when_site_not_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "_field_capture_config", lambda: object())
    monkeypatch.setattr(shared, "load_prospect", lambda _config, prospect_id: {"_id": f"prospect_{prospect_id}", "prospect_id": prospect_id, "status": "open"})
    monkeypatch.setattr(shared, "_site_id_registered", lambda _site_id: False)

    with pytest.raises(qp.QueueProcessorError, match="site not registered"):
        qp.handle_promote_prospect({"prospect_id": "x", "site_id": "9999", "actor": "Jordan"}, context(tmp_path))


def test_promote_prospect_idempotent_on_already_won_prospect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "_field_capture_config", lambda: object())
    monkeypatch.setattr(
        shared,
        "load_prospect",
        lambda _config, prospect_id: {
            "_id": f"prospect_{prospect_id}",
            "prospect_id": prospect_id,
            "status": "won",
            "promoted_to_site_id": "7050",
        },
    )

    result = qp.handle_promote_prospect({"prospect_id": "x", "site_id": "7050", "actor": "Jordan"}, context(tmp_path))

    assert result == {"status": "already_promoted", "capture_count": 0, "intake_count": 0}
