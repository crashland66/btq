"""Adversarial verification for BTQ prompt 338: live-mesh data migration
``action_candidate`` -> ``job_draft``.

Authored by the INDEPENDENT VERIFIER (not the executor).

The stakes: a bug here either LOSES the review backlog (pending candidates that
never become drafts) or, far worse, DOUBLE-EXECUTES an already-run job (an
``approved`` candidate whose job was already materialized gets re-materialized by
the 336 ``job_draft_queue_watcher`` and runs the canonical mutation a second
time). So these tests run against a REAL CouchDB throwaway database, seed
``action_candidate`` docs of every status with the REAL candidate writer, run the
REAL migration, then drive the REAL watcher selector
(``find_approved_unmaterialized_drafts``) + ``process_pass`` to PROVE the
migrated approved draft is excluded and produces no queue file.

If no CouchDB is reachable the real_couchdb tests skip loudly (must not silently
pass). Creds come from env (BTQ_TEST_COUCHDB_* / BTQ_COUCHDB_*) then from
``~/.config/btq/config.json`` so they actually RUN on dev boxes (Dell).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib import error, request

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import (
    get_action_candidate,
    upsert_action_candidate,
)
from event_pipeline.couchdb_job_draft_writer import get_job_draft, job_draft_doc_id
from event_pipeline.couchdb.migrate_candidates_to_drafts import (
    REPORT_KEYS,
    migrate_candidates_to_drafts,
)
from queue_processor import job_draft_queue_watcher


# --------------------------------------------------------------------------- #
# Real design doc + live CouchDB helpers (pattern from test_couchdb_job_draft_gate)
# --------------------------------------------------------------------------- #
DESIGN_DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "event_pipeline"
    / "couchdb"
    / "design_btq_field_captures.json"
)


def _design_doc() -> dict:
    return json.loads(DESIGN_DOC_PATH.read_text(encoding="utf-8"))


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
    db = f"btq_verify_338_{uuid.uuid4().hex[:12]}"
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


# --------------------------------------------------------------------------- #
# Candidate seed builders (use the REAL candidate writer downstream).
# --------------------------------------------------------------------------- #
def _single_job_candidate(candidate_id: str, status: str, **extra) -> dict:
    """A candidate carrying an explicit proposed_queue_job -> exactly ONE job."""
    cand = {
        "candidate_id": candidate_id,
        "status": status,
        "site_id": "7050",
        "summary": "Site 7050 needs towel restock",
        "capture_id": f"cap-{candidate_id}",
        "proposed_queue_job": {
            "job_type": "log_supply_need",
            "payload": {"site_id": "7050", "items": ["towels"]},
        },
    }
    cand.update(extra)
    return cand


def _multi_job_candidate(candidate_id: str, status: str, **extra) -> dict:
    """A voice_memo operator action naming TWO employees -> TWO valid jobs."""
    cand = {
        "candidate_id": candidate_id,
        "status": status,
        "candidate_type": "voice_memo_operator_action",
        "summary": "Two workers left the company",
        "source_text": "Alice and Bob are no longer with us",
        "capture_id": f"cap-{candidate_id}",
        "channel_metadata": {
            "intent": "employee_status_change",
            "action": "set_inactive",
            "employee_names": ["Alice Worker", "Bob Worker"],
            "site_id": "7050",
        },
    }
    cand.update(extra)
    return cand


def _no_job_candidate(candidate_id: str, status: str = "pending_review", **extra) -> dict:
    """Pending candidate with no proposed job and no site -> NO valid job."""
    cand = {
        "candidate_id": candidate_id,
        "status": status,
        "summary": "vague operator note with no actionable target",
    }
    cand.update(extra)
    return cand


def _seed(cfg, db, cand: dict) -> dict:
    upsert_action_candidate(cfg, db, cand)
    return get_action_candidate(cfg, db, cand["candidate_id"])


def _empty_report_counts(report: dict) -> dict:
    return {k: report[k] for k in REPORT_KEYS}


# --------------------------------------------------------------------------- #
# Per-status migration behavior.
# --------------------------------------------------------------------------- #
@pytest.mark.real_couchdb
class TestPerStatusMigration:
    def test_pending_one_job_creates_one_pending_approval_draft(self, live_db):
        """pending_review + 1 job -> 1 pending_approval draft, correct fields."""
        cfg, db = live_db
        _seed(cfg, db, _single_job_candidate("p1", "pending_review"))
        report = migrate_candidates_to_drafts(cfg, db, dry_run=False)
        assert report["drafts_created"] == 1
        assert report["pending_migrated"] == 1
        assert report["errors"] == []

        draft_id = "cap-p1-log_supply_need-0"
        draft = get_job_draft(cfg, db, draft_id)
        assert draft is not None, "migrated pending draft must be readable"
        assert draft["review_status"] == "pending_approval"
        assert draft["job_type"] == "log_supply_need"
        assert draft["payload"] == {"site_id": "7050", "items": ["towels"]}
        assert draft["site_id"] == "7050"
        assert draft["message"] == "Site 7050 needs towel restock"
        assert draft["group_id"] == "cap-p1"
        # ordinal draft_id ends with -0
        assert draft["draft_id"].endswith("-0")
        # pending drafts must NOT carry a queue_materialized_at
        assert not draft.get("queue_materialized_at")

    def test_pending_n_jobs_share_group_distinct_ordinals(self, live_db):
        """pending_review + N jobs -> N drafts, shared group_id, distinct ordinals."""
        cfg, db = live_db
        _seed(cfg, db, _multi_job_candidate("m1", "pending_review"))
        report = migrate_candidates_to_drafts(cfg, db, dry_run=False)
        assert report["drafts_created"] == 2
        assert report["pending_migrated"] == 2

        d0 = get_job_draft(cfg, db, "cap-m1-set_entity_status-0")
        d1 = get_job_draft(cfg, db, "cap-m1-set_entity_status-1")
        assert d0 is not None and d1 is not None
        # shared group_id
        assert d0["group_id"] == d1["group_id"] == "cap-m1"
        # distinct ordinal draft_ids
        assert d0["draft_id"] != d1["draft_id"]
        assert {d0["draft_id"], d1["draft_id"]} == {
            "cap-m1-set_entity_status-0",
            "cap-m1-set_entity_status-1",
        }
        # both pending_approval, distinct employee payloads carried
        assert d0["review_status"] == d1["review_status"] == "pending_approval"
        targets = {d0["payload"]["entity_id"], d1["payload"]["entity_id"]}
        assert targets == {"Alice Worker", "Bob Worker"}

    def test_rejected_creates_kept_rejected_draft(self, live_db):
        """rejected -> rejected draft, re-readable, reviewer/rationale carried."""
        cfg, db = live_db
        _seed(
            cfg,
            db,
            _single_job_candidate(
                "r1",
                "rejected",
                reviewed_by="greg",
                review_rationale="not a real need",
            ),
        )
        report = migrate_candidates_to_drafts(cfg, db, dry_run=False)
        assert report["drafts_created"] == 1
        assert report["rejected_migrated"] == 1

        draft = get_job_draft(cfg, db, "cap-r1-log_supply_need-0")
        assert draft is not None, "rejected draft must be KEPT and re-readable"
        assert draft["review_status"] == "rejected"
        assert draft["reviewed_by"] == "greg"
        assert draft["review_rationale"] == "not a real need"

    def test_archived_maps_to_rejected(self, live_db):
        """archived (pending_review + archived) -> rejected draft."""
        cfg, db = live_db
        _seed(
            cfg,
            db,
            _single_job_candidate(
                "a1",
                "pending_review",
                archived=True,
                archived_by="greg",
                archived_at="2026-06-10T00:00:00Z",
            ),
        )
        report = migrate_candidates_to_drafts(cfg, db, dry_run=False)
        assert report["rejected_migrated"] == 1
        draft = get_job_draft(cfg, db, "cap-a1-log_supply_need-0")
        assert draft is not None
        assert draft["review_status"] == "rejected"
        assert "Archived action candidate" in draft["review_rationale"]

    def test_failed_creates_no_draft_but_marks_migrated(self, live_db):
        """failed -> no draft (skipped_no_job), source still marked migrated."""
        cfg, db = live_db
        seeded = _seed(cfg, db, _single_job_candidate("f1", "failed"))
        report = migrate_candidates_to_drafts(cfg, db, dry_run=False)
        assert report["drafts_created"] == 0
        assert report["skipped_no_job"] == 1
        assert get_job_draft(cfg, db, "cap-f1-log_supply_need-0") is None
        # source candidate marked migrated so a re-scan won't re-touch it
        after = get_action_candidate(cfg, db, "f1")
        assert after.get("migrated_to_draft_at")

    def test_no_valid_job_creates_no_draft_but_marks_migrated(self, live_db):
        """pending with no resolvable job -> skipped_no_job, source marked."""
        cfg, db = live_db
        _seed(cfg, db, _no_job_candidate("n1"))
        report = migrate_candidates_to_drafts(cfg, db, dry_run=False)
        assert report["drafts_created"] == 0
        assert report["skipped_no_job"] == 1
        after = get_action_candidate(cfg, db, "n1")
        assert after.get("migrated_to_draft_at")


# --------------------------------------------------------------------------- #
# THE DOUBLE-EXECUTION GATE (most important).
# --------------------------------------------------------------------------- #
@pytest.mark.real_couchdb
class TestApprovedDoubleExecutionGate:
    def test_approved_draft_is_approved_and_pre_materialized(self, live_db):
        """The migrated approved draft is review_status==approved AND has a
        non-empty queue_materialized_at (its job already ran; must not re-run)."""
        cfg, db = live_db
        _seed(
            cfg,
            db,
            _single_job_candidate(
                "ap1",
                "approved",
                reviewed_by="greg",
                review_rationale="approved earlier",
                staged_at="2026-06-01T12:00:00Z",
            ),
        )
        report = migrate_candidates_to_drafts(cfg, db, dry_run=False)
        assert report["approved_marked_materialized"] == 1

        draft = get_job_draft(cfg, db, "cap-ap1-log_supply_need-0")
        assert draft is not None
        assert draft["review_status"] == "approved"
        assert draft.get("queue_materialized_at"), (
            "approved migrated draft MUST carry queue_materialized_at or the "
            "336 watcher will re-execute the already-run job"
        )

    def test_watcher_selector_excludes_migrated_approved_draft(self, live_db):
        """The REAL watcher query find_approved_unmaterialized_drafts must NOT
        return the migrated approved draft -- this is the gate that prevents
        double canonical mutation."""
        cfg, db = live_db
        _seed(
            cfg,
            db,
            _single_job_candidate(
                "ap2", "approved", reviewed_by="greg", review_rationale="ok"
            ),
        )
        migrate_candidates_to_drafts(cfg, db, dry_run=False)

        unmaterialized = job_draft_queue_watcher.find_approved_unmaterialized_drafts(
            cfg, db
        )
        migrated_ids = {d.get("draft_id") for d in unmaterialized}
        assert "cap-ap2-log_supply_need-0" not in migrated_ids, (
            "migrated approved draft selected for re-materialization -> "
            "DOUBLE EXECUTION"
        )

    def test_watcher_process_pass_produces_no_queue_file_for_migrated_approved(
        self, live_db, tmp_path
    ):
        """Drive the watcher's real process_pass over the migrated db and assert
        it writes NO queue file for the migrated approved draft."""
        cfg, db = live_db
        _seed(
            cfg,
            db,
            _single_job_candidate(
                "ap3", "approved", reviewed_by="greg", review_rationale="ok"
            ),
        )
        migrate_candidates_to_drafts(cfg, db, dry_run=False)

        logger = logging.getLogger("test_338_watcher")
        results = job_draft_queue_watcher.process_pass(
            config=cfg,
            db=db,
            runtime_root=tmp_path,
            logger=logger,
            dry_run=False,
        )
        # No queue file materialized for the migrated approved draft.
        queue_dir = tmp_path / "queue"
        files = list(queue_dir.glob("*.json")) if queue_dir.exists() else []
        assert files == [], f"watcher re-materialized a queue file: {files}"
        # And the result set does not contain a materialized==True for our draft.
        materialized = [
            r for r in results if r.get("draft_id") == "cap-ap3-log_supply_need-0"
        ]
        assert all(not r.get("materialized") for r in materialized), results

    def test_pending_draft_is_NOT_excluded_baseline(self, live_db, tmp_path, enqueue_capture):
        """Sanity baseline so the gate above is meaningful: a genuinely
        approved-but-UNmaterialized draft IS selected + materialized. Proves the
        watcher would have re-run the migrated draft if it lacked the timestamp."""
        cfg, db = live_db
        # Approve a pending draft via the real review writer path: create a
        # pending draft then advance it to approved WITHOUT a materialized_at.
        from event_pipeline.couchdb_job_draft_writer import (
            set_job_draft_review_status,
            upsert_job_draft,
        )

        upsert_job_draft(
            cfg,
            db,
            {
                "draft_id": "live-approved-1",
                "job_type": "set_entity_status",
                "payload": {
                    "entity_type": "employee",
                    "entity_id": "Alice Worker",
                    "status": "inactive",
                    "reason": "left the company",
                    "source": "voice_memo",
                },
            },
        )
        set_job_draft_review_status(
            cfg,
            db,
            "live-approved-1",
            review_status="approved",
            reviewed_by="greg",
            rationale="ok",
        )
        unmaterialized = job_draft_queue_watcher.find_approved_unmaterialized_drafts(
            cfg, db
        )
        ids = {d.get("draft_id") for d in unmaterialized}
        assert "live-approved-1" in ids, (
            "a truly unmaterialized approved draft MUST be selected; otherwise "
            "the exclusion test proves nothing"
        )
        results = job_draft_queue_watcher.process_pass(
            config=cfg, db=db, runtime_root=tmp_path,
            logger=logging.getLogger("test_338_baseline"), dry_run=False,
        )
        # The watcher enqueues via the unified CouchDB transport with a
        # deterministic per-draft doc id (queue_doc_id_for_draft_job embeds the
        # draft_id), so inspect the captured enqueues instead of queue files.
        staged_ids = [str(e["job"].get("job_id", "")) for e in enqueue_capture]
        assert any("live-approved-1" in job_id for job_id in staged_ids), staged_ids


# --------------------------------------------------------------------------- #
# Idempotency + dry-run.
# --------------------------------------------------------------------------- #
@pytest.mark.real_couchdb
class TestIdempotencyAndDryRun:
    def test_second_apply_creates_no_new_drafts(self, live_db):
        """Run --apply twice -> second run creates 0 drafts, counts them as
        skipped_already_migrated, draft _id/_rev unchanged by run 2."""
        cfg, db = live_db
        _seed(cfg, db, _single_job_candidate("idem1", "pending_review"))

        r1 = migrate_candidates_to_drafts(cfg, db, dry_run=False)
        assert r1["drafts_created"] == 1
        draft1 = get_job_draft(cfg, db, "cap-idem1-log_supply_need-0")
        rev1 = draft1["_rev"]

        r2 = migrate_candidates_to_drafts(cfg, db, dry_run=False)
        assert r2["drafts_created"] == 0, "re-run must NOT create duplicate drafts"
        assert r2["skipped_already_migrated"] >= 1
        draft2 = get_job_draft(cfg, db, "cap-idem1-log_supply_need-0")
        assert draft2["_rev"] == rev1, "re-run must NOT clobber the draft (rev bump)"

    def test_idempotency_no_duplicate_docs(self, live_db):
        """After two applies exactly one job_draft doc exists for the draft."""
        cfg, db = live_db
        _seed(cfg, db, _single_job_candidate("idem2", "pending_review"))
        migrate_candidates_to_drafts(cfg, db, dry_run=False)
        migrate_candidates_to_drafts(cfg, db, dry_run=False)
        target = job_draft_doc_id("cap-idem2-log_supply_need-0")
        _, alldocs = _http(
            cfg,
            "GET",
            f"{db}/_all_docs?startkey=%22job_draft_%22&endkey=%22job_draft_%5Cufff0%22",
        )
        matching = [r for r in alldocs["rows"] if r["id"] == target]
        assert len(matching) == 1

    def test_dry_run_writes_nothing(self, live_db):
        """dry_run=True -> zero drafts written, zero candidates marked, and the
        counters match what a subsequent --apply actually does."""
        cfg, db = live_db
        _seed(cfg, db, _single_job_candidate("dr1", "pending_review"))
        _seed(cfg, db, _multi_job_candidate("dr2", "pending_review"))
        _seed(cfg, db, _single_job_candidate("dr3", "approved", reviewed_by="g"))
        _seed(cfg, db, _single_job_candidate("dr4", "rejected", reviewed_by="g"))
        _seed(cfg, db, _single_job_candidate("dr5", "failed"))

        dry = migrate_candidates_to_drafts(cfg, db, dry_run=True)
        # No drafts written.
        for did in (
            "cap-dr1-log_supply_need-0",
            "cap-dr2-set_entity_status-0",
            "cap-dr3-log_supply_need-0",
        ):
            assert get_job_draft(cfg, db, did) is None, did
        # No candidate marked migrated.
        for cid in ("dr1", "dr2", "dr3", "dr4", "dr5"):
            after = get_action_candidate(cfg, db, cid)
            assert not after.get("migrated_to_draft_at"), cid

        # Counters predict the real apply exactly.
        applied = migrate_candidates_to_drafts(cfg, db, dry_run=False)
        assert _empty_report_counts(dry) == _empty_report_counts(applied)


# --------------------------------------------------------------------------- #
# CLI smoke.
# --------------------------------------------------------------------------- #
class TestCLI:
    def test_help(self):
        proc = subprocess.run(
            [sys.executable, "-m", "event_pipeline.couchdb.migrate_candidates_to_drafts", "--help"],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Migrate" in proc.stdout
        assert "--apply" in proc.stdout
        assert "--dry-run" in proc.stdout

    @pytest.mark.real_couchdb
    def test_dry_run_json_smoke(self, live_db):
        cfg, db = live_db
        _seed(cfg, db, _single_job_candidate("cli1", "pending_review"))
        env = dict(os.environ)
        env["BTQ_COUCHDB_URL"] = cfg.base_url
        # carry creds through env so the CLI's from_env() reaches the same db
        creds = _config_file_creds()
        if creds:
            env.setdefault("BTQ_COUCHDB_USER", creds[1])
            env.setdefault("BTQ_COUCHDB_PASSWORD", creds[2])
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "event_pipeline.couchdb.migrate_candidates_to_drafts",
                "--database",
                db,
                "--dry-run",
                "--json",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        report = json.loads(proc.stdout)
        assert report["candidates_scanned"] >= 1
        assert report["drafts_created"] >= 1
        # dry-run wrote nothing
        assert get_job_draft(cfg, db, "cap-cli1-log_supply_need-0") is None
