"""Verifier-authored tests for 415: deep-analysis CouchDB write made CANONICAL.

The SUCCESS path is CouchDB-first / sidecar-on-success: build the candidate
sidecar + photo-vision doc in memory, write CouchDB FIRST, write the local
sidecar JSON to disk ONLY after CouchDB succeeds (or is unconfigured). A
configured-CouchDB write failure on the success path:
  * does NOT write the sidecar (no orphan entry),
  * records a deep-analysis metric with status="couchdb_write_failed",
  * raises DeepAnalysisCanonicalWriteError (propagates out of run_deep_analysis),
which makes the queue route the job to failed_dir.

MODEL/analysis failures keep current fail-closed behavior: build a failed
entry, best-effort record to sidecar, record metric, RETURN (no raise).

Mutation checks (tamper deep_analysis.py -> a test reddens -> restore):
  1. Revert the configured-couch swallow->raise (warn+continue) ->
     test_configured_couch_put_raises_propagates_and_no_orphan reddens.
  2. Revert CouchDB-first ordering (write sidecar before couch) ->
     test_configured_couch_put_raises_propagates_and_no_orphan reddens
     (orphan sidecar would be written).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from field_capture import deep_analysis
from field_capture import photo_vision

# Reuse the proven fixtures/stubs from the base deep-analysis test module.
from test_deep_analysis import (  # type: ignore
    StubMediaStore,
    StubVisionClient,
    _fixed_now,
    _seed_capture,
)


def _run(runtime, upload_root, asset, *, vision, store, put, **kw):
    return deep_analysis.run_deep_analysis(
        capture_id=asset.capture_id,
        photo_asset_id=asset.photo_asset_id,
        actor="operator-greg",
        runtime_root=runtime,
        upload_root=upload_root,
        vision_client=vision,
        media_store=store,
        couchdb_put=put,
        now=_fixed_now,
        **kw,
    )


def _metric_lines(runtime: Path) -> list[dict]:
    path = runtime / "metrics" / deep_analysis.DEEP_ANALYSIS_METRICS_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _sidecar_path(runtime: Path, asset) -> Path:
    return photo_vision.photo_vision_path_for(
        photo_vision.default_photo_vision_dir(runtime), asset.photo_asset_id
    )


# --------------------------------------------------------------------------- #
# 1. New exception type exists and is a DeepAnalysisError subclass
# --------------------------------------------------------------------------- #


def test_canonical_write_error_is_deep_analysis_error_subclass():
    assert issubclass(
        deep_analysis.DeepAnalysisCanonicalWriteError, deep_analysis.DeepAnalysisError
    )


# --------------------------------------------------------------------------- #
# 2. Configured + couch put RAISES -> propagates, no orphan, couchdb_write_failed
#    (injected couchdb_put that raises represents the canonical publish failing)
# --------------------------------------------------------------------------- #


def test_configured_couch_put_raises_propagates_and_no_orphan(tmp_path):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)

    def boom(_doc):
        raise RuntimeError("couch 503")

    with pytest.raises(deep_analysis.DeepAnalysisCanonicalWriteError):
        _run(
            runtime,
            upload_root,
            asset,
            vision=StubVisionClient(result="rich prose"),
            store=StubMediaStore(),
            put=boom,
            preset_id="condition_detail",
        )

    # No orphan: the success-path sidecar must NOT have been written with the
    # new entry (CouchDB-first ordering). The file should not exist at all here.
    sidecar_path = _sidecar_path(runtime, asset)
    if sidecar_path.exists():
        sidecar = json.loads(sidecar_path.read_text())
        assert not sidecar.get("deep_analysis"), (
            "orphan deep_analysis entry written despite canonical-write failure"
        )

    # Metric row recorded with the canonical-write-failed status.
    lines = _metric_lines(runtime)
    assert len(lines) == 1
    assert lines[0]["status"] == "couchdb_write_failed"
    assert lines[0]["capture_id"] == cap
    assert lines[0]["photo_asset_id"] == asset.photo_asset_id
    assert "duration_ms" in lines[0]


def test_configured_couch_put_raises_preserves_existing_sidecar_only(tmp_path):
    """If a base sidecar already exists, a canonical-write failure must NOT
    append the new completed entry to it (no orphan), but may leave the
    pre-existing file untouched."""
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)

    sidecar_path = _sidecar_path(runtime, asset)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "artifact_type": photo_vision.ARTIFACT_TYPE,
        "capture_id": cap,
        "photo_asset_id": asset.photo_asset_id,
        "description": "BASE VISION DESCRIPTION",
        "status": "completed",
    }
    sidecar_path.write_text(json.dumps(base))

    def boom(_doc):
        raise RuntimeError("couch down")

    with pytest.raises(deep_analysis.DeepAnalysisCanonicalWriteError):
        _run(
            runtime,
            upload_root,
            asset,
            vision=StubVisionClient(),
            store=StubMediaStore(),
            put=boom,
            preset_id="text_ocr",
        )

    after = json.loads(sidecar_path.read_text())
    # Base content preserved; NO new completed deep_analysis entry leaked in.
    assert after["description"] == "BASE VISION DESCRIPTION"
    assert not after.get("deep_analysis"), "no orphan completed entry on canonical failure"


# --------------------------------------------------------------------------- #
# 3. Configured-via-default-gate: monkeypatch the config gate + put fn to raise
#    (proves _default_couchdb_put path raises, not the injected-put path)
# --------------------------------------------------------------------------- #


def test_default_path_configured_put_raises_propagates(tmp_path, monkeypatch):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)

    # Pretend CouchDB is CONFIGURED.
    monkeypatch.setattr(photo_vision, "_photo_vision_couchdb_config", lambda: object())

    def boom_put(_config, _doc):
        raise RuntimeError("put failed against canonical store")

    monkeypatch.setattr(deep_analysis, "put_photo_vision_document", boom_put)

    with pytest.raises(deep_analysis.DeepAnalysisCanonicalWriteError):
        deep_analysis.run_deep_analysis(
            capture_id=asset.capture_id,
            photo_asset_id=asset.photo_asset_id,
            actor="operator-greg",
            runtime_root=runtime,
            upload_root=upload_root,
            vision_client=StubVisionClient(),
            media_store=StubMediaStore(),
            couchdb_put=None,  # use the default (configured) path
            now=_fixed_now,
            preset_id="condition_detail",
        )

    # No orphan sidecar entry.
    sidecar_path = _sidecar_path(runtime, asset)
    if sidecar_path.exists():
        sidecar = json.loads(sidecar_path.read_text())
        assert not sidecar.get("deep_analysis")

    lines = _metric_lines(runtime)
    assert len(lines) == 1
    assert lines[0]["status"] == "couchdb_write_failed"


def test_default_path_config_gate_raises_propagates(tmp_path, monkeypatch):
    """An enabled-but-broken CouchDB config (gate raises) is a canonical
    publish failure, not a silent no-op."""
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)

    def broken_config():
        raise RuntimeError("invalid couch config")

    monkeypatch.setattr(photo_vision, "_photo_vision_couchdb_config", broken_config)

    with pytest.raises(deep_analysis.DeepAnalysisCanonicalWriteError):
        deep_analysis.run_deep_analysis(
            capture_id=asset.capture_id,
            photo_asset_id=asset.photo_asset_id,
            actor="operator-greg",
            runtime_root=runtime,
            upload_root=upload_root,
            vision_client=StubVisionClient(),
            media_store=StubMediaStore(),
            couchdb_put=None,
            now=_fixed_now,
            preset_id="condition_detail",
        )


# --------------------------------------------------------------------------- #
# 4. Configured + success -> sidecar IS written, completed entry returned
# --------------------------------------------------------------------------- #


def test_configured_couch_put_success_writes_sidecar_and_returns_entry(tmp_path):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)
    puts: list[dict] = []

    entry = _run(
        runtime,
        upload_root,
        asset,
        vision=StubVisionClient(result="all good prose"),
        store=StubMediaStore(),
        put=puts.append,
        preset_id="condition_detail",
    )

    assert entry["status"] == "completed"
    assert entry["result"] == "all good prose"

    # CouchDB was written with the doc carrying the new entry.
    assert len(puts) == 1
    assert isinstance(puts[0]["deep_analysis"], list)
    assert len(puts[0]["deep_analysis"]) == 1

    # Sidecar written AFTER couch success, containing the completed entry.
    sidecar_path = _sidecar_path(runtime, asset)
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["deep_analysis"][-1] == entry

    lines = _metric_lines(runtime)
    assert len(lines) == 1
    assert lines[0]["status"] == "completed"


def test_configured_couch_first_ordering_put_before_sidecar_write(tmp_path):
    """Prove CouchDB-first: the doc handed to the put callable already carries
    the new completed entry (built in memory), and the put happens before disk."""
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)
    sidecar_path = _sidecar_path(runtime, asset)

    observed: dict[str, object] = {}

    def put(doc):
        # At put time, the sidecar file must not yet contain the new entry.
        if sidecar_path.exists():
            existing = json.loads(sidecar_path.read_text())
            observed["sidecar_da_at_put"] = existing.get("deep_analysis")
        else:
            observed["sidecar_da_at_put"] = None
        observed["doc_da_len"] = len(doc.get("deep_analysis") or [])

    _run(
        runtime,
        upload_root,
        asset,
        vision=StubVisionClient(),
        store=StubMediaStore(),
        put=put,
        preset_id="text_ocr",
    )

    # Doc carried the candidate entry; disk had no new entry yet at put time.
    assert observed["doc_da_len"] == 1
    assert not observed["sidecar_da_at_put"]


# --------------------------------------------------------------------------- #
# 5. Unconfigured (no couch) -> sidecar written, completed entry, no raise
#    couchdb_put=None AND config gate None => sidecar-only mode preserved
# --------------------------------------------------------------------------- #


def test_unconfigured_no_couch_writes_sidecar_no_raise(tmp_path, monkeypatch):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)

    # Explicitly unconfigured.
    monkeypatch.setattr(photo_vision, "_photo_vision_couchdb_config", lambda: None)

    entry = deep_analysis.run_deep_analysis(
        capture_id=asset.capture_id,
        photo_asset_id=asset.photo_asset_id,
        actor="operator-greg",
        runtime_root=runtime,
        upload_root=upload_root,
        vision_client=StubVisionClient(result="offline prose"),
        media_store=StubMediaStore(),
        couchdb_put=None,  # default path; config None -> no-op
        now=_fixed_now,
        preset_id="condition_detail",
    )

    assert entry["status"] == "completed"
    assert entry["result"] == "offline prose"

    sidecar_path = _sidecar_path(runtime, asset)
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["deep_analysis"][-1] == entry

    lines = _metric_lines(runtime)
    assert len(lines) == 1
    assert lines[0]["status"] == "completed"


def test_default_couchdb_put_unconfigured_is_noop_no_raise(monkeypatch):
    """The unconfigured no-op path must NOT raise (would break sidecar-only)."""
    monkeypatch.setattr(photo_vision, "_photo_vision_couchdb_config", lambda: None)
    # Should be a clean no-op, never raising.
    deep_analysis._default_couchdb_put({"_id": "fcp_x", "description": "x"})


# --------------------------------------------------------------------------- #
# 6. Model-failure unchanged: returns failed entry, no raise, no couchdb_write_failed
# --------------------------------------------------------------------------- #


def test_model_failure_returns_failed_entry_no_raise(tmp_path):
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)
    vision = StubVisionClient(raises=RuntimeError("model exploded"))

    entry = _run(
        runtime,
        upload_root,
        asset,
        vision=vision,
        store=StubMediaStore(),
        put=lambda d: None,
        preset_id="equipment_id",
    )

    assert entry["status"] == "failed"
    assert entry["error"]["type"] == "RuntimeError"

    lines = _metric_lines(runtime)
    assert len(lines) == 1
    # Model failure must NOT be reported as a canonical-write failure.
    assert lines[0]["status"] == "failed"
    assert lines[0]["status"] != "couchdb_write_failed"


def test_model_failure_with_configured_couch_still_no_raise(tmp_path):
    """Even with a (would-fail) couch put injected, a MODEL failure stays
    fail-closed: the failed entry is recorded best-effort and NO
    DeepAnalysisCanonicalWriteError propagates."""
    runtime = tmp_path / "rt"
    cap, upload_root, asset = _seed_capture(runtime)

    def boom(_doc):
        raise RuntimeError("couch would fail too")

    entry = _run(
        runtime,
        upload_root,
        asset,
        vision=StubVisionClient(raises=RuntimeError("model down")),
        store=StubMediaStore(),
        put=boom,  # failed-path write-through is best-effort, swallowed
        preset_id="cleanliness_qc",
    )

    assert entry["status"] == "failed"
    lines = _metric_lines(runtime)
    assert len(lines) == 1
    assert lines[0]["status"] == "failed"


# --------------------------------------------------------------------------- #
# 7. Handler propagation: a canonical-write failure propagates out of the
#    handler (-> queue routes to failed_dir), does NOT log success, does NOT
#    move the job to processed. Uses the REAL run_deep_analysis flow.
# --------------------------------------------------------------------------- #


def _seed_intake_for_handler(runtime: Path) -> tuple[str, str]:
    """Seed a real photo_capture intake + media under runtime so the handler's
    real run_deep_analysis can resolve the asset. Returns (capture_id, photo_asset_id)."""
    cap = "cap-handler-415"
    date = "2026-06-21"
    filename = "photo1.jpg"
    intake_dir = runtime / "field_capture" / "intake"
    uploads = runtime / "uploads"
    intake_dir.mkdir(parents=True, exist_ok=True)
    media_dir = uploads / date / cap
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / filename).write_bytes(b"\xff\xd8\xff\xd9")
    stored_path = f"{date}/{cap}/{filename}"
    intake = {
        "job_type": "photo_capture",
        "metadata": {"capture_id": cap, "site_id": "SANDBOX"},
        "payload": {
            "site": "SANDBOX",
            "captured_at": f"{date}T12:00:00Z",
            "area": "restroom",
            "phase": "after",
            "photos": [
                {"filename": filename, "stored_path": stored_path, "upload_id": stored_path}
            ],
        },
    }
    (intake_dir / f"{cap}.json").write_text(json.dumps(intake))
    assets = photo_vision.discover_photo_assets(intake_dir, uploads, capture_id=cap)
    assert assets
    return cap, assets[0].photo_asset_id


def test_handler_canonical_write_failure_propagates_to_failed_dir(tmp_path, monkeypatch):
    import queue_processor.handlers.deep_analysis as handler_mod
    import queue_spec
    from queue_processor.handlers._shared import QueueJob, RunContext

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    cap, photo_asset_id = _seed_intake_for_handler(runtime)

    # Make CouchDB CONFIGURED and the canonical put FAIL.
    monkeypatch.setattr(photo_vision, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr(
        deep_analysis,
        "put_photo_vision_document",
        lambda _cfg, _doc: (_ for _ in ()).throw(RuntimeError("canonical store down")),
    )
    # Real vision/media so analysis succeeds and we reach the canonical write.
    monkeypatch.setattr(
        deep_analysis, "_build_default_vision_client", lambda: StubVisionClient()
    )
    monkeypatch.setattr(deep_analysis, "get_media_store", lambda _root: StubMediaStore())

    context = RunContext(
        project_root=tmp_path,
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=False,
        valid_site_ids=set(),
        site_id_to_opportunities_dir={},
    )
    queue_file = runtime / "job-1.json"
    queue_file.write_text("{}\n", encoding="utf-8")
    processed_dir = runtime / "processed"

    job = QueueJob(
        job_id="job-1",
        job_type=queue_spec.JOB_DEEP_ANALYSIS,
        payload={
            "capture_id": cap,
            "photo_asset_id": photo_asset_id,
            "actor": "operator:greg",
            "preset_id": "condition_detail",
        },
        metadata={},
        intent={},
    )

    # The canonical-write failure must propagate so the queue routes to failed_dir.
    with pytest.raises(deep_analysis.DeepAnalysisCanonicalWriteError):
        handler_mod.process_deep_analysis_job(queue_file, job, context, processed_dir)

    # Did NOT move the job to processed (queue will route it to failed_dir).
    assert queue_file.exists()
    assert not (processed_dir / queue_file.name).exists()

    # Did NOT write a status=success log line.
    log_path = context.log_path
    log_text = log_path.read_text() if log_path.exists() else ""
    assert "status=success" not in log_text

    # The canonical-write-failed metric was recorded.
    lines = _metric_lines(runtime)
    assert any(m["status"] == "couchdb_write_failed" for m in lines)


def test_handler_success_path_moves_to_processed_and_logs(tmp_path, monkeypatch):
    """Companion: with couch unconfigured + a successful analysis, the real
    handler flow moves the job to processed and logs success (414 fallback
    data source keeps working)."""
    import queue_processor.handlers.deep_analysis as handler_mod
    import queue_spec
    from queue_processor.handlers._shared import QueueJob, RunContext

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    cap, photo_asset_id = _seed_intake_for_handler(runtime)

    monkeypatch.setattr(photo_vision, "_photo_vision_couchdb_config", lambda: None)
    monkeypatch.setattr(
        deep_analysis, "_build_default_vision_client", lambda: StubVisionClient()
    )
    monkeypatch.setattr(deep_analysis, "get_media_store", lambda _root: StubMediaStore())

    context = RunContext(
        project_root=tmp_path,
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=False,
        valid_site_ids=set(),
        site_id_to_opportunities_dir={},
    )
    queue_file = runtime / "job-2.json"
    queue_file.write_text("{}\n", encoding="utf-8")
    processed_dir = runtime / "processed"

    job = QueueJob(
        job_id="job-2",
        job_type=queue_spec.JOB_DEEP_ANALYSIS,
        payload={
            "capture_id": cap,
            "photo_asset_id": photo_asset_id,
            "actor": "operator:greg",
            "preset_id": "condition_detail",
        },
        metadata={},
        intent={},
    )

    handler_mod.process_deep_analysis_job(queue_file, job, context, processed_dir)

    assert (processed_dir / queue_file.name).exists()
    assert not queue_file.exists()
    assert "status=success" in context.log_path.read_text()

    sidecar = json.loads(_sidecar_path(runtime, type("A", (), {"photo_asset_id": photo_asset_id})()).read_text())
    assert sidecar["deep_analysis"][-1]["status"] == "completed"
