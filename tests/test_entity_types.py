from __future__ import annotations

import re

from btq_vault.entity_types import CANONICAL_ENTITY_TYPES, DEPRECATED_TYPE_ALIASES, OPERATOR_ID_GREG, OPERATOR_PERSON_ID


def test_canonical_entity_types_includes_all_known_vault_types() -> None:
    expected = {
        "account",
        "location",
        "employee",
        "visit",
        "visit_gap",
        "opportunity",
        "shift_report",
        "journal",
        "unknown_capture",
        "personnel_event",
        "site_issue",
        "supply_need",
        "supply_request",
        "monthly_summary",
        "equipment_request",
        "supply_order",
        "plan",
        "inventory",
        "note",
        "operator",
    }

    assert expected <= CANONICAL_ENTITY_TYPES


def test_deprecated_type_aliases_not_in_canonical_set() -> None:
    assert "site_visit" not in CANONICAL_ENTITY_TYPES
    assert "person" not in CANONICAL_ENTITY_TYPES
    assert DEPRECATED_TYPE_ALIASES == {"site_visit": "visit", "person": "employee"}


def test_operator_id_greg_format() -> None:
    assert isinstance(OPERATOR_ID_GREG, str)
    assert OPERATOR_ID_GREG
    assert OPERATOR_ID_GREG.startswith("op_")


def test_operator_person_id_is_set() -> None:
    assert isinstance(OPERATOR_PERSON_ID, str)
    assert len(OPERATOR_PERSON_ID) > 4
    assert OPERATOR_PERSON_ID.startswith("prs_")


def test_operator_person_id_differs_from_operator_id() -> None:
    assert OPERATOR_PERSON_ID != OPERATOR_ID_GREG


def test_operator_person_id_format() -> None:
    # prs_ followed by 26 uppercase base-32 chars (ULID format)
    assert re.match(r"^prs_[0-9A-Z]{26}$", OPERATOR_PERSON_ID), \
        f"Expected prs_<26-char ULID>, got {OPERATOR_PERSON_ID!r}"
