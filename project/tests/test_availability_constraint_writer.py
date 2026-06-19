"""Independent gating tests for prompt 399 — canonical availability_constraint.

VERIFIER-AUTHORED (not the change author). Drives the new
``process_log_availability_constraint_job`` handler through the same
RunContext + recording-vault-store harness the existing personnel_event /
photo-capture handler tests use (test_queue_processor_couchdb_write.py), and
executes the REAL shipped CouchDB JS map functions via ``node`` (mirroring
test_employees_by_site_roster_consistency.py).

Design contract under test:
  * forward-looking, human-provenanced availability fact.
  * deterministic id from {canonical person, constraint_type, date} ONLY ->
    one doc per person+kind+date -> replay-safe.
  * constraint_type in {unavailable_date, last_working_day}.
  * source_text (verbatim) + reported_by REQUIRED.
  * last_working_day writes its own dated doc, does NOT touch person status.
  * related_site optional, NOT site-routability gated.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from queue_processor.handlers import people as people_handlers
from btq_vault.entity_types import OPERATOR_ID_GREG
from queue_processor.handlers import _shared as shared
from queue_spec import JOB_LOG_AVAILABILITY_CONSTRAINT, validate_job

# Reuse the exact harness the under-review handler is meant to plug into.
from test_queue_processor_couchdb_write import (
    RecordingRmwVaultStore,
    context_for,
    job,
    make_queue_file,
)

# Sandbox identity per the verifier brief.
_EMPLOYEE_NAME = "Adriana Forsythe"
_CANONICAL_PERSON = "Forsythe, Adriana"  # person_file_name(name)[:-3]
_SITE = "789"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _payload(
    *,
    constraint_type: str = "unavailable_date",
    date: str = "2026-06-24",
    employee: str = _EMPLOYEE_NAME,
    reported_by: str | None = "Jordan",
    source_text: str | None = "Adriana texted she can't work the 24th, dentist.",
    related_site: str | None = None,
    extra: dict | None = None,
) -> dict:
    p: dict[str, Any] = {
        "employee": employee,
        "constraint_type": constraint_type,
        "date": date,
    }
    if reported_by is not None:
        p["reported_by"] = reported_by
    if source_text is not None:
        p["source_text"] = source_text
    if related_site is not None:
        p["related_site"] = related_site
    if extra:
        p.update(extra)
    return p


def _run_handler(context, store, payload, job_id: str):
    # Callers monkeypatch shared._VAULT_STORE to ``store`` before invoking.
    queue_file = make_queue_file(context, job_id)
    processed_dir = context.runtime_root / "processed"
    people_handlers.process_log_availability_constraint_job(
        queue_file,
        job(JOB_LOG_AVAILABILITY_CONSTRAINT, payload, job_id=job_id),
        context,
        processed_dir,
    )
    return queue_file, processed_dir


def _avail_docs(store) -> list[dict]:
    return [d for d in store.docs if d.get("type") == "availability_constraint"]


# --------------------------------------------------------------------------- #
# Sanity — job participates in validate_job + registry dispatch
# --------------------------------------------------------------------------- #
def test_sanity_registry_routes_job_type_to_handler():
    from queue_processor.registry import JOB_HANDLERS

    assert (
        JOB_HANDLERS[JOB_LOG_AVAILABILITY_CONSTRAINT]
        is people_handlers.process_log_availability_constraint_job
    )


# --------------------------------------------------------------------------- #
# Criterion 1 — Write
# --------------------------------------------------------------------------- #
def test_c1_valid_job_writes_single_canonical_doc(tmp_path, monkeypatch):
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    queue_file, processed_dir = _run_handler(
        context, store, _payload(related_site=_SITE), "avail-job-1"
    )

    docs = _avail_docs(store)
    assert len(docs) == 1
    doc = docs[0]
    assert doc["_id"].startswith("availability_constraint_")
    assert doc["type"] == "availability_constraint"
    assert doc["person_id"] == _CANONICAL_PERSON
    assert doc["constraint_type"] == "unavailable_date"
    assert doc["date"] == "2026-06-24"
    assert doc["reported_by"] == "Jordan"
    assert doc["source_text"] == "Adriana texted she can't work the 24th, dentist."
    assert doc["related_site"] == _SITE
    assert doc["operator"] == OPERATOR_ID_GREG
    assert doc["btq_job_ids"] == ["avail-job-1"]
    assert "created_at" in doc
    # queue file consumed (moved to processed)
    assert (processed_dir / queue_file.name).exists()


def test_c1_related_site_optional(tmp_path, monkeypatch):
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _run_handler(context, store, _payload(related_site=None), "avail-nosite")

    docs = _avail_docs(store)
    assert len(docs) == 1
    # NOT site-routability gated: writes fine with no related_site, and field absent.
    assert "related_site" not in docs[0]


# --------------------------------------------------------------------------- #
# Criterion 2 — Idempotent replay
# --------------------------------------------------------------------------- #
def test_c2_replay_same_key_updates_same_doc_preserves_created_at(tmp_path, monkeypatch):
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _run_handler(context, store, _payload(), "avail-replay-a")
    first = _avail_docs(store)[0]
    first_id = first["_id"]
    first_created = first["created_at"]

    # Re-run the SAME (person, constraint_type, date), different job id +
    # mutated source_text -> must update the SAME doc, never duplicate.
    _run_handler(
        context,
        store,
        _payload(source_text="Updated wording, still the 24th."),
        "avail-replay-b",
    )

    docs = _avail_docs(store)
    assert len(docs) == 1, "replay must not create a duplicate doc"
    doc = docs[0]
    assert doc["_id"] == first_id
    assert doc["created_at"] == first_created, "created_at must be preserved on replay"
    assert doc["source_text"] == "Updated wording, still the 24th."


def test_c2_different_date_yields_different_doc(tmp_path, monkeypatch):
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _run_handler(context, store, _payload(date="2026-06-24"), "avail-d1")
    _run_handler(context, store, _payload(date="2026-06-25"), "avail-d2")

    ids = {d["_id"] for d in _avail_docs(store)}
    assert len(ids) == 2


def test_c2_different_constraint_type_yields_different_doc(tmp_path, monkeypatch):
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _run_handler(context, store, _payload(constraint_type="unavailable_date"), "avail-t1")
    _run_handler(context, store, _payload(constraint_type="last_working_day"), "avail-t2")

    ids = {d["_id"] for d in _avail_docs(store)}
    assert len(ids) == 2


# --------------------------------------------------------------------------- #
# Criterion 3 — Provenance enforced (the gate, at validate_job level)
# --------------------------------------------------------------------------- #
def _validatable(payload: dict) -> dict:
    return {
        "job_id": "j",
        "job_type": JOB_LOG_AVAILABILITY_CONSTRAINT,
        "payload": payload,
    }


def test_c3_valid_job_passes_validation():
    assert validate_job(_validatable(_payload(related_site=_SITE))) is True


def test_c3_missing_source_text_fails(tmp_path, monkeypatch):
    # validation rejects
    assert validate_job(_validatable(_payload(source_text=None))) is False
    # and no doc is written if the handler somehow ran on a gated job:
    # (handler relies on payload["source_text"]; KeyError would surface — confirm
    #  validation is the gate that prevents that.)


def test_c3_missing_reported_by_fails():
    assert validate_job(_validatable(_payload(reported_by=None))) is False


def test_c3_empty_source_text_fails():
    assert validate_job(_validatable(_payload(source_text="   "))) is False


def test_c3_empty_reported_by_fails():
    assert validate_job(_validatable(_payload(reported_by=" "))) is False


def test_c3_constraint_type_outside_enum_fails():
    assert validate_job(_validatable(_payload(constraint_type="maybe_someday"))) is False


def test_c3_malformed_date_fails():
    for bad in ["2026-6-24", "06/24/2026", "2026-06-24T00:00:00", "", "tomorrow"]:
        assert validate_job(_validatable(_payload(date=bad))) is False, bad


def test_c3_missing_employee_fails():
    p = _payload()
    del p["employee"]
    assert validate_job(_validatable(p)) is False


def test_c3_unknown_payload_field_fails():
    assert validate_job(_validatable(_payload(extra={"surprise": "x"}))) is False


def test_c3_known_optional_fields_allowed():
    # related_site, reported_at, event_id are part of the allowed set.
    assert validate_job(
        _validatable(
            _payload(
                related_site=_SITE,
                extra={"reported_at": "2026-06-19T10:00:00+00:00", "event_id": "fixed-id"},
            )
        )
    ) is True


# --------------------------------------------------------------------------- #
# Criterion 4 — last_working_day isolation
# --------------------------------------------------------------------------- #
def test_c4_last_working_day_does_not_mutate_person_doc(tmp_path, monkeypatch):
    context = context_for(tmp_path)
    # Seed an existing employee doc; the handler must not touch it.
    employee_doc = {
        "_id": f"employee_{_CANONICAL_PERSON}",
        "type": "employee",
        "operator": OPERATOR_ID_GREG,
        "name": _EMPLOYEE_NAME,
        "person_id": _CANONICAL_PERSON,
        "status": "active",
        "status_date": "2024-01-02",
        "site_ids": [_SITE],
    }
    store = RecordingRmwVaultStore([dict(employee_doc)])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _run_handler(
        context,
        store,
        _payload(constraint_type="last_working_day", date="2026-07-01", related_site=_SITE),
        "avail-lwd",
    )

    # availability_constraint doc written (dated).
    avail = _avail_docs(store)
    assert len(avail) == 1
    assert avail[0]["constraint_type"] == "last_working_day"
    assert avail[0]["date"] == "2026-07-01"

    # employee doc UNCHANGED: status/status_date untouched, no extra writes to it.
    emp = store.get_optional(f"employee_{_CANONICAL_PERSON}")
    assert emp == employee_doc, "person doc must not be mutated by last_working_day"
    # The RMW only ever targeted the availability_constraint doc id.
    assert all(
        call.startswith("availability_constraint_") for call in store.update_doc_calls
    ), store.update_doc_calls


# --------------------------------------------------------------------------- #
# Criterion 5 — Views (REAL shipped JS maps via node)
# --------------------------------------------------------------------------- #
_DESIGN_DOC = (
    Path(__file__).resolve().parents[1]
    / "event_pipeline"
    / "couchdb"
    / "design_btq_vault.json"
)
_VIEWS = json.loads(_DESIGN_DOC.read_text(encoding="utf-8"))["views"]
_NODE = shutil.which("node")
requires_node = pytest.mark.skipif(_NODE is None, reason="node not on PATH")


def _run_view(view_name: str, doc: dict) -> list[dict]:
    assert _NODE is not None
    map_src = _VIEWS[view_name]["map"]
    harness = textwrap.dedent(
        """
        const mapFn = (%(map_src)s);
        const doc = %(doc_json)s;
        const rows = [];
        function emit(key, value) { rows.push({key: key, value: value}); }
        mapFn(doc);
        process.stdout.write(JSON.stringify(rows));
        """
    ) % {"map_src": map_src, "doc_json": json.dumps(doc)}
    out = subprocess.run([_NODE, "-e", harness], capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        raise AssertionError(f"node map execution failed: {out.stderr}")
    return json.loads(out.stdout)


def _avail_doc(**over) -> dict:
    base = {
        "_id": "availability_constraint_x",
        "type": "availability_constraint",
        "person_id": _CANONICAL_PERSON,
        "constraint_type": "unavailable_date",
        "date": "2026-06-24",
        "reported_by": "Jordan",
        "source_text": "verbatim",
        "related_site": _SITE,
    }
    base.update(over)
    return base


@requires_node
def test_c5_by_person_emits_keyed_by_person_id():
    rows = _run_view("availability_constraints_by_person", _avail_doc())
    assert len(rows) == 1
    assert rows[0]["key"] == _CANONICAL_PERSON
    val = rows[0]["value"]
    assert val["constraint_type"] == "unavailable_date"
    assert val["date"] == "2026-06-24"
    assert val["related_site"] == _SITE
    assert val["reported_by"] == "Jordan"
    assert val["source_text"] == "verbatim"


@requires_node
def test_c5_by_person_ignores_non_availability_and_deleted():
    assert _run_view("availability_constraints_by_person", {"_id": "e", "type": "employee"}) == []
    assert _run_view(
        "availability_constraints_by_person",
        _avail_doc(_deleted=True),
    ) == []


@requires_node
def test_c5_by_site_emits_bare_site_date_key():
    rows = _run_view("availability_constraints_by_site", _avail_doc(related_site=_SITE))
    assert len(rows) == 1
    assert rows[0]["key"] == [_SITE, "2026-06-24"]
    val = rows[0]["value"]
    assert val["person_id"] == _CANONICAL_PERSON
    assert val["constraint_type"] == "unavailable_date"
    assert val["reported_by"] == "Jordan"


@requires_node
@pytest.mark.parametrize("raw", ["location_789", "site_789", '"789"', "789", '"location_789"'])
def test_c5_by_site_bare_normalizes_like_employees_by_site(raw):
    rows = _run_view("availability_constraints_by_site", _avail_doc(related_site=raw))
    assert len(rows) == 1
    assert rows[0]["key"][0] == _SITE, raw


@requires_node
def test_c5_by_site_no_related_site_emits_nothing():
    doc = _avail_doc()
    del doc["related_site"]
    assert _run_view("availability_constraints_by_site", doc) == []


@requires_node
def test_c5_by_site_ignores_non_availability_and_deleted():
    assert _run_view("availability_constraints_by_site", {"_id": "e", "type": "employee", "related_site": "789"}) == []
    assert _run_view("availability_constraints_by_site", _avail_doc(_deleted=True)) == []


# --------------------------------------------------------------------------- #
# Cross-check: deterministic id is a pure function of person+type+date.
# --------------------------------------------------------------------------- #
def test_id_determinism_independent_of_optional_fields():
    from queue_processor.handlers.people import availability_constraint_doc_id

    a = availability_constraint_doc_id(_payload(related_site=_SITE, source_text="one"))
    b = availability_constraint_doc_id(_payload(related_site="999", source_text="two"))
    assert a == b, "id must depend only on person+constraint_type+date"
    c = availability_constraint_doc_id(_payload(date="2026-06-25"))
    assert a != c
