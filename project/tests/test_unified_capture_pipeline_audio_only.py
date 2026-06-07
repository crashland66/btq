"""Build E behavioral tests: audio-only field_capture through the SHARED pipeline.

Authored by the INDEPENDENT verification agent (NOT the implementer, Codex).

Build E relaxed two shared-pipeline gates so an audio-only capture
(``type="field_capture"`` with ``photos: []`` + one ``audio`` element) flows
through the same path as photo-only / combined, while leaving the photo path
unchanged:

  * ``queue_spec.validate_job`` (JOB_PHOTO_CAPTURE branch): ``photos`` must be a
    list (may be empty); ``audio`` (if present) a list of <=1 validated record;
    reject only when BOTH are empty.
  * ``event_pipeline.couchdb_capture_adapter.import_couchdb_capture``: removed
    the pre-copy-plan "must include at least one photo" gate; rejects only AFTER
    both media plans normalize, when ``not local_photos and not local_audio``.

These tests run the REAL functions end to end (no structural source scraping for
the behavioral assertions). They cover:

  * ``validate_job`` unit matrix (photo-only unchanged, audio-only new,
    combined, both-empty rejected, >1 audio rejected, malformed audio rejected,
    photos-not-a-list rejected).
  * ``import_couchdb_capture`` end-to-end via the existing adapter fixtures
    (CopyingRunner + FakeRegistry) for audio-only / photo-only / combined /
    both-empty.
  * A no-regression proof for the photo path: the photo-only intake job built by
    the CURRENT adapter is compared field-for-field against the job built by the
    ARC-BASE (c3cab43) adapter in a throwaway git worktree.
  * A genuine D->E chain: drive a real audio-only multipart submit through the
    Build-D ``/api/submit`` handler, capture the EXACT field_capture doc it
    writes, then feed that doc into ``import_couchdb_capture`` -> ``validate_job``.

The adapter fixtures (sample_doc / CopyingRunner / FakeRegistry / read_intake)
are imported from the committed adapter test module so the doc shape and runner
behavior stay in lock-step with the implementer's own fixtures.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from event_pipeline.couchdb_capture_adapter import (
    CaptureAdapterError,
    import_couchdb_capture,
)
from queue_spec import JOB_PHOTO_CAPTURE, validate_job

# Reuse the implementer's own adapter-test fixtures so doc shape / runner stay
# in lock-step with the committed adapter tests.
from tests.test_couchdb_capture_adapter import (
    REMOTE_PATH,
    CopyingRunner,
    FakeRegistry,
    read_intake,
    sample_doc,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARC_BASE = "c3cab43"

# A resolvable remote audio path for the adapter to "scp" via CopyingRunner.
REMOTE_AUDIO_PATH = "/srv/btq/runtime/uploads/cap-2026-05-09-abc123/voice_001.webm"


def _audio_record(
    *,
    filename: str = "voice_001.webm",
    mime_type: str = "audio/webm",
    stored_path: str = REMOTE_AUDIO_PATH,
) -> dict[str, Any]:
    return {
        "filename": filename,
        "mime_type": mime_type,
        "stored_path": stored_path,
        "duration_seconds": 12,
    }


def _audio_runner() -> CopyingRunner:
    """CopyingRunner that can resolve both the photo and audio remote paths."""
    return CopyingRunner(remote_sizes={REMOTE_PATH: 5, REMOTE_AUDIO_PATH: 5})


# --------------------------------------------------------------------------- #
# validate_job unit matrix
# --------------------------------------------------------------------------- #


def _photo_capture_job(payload_overrides: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "site": "Summit Wire",
        "qc_category": "completed_area",
        "note": "Lobby complete.",
        "captured_at": "2026-05-09T14:30:00Z",
        "exported_at": "2026-05-09T14:31:00Z",
        "photos": [
            {
                "filename": "photo_001.jpg",
                "mime_type": "image/jpeg",
                "stored_path": "/tmp/photo_001.jpg",
            }
        ],
    }
    payload.update(payload_overrides)
    return {"job_id": "cap-x", "job_type": JOB_PHOTO_CAPTURE, "payload": payload}


def _valid_audio_payload_record() -> dict[str, Any]:
    return {
        "filename": "voice_001.webm",
        "mime_type": "audio/webm",
        "stored_path": "/tmp/voice_001.webm",
    }


def test_validate_photo_only_no_audio_true_unchanged() -> None:
    job = _photo_capture_job({})
    assert validate_job(job) is True


def test_validate_audio_only_empty_photos_true_new() -> None:
    job = _photo_capture_job({"photos": [], "audio": [_valid_audio_payload_record()]})
    assert validate_job(job) is True


def test_validate_combined_photos_and_audio_true() -> None:
    job = _photo_capture_job({"audio": [_valid_audio_payload_record()]})
    assert validate_job(job) is True


def test_validate_both_empty_no_audio_key_false() -> None:
    job = _photo_capture_job({"photos": []})
    assert validate_job(job) is False


def test_validate_both_empty_empty_audio_list_false() -> None:
    job = _photo_capture_job({"photos": [], "audio": []})
    assert validate_job(job) is False


def test_validate_two_audio_records_false() -> None:
    job = _photo_capture_job(
        {"photos": [], "audio": [_valid_audio_payload_record(), _valid_audio_payload_record()]}
    )
    assert validate_job(job) is False


def test_validate_audio_missing_filename_false() -> None:
    rec = _valid_audio_payload_record()
    del rec["filename"]
    job = _photo_capture_job({"photos": [], "audio": [rec]})
    assert validate_job(job) is False


def test_validate_audio_missing_mime_type_false() -> None:
    rec = _valid_audio_payload_record()
    del rec["mime_type"]
    job = _photo_capture_job({"photos": [], "audio": [rec]})
    assert validate_job(job) is False


def test_validate_audio_missing_both_data_url_and_stored_path_false() -> None:
    rec = {"filename": "voice_001.webm", "mime_type": "audio/webm"}
    job = _photo_capture_job({"photos": [], "audio": [rec]})
    assert validate_job(job) is False


def test_validate_audio_with_data_url_only_true() -> None:
    rec = {"filename": "voice_001.webm", "mime_type": "audio/webm", "data_url": "data:audio/webm;base64,AAAA"}
    job = _photo_capture_job({"photos": [], "audio": [rec]})
    assert validate_job(job) is True


def test_validate_photos_not_a_list_false() -> None:
    job = _photo_capture_job({"photos": "nope"})
    assert validate_job(job) is False


def test_validate_audio_not_a_list_false() -> None:
    job = _photo_capture_job({"audio": _valid_audio_payload_record()})
    assert validate_job(job) is False


# --------------------------------------------------------------------------- #
# import_couchdb_capture end-to-end
# --------------------------------------------------------------------------- #


def _audio_only_doc(**overrides: Any) -> dict[str, Any]:
    return sample_doc(photos=[], audio=[_audio_record()], **overrides)


def _combined_doc(**overrides: Any) -> dict[str, Any]:
    return sample_doc(audio=[_audio_record()], **overrides)


def test_adapter_audio_only_produces_valid_intake_with_empty_photos(tmp_path: Path) -> None:
    runner = _audio_runner()
    result = import_couchdb_capture(
        doc=_audio_only_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=runner,
    )
    assert result["ok"] is True
    intake = read_intake(tmp_path, "cap-2026-05-09-abc123")
    assert intake["payload"]["photos"] == []
    assert len(intake["payload"]["audio"]) == 1
    assert intake["payload"]["audio"][0]["filename"] == "voice_001.webm"
    # stored_path was rewritten to the LOCAL copy destination, not the remote.
    local_audio = tmp_path / "uploads" / "2026-05-09" / "cap-2026-05-09-abc123" / "voice_001.webm"
    assert intake["payload"]["audio"][0]["stored_path"] == str(local_audio)
    assert local_audio.exists()
    assert validate_job(intake) is True


def test_adapter_combined_produces_valid_intake_with_both(tmp_path: Path) -> None:
    runner = _audio_runner()
    result = import_couchdb_capture(
        doc=_combined_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=runner,
    )
    assert result["ok"] is True
    intake = read_intake(tmp_path, "cap-2026-05-09-abc123")
    assert len(intake["payload"]["photos"]) == 1
    assert len(intake["payload"]["audio"]) == 1
    assert validate_job(intake) is True


def test_adapter_both_empty_raises(tmp_path: Path) -> None:
    doc = sample_doc(photos=[])  # no audio key at all
    with pytest.raises(CaptureAdapterError):
        import_couchdb_capture(
            doc=doc,
            runtime_root=tmp_path,
            remote_host="vps.example",
            registry=FakeRegistry(),
            runner=_audio_runner(),
        )


def test_adapter_both_empty_empty_audio_list_raises(tmp_path: Path) -> None:
    doc = sample_doc(photos=[], audio=[])
    with pytest.raises(CaptureAdapterError):
        import_couchdb_capture(
            doc=doc,
            runtime_root=tmp_path,
            remote_host="vps.example",
            registry=FakeRegistry(),
            runner=_audio_runner(),
        )


def test_adapter_photo_only_still_works_and_validates(tmp_path: Path) -> None:
    result = import_couchdb_capture(
        doc=sample_doc(),
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=CopyingRunner(),
    )
    assert result["ok"] is True
    intake = read_intake(tmp_path, "cap-2026-05-09-abc123")
    assert len(intake["payload"]["photos"]) == 1
    # photo-only must NOT carry an audio key (build_intake_job only adds it when
    # local_audio is non-empty).
    assert "audio" not in intake["payload"]
    assert validate_job(intake) is True


# --------------------------------------------------------------------------- #
# No-regression proof for the photo path: compare CURRENT adapter output to the
# ARC-BASE (c3cab43) adapter output for an identical photo-only doc.
# --------------------------------------------------------------------------- #


def _build_photo_only_intake_via_subprocess(python_path_root: Path, doc: dict[str, Any]) -> dict[str, Any]:
    """Run import_couchdb_capture in a child process rooted at ``python_path_root``.

    Used to drive the ARC-BASE adapter code from a throwaway git worktree without
    polluting this process's already-imported modules.
    """
    import json as _json

    driver = r"""
