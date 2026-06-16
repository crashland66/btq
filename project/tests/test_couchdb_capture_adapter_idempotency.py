from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from event_pipeline.couchdb_capture_adapter import (
    CaptureAdapterError,
    import_couchdb_capture,
)


# Sandbox-only identity. No real ids/names/paths/hosts.
SANDBOX_CAPTURE_ID = "cap-sandbox-20260615-aaa111"
SANDBOX_PHOTO_REMOTE = (
    f"/srv/sandbox/runtime/uploads/{SANDBOX_CAPTURE_ID}/photo_001.jpg"
)
SANDBOX_HOST = "sandbox.invalid"


class SandboxRegistry:
    def __init__(self, canonical: str | None = "Sandbox Site") -> None:
        self.canonical = canonical
        self.site_ids: list[str] = []

    def resolve_canonical(self, site_id: str) -> str | None:
        self.site_ids.append(site_id)
        return self.canonical


def sandbox_doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": SANDBOX_CAPTURE_ID,
        "_rev": "2-claimed",
        "type": "field_capture",
        "capture_id": SANDBOX_CAPTURE_ID,
        "site_id": "SANDBOX",
        "person_id": "sandbox-user",
        "person_name": "Sandy Sandbox",
        "field_capture_token_id": "fct_sandbox",
        "field_capture_token_label": "Sandbox porter",
        "captured_at": "2026-06-15T14:30:00Z",
        "qc_category": "completed_area",
        "note": "Sandbox lobby complete.",
        "photos": [
            {
                "filename": "photo_001.jpg",
                "mime_type": "image/jpeg",
                "stored_path": SANDBOX_PHOTO_REMOTE,
            }
        ],
        "processing_state": "claimed",
    }
    doc.update(overrides)
    return doc


class RecordingRunner:
    """Fake runner: records calls, fabricates remote stat + scp without real I/O."""

    def __init__(self, remote_sizes: dict[str, int] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.remote_sizes = (
            remote_sizes if remote_sizes is not None else {SANDBOX_PHOTO_REMOTE: 5}
        )

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        if args[0] == "ssh":
            remote_path = args[-1]
            size = self.remote_sizes.get(remote_path, 5)
            return subprocess.CompletedProcess(args, 0, stdout=f"{size}\n", stderr="")
        if args[0] == "scp":
            local_path = Path(args[-1])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"photo")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)

    @property
    def scp_commands(self) -> list[list[str]]:
        return [c for c in self.commands if c and c[0] == "scp"]


def intake_path(runtime_root: Path, capture_id: str) -> Path:
    return runtime_root / "field_capture" / "intake" / f"{capture_id}.json"


def local_photo_path(runtime_root: Path, capture_id: str) -> Path:
    return (
        runtime_root
        / "uploads"
        / "2026-06-15"
        / capture_id
        / "photo_001.jpg"
    )


def seed_first_import(runtime_root: Path) -> RecordingRunner:
    runner = RecordingRunner()
    result = import_couchdb_capture(
        doc=sandbox_doc(),
        runtime_root=runtime_root,
        remote_host=SANDBOX_HOST,
        registry=SandboxRegistry(),
        runner=runner,
    )
    assert result["ok"] is True
    return runner


# --- (crit) existing intake differs -> success, file unchanged, no media re-scp ---


def test_existing_intake_differs_returns_success_and_retains_file(
    tmp_path: Path,
) -> None:
    seed_first_import(tmp_path)

    path = intake_path(tmp_path, SANDBOX_CAPTURE_ID)
    photo = local_photo_path(tmp_path, SANDBOX_CAPTURE_ID)
    assert path.exists() and photo.exists()

    # Mutate the on-disk intake so a freshly regenerated job will DIFFER from it.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["payload"]["note"] = "MANUALLY EDITED NOTE THAT DIFFERS"
    path.write_text(json.dumps(on_disk, indent=2), encoding="utf-8")
    before_bytes = path.read_bytes()

    second_runner = RecordingRunner()
    result = import_couchdb_capture(
        doc=sandbox_doc(),  # regenerated job has the original note -> differs from disk
        runtime_root=tmp_path,
        remote_host=SANDBOX_HOST,
        registry=SandboxRegistry(),
        runner=second_runner,
    )

    # Success, no raise.
    assert result["ok"] is True
    assert result["counts"]["failed"] == 0
    # Existing intake byte-unchanged (not overwritten).
    assert path.read_bytes() == before_bytes
    # Media already present -> runner NOT invoked at all (no scp, no stat).
    assert second_runner.commands == []
    # Intake result carries the retained-on-drift note.
    intake_results = [r for r in result["results"] if r["type"] == "intake_json"]
    assert intake_results and intake_results[0]["action"] == "skipped"
    assert "retained" in intake_results[0].get("note", "")


