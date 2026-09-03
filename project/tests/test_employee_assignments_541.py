"""INDEPENDENT VERIFIER gating tests (prompt 541) — the canonical-first
employee site-assignment rule (``btq_vault.employee_assignments``) and its
four call sites: dashboard save-time recompute, the legacy vault migration
helper, the operator context resolver, and the assign/unassign queue job.

Authored against the planner's acceptance contract, NOT by the implementer.
Sandbox identities only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from btq_vault.employee_assignments import bare_site_id, employee_assigned_site_ids
from event_pipeline import context_resolver
from event_pipeline.couchdb.migrate_vault import _employee_site_ids
from ops_dashboard.sections import employee_detail as ed
from ops_dashboard.sections import entity_edit
from ops_dashboard.sections import tokens as tokens_module
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import employee_sites
from queue_spec import JOB_ASSIGN_EMPLOYEE_SITE

# Reuse the exact node-driven map harness + battery from the roster-parity suite.
from test_employees_by_site_roster_consistency import (
    _BATTERY,
    _emitted_bare_sites,
    requires_node,
)

# Reuse the exact canonical-RMW harness the queue handler plugs into.
from test_queue_processor_couchdb_write import (
    RecordingRmwVaultStore,
    context_for,
    job,
    make_queue_file,
)


# --------------------------------------------------------------------------- #
# A. The shared rule — employee_assigned_site_ids / bare_site_id
# --------------------------------------------------------------------------- #
def test_rule_canonical_only():
    assert employee_assigned_site_ids({"site_ids": ["S1"]}) == ["S1"]


def test_rule_canonical_wins_legacy_ignored():
    doc = {"site_ids": ["S1"], "job": "S2", "additional_jobs": ["S3"], "sites": ["S4"]}
    assert employee_assigned_site_ids(doc) == ["S1"]


def test_rule_legacy_job_string():
    assert employee_assigned_site_ids({"site_ids": [], "job": "705"}) == ["705"]


def test_rule_legacy_job_int_stringified():
    assert employee_assigned_site_ids({"site_ids": [], "job": 1337}) == ["1337"]


def test_rule_legacy_additional_jobs_list_of_ints_and_strs():
    doc = {"site_ids": [], "job": "", "additional_jobs": [705, "706"]}
    assert employee_assigned_site_ids(doc) == ["705", "706"]


def test_rule_sites_used_only_when_job_and_additional_jobs_both_empty():
    doc = {"site_ids": [], "job": "", "additional_jobs": [], "sites": ["654"]}
    assert employee_assigned_site_ids(doc) == ["654"]


def test_rule_sites_ignored_when_job_present():
    doc = {"site_ids": [], "job": "789", "additional_jobs": [], "sites": ["654"]}
    assert employee_assigned_site_ids(doc) == ["789"]


def test_rule_sites_ignored_when_additional_jobs_present():
    doc = {"site_ids": [], "job": "", "additional_jobs": ["456"], "sites": ["654"]}
    assert employee_assigned_site_ids(doc) == ["456"]


def test_rule_prefixed_ids_stripped():
    assert employee_assigned_site_ids({"site_ids": ["location_789"]}) == ["789"]
    assert employee_assigned_site_ids({"site_ids": ["site_789"]}) == ["789"]


def test_rule_duplicates_across_job_and_additional_jobs_deduped():
    doc = {"site_ids": [], "job": "705", "additional_jobs": ["705", "706"]}
    assert employee_assigned_site_ids(doc) == ["705", "706"]


def test_rule_whitespace_and_quotes_stripped():
    assert employee_assigned_site_ids({"site_ids": ["  789  "]}) == ["789"]
    assert employee_assigned_site_ids({"site_ids": [], "job": '"789"'}) == ["789"]


def test_rule_none_and_missing_yield_empty():
    assert employee_assigned_site_ids({}) == []
    assert employee_assigned_site_ids({"site_ids": None, "job": None}) == []


def test_rule_blanks_dropped():
    assert employee_assigned_site_ids({"site_ids": ["", "  ", "789"]}) == ["789"]


def test_bare_site_id_direct():
    assert bare_site_id('"789"') == "789"
    assert bare_site_id("location_789") == "789"
    assert bare_site_id("site_789") == "789"
    assert bare_site_id("  789  ") == "789"
    assert bare_site_id(1337) == "1337"
    assert bare_site_id(None) == ""


# --------------------------------------------------------------------------- #
# B. Map parity — employees_by_site JS map vs. the shared Python rule.
# --------------------------------------------------------------------------- #
_INT_BATTERY = [
    {
        "_id": "emp_int_job",
        "type": "employee",
        "name": "Int Job",
        "person_id": "emp_int_job",
        "status": "active",
        "site_ids": [],
        "job": 1337,
    },
    {
        "_id": "emp_int_additional",
        "type": "employee",
        "name": "Int Additional",
        "person_id": "emp_int_additional",
        "status": "active",
        "site_ids": [],
        "job": "",
        "additional_jobs": [705],
    },
]


@requires_node
@pytest.mark.parametrize("doc", _BATTERY, ids=lambda d: d["_id"])
def test_map_parity_existing_battery(doc):
    canonical = employee_assigned_site_ids(doc)
    emitted = _emitted_bare_sites(doc)
    if canonical:
        assert emitted == canonical, (emitted, canonical)
    else:
        assert emitted == [None], emitted


@requires_node
@pytest.mark.parametrize("doc", _INT_BATTERY, ids=lambda d: d["_id"])
def test_map_parity_int_valued_legacy_fields(doc):
    canonical = employee_assigned_site_ids(doc)
    assert canonical, "battery docs are expected to resolve to a non-empty id"
    emitted = _emitted_bare_sites(doc)
    assert emitted == canonical, (emitted, canonical)


def test_node_present_marker():
    # Documents whether the node-gated tests above actually ran on this host.
    from test_employees_by_site_roster_consistency import _NODE

    assert _NODE is not None, "node not on PATH — map-parity tests were skipped, not proven"


# --------------------------------------------------------------------------- #
# C1. entity_edit.recompute_employee_derived — preservation
# --------------------------------------------------------------------------- #
def test_recompute_preserves_nonempty_site_ids_verbatim_ignoring_legacy():
    doc = {"first": "A", "last": "B", "site_ids": ["S1", "S2"], "job": "different"}
    entity_edit.recompute_employee_derived(doc)
    assert doc["site_ids"] == ["S1", "S2"]


def test_recompute_preserves_prefixed_form_and_order_exactly():
    # Contract: "leaves a non-empty site_ids EXACTLY as stored (including
    # order and any prefixed form)" — the rule's own bare-id normalization
    # must NOT be projected back onto the stored value.
    doc = {"first": "A", "last": "B", "site_ids": ["location_S2", "S1"], "job": "S1"}
    entity_edit.recompute_employee_derived(doc)
    assert doc["site_ids"] == ["location_S2", "S1"]


def test_recompute_sets_from_rule_when_site_ids_missing():
    doc = {"first": "A", "last": "B", "job": "789"}
    entity_edit.recompute_employee_derived(doc)
    assert doc["site_ids"] == ["789"]


def test_recompute_sets_from_rule_when_site_ids_empty_list():
    doc = {"first": "A", "last": "B", "site_ids": [], "job": "789"}
    entity_edit.recompute_employee_derived(doc)
    assert doc["site_ids"] == ["789"]


def test_recompute_sets_from_rule_when_site_ids_all_blank():
    doc = {"first": "A", "last": "B", "site_ids": ["", "  "], "job": "789"}
    entity_edit.recompute_employee_derived(doc)
    assert doc["site_ids"] == ["789"]


def test_recompute_always_recomputes_name():
    doc = {"first": "Alice", "last": "Smith", "site_ids": ["S1"]}
    entity_edit.recompute_employee_derived(doc)
    assert doc["name"] == "Alice Smith"


# --------------------------------------------------------------------------- #
# C2. employee_detail.handle_save_section — PUT preserves site_ids end-to-end
# --------------------------------------------------------------------------- #
class DummyContext(SimpleNamespace):
    def __init__(self, tmp_path: Path, query: dict[str, list[str]] | None = None) -> None:
        super().__init__(runtime_root=tmp_path / "runtime", query=query or {})
        self.audit_entries: list[tuple[str, dict[str, object], str]] = []

    def redirect(self, location: str):
        return 303, "text/html; charset=utf-8", f'<a href="{location}">Return</a>'.encode(), {"Location": location}

    def audit(self, route: str, payload: dict[str, object], result: str) -> None:
        self.audit_entries.append((route, payload, result))


def _employee_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "employee_x",
        "_rev": "1-abc",
        "type": "employee",
        "operator": "op_greg",
        "first": "Old",
        "last": "Name",
        "name": "Old Name",
        "status": "active",
        "site_ids": ["SANDBOX", "S2"],
        "job": "699",
        "additional_jobs": ["S3"],
    }
    doc.update(overrides)
    return doc


def _capture_put(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_request_json(method: str, path: str, payload: dict[str, object]):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(ed.sites, "request_json", fake_request_json)
    return captured


def _no_active_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens_module, "active_tokens_for_identity", lambda *a, **k: [])


def test_save_identity_preserves_canonical_site_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)
    ed.handle_save_section(ctx, "x", b"_section=identity&first=Alice&last=Smith&_entity_id=x")
    assert captured["payload"]["site_ids"] == ["SANDBOX", "S2"]


def test_save_contact_preserves_canonical_site_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)
    ed.handle_save_section(
        ctx, "x", b"_section=contact&phone=555-1234&email=a%40example.com&_entity_id=x"
    )
    assert captured["payload"]["site_ids"] == ["SANDBOX", "S2"]


def test_save_status_inactive_preserves_canonical_site_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: _employee_doc(status="active"))
    _no_active_tokens(monkeypatch)
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)
    status, *_ = ed.handle_save_section(
        ctx, "x", b"_section=identity&first=Old&last=Name&status=inactive&_entity_id=x"
    )
    assert "payload" in captured, "no PUT occurred — a confirmation page likely rendered instead"
    assert captured["payload"]["site_ids"] == ["SANDBOX", "S2"]


def test_save_assignment_role_only_preserves_canonical_site_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)
    ed.handle_save_section(ctx, "x", b"_section=assignment&role=site_admin&_entity_id=x")
    assert captured["payload"]["site_ids"] == ["SANDBOX", "S2"]


def test_save_assignment_job_change_does_not_touch_canonical_site_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)
    ed.handle_save_section(ctx, "x", b"_section=assignment&job=700&_entity_id=x")
    assert captured["payload"]["site_ids"] == ["SANDBOX", "S2"]
    # The legacy job value itself does change (assignment section owns it) —
    # confirms the preservation is about site_ids specifically, not a no-op save.
    assert captured["payload"]["job"] == "700"


def test_save_identity_int_legacy_job_seeds_stringified_site_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        ed, "_load_vault_doc", lambda _doc_id: _employee_doc(site_ids=[], job=1337, additional_jobs=[])
    )
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)
    ed.handle_save_section(ctx, "x", b"_section=identity&first=Old&last=Name&_entity_id=x")
    assert captured["payload"]["site_ids"] == ["1337"]


def test_save_identity_str_legacy_job_seeds_site_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        ed, "_load_vault_doc", lambda _doc_id: _employee_doc(site_ids=[], job="789", additional_jobs=[])
    )
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)
    ed.handle_save_section(ctx, "x", b"_section=identity&first=Old&last=Name&_entity_id=x")
    assert captured["payload"]["site_ids"] == ["789"]


# --------------------------------------------------------------------------- #
# D. context_resolver.operator_context_snapshot — resolver agreement
# --------------------------------------------------------------------------- #
def _location_doc(site_id: str, name: str) -> dict[str, Any]:
    return {
        "_id": f"site_{site_id}",
        "type": "location",
        "site_id": site_id,
        "location": name,
        "status": "active",
    }


def test_resolver_legacy_only_operator_resolves_via_job():
    operator_doc = {
        "_id": "employee_op_legacy",
        "type": "employee",
        "name": "Op Legacy",
        "site_ids": [],
        "job": "SANDBOX",
    }
    accounts = [_location_doc("SANDBOX", "Sandbox Account")]
    snapshot = context_resolver.operator_context_snapshot(
        "Op Legacy", account_docs=accounts, person_docs=[operator_doc]
    )
    job_numbers = {a["job_number"] for a in snapshot["accounts"]}
    assert job_numbers == {"SANDBOX"}, snapshot


def test_resolver_canonical_operator_ignores_legacy_job():
    operator_doc = {
        "_id": "employee_op_canonical",
        "type": "employee",
        "name": "Op Canonical",
        "site_ids": ["S2"],
        "job": "SANDBOX",
    }
    accounts = [_location_doc("SANDBOX", "Sandbox Account"), _location_doc("S2", "Second Account")]
    snapshot = context_resolver.operator_context_snapshot(
        "Op Canonical", account_docs=accounts, person_docs=[operator_doc]
    )
    job_numbers = {a["job_number"] for a in snapshot["accounts"]}
    assert job_numbers == {"S2"}, snapshot


# --------------------------------------------------------------------------- #
# E. Unassign scrubs legacy — queue_processor.handlers.employee_sites
# --------------------------------------------------------------------------- #
_EMPLOYEE_DOC_ID = "employee_sandbox_scrub"


def _scrub_seed(**over: Any) -> dict[str, Any]:
    doc = {
        "_id": _EMPLOYEE_DOC_ID,
        "type": "employee",
        "operator": "op_greg",
        "name": "Sandbox Scrub",
        "status": "active",
        "phone": "555-0000",
        "content": "## Notes\nsandbox\n",
        "site_ids": ["X", "Y"],
        "job": "X",
        "additional_jobs": ["Y", "X"],
        "sites": ["X"],
        "btq_job_ids": ["seed-job"],
    }
    doc.update(over)
    return doc


def _run_job(
    store: RecordingRmwVaultStore,
    context,
    payload: dict[str, Any],
    job_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    queue_name: str | None = None,
):
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, queue_name or job_id)
    processed_dir = context.runtime_root / "processed"
    employee_sites.process_assign_employee_site_job(
        queue_file,
        job(JOB_ASSIGN_EMPLOYEE_SITE, payload, job_id=job_id),
        context,
        processed_dir,
    )
    return queue_file, processed_dir


def _get(store: RecordingRmwVaultStore) -> dict[str, Any]:
    doc = store.get_optional(_EMPLOYEE_DOC_ID)
    assert doc is not None
    return doc


def test_unassign_scrubs_legacy_fields_and_preserves_unrelated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_scrub_seed()])
    _run_job(
        store, context, {"employee_id": "sandbox_scrub", "site_id": "X", "actor": "Greg", "action": "unassign"},
        "unassign-scrub-1", monkeypatch,
    )
    doc = _get(store)
    assert doc["site_ids"] == ["Y"]
    assert "job" not in doc, f"expected 'job' key removed entirely, found: {doc.get('job')!r}"
    assert doc["additional_jobs"] == ["Y"]
    assert doc["sites"] == []
    # Unrelated fields untouched.
    assert doc["status"] == "active"
    assert doc["phone"] == "555-0000"
    assert doc["content"] == "## Notes\nsandbox\n"
    # Job-id marker appended.
    assert doc["btq_job_ids"] == ["seed-job", "unassign-scrub-1"]


def test_unassign_scrubs_int_legacy_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_scrub_seed(job=1337, site_ids=["1337", "Y"], additional_jobs=["Y"], sites=[])])
    _run_job(
        store, context, {"employee_id": "sandbox_scrub", "site_id": "1337", "actor": "Greg", "action": "unassign"},
        "unassign-scrub-int", monkeypatch,
    )
    doc = _get(store)
    assert doc["site_ids"] == ["Y"]
    assert "job" not in doc, f"expected int-valued 'job' scrubbed, found: {doc.get('job')!r}"


def test_unassign_last_site_leaves_empty_no_legacy_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore(
        [_scrub_seed(site_ids=["X"], job="X", additional_jobs=[], sites=[])]
    )
    _run_job(
        store, context, {"employee_id": "sandbox_scrub", "site_id": "X", "actor": "Greg", "action": "unassign"},
        "unassign-last", monkeypatch,
    )
    doc = _get(store)
    assert doc["site_ids"] == []
    assert "job" not in doc
    # A subsequent dashboard recompute must NOT resurrect the removal — with
    # site_ids present as [] (non-empty-list check fails -> would fall back to
    # legacy) but legacy is scrubbed, so the rule still yields [].
    entity_edit.recompute_employee_derived(doc)
    assert doc["site_ids"] == []


def test_assign_still_appends_without_touching_legacy_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_scrub_seed(site_ids=["X"])])
    _run_job(
        store, context, {"employee_id": "sandbox_scrub", "site_id": "Z", "actor": "Greg", "action": "assign"},
        "assign-scrub-1", monkeypatch,
    )
    doc = _get(store)
    assert doc["site_ids"] == ["X", "Z"]
    # Legacy fields from the seed are untouched by assign.
    assert doc["job"] == "X"
    assert doc["additional_jobs"] == ["Y", "X"]
    assert doc["sites"] == ["X"]


def test_replay_unassign_same_job_id_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_scrub_seed()])
    _run_job(
        store, context, {"employee_id": "sandbox_scrub", "site_id": "X", "actor": "Greg", "action": "unassign"},
        "unassign-replay", monkeypatch,
    )
    after_first = _get(store)
    assert after_first["site_ids"] == ["Y"]
    assert after_first["btq_job_ids"] == ["seed-job", "unassign-replay"]

    _run_job(
        store, context, {"employee_id": "sandbox_scrub", "site_id": "X", "actor": "Greg", "action": "unassign"},
        "unassign-replay", monkeypatch, queue_name="unassign-replay-2",
    )
    doc = _get(store)
    assert doc == after_first, "replay of the same job_id must be a no-op"


def test_replay_assign_same_job_id_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_scrub_seed(site_ids=["X"])])
    _run_job(
        store, context, {"employee_id": "sandbox_scrub", "site_id": "Z", "actor": "Greg", "action": "assign"},
        "assign-replay", monkeypatch,
    )
    after_first = _get(store)
    assert after_first["site_ids"] == ["X", "Z"]

    _run_job(
        store, context, {"employee_id": "sandbox_scrub", "site_id": "Z", "actor": "Greg", "action": "assign"},
        "assign-replay", monkeypatch, queue_name="assign-replay-2",
    )
    doc = _get(store)
    assert doc == after_first, "replay of the same job_id must be a no-op"


def test_unassign_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    object.__setattr__(context, "dry_run", True)
    store = RecordingRmwVaultStore([_scrub_seed()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "unassign-dry")
    processed_dir = context.runtime_root / "processed"
    employee_sites.process_assign_employee_site_job(
        queue_file,
        job(
            JOB_ASSIGN_EMPLOYEE_SITE,
            {"employee_id": "sandbox_scrub", "site_id": "X", "actor": "Greg", "action": "unassign"},
            job_id="unassign-dry",
        ),
        context,
        processed_dir,
    )
    doc = _get(store)
    assert doc["site_ids"] == ["X", "Y"]
    assert doc["job"] == "X"
    assert store.update_doc_calls == []
    assert not (processed_dir / queue_file.name).exists()
    assert queue_file.exists()


# --------------------------------------------------------------------------- #
# F. migrate_vault._employee_site_ids delegates to the shared rule.
# --------------------------------------------------------------------------- #
def test_migrate_vault_delegates_to_shared_rule():
    frontmatter = {"site_ids": ["S1"], "job": "S2"}
    assert _employee_site_ids(frontmatter) == employee_assigned_site_ids(frontmatter) == ["S1"]


def test_migrate_vault_int_job_stringified():
    assert _employee_site_ids({"job": 1337}) == ["1337"]
