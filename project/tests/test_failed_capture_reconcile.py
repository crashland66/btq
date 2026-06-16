"""Independent gating tests for the failed-capture reconcile tool.

Sandbox identity only: capture ids cap-sandbox-*, site SANDBOX / "Sandbox Site",
person sandbox-user.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

from field_capture.failed_capture_reconcile import (
    CaptureArtifactSnapshot,
    PhotoArtifact,
    categorize_failed_captures,
    classify_failed_capture,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "reconcile-failed-captures"


def _load_cli():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("reconcile_failed_captures_cli", str(CLI_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CLI = _load_cli()


def _doc(capture_id: str, **extra):
    doc = {
        "_id": capture_id,
        "capture_id": capture_id,
        "site_id": "SANDBOX",
        "processing_state": "failed",
        "error_reason": "redundant reimport",
    }
    doc.update(extra)
    return doc


def _complete_snapshot(capture_id: str) -> CaptureArtifactSnapshot:
    # A capture whose single photo was discovered from intake with a present
    # sidecar that belongs to this capture.
    return CaptureArtifactSnapshot(
        capture_id=capture_id,
        intake_present=True,
        intake_valid=True,
        photos=(
            PhotoArtifact(
                filename="photo1.jpg",
                source="intake",
                media_present=True,
                sidecar_present=True,
                sidecar_capture_id=capture_id,
                expects_sidecar=True,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Pure module
# ---------------------------------------------------------------------------


def test_complete_on_disk_is_reconcilable():
    cap = "cap-sandbox-complete"
    res = classify_failed_capture(_doc(cap), _complete_snapshot(cap))
    assert res.complete_on_disk is True
    assert res.reason == "complete on disk"


def test_missing_intake_needs_attention():
    cap = "cap-sandbox-no-intake"
    snap = CaptureArtifactSnapshot(
        capture_id=cap, intake_present=False, intake_valid=False, photos=()
    )
    res = classify_failed_capture(_doc(cap), snap)
    assert res.complete_on_disk is False
    assert res.reason == "missing intake"


def test_invalid_intake_needs_attention():
    cap = "cap-sandbox-bad-intake"
    snap = CaptureArtifactSnapshot(
        capture_id=cap, intake_present=True, intake_valid=False, photos=()
    )
    res = classify_failed_capture(_doc(cap), snap)
    assert res.complete_on_disk is False
    assert res.reason == "invalid intake"


def test_missing_media_needs_attention():
    cap = "cap-sandbox-no-media"
    snap = CaptureArtifactSnapshot(
        capture_id=cap,
        intake_present=True,
        intake_valid=True,
        photos=(
            PhotoArtifact(
                filename="photo1.jpg",
                source="intake",
                media_present=False,
                sidecar_present=True,
                sidecar_capture_id=cap,
                expects_sidecar=True,
            ),
        ),
    )
    res = classify_failed_capture(_doc(cap), snap)
    assert res.complete_on_disk is False
    assert res.reason == "missing media"
    assert res.missing_media == ("photo1.jpg",)


def test_missing_sidecar_needs_attention():
    cap = "cap-sandbox-no-sidecar"
    snap = CaptureArtifactSnapshot(
        capture_id=cap,
        intake_present=True,
        intake_valid=True,
        photos=(
            PhotoArtifact(
                filename="photo1.jpg",
                source="intake",
                media_present=True,
                sidecar_present=False,
                sidecar_capture_id="",
                expects_sidecar=True,
            ),
        ),
    )
    res = classify_failed_capture(_doc(cap), snap)
    assert res.complete_on_disk is False
    assert res.reason == "missing sidecar"
    assert res.missing_sidecars == ("photo1.jpg",)


def test_sidecar_belongs_to_other_capture_needs_attention():
    cap = "cap-sandbox-foreign-sidecar"
    snap = CaptureArtifactSnapshot(
        capture_id=cap,
        intake_present=True,
        intake_valid=True,
        photos=(
            PhotoArtifact(
                filename="photo1.jpg",
                source="intake",
                media_present=True,
                sidecar_present=True,
                sidecar_capture_id="cap-sandbox-someone-else",
                expects_sidecar=True,
            ),
        ),
    )
    res = classify_failed_capture(_doc(cap), snap)
    assert res.complete_on_disk is False
    assert res.reason == "missing sidecar"
    assert res.missing_sidecars == ("photo1.jpg",)


def test_categorize_partitions_mixed_set():
    good = "cap-sandbox-good"
    bad = "cap-sandbox-bad"
    pairs = [
        (_doc(good), _complete_snapshot(good)),
        (
            _doc(bad),
            CaptureArtifactSnapshot(
                capture_id=bad, intake_present=False, intake_valid=False, photos=()
            ),
        ),
    ]
    plan = categorize_failed_captures(pairs)
    assert [r.capture_id for r in plan.reconcilable] == [good]
    assert [r.capture_id for r in plan.needs_attention] == [bad]


# ---------------------------------------------------------------------------
# Filename-divergence regression guard: build a genuinely-complete capture on
# disk where the DOC photo filename diverges from the INTAKE photo filename,
# drive the real snapshot_for_doc, and assert the classifier reconciles it.
#
# This is the exact population the tool targets — two ingest paths write
# divergent filenames for the same capture. The earlier defect keyed the
# sidecar-completeness check on the *doc* filename, so the divergent doc photo
# (which legitimately has no sidecar of its own) flagged "missing sidecar" and
# pushed a genuinely-complete capture into needs_attention (UNDER-reconcile).
# The fix evaluates sidecar completeness on the intake-discovered asset only;
# doc photos are a media-presence signal (expects_sidecar=False). This guard
# fails if that under-reconcile regression ever returns.
# ---------------------------------------------------------------------------


def _write_complete_capture_on_disk(
    runtime: Path,
    cap: str,
    *,
    date: str,
    doc_filename: str,
    intake_filename: str,
):
    intake_dir = runtime / "field_capture" / "intake"
    uploads = runtime / "uploads"
    vision_dir = runtime / "field_capture" / "photo_vision"
    intake_dir.mkdir(parents=True, exist_ok=True)
    vision_dir.mkdir(parents=True, exist_ok=True)

    # Real media file, stored under uploads/<date>/<cap>/<intake_filename>.
    media_dir = uploads / date / cap
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / intake_filename).write_bytes(b"\xff\xd8\xff\xd9")
    stored_path = f"{date}/{cap}/{intake_filename}"

    # Intake job JSON (the canonical, complete representation).
    intake = {
        "job_type": "photo_capture",
        "metadata": {"capture_id": cap, "site_id": "SANDBOX"},
        "payload": {
            "site": "SANDBOX",
            "captured_at": f"{date}T12:00:00Z",
            "photos": [
                {
                    "filename": intake_filename,
                    "stored_path": stored_path,
                    "upload_id": f"{date}/{cap}/{intake_filename}",
                }
            ],
        },
    }
    (intake_dir / f"{cap}.json").write_text(json.dumps(intake))

    # Vision sidecar for the discovered asset, belonging to this capture.
    from field_capture import photo_vision

    assets = photo_vision.discover_photo_assets(intake_dir, uploads, capture_id=cap)
    assert assets, "fixture intake should yield a discovered asset"
    asset = assets[0]
    sidecar_path = photo_vision.photo_vision_path_for(vision_dir, asset.photo_asset_id)
    sidecar_path.write_text(json.dumps({"capture_id": cap, "description": "sandbox"}))

    return stored_path


def test_filename_divergence_is_reconcilable(tmp_path):
    """Regression guard: doc photo filename diverges from the intake filename,
    but the capture is genuinely complete on disk (intake + media + belonging
    sidecar on the intake-discovered asset).

    The intake-side artifact is complete and carries the real on-disk filename.
    The EXTRA doc-side artifact has a divergent filename and no sidecar of its
    own, but it must NOT drag the capture into needs_attention — it is only a
    media-presence signal. This is the precise failure population the tool
    targets; mis-handling it under-reconciles. Guards against the defect where
    sidecar-completeness was keyed on the divergent doc filename.
    """
    cap = "cap-sandbox-divergence"
    date = "2026-06-14"
    stored_path = _write_complete_capture_on_disk(
        tmp_path,
        cap,
        date=date,
        doc_filename="upload-abc123",  # upload_id-derived bare form
        intake_filename="photo1.jpg",  # canonical intake filename
    )

    # Doc records the photo under a DIVERGENT filename form, and the media for
    # that doc-derived path also exists (so this is genuinely complete on disk).
    doc_media_dir = tmp_path / "uploads" / date / cap
    (doc_media_dir / "upload-abc123").write_bytes(b"\xff\xd8\xff\xd9")
    doc = _doc(
        cap,
        captured_at=f"{date}T12:00:00Z",
        photos=[{"filename": "upload-abc123"}],
    )

    snap = CLI.snapshot_for_doc(doc, tmp_path)
    res = classify_failed_capture(doc, snap)

    # The divergent doc photo contributes a media-presence signal only
    # (expects_sidecar=False); the intake-discovered asset carries the sidecar.
    sources = {(p.source, p.filename, p.expects_sidecar, p.sidecar_present) for p in snap.photos}
    assert ("doc", "upload-abc123", False, False) in sources
    assert ("intake", "photo1.jpg", True, True) in sources

    # Correct behavior: a divergent-but-complete capture is reconcilable, and the
    # divergent doc filename is NOT reported as a missing sidecar.
    assert res.complete_on_disk is True
    assert res.reason == "complete on disk"
    assert res.missing_sidecars == ()


def test_matching_filename_is_reconcilable_on_disk(tmp_path):
    """Control: when doc and intake filenames AGREE, snapshot_for_doc yields a
    reconcilable capture. Isolates the divergence as the cause."""
    cap = "cap-sandbox-agree"
    date = "2026-06-14"
    _write_complete_capture_on_disk(
        tmp_path,
        cap,
        date=date,
        doc_filename="photo1.jpg",
        intake_filename="photo1.jpg",
    )
    doc = _doc(
        cap,
        captured_at=f"{date}T12:00:00Z",
        photos=[{"filename": "photo1.jpg"}],
    )
    snap = CLI.snapshot_for_doc(doc, tmp_path)
    res = classify_failed_capture(doc, snap)
    assert res.complete_on_disk is True
    assert res.reason == "complete on disk"


# ---------------------------------------------------------------------------
# CLI --apply path with a fake couch (reset_failed_capture_to_pending)
# ---------------------------------------------------------------------------


class _FakeCouch:
    def __init__(self, live_doc):
        self.live_doc = live_doc
        self.puts = []
        self.conflict = False

    def get(self, config, database, doc_id):
        if self.live_doc is None:
            return None
        return dict(self.live_doc)

    def put(self, config, database, doc_id, doc):
        if self.conflict:
            err = HTTPError(doc_id, 409, "conflict", {}, None)
            raise CLI.CouchDBProspectError("conflict") from err
        self.puts.append((doc_id, dict(doc)))
        return {"ok": True}


@pytest.fixture
def patched_couch(monkeypatch):
    def _install(live_doc):
        fake = _FakeCouch(live_doc)
        monkeypatch.setattr(CLI, "_get_doc", fake.get)
        monkeypatch.setattr(CLI, "_put_doc", fake.put)
        return fake

    return _install


def test_apply_resets_still_failed_doc(patched_couch):
    cap = "cap-sandbox-reset"
    live = _doc(cap, _rev="1-abc", extra_field="keep me")
    fake = patched_couch(live)
    result = CLI.reset_failed_capture_to_pending(object(), {"_id": cap, "capture_id": cap})
    assert result == "reset"
    assert len(fake.puts) == 1
    _, written = fake.puts[0]
    assert written["processing_state"] == "pending"
    # Only processing_state changed; everything else preserved.
    expected = dict(live)
    expected["processing_state"] = "pending"
    assert written == expected


def test_apply_skips_doc_that_flipped_to_complete(patched_couch):
    cap = "cap-sandbox-flipped"
    live = _doc(cap, processing_state="complete", _rev="2-xyz")
    fake = patched_couch(live)
    result = CLI.reset_failed_capture_to_pending(object(), {"_id": cap, "capture_id": cap})
    assert result == "skipped_state_complete"
    assert fake.puts == []


def test_apply_handles_409_conflict(patched_couch):
    cap = "cap-sandbox-conflict"
    live = _doc(cap, _rev="1-abc")
    fake = patched_couch(live)
    fake.conflict = True
    result = CLI.reset_failed_capture_to_pending(object(), {"_id": cap, "capture_id": cap})
    assert result == "skipped_conflict"
    assert fake.puts == []


def test_apply_skips_missing_doc(patched_couch):
    cap = "cap-sandbox-gone"
    fake = patched_couch(None)
    result = CLI.reset_failed_capture_to_pending(object(), {"_id": cap, "capture_id": cap})
    assert result == "skipped_missing"
    assert fake.puts == []


def test_apply_is_idempotent(patched_couch):
    """Second run sees a doc already pending -> skipped_state_pending, no write."""
    cap = "cap-sandbox-idem"
    live = _doc(cap, processing_state="pending", _rev="2-xyz")
    fake = patched_couch(live)
    result = CLI.reset_failed_capture_to_pending(object(), {"_id": cap, "capture_id": cap})
    assert result == "skipped_state_pending"
    assert fake.puts == []


def test_dry_run_issues_zero_puts(monkeypatch, tmp_path, capsys):
    """Default dry-run reads couch + disk and prints, but never PUTs."""
    cap = "cap-sandbox-dry"
    date = "2026-06-14"
    _write_complete_capture_on_disk(
        tmp_path, cap, date=date, doc_filename="photo1.jpg", intake_filename="photo1.jpg"
    )
    doc = _doc(cap, captured_at=f"{date}T12:00:00Z", photos=[{"filename": "photo1.jpg"}])

    puts = []
    monkeypatch.setattr(CLI, "_put_doc", lambda *a, **k: puts.append(a))
    monkeypatch.setattr(CLI.couchdb_config, "from_env", lambda: object())
    monkeypatch.setattr(CLI, "failed_capture_docs", lambda *a, **k: [doc])

    rc = CLI.run(["--runtime-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert puts == []
    assert "reset=0" in out
    assert "(dry-run)" in out


def test_needs_attention_never_written_in_apply(monkeypatch, tmp_path, capsys):
    """An incomplete capture lands in needs_attention and apply issues no PUT."""
    cap = "cap-sandbox-incomplete"
    # Intake dir exists but no intake json for this capture -> missing intake.
    (tmp_path / "field_capture" / "intake").mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "field_capture" / "photo_vision").mkdir(parents=True, exist_ok=True)
    doc = _doc(cap, captured_at="2026-06-14T12:00:00Z", photos=[{"filename": "photo1.jpg"}])

    puts = []
    monkeypatch.setattr(CLI, "_put_doc", lambda *a, **k: puts.append(a))
    monkeypatch.setattr(CLI, "_get_doc", lambda *a, **k: dict(doc))
    monkeypatch.setattr(CLI.couchdb_config, "from_env", lambda: object())
    monkeypatch.setattr(CLI, "failed_capture_docs", lambda *a, **k: [doc])

    rc = CLI.run(["--apply", "--runtime-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert puts == []
    assert "reset=0" in out
    assert "Needs attention: 1" in out
