"""Independent verification of the 542 assignment-editor contract.

Scope: the uncommitted diff to ``ops_dashboard/common.py`` (``write_assign_
employee_site_job``, ``queue_job_states``) and ``ops_dashboard/sections/
employee_detail.py`` (assignment editor prefill, diff-to-jobs staging on
save, stale-baseline guard, and the staged-outcome notice).

All fixtures use sandbox identities only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from event_pipeline import couchdb_config
from ops_dashboard import common
from ops_dashboard.sections import employee_detail as ed
import queue_spec


class DummyContext(SimpleNamespace):
    def __init__(self, tmp_path: Path, query: dict[str, list[str]] | None = None) -> None:
        super().__init__(runtime_root=tmp_path / "runtime", query=query or {})
        self.audit_entries: list[tuple[str, dict[str, object], str]] = []

    def redirect(self, location: str):
        return 303, "text/html; charset=utf-8", f'<a href="{location}">Return</a>'.encode(), {"Location": location}

    def audit(self, route: str, payload: dict[str, object], result: str) -> None:
        self.audit_entries.append((route, payload, result))


def _doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "employee_sandbox_user",
        "_rev": "1-abc",
        "type": "employee",
        "operator": "op_greg",
        "first": "Sandy",
        "last": "Sandbox",
        "name": "Sandy Sandbox",
        "job": "SANDBOX",
        "role": "Cleaner",
        "site_ids": ["SANDBOX", "S2"],
    }
    doc.update(overrides)
    return doc


def _install(monkeypatch: pytest.MonkeyPatch, doc: dict[str, object]) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    monkeypatch.setattr(ed, "_load_location_name", lambda s: f"Site {s}")
    monkeypatch.setattr(ed, "_field_captures", lambda _p: [])
    monkeypatch.setattr(ed, "_personnel_events", lambda _d: [])
    monkeypatch.setattr(ed, "_availability_section", lambda _eid, _doc=None: "<section></section>")


def _quick_facts_region(body: str) -> str:
    start = body.index('class="quick-facts"')
    return body[start:body.index("Recent activity", start)]


def _capture_put(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_request_json(method: str, path: str, payload: dict[str, object]):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(ed.sites, "request_json", fake_request_json)
    return captured


def _record_enqueue(monkeypatch: pytest.MonkeyPatch, fail_on: set[int] | None = None):
    """Patch ops_dashboard.common.enqueue_queue_job with a recorder.

    write_assign_employee_site_job resolves ``enqueue_queue_job`` as a bare
    module-global at call time, so patching the attribute on ``common``
    intercepts every job it stages -- this is the real integration path,
    not a mock of write_assign_employee_site_job itself.
    """
    calls: list[dict[str, object]] = []
    fail_on = fail_on or set()

    def fake_enqueue(job: dict[str, object], *, created_by: str = "ops_dashboard"):
        calls.append(job)
        if len(calls) in fail_on:
            raise RuntimeError("queue unavailable")
        return str(job["job_id"])

    monkeypatch.setattr(common, "enqueue_queue_job", fake_enqueue)
    return calls


# ---------------------------------------------------------------------------
# 1. Editor prefill
# ---------------------------------------------------------------------------

def test_editor_prefill_canonical_list_and_no_legacy_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc(site_ids=["SANDBOX", "S2"])
    _install(monkeypatch, doc)
    body = ed.render(SimpleNamespace(query={"edit": ["assignment"]}), "sandbox_user")
    facts = _quick_facts_region(body)

    assert '<input type="text" name="assigned_sites" value="SANDBOX, S2">' in facts
    assert '<input type="hidden" name="_assigned_baseline" value="SANDBOX,S2">' in facts
    assert 'name="job"' in facts
    assert 'name="role"' in facts
    assert 'name="additional_jobs"' not in facts
    assert 'name="sites"' not in facts


def test_editor_prefill_legacy_only_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc(site_ids=[], job=1337)
    _install(monkeypatch, doc)
    body = ed.render(SimpleNamespace(query={"edit": ["assignment"]}), "sandbox_user")
    facts = _quick_facts_region(body)

    assert '<input type="text" name="assigned_sites" value="1337">' in facts
    assert '<input type="hidden" name="_assigned_baseline" value="1337">' in facts


# ---------------------------------------------------------------------------
# 2. Diff -> jobs
# ---------------------------------------------------------------------------

def test_diff_to_jobs_creates_assign_and_unassign(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _doc(job="SANDBOX", role="Old Role", site_ids=["SANDBOX", "S2"])
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    put = _capture_put(monkeypatch)
    calls = _record_enqueue(monkeypatch)
    ctx = DummyContext(tmp_path)

    status, _ct, body, headers = ed.handle_save_section(
        ctx,
        "sandbox_user",
        b"_section=assignment&job=699&role=Cleaner&assigned_sites=SANDBOX,%20S3"
        b"&_assigned_baseline=SANDBOX,S2&_entity_id=sandbox_user&_rev=1-abc",
    )

    assert len(calls) == 2
    actions_sites = [(job["payload"]["action"], job["payload"]["site_id"]) for job in calls]
    assert ("unassign", "S2") in actions_sites
    assert ("assign", "S3") in actions_sites
    for job in calls:
        assert job["job_type"] == "assign_employee_site"
        assert job["payload"]["employee_id"] == "employee_sandbox_user"
        assert job["payload"]["actor"] == common.default_actor()
        assert set(job["payload"]) == {"employee_id", "site_id", "actor", "action", "source"}
        assert queue_spec.validate_job(job)

    assert any(
        payload.get("section") == "assignment" and set(payload.get("job_ids", []))
        for _route, payload, result in ctx.audit_entries
        if result.startswith("success: staged")
    )
    job_ids = [job["job_id"] for job in calls]
    assert headers["Location"] == f"/employees/sandbox_user?staged=assignment&jobs={','.join(job_ids)}"

    # Direct PUT happened (job/role changed) but never touched site_ids.
    assert put["payload"]["site_ids"] == ["SANDBOX", "S2"]


# ---------------------------------------------------------------------------
# 3. Stale baseline
# ---------------------------------------------------------------------------

def test_stale_baseline_rerenders_notice_with_current_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _doc(site_ids=["SANDBOX", "S2", "S4"])
    _install(monkeypatch, doc)
    put_called = []
    monkeypatch.setattr(ed.sites, "request_json", lambda *a, **k: put_called.append(1))
    calls = _record_enqueue(monkeypatch)
    ctx = DummyContext(tmp_path)

    status, _ct, body, _headers = ed.handle_save_section(
        ctx,
        "sandbox_user",
        b"_section=assignment&job=699&role=Cleaner&assigned_sites=SANDBOX,%20S3"
        b"&_assigned_baseline=SANDBOX,S2&_entity_id=sandbox_user&_rev=1-abc",
    )

    assert status == 200
    assert not put_called
    assert not calls
    assert b"changed underneath" in body
    facts = _quick_facts_region(body.decode("utf-8"))
    assert '<input type="text" name="assigned_sites" value="SANDBOX, S2, S4">' in facts
    assert any(
        result == "failed: assignments changed underneath operator"
        for _route, _payload, result in ctx.audit_entries
    )


def test_missing_baseline_with_assigned_sites_treated_as_stale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _doc(site_ids=["SANDBOX", "S2"])
    _install(monkeypatch, doc)
    calls = _record_enqueue(monkeypatch)
    ctx = DummyContext(tmp_path)

    status, _ct, body, _headers = ed.handle_save_section(
        ctx,
        "sandbox_user",
        b"_section=assignment&job=699&role=Cleaner&assigned_sites=SANDBOX,%20S3&_entity_id=sandbox_user&_rev=1-abc",
    )

    assert status == 200
    assert not calls
    assert b"changed underneath" in body


# ---------------------------------------------------------------------------
# 4. Direct-only changes
# ---------------------------------------------------------------------------

def test_direct_only_change_writes_put_no_jobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _doc(job="SANDBOX", role="Cleaner", site_ids=["SANDBOX", "S2"])
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    put = _capture_put(monkeypatch)
    calls = _record_enqueue(monkeypatch)
    ctx = DummyContext(tmp_path)

    _status, _ct, _body, headers = ed.handle_save_section(
        ctx,
        "sandbox_user",
        b"_section=assignment&job=700&role=Site%20Lead&assigned_sites=SANDBOX,%20S2"
        b"&_assigned_baseline=SANDBOX,S2&_entity_id=sandbox_user&_rev=1-abc",
    )

    assert not calls
    assert put["payload"]["job"] == "700"
    assert put["payload"]["role"] == "Site Lead"
    assert put["payload"]["site_ids"] == ["SANDBOX", "S2"]
    assert headers["Location"] == "/employees/sandbox_user"


def test_extra_legacy_fields_not_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _doc(job="SANDBOX", role="Cleaner", site_ids=["SANDBOX", "S2"])
    assert "additional_jobs" not in doc and "sites" not in doc
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    put = _capture_put(monkeypatch)
    _record_enqueue(monkeypatch)
    ctx = DummyContext(tmp_path)

    ed.handle_save_section(
        ctx,
        "sandbox_user",
        b"_section=assignment&job=700&role=Site%20Lead&assigned_sites=SANDBOX,%20S2"
        b"&_assigned_baseline=SANDBOX,S2&additional_jobs=9999&sites=8888"
        b"&_entity_id=sandbox_user&_rev=1-abc",
    )

    payload = put["payload"]
    assert "additional_jobs" not in payload
    assert "sites" not in payload


def test_no_change_at_all_no_put_no_jobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _doc(job="SANDBOX", role="Cleaner", site_ids=["SANDBOX", "S2"])
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    put_called = []
    monkeypatch.setattr(ed.sites, "request_json", lambda *a, **k: put_called.append(1))
    calls = _record_enqueue(monkeypatch)
    ctx = DummyContext(tmp_path)

    _status, _ct, _body, headers = ed.handle_save_section(
        ctx,
        "sandbox_user",
        b"_section=assignment&job=SANDBOX&role=Cleaner&assigned_sites=SANDBOX,%20S2"
        b"&_assigned_baseline=SANDBOX,S2&_entity_id=sandbox_user&_rev=1-abc",
    )

    assert not put_called
    assert not calls
    assert headers["Location"] == "/employees/sandbox_user"


# ---------------------------------------------------------------------------
# 5. Legacy form posts (no membership fields at all)
# ---------------------------------------------------------------------------

def test_legacy_form_post_no_membership_fields_no_jobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _doc(job="SANDBOX", role="Cleaner", site_ids=["SANDBOX", "S2"])
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    put = _capture_put(monkeypatch)
    calls = _record_enqueue(monkeypatch)
    ctx = DummyContext(tmp_path)

    _status, _ct, _body, headers = ed.handle_save_section(
        ctx,
        "sandbox_user",
        b"_section=assignment&job=700&role=Cleaner&_entity_id=sandbox_user&_rev=1-abc",
    )

    assert not calls
    assert put["payload"]["job"] == "700"
    assert put["payload"]["site_ids"] == ["SANDBOX", "S2"]
    assert headers["Location"] == "/employees/sandbox_user"


# ---------------------------------------------------------------------------
# 6. Empty submitted list
# ---------------------------------------------------------------------------

def test_empty_submitted_list_two_unassign_jobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _doc(job="SANDBOX", role="Cleaner", site_ids=["SANDBOX", "S2"])
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    _capture_put(monkeypatch)
    calls = _record_enqueue(monkeypatch)
    ctx = DummyContext(tmp_path)

    _status, _ct, _body, headers = ed.handle_save_section(
        ctx,
        "sandbox_user",
        b"_section=assignment&job=SANDBOX&role=Cleaner&assigned_sites="
        b"&_assigned_baseline=SANDBOX,S2&_entity_id=sandbox_user&_rev=1-abc",
    )

    assert len(calls) == 2
    assert all(job["payload"]["action"] == "unassign" for job in calls)
    ids = [job["job_id"] for job in calls]
    assert all(job_id in headers["Location"] for job_id in ids)


# ---------------------------------------------------------------------------
# 7. Partial enqueue failure
# ---------------------------------------------------------------------------

def test_partial_enqueue_failure_second_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _doc(job="SANDBOX", role="Cleaner", site_ids=["SANDBOX", "S2"])
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    _capture_put(monkeypatch)
    calls = _record_enqueue(monkeypatch, fail_on={2})
    ctx = DummyContext(tmp_path)

    _status, _ct, _body, headers = ed.handle_save_section(
        ctx,
        "sandbox_user",
        b"_section=assignment&job=699&role=Cleaner&assigned_sites=SANDBOX,%20S3"
        b"&_assigned_baseline=SANDBOX,S2&_entity_id=sandbox_user&_rev=1-abc",
    )

    assert len(calls) == 2  # first succeeded, second attempted then raised
    first_job_id = calls[0]["job_id"]
    location = headers["Location"]
    assert first_job_id in location
    assert "error=assignment_queue_unavailable" in location
    assert location.count(",") == 0  # only one id present

    assert any(
        "failed_site_id" in payload and payload.get("failed_action") == "assign"
        for _route, payload, _result in ctx.audit_entries
    )

    # Rendered page with the error surfaces a visible warning.
    _install(monkeypatch, doc)
    body = ed.render(SimpleNamespace(query={"error": ["assignment_queue_unavailable"]}), "sandbox_user")
    assert "could not be staged" in body


def test_enqueue_failure_on_first_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _doc(job="SANDBOX", role="Cleaner", site_ids=["SANDBOX", "S2"])
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    _capture_put(monkeypatch)
    calls = _record_enqueue(monkeypatch, fail_on={1})
    ctx = DummyContext(tmp_path)

    _status, _ct, _body, headers = ed.handle_save_section(
        ctx,
        "sandbox_user",
        b"_section=assignment&job=699&role=Cleaner&assigned_sites=SANDBOX,%20S3"
        b"&_assigned_baseline=SANDBOX,S2&_entity_id=sandbox_user&_rev=1-abc",
    )

    assert headers["Location"] == "/employees/sandbox_user?edit=assignment&error=assignment_queue_unavailable"


# ---------------------------------------------------------------------------
# 8. Outcome notice
# ---------------------------------------------------------------------------

def test_outcome_notice_partial_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc()
    _install(monkeypatch, doc)

    def fake_states(job_ids):
        return {
            "a": {"state": "complete", "site_id": "S3", "action": "assign"},
            "b": {"state": "failed", "site_id": "S2", "action": "unassign"},
        }

    monkeypatch.setattr(common, "queue_job_states", fake_states)
    body = ed.render(SimpleNamespace(query={"staged": ["assignment"], "jobs": ["a,b"]}), "sandbox_user")

    assert "<strong>1 of 2 applied &mdash; reload to refresh</strong>" in body
    assert "All 2 changes applied" not in body
    assert "S3" in body and "assign" in body
    assert "S2" in body and "unassign" in body


def test_outcome_notice_all_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc()
    _install(monkeypatch, doc)

    def fake_states(job_ids):
        return {
            "a": {"state": "complete", "site_id": "S3", "action": "assign"},
            "b": {"state": "complete", "site_id": "S2", "action": "unassign"},
        }

    monkeypatch.setattr(common, "queue_job_states", fake_states)
    body = ed.render(SimpleNamespace(query={"staged": ["assignment"], "jobs": ["a,b"]}), "sandbox_user")

    assert "All 2 changes applied" in body


def test_outcome_notice_missing_id_renders_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc()
    _install(monkeypatch, doc)

    monkeypatch.setattr(common, "queue_job_states", lambda job_ids: {"a": {"state": "complete", "site_id": "S3", "action": "assign"}})
    body = ed.render(SimpleNamespace(query={"staged": ["assignment"], "jobs": ["a,b"]}), "sandbox_user")

    assert "unknown" in body


# ---------------------------------------------------------------------------
# 9. queue_job_states
# ---------------------------------------------------------------------------

def test_queue_job_states_maps_docs_and_defaults_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_find(database, selector):
        captured["database"] = database
        captured["selector"] = selector
        return [{"_id": "a", "btq_state": "complete", "payload": {"site_id": "S3", "action": "assign"}}]

    monkeypatch.setattr(common, "_selector_couch_find", fake_find)
    monkeypatch.setattr(couchdb_config, "queue_database", lambda: "sentinel_queue_db")

    result = common.queue_job_states(["a", "b"])

    assert result["a"] == {"state": "complete", "site_id": "S3", "action": "assign"}
    assert result["b"] == {"state": "unknown", "site_id": "", "action": ""}
    assert captured["selector"] == {"_id": {"$in": ["a", "b"]}}
    assert captured["database"] == "sentinel_queue_db"


def test_queue_job_states_exception_marks_all_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def raiser(database, selector):
        raise RuntimeError("couchdb down")

    monkeypatch.setattr(common, "_selector_couch_find", raiser)
    result = common.queue_job_states(["a", "b"])

    assert result == {
        "a": {"state": "unknown", "site_id": "", "action": ""},
        "b": {"state": "unknown", "site_id": "", "action": ""},
    }


def test_queue_job_states_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called(*a, **k):
        raise AssertionError("_selector_couch_find must not be called for empty input")

    monkeypatch.setattr(common, "_selector_couch_find", fail_if_called)
    assert common.queue_job_states([]) == {}


def test_queue_job_states_dedupes_whitespace_and_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_find(database, selector):
        captured["selector"] = selector
        return []

    monkeypatch.setattr(common, "_selector_couch_find", fake_find)
    result = common.queue_job_states([" a ", "a", "b", "", "  "])

    assert captured["selector"] == {"_id": {"$in": ["a", "b"]}}
    assert set(result) == {"a", "b"}


# ---------------------------------------------------------------------------
# 10. write_assign_employee_site_job
# ---------------------------------------------------------------------------

def test_write_assign_employee_site_job_builds_validating_job(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_enqueue(monkeypatch)

    job_id = common.write_assign_employee_site_job(
        employee_id="employee_sandbox_user",
        site_id="S3",
        actor="Greg",
        action="assign",
    )

    assert len(calls) == 1
    job = calls[0]
    assert job["job_id"].startswith("assign-employee-site-")
    assert job["job_id"] == job_id
    assert job["job_type"] == "assign_employee_site"
    assert set(job["payload"]) == {"employee_id", "site_id", "actor", "action", "source"}
    assert queue_spec.validate_job(job)


def test_write_assign_employee_site_job_invalid_action_raises_before_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_enqueue(monkeypatch)

    with pytest.raises(ValueError):
        common.write_assign_employee_site_job(
            employee_id="employee_sandbox_user",
            site_id="S3",
            actor="Greg",
            action="drop",
        )

    assert not calls
