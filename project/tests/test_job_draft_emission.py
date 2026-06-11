"""Behavioral verification for prompt 334a: job_draft_emission.

Authored by the INDEPENDENT VERIFIER (not the executor). These tests EXECUTE
the real pipeline: realistic typed notes are run through the actual text
semantic engine (``run_text_semantic_pipeline`` + ``RuleCaptureEngine``) to
produce genuine semantic artifacts, then fed to ``job_drafts_from_semantic`` /
``collect_job_drafts``. No hand-built candidate dicts -- the inputs are whatever
the production rule engine actually emits, so a build-to-the-test impl can not
slip past.

The ``@pytest.mark.real_couchdb`` test pushes the real 333 design doc into a
throwaway database (reusing the helper pattern from
``test_couchdb_job_draft_gate.py``) and confirms ``collect_job_drafts`` lands
real ``job_draft`` docs and is idempotent on a second run.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib import error, request

import pytest

import field_capture.action_candidates as fc
from event_pipeline import couchdb_config
from event_pipeline.couchdb_job_draft_writer import job_draft_doc_id
from field_capture.job_draft_emission import (
    collect_job_drafts,
    job_drafts_from_semantic,
)
from field_capture.text_semantics import run_text_semantic_pipeline
from processing_core.artifacts import write_json_object
from processing_core.capture_semantics import RuleCaptureEngine


# --------------------------------------------------------------------------- #
# Fixture helpers: real semantic artifacts straight from the rule engine.
# (Same construction as test_field_capture_generic_candidate_suppression.py.)
# --------------------------------------------------------------------------- #
def _artifact(text: str, capture_id: str, *, area: str = "", site_id: str = "SANDBOX") -> dict:
    artifact = run_text_semantic_pipeline(
        text,
        site_id=site_id,
        upload_id=capture_id,
        area=area,
        engine=RuleCaptureEngine(),
    )
    # Round-trip through JSON exactly like the on-disk pipeline (tuples -> lists).
    return json.loads(json.dumps(artifact))


# Concrete notes whose real classification is pinned by probing the engine:
#   * ISSUE  -> exactly one log_site_issue draft
#   * EQUIP  -> exactly one log_equipment_request draft
#   * SUPPLY -> exactly one log_supply_need draft (340: clean item_name)
#   * STATUS -> zero candidates / zero drafts (the structural win)
#
# 340 RETARGET NOTE: the pre-340 ``MULTI_NOTE`` ("...order more towels and a new
# vacuum.") fanned out to TWO ``log_equipment_request`` drafts ONLY because the
# rule engine double-fired the supply ``equipment_review`` follow-up on the same
# supply note -- the supply+equipment OVERLAP. 340 collapses that duplicate
# (``handled_supply_equipment`` after the supply branch), so that note now
# yields exactly ONE clean draft. The dropped draft was an empty-``job_type``
# ``equipment_review`` of the SAME job_type as the survivor -- a genuine
# duplicate, NOT a distinct actionable job (verified: legit, not a regression).
# The rule engine emits at most one PROPOSABLE candidate per note, so the
# "one source fans out to N>=2 distinct drafts" guarantee is now exercised at
# the COLLECTION level across several genuinely-distinct single-draft sources
# (issue + equipment + supply), each with its own group_id.
ISSUE_NOTE = "The mop sink in the back is broken and leaking water everywhere."
EQUIP_NOTE = "The vacuum is broken and needs repair."
SUPPLY_NOTE = "We're low on paper towels and soap."
# Kept as a single-draft supply source (item_name now cleaned by 340).
MULTI_NOTE = (
    "The mop sink is broken and leaking. "
    "Also we need to order more towels and a new vacuum."
)
STATUS_NOTE = "Area done. Complete."


# --------------------------------------------------------------------------- #
# Valid single job: assert the actual field VALUES.
# --------------------------------------------------------------------------- #
def test_valid_single_job_emits_one_fully_populated_draft() -> None:
    art = _artifact(ISSUE_NOTE, "cap-issue-single", site_id="SANDBOX")
    drafts = job_drafts_from_semantic(Path("issue.json"), art)

    assert len(drafts) == 1, drafts
    draft = drafts[0]

    assert draft["job_type"] == "log_site_issue"
    assert isinstance(draft["payload"], dict) and draft["payload"]
    # payload really is the proposed-queue-job payload for this site.
    assert draft["payload"]["site_id"] == "SANDBOX"
    assert "cap-issue-single" in draft["payload"]["related_capture_ids"]

    assert draft["review_status"] == "pending_approval"
    assert draft["validation_error"] is None

    # Embedded context populated from the candidate / channel_metadata.
    assert draft["site_id"] == "SANDBOX"
    assert draft["source_capture_id"] == "cap-issue-single"
    assert draft["source_kind"] == "ops_dashboard_text"
    assert draft["group_id"] == "cap-issue-single"
    assert draft["message"]  # non-empty operational message
    assert draft["source"] == "field_capture_pipeline"

    # draft_id is the stable, deterministic f"{group_id}-{job_type}-{ordinal}".
    # First (and only) draft of this source => ordinal 0.
    assert draft["draft_id"] == "cap-issue-single-log_site_issue-0"


# --------------------------------------------------------------------------- #
# Multi-source fan-out: N>=2 drafts across genuinely-distinct sources, EACH with
# its own group_id, all draft_ids distinct, stable f"{group_id}-{job_type}-{i}".
#
# 340 RETARGET (legit, NOT a regression): the legacy single-note ``MULTI_NOTE``
# produced 2 drafts only via the supply+equipment overlap that 340 correctly
# collapses (same ``log_equipment_request`` job_type twice on one supply note --
# a duplicate, not a distinct job). The rule engine emits at most one PROPOSABLE
# candidate per note, so the genuine fan-out guarantee -- multiple distinct
# actionable jobs, shared-vs-distinct group_ids, distinct stable draft_ids -- is
# exercised across distinct sources (issue + equipment + supply) instead.
# --------------------------------------------------------------------------- #
def test_multi_job_source_shares_group_id_distinct_draft_ids() -> None:
    drafts: list[dict] = []
    drafts += job_drafts_from_semantic(Path("issue.json"), _artifact(ISSUE_NOTE, "cap-multi-issue"))
    drafts += job_drafts_from_semantic(Path("equip.json"), _artifact(EQUIP_NOTE, "cap-multi-equip"))
    drafts += job_drafts_from_semantic(Path("supply.json"), _artifact(SUPPLY_NOTE, "cap-multi-supply"))

    assert len(drafts) >= 2, f"expected >=2 drafts, got {len(drafts)}"

    # Genuinely-distinct jobs: at minimum a site issue, an equipment request, and
    # a supply need -- three different actionable job_types.
    job_types = {d["job_type"] for d in drafts}
    assert {"log_site_issue", "log_equipment_request", "log_supply_need"} <= job_types, job_types

    # Each source contributes its own group_id (== its capture id).
    group_ids = {d["group_id"] for d in drafts}
    assert group_ids == {"cap-multi-issue", "cap-multi-equip", "cap-multi-supply"}, group_ids

    draft_ids = [d["draft_id"] for d in drafts]
    assert len(set(draft_ids)) == len(draft_ids), draft_ids  # all distinct

    # Stable, deterministic format f"{group_id}-{job_type}-{ordinal}". Each
    # source emits a single draft of its own job_type, so ordinal is 0.
    for d in drafts:
        assert d["draft_id"] == f"{d['group_id']}-{d['job_type']}-0", d["draft_id"]
        assert d["job_type"]
        assert isinstance(d["payload"], dict) and d["payload"]

    # The single supply draft is a real log_supply_need queue job for the site.
    supply_draft = next(d for d in drafts if d["job_type"] == "log_supply_need")
    assert supply_draft["payload"].get("site_id") == "SANDBOX"


# --------------------------------------------------------------------------- #
# No-job -> nothing. THE structural win: genuinely zero drafts.
# --------------------------------------------------------------------------- #
def test_status_note_produces_no_drafts() -> None:
    art = _artifact(STATUS_NOTE, "cap-status-1")
    # Precondition: the engine itself yields no candidates for a pure status note.
    assert fc.payloads_from_semantic(Path("status.json"), art) == []
    # Therefore the emission layer yields zero drafts.
    assert job_drafts_from_semantic(Path("status.json"), art) == []


def test_candidate_with_only_empty_job_type_tuples_produces_no_drafts(monkeypatch) -> None:
    """A source whose candidates DO exist but whose proposed_queue_jobs only
    return empty-job_type tuples must yield zero drafts (the empty-job_type
    ``continue`` branch). Drive it by forcing ``proposed_queue_jobs`` to return a
    realistic 'failed-derivation' tuple shape: ('', {}, '<reason>')."""
    art = _artifact(ISSUE_NOTE, "cap-emptyjob-1")
    # sanity: this source normally yields a real candidate.
    assert fc.payloads_from_semantic(Path("x.json"), art)

    import field_capture.job_draft_emission as jde

    monkeypatch.setattr(
        jde,
        "proposed_queue_jobs",
        lambda candidate, runtime_root=None: [("", {}, "could not resolve payload")],
    )
    assert jde.job_drafts_from_semantic(Path("x.json"), art) == []


# --------------------------------------------------------------------------- #
# validation_error wiring (driven via a forced tuple, since the real derivation
# never returns job_type+error together -- see the contract note in the report).
# --------------------------------------------------------------------------- #
def test_validation_error_propagates_when_tuple_carries_one(monkeypatch) -> None:
    art = _artifact(ISSUE_NOTE, "cap-valerr-1")
    import field_capture.job_draft_emission as jde

    monkeypatch.setattr(
        jde,
        "proposed_queue_jobs",
        lambda candidate, runtime_root=None: [
            ("log_site_issue", {"site_id": "SANDBOX"}, "queue_spec: missing field")
        ],
    )
    drafts = jde.job_drafts_from_semantic(Path("x.json"), art)
    assert len(drafts) >= 1
    assert all(d["validation_error"] == "queue_spec: missing field" for d in drafts)
    assert all(d["job_type"] == "log_site_issue" for d in drafts)


# --------------------------------------------------------------------------- #
# Purity: no network (proven by surviving the hermetic conftest guard), and the
# per-call SHAPE is stable -- same group, same job_types, same count.
# --------------------------------------------------------------------------- #
def test_pure_stable_group_and_shape() -> None:
    art = _artifact(MULTI_NOTE, "cap-stable-1")
    first = job_drafts_from_semantic(Path("p.json"), art)
    second = job_drafts_from_semantic(Path("p.json"), art)
    # group_id and job_type set ARE stable (they are not time-derived).
    assert {d["group_id"] for d in first} == {d["group_id"] for d in second} == {"cap-stable-1"}
    assert [d["job_type"] for d in first] == [d["job_type"] for d in second]
    assert len(first) == len(second)
    # (Purity: this test runs under the hermetic conftest guard; any :5984 call
    # would have raised. Reaching here proves no network happened.)


def test_draft_ids_idempotent_across_calls() -> None:
    """FIXED (334a): draft_id is now the deterministic
    f"{group_id}-{job_type}-{ordinal}" -- NO payload hashing, so the wall-clock
    ``observed_at`` in the payload no longer leaks into the id. Calling
    ``job_drafts_from_semantic`` twice on the same input yields BYTE-IDENTICAL
    draft_ids, which is what makes the writer's draft_id-keyed upsert idempotent.
    This was the pinned defect; it now genuinely executes the fixed path."""
    art = _artifact(MULTI_NOTE, "cap-stable-2")
    first = job_drafts_from_semantic(Path("p.json"), art)
    second = job_drafts_from_semantic(Path("p.json"), art)

    first_ids = [d["draft_id"] for d in first]
    second_ids = [d["draft_id"] for d in second]

    # Positive idempotency assertion: identical draft_ids across re-emits.
    assert first_ids == second_ids
    # And they really are the new stable, non-empty format (not a stale hash).
    assert first_ids and all("-" in did for did in first_ids)
    assert first_ids == [f"cap-stable-2-{d['job_type']}-{i}" for i, d in enumerate(first)]


# --------------------------------------------------------------------------- #
# Skip gate: wrong status / wrong type -> [].
# --------------------------------------------------------------------------- #
def test_skip_non_complete_status() -> None:
    art = dict(_artifact(ISSUE_NOTE, "cap-skip-status"))
    art["status"] = "pending"
    assert job_drafts_from_semantic(Path("x.json"), art) == []


def test_skip_non_accepted_type() -> None:
    art = dict(_artifact(ISSUE_NOTE, "cap-skip-type"))
    art["type"] = "some_unrelated_artifact"
    assert job_drafts_from_semantic(Path("x.json"), art) == []


# --------------------------------------------------------------------------- #
# collect_job_drafts: hermetic counts (CouchDB unconfigured -> no writes).
# --------------------------------------------------------------------------- #
def _write_artifact_tree(root: Path) -> Path:
    """Write four real artifacts into a semantic dir.

    340 RETARGET: the tree now uses THREE genuinely-distinct single-draft
    sources (issue -> log_site_issue, equipment -> log_equipment_request,
    supply -> log_supply_need) plus a status note (zero drafts). This yields
    >=3 discovered drafts across distinct group_ids -- the same structural
    coverage the old issue+multi(overlap) tree gave, without relying on the
    supply/equipment overlap duplicate that 340 collapsed.
    """
    semantic_dir = root / "field_capture" / "semantics"
    semantic_dir.mkdir(parents=True, exist_ok=True)
    write_json_object(semantic_dir / "issue.json", _artifact(ISSUE_NOTE, "cap-tree-issue"))
    write_json_object(semantic_dir / "equip.json", _artifact(EQUIP_NOTE, "cap-tree-equip"))
    write_json_object(semantic_dir / "supply.json", _artifact(SUPPLY_NOTE, "cap-tree-supply"))
    write_json_object(semantic_dir / "status.json", _artifact(STATUS_NOTE, "cap-tree-status"))
    return semantic_dir


def test_collect_counts_without_couchdb_does_not_write(tmp_path, monkeypatch) -> None:
    # Hermetic: ensure CouchDB is treated as unconfigured.
    monkeypatch.setenv("BTQ_COUCHDB_ACTION_CANDIDATE_WRITE", "0")
    assert fc.couchdb_candidate_config_or_none() is None

    semantic_dir = _write_artifact_tree(tmp_path)
    counts = collect_job_drafts([semantic_dir])

    # status.json yields no drafts -> skipped; issue + equip + supply each yield
    # one genuinely-distinct draft -> >=3 discovered.
    assert counts["skipped"] == 1
    assert counts["discovered"] >= 3
    # CouchDB unconfigured -> nothing emitted, but discovered still counts.
    assert counts["emitted"] == 0


def test_collect_accepts_single_path_argument(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_ACTION_CANDIDATE_WRITE", "0")
    semantic_dir = _write_artifact_tree(tmp_path)
    # Pass a bare Path (not a sequence) -- the impl special-cases this.
    counts = collect_job_drafts(semantic_dir)
    assert counts["discovered"] >= 3
    assert counts["emitted"] == 0


# --------------------------------------------------------------------------- #
# Real CouchDB round-trip: drafts land as job_draft docs; second run no dupes.
# (Throwaway-DB + design-push pattern reused from test_couchdb_job_draft_gate.py)
# --------------------------------------------------------------------------- #
DESIGN_DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "event_pipeline"
    / "couchdb"
    / "design_btq_field_captures.json"
)


def _design_doc() -> dict:
    return json.loads(DESIGN_DOC_PATH.read_text(encoding="utf-8"))


def _config_file_creds():
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


def _live_config():
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
    db = f"btq_verify_334a_{uuid.uuid4().hex[:12]}"
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


@pytest.mark.real_couchdb
class TestCollectRealCouchDB:
    def _point_emission_at(self, cfg, db, monkeypatch):
        """Make the emission module's CouchDB config + db target the throwaway."""
        import field_capture.job_draft_emission as jde

        monkeypatch.setattr(jde, "couchdb_candidate_config_or_none", lambda: cfg)
        monkeypatch.setattr(jde.couchdb_config, "field_captures_database", lambda: db)

    def _count_job_drafts(self, cfg, db):
        _, rows = _http(
            cfg,
            "GET",
            f"{db}/_all_docs?startkey=%22job_draft_%22&endkey=%22job_draft_%5Cufff0%22",
        )
        return sorted(r["id"] for r in rows["rows"])

    def test_drafts_land_as_real_job_draft_docs(self, live_db, tmp_path, monkeypatch) -> None:
        cfg, db = live_db
        self._point_emission_at(cfg, db, monkeypatch)

        semantic_dir = _write_artifact_tree(tmp_path)

        counts = collect_job_drafts([semantic_dir])
        assert counts["emitted"] == counts["discovered"] >= 3
        assert counts["skipped"] == 1  # status.json yields nothing

        draft_ids = self._count_job_drafts(cfg, db)
        assert len(draft_ids) == counts["emitted"]

        # Every emitted doc is a well-formed, server-validated job_draft.
        for doc_id in draft_ids:
            _, doc = _http(cfg, "GET", f"{db}/{doc_id}")
            assert doc["type"] == "job_draft"
            assert doc["review_status"] == "pending_approval"
            assert doc["job_type"]
            assert doc["validation_error"] is None
            assert doc["group_id"] in {"cap-tree-issue", "cap-tree-equip", "cap-tree-supply"}

    def test_second_run_does_not_duplicate(self, live_db, tmp_path, monkeypatch) -> None:
        """FIXED (334a): with stable draft_ids, a second collect_job_drafts run
        over the SAME artifact tree upserts in place (same _id) rather than
        creating fresh docs. Prove it against real CouchDB: the draft_id set is
        identical and the doc COUNT is unchanged after the second run."""
        cfg, db = live_db
        self._point_emission_at(cfg, db, monkeypatch)
        semantic_dir = _write_artifact_tree(tmp_path)

        collect_job_drafts([semantic_dir])
        ids_first = self._count_job_drafts(cfg, db)
        assert ids_first  # the first run actually landed docs

        collect_job_drafts([semantic_dir])
        ids_second = self._count_job_drafts(cfg, db)

        # Idempotent: SAME draft_ids and SAME count -- zero new docs on re-run.
        assert ids_second == ids_first
        assert len(ids_second) == len(ids_first)

    # ------------------------------------------------------------------- #
    # KEYSTONE (334b): no-clobber on re-walk.
    #
    # The pipeline watcher re-walks the artifact tree every cycle. The most
    # important new guarantee of 334b is that a re-walk must NOT overwrite a
    # draft a human has already reviewed. ``build_job_draft_document`` always
    # stamps review_status from the (pipeline-emitted) draft -- i.e.
    # ``pending_approval`` -- so WITHOUT the exists->skip guard a second
    # ``collect_job_drafts`` run would upsert the doc in place and RESET an
    # ``approved`` draft back to ``pending_approval`` (a silent review-loss bug).
    # The exists->skip guard (``couchdb_job_draft_exists``) is what prevents it.
    #
    # This proves the guarantee end-to-end against REAL CouchDB:
    #   1. collect -> a draft lands as pending_approval;
    #   2. out-of-band flip that draft's review_status to approved (raw HTTP PUT);
    #   3. collect again over the SAME tree (the re-walk);
    #   4. the draft is STILL approved, not duplicated, and the second run
    #      reports emitted==0 / existing>=1.
    # ------------------------------------------------------------------- #
    def test_rewalk_does_not_clobber_an_already_approved_draft(self, live_db, tmp_path, monkeypatch) -> None:
        cfg, db = live_db
        self._point_emission_at(cfg, db, monkeypatch)
        semantic_dir = _write_artifact_tree(tmp_path)

        # (1) First walk: drafts land as pending_approval.
        first_counts = collect_job_drafts([semantic_dir])
        assert first_counts["emitted"] >= 1
        assert first_counts["existing"] == 0
        doc_ids = self._count_job_drafts(cfg, db)
        assert doc_ids

        # Pick one concrete draft to "approve" out of band.
        target_id = doc_ids[0]
        _, before = _http(cfg, "GET", f"{db}/{target_id}")
        assert before["review_status"] == "pending_approval"

        # (2) Out-of-band review: a reviewer/Pro watcher flips it to approved.
        approved_doc = dict(before)
        approved_doc["review_status"] = "approved"
        approved_doc["reviewed_by"] = "verifier"
        status, put_resp = _http(cfg, "PUT", f"{db}/{target_id}", approved_doc)
        assert 200 <= status < 300, put_resp
        _, after_review = _http(cfg, "GET", f"{db}/{target_id}")
        assert after_review["review_status"] == "approved"  # the state we must protect
        rev_after_review = after_review["_rev"]

        # (3) Re-walk: the watcher re-runs collect_job_drafts over the SAME tree.
        second_counts = collect_job_drafts([semantic_dir])

        # (4a) No draft was re-emitted: every existing draft hit exists->skip.
        assert second_counts["emitted"] == 0
        assert second_counts["existing"] >= 1
        assert second_counts["existing"] == first_counts["emitted"]

        # (4b) No duplicate doc was created: the draft_id set is unchanged.
        assert self._count_job_drafts(cfg, db) == doc_ids

        # (4c) THE no-clobber assertion: the approved draft is STILL approved and
        # was not even re-written (its _rev is untouched by the re-walk).
        _, after_rewalk = _http(cfg, "GET", f"{db}/{target_id}")
        assert after_rewalk["review_status"] == "approved"
        assert after_rewalk["reviewed_by"] == "verifier"
        assert after_rewalk["_rev"] == rev_after_review  # not even touched