# --- existing intake matches -> skip/success, no raise ---


def test_existing_intake_matches_skips_and_succeeds(tmp_path: Path) -> None:
    seed_first_import(tmp_path)

    second_runner = RecordingRunner()
    result = import_couchdb_capture(
        doc=sandbox_doc(),
        runtime_root=tmp_path,
        remote_host=SANDBOX_HOST,
        registry=SandboxRegistry(),
        runner=second_runner,
    )

    assert result["ok"] is True
    assert result["counts"] == {
        "copied": 0,
        "skipped": 2,
        "failed": 0,
        "would_copy": 0,
    }
    assert second_runner.commands == []


# --- no existing intake (fresh) -> runner scp's media, intake JSON written ---


def test_fresh_import_scps_media_and_writes_intake(tmp_path: Path) -> None:
    runner = RecordingRunner()
    result = import_couchdb_capture(
        doc=sandbox_doc(),
        runtime_root=tmp_path,
        remote_host=SANDBOX_HOST,
        registry=SandboxRegistry(),
        runner=runner,
    )

    path = intake_path(tmp_path, SANDBOX_CAPTURE_ID)
    photo = local_photo_path(tmp_path, SANDBOX_CAPTURE_ID)

    assert result["ok"] is True
    # 1 photo scp'd + 1 intake JSON written, both report "copied".
    assert result["counts"]["copied"] == 2
    # runner WAS invoked to scp media.
    assert runner.scp_commands == [
        ["scp", f"{SANDBOX_HOST}:{SANDBOX_PHOTO_REMOTE}", str(photo)]
    ]
    # intake JSON written.
    assert path.exists()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["payload"]["site"] == "Sandbox Site"


# --- existing intake present but ONE photo missing -> that photo IS scp'd, intake retained ---


def test_existing_intake_with_missing_media_recopies_media_retains_intake(
    tmp_path: Path,
) -> None:
    seed_first_import(tmp_path)

    path = intake_path(tmp_path, SANDBOX_CAPTURE_ID)
    photo = local_photo_path(tmp_path, SANDBOX_CAPTURE_ID)
    assert path.exists() and photo.exists()
    before_bytes = path.read_bytes()

    # Simulate the photo's local file having gone missing while the intake remains.
    photo.unlink()
    assert not photo.exists()

    second_runner = RecordingRunner()
    result = import_couchdb_capture(
        doc=sandbox_doc(),
        runtime_root=tmp_path,
        remote_host=SANDBOX_HOST,
        registry=SandboxRegistry(),
        runner=second_runner,
    )

    assert result["ok"] is True
    # The missing photo IS re-scp'd.
    assert second_runner.scp_commands == [
        ["scp", f"{SANDBOX_HOST}:{SANDBOX_PHOTO_REMOTE}", str(photo)]
    ]
    assert photo.exists()
    # Intake file retained, byte-unchanged.
    assert path.read_bytes() == before_bytes


# --- validation guard still raises ---


def test_validation_guard_still_raises(tmp_path: Path) -> None:
    # Empty qc_category makes validate_job fail (photo_capture requires non-empty
    # qc_category); note is non-empty and photos present so the earlier guards pass.
    doc = sandbox_doc(qc_category="")
    with pytest.raises(
        CaptureAdapterError,
        match="generated photo_capture intake JSON failed queue validation",
    ):
        import_couchdb_capture(
            doc=doc,
            runtime_root=tmp_path,
            remote_host=SANDBOX_HOST,
            registry=SandboxRegistry(),
            runner=RecordingRunner(),
        )


# --- min-content guard still raises ---


def test_min_content_guard_still_raises(tmp_path: Path) -> None:
    doc = sandbox_doc(photos=[], audio=[], note="")
    with pytest.raises(
        CaptureAdapterError,
        match="must include at least one photo, audio, or note",
    ):
        import_couchdb_capture(
            doc=doc,
            runtime_root=tmp_path,
            remote_host=SANDBOX_HOST,
            registry=SandboxRegistry(),
            runner=RecordingRunner(),
        )
