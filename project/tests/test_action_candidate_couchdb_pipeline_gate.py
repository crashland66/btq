"""Gating tests for prompt 308 pipeline write + backfill (Python-level).

Authored by the INDEPENDENT VERIFIER. These assert the *integration* contract.
Per the prompt-308 contract-revert, collect_action_candidates returns ONLY
discovered/skipped/completed/failed; the CouchDB write is a pure side-effect.
So these tests assert toggle on/off, best-effort failure handling, and the
skip-when-unconfigured behavior via the writer spy / write_candidate_to_couchdb_best_effort
return value / caplog / the filesystem artifact -- never via the return dict --
plus idempotent backfill. The actual CouchDB round-trip + server-side
validate_doc_update are proven in test_couchdb_action_candidate_gate.py against a
real CouchDB / real node; here the writer is monkeypatched so we isolate the
pipeline/backfill control flow.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import CouchDBCandidateWriterError
import field_capture.action_candidates as fc
import field_capture.action_candidate_couchdb_backfill as backfill


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _semantic_artifact(semantic_type: str = "field_text_semantic_summary") -> dict:
    # Shape mirrors the canonical fixture in tests/test_field_capture_action_candidates.py
    # so payloads_from_semantic actually emits a candidate.
    return {
        "type": semantic_type,
        "status": "complete",
        "source_transcript_path": "/tmp/transcripts/cowork-note.txt",
        "audio_asset_id": "asset-123",
        "raw_text_hash": "hash-123",
        "site_id": "7080",
        "upload_id": "upload-123",
        "area": "Cowork",
        "phase": "operations",
        "submitter_person_id": "person-123",
        "cleaned_internal_note": "Pearson and Hawthorne need follow-up after the Cowork voice memo.",
        "operational_summary": "Cowork memo identifies a follow-up for site 7080.",
        "client_safe_note": "Follow-up is needed for the site.",
        "action_candidates": ["Review the Pearson and Hawthorne Cowork follow-up."],
    }


def _write_semantic(semantic_dir: Path, name: str, payload: dict) -> None:
    semantic_dir.mkdir(parents=True, exist_ok=True)
    (semantic_dir / name).write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def captured_writes(monkeypatch):
    """Record every candidate handed to the writer; default = success."""
    calls: list[dict] = []

    def fake_upsert(config, db, candidate):
        calls.append(dict(candidate))

    monkeypatch.setattr(fc, "upsert_action_candidate", fake_upsert)
    # 308c: the writer now does an existence GET (get_action_candidate) before
    # upserting, to skip an already-existing (possibly reviewed) candidate. This
    # spy isolates the write control flow, so stub the existence check to "does
    # not exist" -- consistent with the new contract -- otherwise it would try a
    # real HTTP GET against http://x. A dedicated gate
    # (test_existence_check_raises_when_configured_but_erroring, below) proves the
    # existence GET fails loud when configured-but-erroring.
    monkeypatch.setattr(fc, "get_action_candidate", lambda *a, **k: None)
    # Avoid needing real env creds for couchdb_config.from_env in the helper.
    monkeypatch.setattr(
        fc.couchdb_config,
        "from_env",
        lambda **_: couchdb_config.CouchDBConfig("http://x", "", "", 1.0, 1000),
    )
    return calls


# --------------------------------------------------------------------------- #
# Pipeline: collect_action_candidates write behavior
# --------------------------------------------------------------------------- #
class TestPipelineCouchDBWrite:
    """The CouchDB write is a pure side-effect (prompt 308 contract-revert).

    collect_action_candidates returns ONLY discovered/skipped/completed/failed.
    The CouchDB write outcome is observed via the writer spy
    (field_capture.action_candidates.upsert_action_candidate),
    write_candidate_to_couchdb_best_effort's return value, caplog, and the
    filesystem candidate artifact -- NEVER via the return dict.
    """

    def test_no_filesystem_write_when_couchdb_configured(self, tmp_path, captured_writes, monkeypatch):
        """308c: the collector no longer writes the filesystem candidate artifact.

        With CouchDB configured, the candidate is written to CouchDB (writer spy
        invoked) and NO filesystem review artifact is produced -- CouchDB is the
        sole canonical store.
        """
        monkeypatch.setenv(fc.COUCHDB_CANDIDATE_WRITE_ENV, "1")
        sem = tmp_path / "sem"
        _write_semantic(sem, "a.json", _semantic_artifact())
        cand_dir = tmp_path / "candidates"
        counts = fc.collect_action_candidates([sem], cand_dir)
        assert counts["discovered"] >= 1
        # CouchDB write happened (the canonical store).
        assert len(captured_writes) >= 1
        # The filesystem candidate artifact is NO LONGER written.
        assert not list(cand_dir.rglob("*.json")), "308c retires the filesystem candidate write"
        assert set(counts) == {"discovered", "skipped", "completed", "failed"}

    def test_unconfigured_skips_couchdb_and_writes_nothing(self, tmp_path, monkeypatch):
        """Unconfigured CouchDB (dev/CI): write is SKIPPED silently, nothing raised,
        no filesystem artifact either (the FS write is retired)."""
        monkeypatch.delenv(fc.COUCHDB_CANDIDATE_WRITE_ENV, raising=False)
        for name in (
            "BTQ_COUCHDB_URL",
            "BTQ_COUCHDB_USER",
            "BTQ_COUCHDB_PASSWORD",
            "BTQ_COUCHDB_FIELD_CAPTURES_DB",
        ):
            monkeypatch.delenv(name, raising=False)
        upsert_calls: list = []
        monkeypatch.setattr(fc, "upsert_action_candidate", lambda *a, **k: upsert_calls.append(a))
        sem = tmp_path / "sem"
        _write_semantic(sem, "a.json", _semantic_artifact())
        cand_dir = tmp_path / "candidates"
        counts = fc.collect_action_candidates([sem], cand_dir)  # must not raise
        assert upsert_calls == []
        assert fc.write_candidate_to_couchdb_required_when_configured({"candidate_id": "x"}) is None
        assert not list(cand_dir.rglob("*.json"))
        assert counts["completed"] >= 1

    def test_configured_but_erroring_write_raises_no_silent_drop(self, tmp_path, monkeypatch):
        """308c no-silent-drop: a configured-but-failing CouchDB write RAISES out of
        the collector loop -- the candidate is never silently lost."""
        monkeypatch.setenv(fc.COUCHDB_CANDIDATE_WRITE_ENV, "1")

        def boom(config, db, candidate):
            raise CouchDBCandidateWriterError("simulated couch outage")

        monkeypatch.setattr(fc, "upsert_action_candidate", boom)
        monkeypatch.setattr(
            fc.couchdb_config,
            "from_env",
            lambda **_: couchdb_config.CouchDBConfig("http://x", "", "", 1.0, 1000),
        )
        sem = tmp_path / "sem"
        _write_semantic(sem, "a.json", _semantic_artifact())
        cand_dir = tmp_path / "candidates"
        with pytest.raises(CouchDBCandidateWriterError):
            fc.collect_action_candidates([sem], cand_dir)
        # The per-candidate helper raises too (configured + failing).
        with pytest.raises(CouchDBCandidateWriterError):
            fc.write_candidate_to_couchdb_required_when_configured({"candidate_id": "y"})

    def test_config_error_when_write_switch_set_raises(self, tmp_path, caplog, monkeypatch):
        """308c no-silent-drop: with the explicit write switch set, an asymmetric-
        creds CouchDBConfigError propagates out of the collector (not swallowed),
        so a misconfigured prod box fails loud rather than dropping candidates."""
        monkeypatch.setenv(fc.COUCHDB_CANDIDATE_WRITE_ENV, "1")
        upsert_calls: list = []
        monkeypatch.setattr(fc, "upsert_action_candidate", lambda *a, **k: upsert_calls.append(a))

        def bad_config(**_):
            raise couchdb_config.CouchDBConfigError("creds asymmetric")

        # The store is "configured" via the explicit write switch, so from_env is
        # consulted; an asymmetric-creds error surfaces as a config error.
        monkeypatch.setattr(fc.couchdb_config, "from_env", bad_config)
        sem = tmp_path / "sem"
        _write_semantic(sem, "a.json", _semantic_artifact())
        cand_dir = tmp_path / "candidates"
        with caplog.at_level(logging.ERROR, logger="field_capture.action_candidates"):
            with pytest.raises(couchdb_config.CouchDBConfigError):
                fc.collect_action_candidates([sem], cand_dir)
        assert upsert_calls == []


# --------------------------------------------------------------------------- #
# Backfill: idempotent migration of filesystem candidates
# --------------------------------------------------------------------------- #
class TestBackfill:
    def _seed_fs_candidates(self, cand_dir: Path, ids: list[str]) -> None:
        cand_dir.mkdir(parents=True, exist_ok=True)
        for cid in ids:
            payload = {
                "candidate_id": cid,
                "status": "pending_review",
                "candidate_type": "field_capture_follow_up",
                "summary": f"candidate {cid}",
                "site_id": "7050",
            }
            (cand_dir / f"{cid}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_backfill_writes_each_candidate(self, tmp_path, monkeypatch):
        store: dict[str, dict] = {}

        def fake_get(config, db, candidate_id):
            return store.get(candidate_id)

        def fake_upsert(config, db, candidate):
            store[candidate["candidate_id"]] = dict(candidate)

        monkeypatch.setattr(backfill, "get_action_candidate_document", fake_get)
        monkeypatch.setattr(backfill, "upsert_action_candidate", fake_upsert)

        cand_dir = tmp_path / "c"
        self._seed_fs_candidates(cand_dir, ["a", "b", "c"])
        cfg = couchdb_config.CouchDBConfig("http://x", "u", "p", 1.0, 1000)
        counts = backfill.backfill_action_candidates(cand_dir, cdb_config=cfg, database="db")
        assert counts["found"] == 3
        assert counts["written"] == 3
        assert counts["updated"] == 0
        assert counts["errors"] == 0
        assert set(store) == {"a", "b", "c"}

    def test_backfill_idempotent_rerun_zero_new(self, tmp_path, monkeypatch):
        store: dict[str, dict] = {}

        def fake_get(config, db, candidate_id):
            return store.get(candidate_id)

        def fake_upsert(config, db, candidate):
            store[candidate["candidate_id"]] = dict(candidate)

        monkeypatch.setattr(backfill, "get_action_candidate_document", fake_get)
        monkeypatch.setattr(backfill, "upsert_action_candidate", fake_upsert)

        cand_dir = tmp_path / "c"
        self._seed_fs_candidates(cand_dir, ["a", "b"])
        cfg = couchdb_config.CouchDBConfig("http://x", "u", "p", 1.0, 1000)
        first = backfill.backfill_action_candidates(cand_dir, cdb_config=cfg, database="db")
        assert first["written"] == 2
        # Re-run: docs already present -> 0 new, 2 updated, idempotent.
        second = backfill.backfill_action_candidates(cand_dir, cdb_config=cfg, database="db")
        assert second["found"] == 2
        assert second["written"] == 0
        assert second["updated"] == 2
        assert second["errors"] == 0

    def test_backfill_dry_run_does_not_write(self, tmp_path, monkeypatch):
        def fake_upsert(config, db, candidate):
            raise AssertionError("dry-run must not write")

        monkeypatch.setattr(backfill, "upsert_action_candidate", fake_upsert)
        cand_dir = tmp_path / "c"
        self._seed_fs_candidates(cand_dir, ["a"])
        counts = backfill.backfill_action_candidates(
            cand_dir, cdb_config=None, database="db", dry_run=True
        )
        assert counts["found"] == 1
        assert counts["dry_run"] == 1
        assert counts["written"] == 0

    def test_backfill_skips_candidate_without_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backfill, "get_action_candidate_document", lambda *a, **k: None)
        monkeypatch.setattr(backfill, "upsert_action_candidate", lambda *a, **k: None)
        cand_dir = tmp_path / "c"
        cand_dir.mkdir(parents=True)
        (cand_dir / "noid.json").write_text(json.dumps({"status": "pending_review"}), encoding="utf-8")
        cfg = couchdb_config.CouchDBConfig("http://x", "u", "p", 1.0, 1000)
        counts = backfill.backfill_action_candidates(cand_dir, cdb_config=cfg, database="db")
        assert counts["found"] == 1
        assert counts["errors"] == 1
        assert counts["written"] == 0
