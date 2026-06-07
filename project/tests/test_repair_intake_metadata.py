from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from event_pipeline.couchdb.repair_intake_metadata import run_repair


def write_intake(runtime_root: Path, capture_id: str, metadata: dict[str, Any]) -> Path:
    intake_dir = runtime_root / "field_capture" / "intake"
    intake_dir.mkdir(parents=True, exist_ok=True)
    path = intake_dir / f"{capture_id}.json"
    payload = {
        "job_id": capture_id,
        "job_type": "photo_capture",
        "payload": {"photos": []},
        "metadata": metadata,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def couch_doc(**overrides: Any) -> dict[str, Any]:
    doc = {
        "_id": "cap-1",
        "capture_id": "cap-1",
        "site_id": "7050",
        "person_id": "person-1",
        "person_name": "Person One",
        "field_capture_token_id": "fct_test123",
        "field_capture_token_label": "Summit porter",
    }
    doc.update(overrides)
    return doc


def complete_metadata(**overrides: Any) -> dict[str, Any]:
    metadata = {
        "capture_id": "cap-1",
        "source": "couchdb",
        "site_id": "7050",
        "person_id": "person-1",
        "person_name": "Person One",
        "field_capture_token_id": "fct_test123",
        "field_capture_token_label": "Summit porter",
    }
    metadata.update(overrides)
    return metadata


def test_repair_dry_run_does_not_write_files(tmp_path: Path) -> None:
    path = write_intake(tmp_path, "cap-1", {"capture_id": "cap-1", "source": "couchdb"})
    before = path.read_text(encoding="utf-8")

    report = run_repair(runtime_root=tmp_path, database="btq_field_captures", fetch_doc=lambda _capture_id: couch_doc())

    assert report["patched"] == 1
    assert path.read_text(encoding="utf-8") == before


def test_repair_apply_patches_missing_identity_fields(tmp_path: Path) -> None:
    path = write_intake(tmp_path, "cap-1", {"capture_id": "cap-1", "source": "couchdb"})

    report = run_repair(runtime_root=tmp_path, database="btq_field_captures", apply=True, fetch_doc=lambda _capture_id: couch_doc())

    metadata = read_json(path)["metadata"]
    assert report["patched"] == 1
    assert metadata["site_id"] == "7050"
    assert metadata["person_id"] == "person-1"
    assert metadata["person_name"] == "Person One"
    assert metadata["field_capture_token_id"] == "fct_test123"
    assert metadata["field_capture_token_label"] == "Summit porter"


def test_repair_skips_non_couchdb_source(tmp_path: Path) -> None:
    path = write_intake(tmp_path, "cap-1", {"capture_id": "cap-1", "source": "field_capture_app"})
    before = path.read_text(encoding="utf-8")

    report = run_repair(runtime_root=tmp_path, database="btq_field_captures", apply=True, fetch_doc=lambda _capture_id: couch_doc())

    assert report["skipped_not_couchdb"] == 1
    assert path.read_text(encoding="utf-8") == before


def test_repair_skips_already_complete_metadata(tmp_path: Path) -> None:
    path = write_intake(tmp_path, "cap-1", complete_metadata())
    before = path.read_text(encoding="utf-8")

    report = run_repair(runtime_root=tmp_path, database="btq_field_captures", apply=True, fetch_doc=lambda _capture_id: couch_doc())

    assert report["already_complete"] == 1
    assert path.read_text(encoding="utf-8") == before


def test_repair_handles_missing_couchdb_doc(tmp_path: Path) -> None:
    path = write_intake(tmp_path, "cap-1", {"capture_id": "cap-1", "source": "couchdb"})
    before = path.read_text(encoding="utf-8")

    report = run_repair(runtime_root=tmp_path, database="btq_field_captures", apply=True, fetch_doc=lambda _capture_id: None)

    assert report["no_couchdb_doc"] == 1
    assert report["errors"] == 0
    assert path.read_text(encoding="utf-8") == before


def test_repair_does_not_overwrite_present_identity_fields(tmp_path: Path) -> None:
    path = write_intake(
        tmp_path,
        "cap-1",
        {
            "capture_id": "cap-1",
            "source": "couchdb",
            "person_id": "original-person",
        },
    )

    run_repair(
        runtime_root=tmp_path,
        database="btq_field_captures",
        apply=True,
        fetch_doc=lambda _capture_id: couch_doc(person_id="different-person"),
    )

    metadata = read_json(path)["metadata"]
    assert metadata["person_id"] == "original-person"
    assert metadata["person_name"] == "Person One"