import json, sys, tempfile
from pathlib import Path
sys.argv = ["x"]
from event_pipeline.couchdb_capture_adapter import import_couchdb_capture

class FakeRegistry:
    def resolve_canonical(self, site_id):
        return "Summit Wire"

class CopyingRunner:
    def __init__(self):
        self.commands = []
    def __call__(self, args):
        import subprocess
        self.commands.append(args)
        if args[0] == "ssh":
            return subprocess.CompletedProcess(args, 0, stdout="5\n", stderr="")
        if args[0] == "scp":
            local_path = Path(args[-1])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"photo")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)

doc = json.loads(sys.stdin.read())
runtime_root = Path(tempfile.mkdtemp())
import_couchdb_capture(
    doc=doc,
    runtime_root=runtime_root,
    remote_host="vps.example",
    registry=FakeRegistry(),
    runner=CopyingRunner(),
)
intake_path = runtime_root / "field_capture" / "intake" / (str(doc["capture_id"]) + ".json")
# Normalize the runtime-root-dependent stored_path to a stable token so the two
# checkouts (different tempdirs) compare equal on everything but the temp prefix.
data = json.loads(intake_path.read_text())
for photo in data["payload"].get("photos", []):
    photo["stored_path"] = "STORED:" + Path(photo["stored_path"]).name
