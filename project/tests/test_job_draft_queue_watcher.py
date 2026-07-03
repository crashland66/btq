"""Behavioral tests for BTQ prompt 336: the job_draft -> runtime-queue GATE.

Authored by the INDEPENDENT VERIFIER (not the executor).

These tests EXECUTE the real watcher
(``queue_processor.job_draft_queue_watcher``) against a throwaway real CouchDB
(the same pattern as ``test_couchdb_job_draft_gate.py``: push the 333 design
doc into a freshly-created database so server-side validate_doc_update runs),
using a ``tmp_path`` runtime_root for the queue directory. They prove the gate
both ways and that the materialized file round-trips through the REAL processor
loader (``queue_processor.main.load_job`` / ``load_job_payload`` +
``queue_spec.validate_job``).

If no CouchDB is reachable these tests skip LOUDLY (they must not silently
pass). Creds come from the env (BTQ_TEST_COUCHDB_* / BTQ_COUCHDB_*) and, failing
that, from ``~/.config/btq/config.json`` so they actually RUN on dev boxes.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from urllib import error, request

import pytest

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import AlreadyDecided
from event_pipeline.couchdb_job_draft_writer import (
    get_job_draft,
    job_draft_doc_id,
    set_job_draft_queue_materialized_at,
    upsert_job_draft,
)
from queue_processor import job_draft_queue_watcher as jdw
from queue_processor import main as qp_main
from queue_spec import validate_job


DESIGN_DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "event_pipeline"
    / "couchdb"
    / "design_btq_field_captures.json"
)


def _design_doc() -> dict:
    return json.loads(DESIGN_DOC_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Reachable-CouchDB plumbing (mirrors test_couchdb_job_draft_gate.py).
# --------------------------------------------------------------------------- #
def _config_file_creds() -> tuple[str, str, str] | None:
    path = Path(os.path.expanduser("~/.config/btq/config.json"))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = data.get("couchdb_url")
    user = data.get("couchdb_user")
    pwd = data.get("couchdb_password")
    if url and user and pwd:
        return url, user, pwd
    return None


def _live_config() -> couchdb_config.CouchDBConfig | None:
    url = os.environ.get("BTQ_TEST_COUCHDB_URL") or os.environ.get("BTQ_COUCHDB_URL")
    user = os.environ.get("BTQ_TEST_COUCHDB_USER") or os.environ.get("BTQ_COUCHDB_USER")
    pwd = os.environ.get("BTQ_TEST_COUCHDB_PASSWORD") or os.environ.get("BTQ_COUCHDB_PASSWORD")
    if not (url and user and pwd):
        creds = _config_file_creds()
        if creds is None:
            return None
        url, user, pwd = creds
    cfg = couchdb_config.from_env(
        base_url_override=url,
        username_override=user,
        password_override=pwd,
        timeout_override=10.0,
    )
    req = request.Request(
        f"{cfg.base_url}/_session",
        headers={**cfg.auth_header(), "Accept": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=5) as resp:
            if not 200 <= getattr(resp, "status", 200) < 300:
                return None
    except (error.URLError, OSError):
        return None
    return cfg


def _http(cfg, method, path, body=None):
    url = f"{cfg.base_url}/{path}"
    headers = {"Accept": "application/json", **cfg.auth_header()}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=10) as resp:
        return int(getattr(resp, "status", 200)), json.loads(resp.read() or b"{}")


@pytest.fixture()
def live_db():
    cfg = _live_config()
    if cfg is None:
        pytest.skip(
            "no reachable CouchDB (set BTQ_COUCHDB_URL/USER/PASSWORD or "
            "populate ~/.config/btq/config.json)"
        )
    db = f"btq_verify_336_{uuid.uuid4().hex[:12]}"
    _http(cfg, "PUT", db)
    design = _design_doc()
    _http(
        cfg,
        "PUT",
        f"{db}/_design/btq_field_captures",
        {k: v for k, v in design.items() if k != "_rev"},
    )
    try:
        yield cfg, db
    finally:
        try:
            request.urlopen(
                request.Request(
                    f"{cfg.base_url}/{db}",
                    headers={**cfg.auth_header(), "Accept": "application/json"},
                    method="DELETE",
                ),
                timeout=10,
            )
        except (error.URLError, OSError):
            pass


@pytest.fixture()
def logger() -> logging.Logger:
    return logging.getLogger("test.job_draft_queue_watch")


# --------------------------------------------------------------------------- #
# Helpers: a queue_spec-VALID job draft, and a queue-file lister.
# --------------------------------------------------------------------------- #
def _valid_payload(supply_id: str = "supply-1", actor: str = "verifier") -> dict:
    # mark_supply_ordered requires exactly {supply_id, actor} -> passes validate_job.
    return {"supply_id": supply_id, "actor": actor}


def _seed_draft(
    cfg,
    db,
    draft_id: str,
    *,
    review_status: str,
    job_type: str = "mark_supply_ordered",
    payload: dict | None = None,
    source_capture_id: str = "cap-336",
    group_id: str | None = None,
) -> dict:
    """Write a job_draft via the real writer.

    The writer defaults review_status to pending_approval; an approved/rejected
    draft is written by a follow-up upsert with the explicit status (the
    server-side validate fn whitelists all three).
    """
    draft = {
        "draft_id": draft_id,
        "job_type": job_type,
        "payload": _valid_payload() if payload is None else payload,
        "source_capture_id": source_capture_id,
        "group_id": group_id or draft_id,
        "message": "operator note that must NOT leak into the queue file",
    }
    upsert_job_draft(cfg, db, dict(draft))
    if review_status != "pending_approval":
        draft["review_status"] = review_status
        upsert_job_draft(cfg, db, dict(draft))
    return get_job_draft(cfg, db, draft_id)


def _queue_files(runtime_root: Path) -> list[Path]:
    qd = runtime_root / "queue"
    if not qd.exists():
        return []
    return sorted(p for p in qd.iterdir() if p.is_file() and p.suffix == ".json")


def test_process_one_no_action_marks_without_queue_file(tmp_path, logger, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_mark(config, db, draft_id, *, materialized_at, expected_rev=None):
        calls.append(
            {
                "config": config,
                "db": db,
                "draft_id": draft_id,
                "materialized_at": materialized_at,
                "expected_rev": expected_rev,
            }
        )
        return {"_rev": "2-noaction", "queue_materialized_at": materialized_at, "queue_materialized_as": "no_action"}

    monkeypatch.setattr(jdw, "set_job_draft_queue_no_action_at", fake_mark)
    doc = {
        "_rev": "1-noaction",
        "type": "job_draft",
        "draft_id": "draft-no-action",
        "review_status": "approved",
        "route": "no_action",
        "job_type": "log_supply_need",
        "payload": {"site_id": "7060", "item_name": "liners", "requested_by": "Casey"},
    }

    result = jdw.process_one(config=object(), db="db", doc=doc, runtime_root=tmp_path, logger=logger)

    assert result["ok"] is True
    assert result["materialized"] is False
    assert result["skipped"] == "no_action"
    assert result["queue_materialized_as"] == "no_action"
    assert result["error"] == ""
    assert calls and calls[0]["draft_id"] == "draft-no-action"
    assert calls[0]["expected_rev"] == "1-noaction"
    assert _queue_files(tmp_path) == []


def test_process_one_no_action_dry_run_does_not_mark_or_write(tmp_path, logger, monkeypatch) -> None:
    def fail_mark(*_args, **_kwargs):
        raise AssertionError("dry-run no_action should not mark CouchDB")

    monkeypatch.setattr(jdw, "set_job_draft_queue_no_action_at", fail_mark)
    doc = {
        "_rev": "1-noaction",
        "type": "job_draft",
        "draft_id": "draft-no-action",
        "review_status": "approved",
        "route": "no_action",
        "job_type": "log_supply_need",
        "payload": {"site_id": "7060", "item_name": "liners", "requested_by": "Casey"},
    }

    result = jdw.process_one(config=object(), db="db", doc=doc, runtime_root=tmp_path, logger=logger, dry_run=True)

    assert result == {
        "draft_id": "draft-no-action",
        "ok": True,
        "materialized": False,
        "error": "",
        "skipped": "no_action",
        "dry_run": True,
    }
    assert _queue_files(tmp_path) == []


# --------------------------------------------------------------------------- #
# THE GATE — approved materializes, pending/rejected NEVER do (both ways).
# --------------------------------------------------------------------------- #
@pytest.mark.real_couchdb
class TestTheGate:
    def test_only_approved_materializes(self, live_db, tmp_path, logger) -> None:
        cfg, db = live_db
        runtime_root = tmp_path
        _seed_draft(cfg, db, "d-approved", review_status="approved",
                    payload=_valid_payload("sup-app"))
        _seed_draft(cfg, db, "d-pending", review_status="pending_approval",
                    payload=_valid_payload("sup-pend"))
        _seed_draft(cfg, db, "d-rejected", review_status="rejected",
                    payload=_valid_payload("sup-rej"))

        # The _find selector must not even RETURN the non-approved drafts.
        selected = jdw.find_approved_unmaterialized_drafts(cfg, db)
        selected_ids = {d.get("draft_id") for d in selected}
        assert selected_ids == {"d-approved"}, selected_ids

        results = jdw.process_pass(config=cfg, db=db, runtime_root=runtime_root, logger=logger)
        assert all(r["ok"] for r in results), results

        # Exactly ONE queue file, and it belongs to the approved draft.
        files = _queue_files(runtime_root)
        assert len(files) == 1, files
        assert "d-approved" in files[0].name

        # Approved got the mark; pending/rejected did NOT (no mark, no error).
        approved = get_job_draft(cfg, db, "d-approved")
        assert approved.get("queue_materialized_at")
        assert not approved.get("queue_materialize_error")
        for did in ("d-pending", "d-rejected"):
            doc = get_job_draft(cfg, db, did)
            assert not doc.get("queue_materialized_at"), did
            assert not doc.get("queue_materialize_error"), did

    def test_pending_then_approve_flips_the_gate(self, live_db, tmp_path, logger) -> None:
        cfg, db = live_db
        runtime_root = tmp_path
        _seed_draft(cfg, db, "d-flip", review_status="pending_approval")
        # While pending: gate closed -> nothing.
        jdw.process_pass(config=cfg, db=db, runtime_root=runtime_root, logger=logger)
        assert _queue_files(runtime_root) == []
        assert not get_job_draft(cfg, db, "d-flip").get("queue_materialized_at")
        # Approve it -> gate opens -> exactly one file + mark.
        _seed_draft(cfg, db, "d-flip", review_status="approved")
        jdw.process_pass(config=cfg, db=db, runtime_root=runtime_root, logger=logger)
        files = _queue_files(runtime_root)
        assert len(files) == 1, files
        assert get_job_draft(cfg, db, "d-flip").get("queue_materialized_at")


# --------------------------------------------------------------------------- #
# CLEAN JOB-FILE FORMAT — round-trips through the REAL processor loader.
# --------------------------------------------------------------------------- #
@pytest.mark.real_couchdb
class TestCleanJobFileFormat:
    def test_exact_shape_and_loader_acceptance(self, live_db, tmp_path, logger) -> None:
        cfg, db = live_db
        runtime_root = tmp_path
        payload = _valid_payload("sup-clean", "actor-x")
        draft = _seed_draft(cfg, db, "d-clean", review_status="approved",
                            payload=payload, source_capture_id="cap-clean",
                            group_id="grp-clean")

        jdw.process_pass(config=cfg, db=db, runtime_root=runtime_root, logger=logger)
        files = _queue_files(runtime_root)
        assert len(files) == 1, files
        job = json.loads(files[0].read_text(encoding="utf-8"))

        # Exact clean shape.
        assert job == {
            "job_type": "mark_supply_ordered",
            "payload": payload,
            "metadata": {
                "source": "job_draft",
                "draft_id": "d-clean",
                "source_capture_id": "cap-clean",
                "group_id": "grp-clean",
            },
        }, job

        # Top-level type is NOT job_draft; no review fields leaked.
        assert job.get("type") != "job_draft"
        assert "type" not in job
        for leak in ("review_status", "message", "_id", "_rev", "draft_id",
                     "reviewed_by", "reviewed_at", "created_at"):
            assert leak not in job, f"{leak} leaked into job file"

        # The REAL processor loader accepts it, and queue_spec validates it.
        assert validate_job(job) is True
        payload_back = qp_main.load_job_payload(files[0])
        assert payload_back["job_type"] == "mark_supply_ordered"
        loaded = qp_main.load_job(files[0])
        assert loaded.job_type == "mark_supply_ordered"
        assert loaded.payload == payload
        assert loaded.metadata["source"] == "job_draft"
        assert loaded.metadata["draft_id"] == "d-clean"

    def test_build_helper_drops_review_fields(self, live_db, tmp_path, logger) -> None:
        cfg, db = live_db
        draft = _seed_draft(cfg, db, "d-build", review_status="approved")
        # The doc we pass carries review_status/_id/_rev/message; the builder
        # must emit ONLY the clean {job_type, payload, metadata}.
        assert "review_status" in draft and "_rev" in draft
        job = jdw.build_queue_job_from_draft(draft)
        assert set(job) == {"job_type", "payload", "metadata"}
        assert validate_job(job) is True


# --------------------------------------------------------------------------- #
# IDEMPOTENCY — two passes -> exactly one file, mark unchanged.
# --------------------------------------------------------------------------- #
@pytest.mark.real_couchdb
class TestIdempotency:
    def test_double_pass_one_file(self, live_db, tmp_path, logger) -> None:
        cfg, db = live_db
        runtime_root = tmp_path
        _seed_draft(cfg, db, "d-idem", review_status="approved")

        r1 = jdw.process_pass(config=cfg, db=db, runtime_root=runtime_root, logger=logger)
        assert [r for r in r1 if r.get("materialized")], r1
        mark1 = get_job_draft(cfg, db, "d-idem").get("queue_materialized_at")
        assert mark1
        files1 = _queue_files(runtime_root)
        assert len(files1) == 1

        # Second pass: selector no longer returns it (materialized) -> no work.
        selected = jdw.find_approved_unmaterialized_drafts(cfg, db)
        assert {d.get("draft_id") for d in selected} == set()
        r2 = jdw.process_pass(config=cfg, db=db, runtime_root=runtime_root, logger=logger)
        assert not any(r.get("materialized") for r in r2), r2

        files2 = _queue_files(runtime_root)
        assert len(files2) == 1
        assert files2 == files1
        mark2 = get_job_draft(cfg, db, "d-idem").get("queue_materialized_at")
        assert mark2 == mark1  # mark unchanged on re-run

    def test_existing_queue_file_not_duplicated(self, live_db, tmp_path, logger) -> None:
        # If the file already exists (e.g. the mark write failed last time) the
        # second pass must NOT create a duplicate -- FileExistsError path.
        cfg, db = live_db
        runtime_root = tmp_path
        doc = _seed_draft(cfg, db, "d-exists", review_status="approved")
        job = jdw.build_queue_job_from_draft(doc)
        dest = jdw.queue_path_for_draft_job(job, "d-exists", runtime_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(job) + "\n", encoding="utf-8")
        before = dest.read_text(encoding="utf-8")

        jdw.process_pass(config=cfg, db=db, runtime_root=runtime_root, logger=logger)
        files = _queue_files(runtime_root)
        assert len(files) == 1, files
        assert dest.read_text(encoding="utf-8") == before  # untouched, no dup
        # The mark still gets written so the draft leaves the selector.
        assert get_job_draft(cfg, db, "d-exists").get("queue_materialized_at")


# --------------------------------------------------------------------------- #
# _rev RACE — stale _rev on the mark -> AlreadyDecided, no double file.
# --------------------------------------------------------------------------- #
@pytest.mark.real_couchdb
class TestRevRace:
    def test_stale_expected_rev_raises_already_decided(self, live_db, tmp_path) -> None:
        cfg, db = live_db
        doc = _seed_draft(cfg, db, "d-race", review_status="approved")
        stale_rev = doc["_rev"]
        # Mutate the doc so stale_rev is no longer current.
        upsert_job_draft(cfg, db, {
            "draft_id": "d-race",
            "job_type": "mark_supply_ordered",
            "payload": _valid_payload(),
            "review_status": "approved",
            "message": "touched",
        })
        with pytest.raises(AlreadyDecided):
            set_job_draft_queue_materialized_at(
                cfg, db, "d-race",
                materialized_at="2026-06-11T00:00:00+00:00",
                expected_rev=stale_rev,
            )
        # The stale-rev attempt did NOT mark.
        assert not get_job_draft(cfg, db, "d-race").get("queue_materialized_at")

    def test_already_materialized_blocks_second_mark(self, live_db, tmp_path) -> None:
        cfg, db = live_db
        _seed_draft(cfg, db, "d-race2", review_status="approved")
        first = set_job_draft_queue_materialized_at(
            cfg, db, "d-race2", materialized_at="2026-06-11T00:00:00+00:00"
        )
        assert first.get("queue_materialized_at")
        # A second mark must be refused (already materialized) -> AlreadyDecided.
        with pytest.raises(AlreadyDecided):
            set_job_draft_queue_materialized_at(
                cfg, db, "d-race2", materialized_at="2026-06-11T11:11:11+00:00"
            )
        # Mark value is the original, not overwritten.
        assert get_job_draft(cfg, db, "d-race2")["queue_materialized_at"] == \
            "2026-06-11T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# POISON-PILL — approved but invalid payload -> error mark, no file, no loop.
# --------------------------------------------------------------------------- #
@pytest.mark.real_couchdb
class TestPoisonPill:
    def test_invalid_payload_marks_error_no_file(self, live_db, tmp_path, logger) -> None:
        cfg, db = live_db
        runtime_root = tmp_path
        # mark_supply_ordered requires {supply_id, actor}; omit actor -> invalid.
        _seed_draft(cfg, db, "d-poison", review_status="approved",
                    payload={"supply_id": "only-id"})
        # Sanity: this job really is invalid per queue_spec.
        bad_job = {"job_type": "mark_supply_ordered", "payload": {"supply_id": "only-id"}}
        assert validate_job(bad_job) is False

        jdw.process_pass(config=cfg, db=db, runtime_root=runtime_root, logger=logger)

        # No queue file produced.
        assert _queue_files(runtime_root) == []
        doc = get_job_draft(cfg, db, "d-poison")
        # Got the error mark, NOT the success mark.
        assert doc.get("queue_materialize_error")
        assert not doc.get("queue_materialized_at")

        # Re-run does NOT loop / re-attempt: selector excludes errored drafts.
        selected = jdw.find_approved_unmaterialized_drafts(cfg, db)
        assert "d-poison" not in {d.get("draft_id") for d in selected}
        r2 = jdw.process_pass(config=cfg, db=db, runtime_root=runtime_root, logger=logger)
        assert not any(r.get("materialized") for r in r2), r2
        assert _queue_files(runtime_root) == []
        # Error mark unchanged (no flip to success, no new file).
        doc2 = get_job_draft(cfg, db, "d-poison")
        assert doc2.get("queue_materialize_error")
        assert not doc2.get("queue_materialized_at")


# --------------------------------------------------------------------------- #
# CLI SMOKE — watch-couchdb-job-drafts --once --dry-run writes nothing.
# --------------------------------------------------------------------------- #
@pytest.mark.real_couchdb
class TestCliSmoke:
    def test_once_dry_run_writes_nothing(self, live_db, tmp_path) -> None:
        cfg, db = live_db
        runtime_root = tmp_path
        _seed_draft(cfg, db, "d-cli", review_status="approved")
        # Drive the real watcher.run() entrypoint (what the CLI subcommand calls)
        # with explicit live creds via env so from_env() resolves.
        env_keys = {
            "BTQ_COUCHDB_URL": cfg.base_url,
        }
        # Use the watcher's own run() with --dry-run --once; supply creds through
        # the config object the CLI ultimately reads from env. We set env from
        # the resolved live config so from_env() succeeds in-process.
        creds = _config_file_creds()
        prior = {}
        try:
            if creds is not None:
                url, user, pwd = creds
                for k, v in (("BTQ_COUCHDB_URL", url), ("BTQ_COUCHDB_USER", user),
                             ("BTQ_COUCHDB_PASSWORD", pwd)):
                    prior[k] = os.environ.get(k)
                    os.environ[k] = v
            else:
                for k in ("BTQ_COUCHDB_URL", "BTQ_COUCHDB_USER", "BTQ_COUCHDB_PASSWORD"):
                    if not os.environ.get(k):
                        pytest.skip("live creds only available via env; cannot drive run() smoke")
            rc = jdw.run([
                "--runtime-root", str(runtime_root),
                "--database", db,
                "--once", "--dry-run",
            ])
        finally:
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        assert rc == 0
        # Dry-run wrote NO queue file and left NO mark.
        assert _queue_files(runtime_root) == []
        assert not get_job_draft(cfg, db, "d-cli").get("queue_materialized_at")
