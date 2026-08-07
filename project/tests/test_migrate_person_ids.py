"""Gates for the person_id unification migration (btq_people retirement).

The invariants pinned here:

  * Target ids derive through the queue processor's own minting logic
    (``derive_person_id_base``), so migrated ids and freshly minted ids can
    never diverge — including compound surnames ("Caraballo Ortiz" stays one
    surname, it is not re-split).
  * Dash-form ids in EITHER name order land on ``lastname_firstname``.
  * The sandbox persona is exempt.
  * Reference rewriting replaces exact full-string matches only — prose that
    merely mentions an old slug is untouched, and display-name fields are
    never rewritten to ids.
  * Renamed docs keep their old ids as aliases, and field-capture auth
    resolves BOTH the old and the new person_id against the renamed doc —
    the property that keeps not-yet-rewritten tokens working mid-migration.
"""

from __future__ import annotations

import pytest

from event_pipeline.couchdb.migrate_person_ids import (
    EXTRA_LEGACY_PERSON_IDS,
    PersonIdMigrationError,
    fix_name_form_person_id,
    person_name_mapping,
    plan_employee_migration,
    renamed_employee_doc,
    rewrite_doc,
    target_person_id,
    token_store_sql,
)
from field_capture.auth import _doc_matches_person_id


# Fixture docs synthesized from the real vault employee field set (no real
# names): the three id shapes the migration must handle, plus the sandbox
# persona and a compound surname.
def employee(doc_id, *, first, last, name=None, person_id=None, status="active"):
    doc = {
        "_id": doc_id,
        "_rev": "1-abc",
        "type": "employee",
        "first": first,
        "last": last,
        "name": name or f"{first} {last}",
        "job": "1200",
        "site_ids": ["1200"],
        "status": status,
        "operator": "op_greg",
    }
    if person_id is not None:
        doc["person_id"] = person_id
    return doc


FIRST_LAST_DASH = employee("employee_alice-anderson", first="Alice", last="Anderson", person_id="alice-anderson")
LAST_FIRST_DASH = employee("employee_burton-carol", first="Carol", last="Burton", person_id="burton-carol")
MISSING_PERSON_ID = employee("employee_dawson_erin", first="Erin", last="Dawson")
COMPOUND_SURNAME = employee(
    "employee_castro_diaz_ramona", first="Ramona", last="Castro Diaz", name="Ramona Castro Diaz"
)
CONFORMING = employee("employee_frank_gina", first="Gina", last="Frank", person_id="frank_gina")
SANDBOX = employee("employee_sandbox-user", first="Sandy", last="Sandbox", name="Sandy Sandbox")

ROSTER = [FIRST_LAST_DASH, LAST_FIRST_DASH, MISSING_PERSON_ID, COMPOUND_SURNAME, CONFORMING, SANDBOX]


# ---------------------------------------------------------------------------
# Target derivation


def test_target_ids_are_lastname_firstname_in_both_dash_orders():
    assert target_person_id(FIRST_LAST_DASH) == "anderson_alice"
    assert target_person_id(LAST_FIRST_DASH) == "burton_carol"


def test_compound_surname_stays_one_surname():
    assert target_person_id(COMPOUND_SURNAME) == "castro_diaz_ramona"


def test_target_matches_queue_processor_minting():
    # Parity gate: the migration derives through the SAME function the queue
    # processor mints new person_ids with. If that import ever changes, this
    # test is the tripwire.
    from queue_processor.handlers.people import derive_person_id_base

    for doc in ROSTER:
        assert target_person_id(doc) == derive_person_id_base(
            {"first": doc["first"], "last": doc["last"], "name": doc["name"]}
        )


# ---------------------------------------------------------------------------
# Planning


def test_plan_separates_renames_backfills_unchanged_and_sandbox():
    plan = plan_employee_migration(ROSTER)
    renames = {(r.old_doc_id, r.new_doc_id, r.old_person_id, r.new_person_id) for r in plan.renames}
    assert renames == {
        ("employee_alice-anderson", "employee_anderson_alice", "alice-anderson", "anderson_alice"),
        ("employee_burton-carol", "employee_burton_carol", "burton-carol", "burton_carol"),
    }
    assert plan.backfills == [
        ("employee_castro_diaz_ramona", "castro_diaz_ramona"),
        ("employee_dawson_erin", "dawson_erin"),
    ]
    assert plan.unchanged == ["employee_frank_gina"]
    assert plan.excluded == ["employee_sandbox-user"]


def test_plan_collision_with_existing_doc_raises():
    docs = [FIRST_LAST_DASH, employee("employee_anderson_alice", first="Alice", last="Anderson", person_id="anderson_alice")]
    with pytest.raises(PersonIdMigrationError, match="collision"):
        plan_employee_migration(docs)


def test_plan_mapping_covers_bare_and_prefixed_ids_plus_legacy():
    mapping = plan_employee_migration(ROSTER).person_id_mapping()
    assert mapping["alice-anderson"] == "anderson_alice"
    assert mapping["employee_alice-anderson"] == "employee_anderson_alice"
    for legacy, target in EXTRA_LEGACY_PERSON_IDS.items():
        assert mapping[legacy] == target