for audio in data["payload"].get("audio", []):
    audio["stored_path"] = "STORED:" + Path(audio["stored_path"]).name
# exported_at is a wall-clock timestamp; blank it for the structural compare.
data["payload"]["exported_at"] = "EXPORTED_AT"
print(json.dumps(data))
"""
    project_dir = python_path_root / "project"
    result = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=str(project_dir),
        input=_json.dumps(doc),
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(project_dir), "PATH": __import__("os").environ.get("PATH", "")},
    )
    if result.returncode != 0:
        raise RuntimeError(f"subprocess failed: {result.stderr}")
    return _json.loads(result.stdout.strip().splitlines()[-1])


def test_photo_only_intake_unchanged_vs_arc_base(tmp_path: Path) -> None:
    """Photo-only intake job from CURRENT adapter == from ARC-BASE adapter.

    Builds the SAME photo-only doc through both the current working-tree adapter
    and the arc-base (c3cab43) adapter (extracted into a throwaway git worktree),
    normalizing only the tempdir-dependent stored_path prefix and the wall-clock
    exported_at, then asserts byte-equal JSON. Proves Build E did not change the
    photo path's output.
    """
    doc = sample_doc()

    worktree = tmp_path / "arcbase_worktree"
    add = subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), _ARC_BASE],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        pytest.skip(f"cannot create arc-base worktree: {add.stderr.strip()}")
    try:
        current = _build_photo_only_intake_via_subprocess(_REPO_ROOT, doc)
        arc_base = _build_photo_only_intake_via_subprocess(worktree, doc)
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
        )

    assert current == arc_base, (
        "photo-only intake job differs from arc-base (REGRESSION):\n"
        f"current={current}\narc_base={arc_base}"
    )
    # Sanity: the compared object is a real photo_capture intake job.
    assert current["job_type"] == JOB_PHOTO_CAPTURE
    assert len(current["payload"]["photos"]) == 1


# --------------------------------------------------------------------------- #
# Genuine D->E chain: real /api/submit (Build D) produces an audio-only doc,
# which is then fed into the Build-E pipeline (import_couchdb_capture ->
# validate_job). Only the CouchDB writer is faked (to capture the doc) and the
# adapter's scp/ssh runner is faked.
# --------------------------------------------------------------------------- #

from unittest import mock  # noqa: E402

from tests.test_unified_capture import (  # noqa: E402
    EMP_SINGLE,
    SITES_TWO,
    _SubmitFakeServer,
    _TINY_WAV,
    _WriterCapture,
    _multipart_body,
    _valid_submit_fields,
    drive_post_multipart,
    install_couch_fakes,
    make_store,
    stop_all,
)
from unified_capture import server as uc_server  # noqa: E402


def test_d_to_e_chain_audio_only_submit_flows_through_pipeline(tmp_path: Path) -> None:
    """Real Build-D /api/submit audio-only -> Build-E import -> validate_job."""
    vault = tmp_path / "vault"
    vault.mkdir()
    upload_dir = tmp_path / "submit_uploads"
    store, token = make_store(str(tmp_path), site_ids=["7060"], can_submit=True, role="cleaner")
    server = _SubmitFakeServer(store, vault, upload_dir)
    writer = _WriterCapture()

    started = install_couch_fakes(EMP_SINGLE, SITES_TWO)
    put_patch = mock.patch.object(uc_server, "put_field_capture_document", side_effect=writer.put)
    get_patch = mock.patch.object(uc_server, "get_field_capture_document", side_effect=writer.get)
    put_patch.start()
    get_patch.start()
    try:
        body, content_type = _multipart_body(
            _valid_submit_fields(extra={"audio_duration_seconds": "12"}),
            audio=[("note.wav", "audio/wav", _TINY_WAV)],
        )
        resp = drive_post_multipart(
            server,
            "/api/submit",
            body,
            content_type,
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        put_patch.stop()
        get_patch.stop()
        stop_all(started)

    assert resp.status == 201, resp.text
    assert len(writer.put_calls) == 1
    d_doc = copy.deepcopy(writer.put_calls[0])
    assert d_doc["type"] == "field_capture"
    assert d_doc["photos"] == []
    assert len(d_doc["audio"]) == 1

    # The doc D wrote references LOCAL upload paths (where /api/submit persisted
    # the media). For the adapter (which scp's from a remote host) to consume it,
    # point its CopyingRunner at the stored_path values D produced.
    audio_paths = {str(rec["stored_path"]): rec["content_length"] if "content_length" in rec else 5
                   for rec in d_doc["audio"]}
    # content_length may not be present; fall back to actual file size on disk.
    remote_sizes: dict[str, int] = {}
    for rec in d_doc["audio"]:
        sp = str(rec["stored_path"])
        disk = Path(sp)
        remote_sizes[sp] = disk.stat().st_size if disk.exists() else 5

    runner = CopyingRunner(remote_sizes=remote_sizes)

    pipeline_root = tmp_path / "pipeline_runtime"
    result = import_couchdb_capture(
        doc=d_doc,
        runtime_root=pipeline_root,
        remote_host="vps.example",
        registry=FakeRegistry(),
        runner=runner,
    )
    assert result["ok"] is True
    capture_id = str(d_doc["capture_id"])
    intake = read_intake(pipeline_root, capture_id)
    assert intake["payload"]["photos"] == []
    assert len(intake["payload"]["audio"]) == 1
    assert validate_job(intake) is True
