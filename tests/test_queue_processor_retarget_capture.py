from __future__ import annotations

import json
from pathlib import Path

import pytest

from queue_processor import main as qp
from queue_processor.handlers import _shared as shared


def context(tmp_path: Path) -> qp.RunContext:
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return qp.RunContext(
        project_root=tmp_path,
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=False,
        valid_site_ids={"7050"},
        site_id_to_opportunities_dir={},
    )


def patch_common(monkeypatch: pytest.MonkeyPatch, capture: dict[str, object] | None, written: list[dict[str, object]]) -> None:
    monkeypatch.setattr(shared, "_field_capture_config", lambda: object())
    monkeypatch.setattr(shared, "_field_capture_database", lambda: "btq_field_captures")
    monkeypatch.setattr(shared, "get_field_capture_document", lambda _config, _database, _capture_id: capture)
    monkeypatch.setattr(shared, "put_field_capture_document", lambda _config, doc, *, database: written.append(dict(doc)) or {"ok": True})


def test_retarget_to_location_updates_doc_target_and_intake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = {"_id": "cap-1", "capture_id": "cap-1", "target_type": "prospect", "target_id": "kmf", "site_id": ""}
    written: list[dict[str, object]] = []
    patch_common(monkeypatch, capture, written)
    monkeypatch.setattr(shared, "_site_id_registered", lambda site_id: site_id == "7050")
    intake = tmp_path / "runtime" / "field_capture" / "intake"
    intake.mkdir(parents=True)
    (intake / "cap-1.json").write_text(json.dumps({"capture_id": "cap-1", "target_type": "prospect", "target_id": "kmf", "site_id": ""}), encoding="utf-8")

    result = qp.handle_retarget_capture({"capture_id": "cap-1", "new_target_type": "location", "new_target_id": "7050", "actor": "Jordan"}, context(tmp_path))

    assert result == {"status": "retargeted", "capture_id": "cap-1", "intake_updated": True}
    assert written[0]["target_type"] == "location"
    assert written[0]["target_id"] == "7050"
    assert written[0]["site_id"] == "7050"
    payload = json.loads((intake / "cap-1.json").read_text(encoding="utf-8"))
    assert payload["target_type"] == "location"
    assert payload["target_id"] == "7050"
    assert payload["site_id"] == "7050"


def test_retarget_to_prospect_clears_site_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = {"_id": "cap-1", "capture_id": "cap-1", "target_type": "location", "target_id": "7050", "site_id": "7050"}
    written: list[dict[str, object]] = []
    patch_common(monkeypatch, capture, written)
    monkeypatch.setattr(shared, "load_prospect", lambda _config, prospect_id: {"prospect_id": prospect_id, "status": "active"})

    result = qp.handle_retarget_capture({"capture_id": "cap-1", "new_target_type": "prospect", "new_target_id": "kmf", "actor": "Jordan"}, context(tmp_path))

    assert result["status"] == "retargeted"
    assert written[0]["target_type"] == "prospect"
    assert written[0]["target_id"] == "kmf"
    assert written[0]["site_id"] == ""


def test_retarget_idempotent_when_already_at_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = {"_id": "cap-1", "capture_id": "cap-1", "target_type": "location", "target_id": "7050", "site_id": "7050"}
    written: list[dict[str, object]] = []
    patch_common(monkeypatch, capture, written)
    monkeypatch.setattr(shared, "_site_id_registered", lambda site_id: site_id == "7050")

    result = qp.handle_retarget_capture({"capture_id": "cap-1", "new_target_type": "location", "new_target_id": "7050", "actor": "Jordan"}, context(tmp_path))

    assert result == {"status": "already_at_target", "capture_id": "cap-1", "intake_updated": False}
    assert written == []


def test_retarget_fails_when_capture_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_common(monkeypatch, None, [])

    with pytest.raises(qp.QueueProcessorError, match="capture not found"):
        qp.handle_retarget_capture({"capture_id": "missing", "new_target_type": "location", "new_target_id": "7050", "actor": "Jordan"}, context(tmp_path))


def test_retarget_fails_when_destination_site_not_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_common(monkeypatch, {"capture_id": "cap-1", "target_type": "prospect", "target_id": "kmf"}, [])
    monkeypatch.setattr(shared, "_site_id_registered", lambda _site_id: False)

    with pytest.raises(qp.QueueProcessorError, match="site not registered"):
        qp.handle_retarget_capture({"capture_id": "cap-1", "new_target_type": "location", "new_target_id": "9999", "actor": "Jordan"}, context(tmp_path))


def test_retarget_fails_when_destination_prospect_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_common(monkeypatch, {"capture_id": "cap-1", "target_type": "location", "target_id": "7050"}, [])
    monkeypatch.setattr(shared, "load_prospect", lambda _config, prospect_id: {"prospect_id": prospect_id, "status": "lost"})

    with pytest.raises(qp.QueueProcessorError, match="prospect is terminal"):
        qp.handle_retarget_capture({"capture_id": "cap-1", "new_target_type": "prospect", "new_target_id": "kmf", "actor": "Jordan"}, context(tmp_path))


def test_retarget_appends_to_history_each_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = {"_id": "cap-1", "capture_id": "cap-1", "target_type": "location", "target_id": "7050", "site_id": "7050"}
    written: list[dict[str, object]] = []
    patch_common(monkeypatch, capture, written)
    monkeypatch.setattr(shared, "_site_id_registered", lambda site_id: site_id in {"7050", "1801"})

    qp.handle_retarget_capture({"capture_id": "cap-1", "new_target_type": "location", "new_target_id": "1801", "actor": "Jordan"}, context(tmp_path))
    qp.handle_retarget_capture({"capture_id": "cap-1", "new_target_type": "location", "new_target_id": "7050", "actor": "Jordan"}, context(tmp_path))

    assert len(capture["retarget_history"]) == 2
    assert capture["retarget_history"][0]["from_target_id"] == "7050"
    assert capture["retarget_history"][1]["from_target_id"] == "1801"