# ---------------------------------------------------------------------------
# Renamed docs and auth continuity


def test_renamed_doc_keeps_old_ids_as_aliases_and_drops_rev():
    plan = plan_employee_migration(ROSTER)
    rename = next(r for r in plan.renames if r.old_doc_id == "employee_alice-anderson")
    new_doc = renamed_employee_doc(FIRST_LAST_DASH, rename)
    assert new_doc["_id"] == "employee_anderson_alice"
    assert new_doc["person_id"] == "anderson_alice"
    assert "alice-anderson" in new_doc["aliases"]
    assert "_rev" not in new_doc


def test_auth_resolves_old_and_new_person_id_against_renamed_doc():
    # THE mid-migration safety property: a token still carrying the old dash
    # id must authorize against the renamed doc (via aliases) while a token
    # already carrying the new id also resolves.
    plan = plan_employee_migration(ROSTER)
    for rename in plan.renames:
        old_doc = next(d for d in ROSTER if d["_id"] == rename.old_doc_id)
        new_doc = renamed_employee_doc(old_doc, rename)
        assert _doc_matches_person_id(new_doc, rename.new_person_id)
        assert _doc_matches_person_id(new_doc, rename.old_person_id)


# ---------------------------------------------------------------------------
# Reference rewriting


def test_rewrite_replaces_exact_matches_only():
    mapping = plan_employee_migration(ROSTER).person_id_mapping()
    doc = {
        "_id": "cap-123",
        "_rev": "4-def",
        "person_id": "alice-anderson",
        "reported_by": "burton-carol",
        "employee_slugs": ["alice-anderson", "frank_gina"],
        "payload": {"requested_by": "alice-anderson", "entity_id": "employee_burton-carol"},
        "message": "Review status change: alice-anderson,burton-carol -> inactive.",
        "note": "alice-anderson mentioned in passing prose is left alone",
    }
    rewritten, hits = rewrite_doc(doc, mapping)
    assert rewritten["person_id"] == "anderson_alice"
    assert rewritten["reported_by"] == "burton_carol"
    assert rewritten["employee_slugs"] == ["anderson_alice", "frank_gina"]
    assert rewritten["payload"] == {"requested_by": "anderson_alice", "entity_id": "employee_burton_carol"}
    # Prose is not exact-equal to an old id, so it is untouched.
    assert rewritten["message"] == doc["message"]
    assert rewritten["note"] == doc["note"]
    assert rewritten["_id"] == "cap-123"
    assert rewritten["_rev"] == "4-def"
    assert hits == 5


def test_rewrite_maps_legacy_ulids_to_greg():
    mapping = plan_employee_migration(ROSTER).person_id_mapping()
    for legacy, target in EXTRA_LEGACY_PERSON_IDS.items():
        rewritten, hits = rewrite_doc({"_id": "vm-1", "person_id": legacy}, mapping)
        assert rewritten["person_id"] == target
        assert hits == 1


def test_name_form_person_id_fixed_in_person_id_field_only():
    name_mapping = person_name_mapping(ROSTER)
    constraint = {"_id": "availability_constraint_avail_1", "type": "availability_constraint", "person_id": "Anderson, Alice"}
    fixed, hits = fix_name_form_person_id(constraint, name_mapping)
    assert fixed["person_id"] == "anderson_alice"
    assert hits == 1

    # Display-name fields are NEVER rewritten to ids: neither by the name
    # mapping (person_id field only) nor by the exact-match id walker.
    event = {"_id": "personnel_event_evt_1", "type": "personnel_event", "employee": "Alice Anderson", "person_id": "ok_id"}
    fixed_event, event_hits = fix_name_form_person_id(event, name_mapping)
    assert event_hits == 0
    rewritten_event, walker_hits = rewrite_doc(event, plan_employee_migration(ROSTER).person_id_mapping())
    assert walker_hits == 0
    assert rewritten_event["employee"] == "Alice Anderson"


def test_sandbox_persona_never_rewritten():
    mapping = plan_employee_migration(ROSTER).person_id_mapping()
    name_mapping = person_name_mapping(ROSTER)
    assert "sandbox-user" not in mapping
    assert "Sandy Sandbox" not in name_mapping
    doc = {"_id": "cap-9", "person_id": "sandbox-user"}
    rewritten, hits = rewrite_doc(doc, mapping)
    assert hits == 0
    assert rewritten["person_id"] == "sandbox-user"


# ---------------------------------------------------------------------------
# Token store SQL


def test_token_sql_updates_bare_ids_and_legacy_ulids_only():
    sql = token_store_sql(plan_employee_migration(ROSTER).person_id_mapping())
    assert "WHERE person_id = 'alice-anderson'" in sql
    assert "SET person_id = 'anderson_alice'" in sql
    for legacy in EXTRA_LEGACY_PERSON_IDS:
        assert f"WHERE person_id = '{legacy}'" in sql
    # Doc-id-shaped entries never appear: tokens store bare person_ids.
    assert "employee_" not in sql
    assert sql.startswith("BEGIN;") and sql.endswith("COMMIT;")
