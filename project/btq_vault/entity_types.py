from __future__ import annotations

import os


# Canonical entity type set. Mirrors the validate_doc_update whitelist in
# design_btq_vault.json. Must be kept in sync.
CANONICAL_ENTITY_TYPES: frozenset[str] = frozenset({
    "account", "location", "employee", "visit", "visit_gap", "opportunity",
    "shift_report", "shift_report_note", "day_record", "journal", "unknown_capture", "personnel_event",
    "availability_constraint",
    "prospect", "site_issue", "supply_need", "supply_request", "monthly_summary",
    "equipment_request", "supply_order", "plan", "inventory", "note", "operator",
    "system_defaults",
})

# Types that existed in the vault but are resolved to canonical names at
# migration time. Not accepted by validate_doc_update.
DEPRECATED_TYPE_ALIASES: dict[str, str] = {
    "site_visit": "visit",
    "person": "employee",
}

# The operator id for Jordan's data. Used at migration time to stamp all
# migrated documents.
OPERATOR_ID_GREG = "op_greg"
OPERATOR_PERSON_ID = "prs_01KSGY3B8A0CZT6ZB05VQ9HP32"


def current_operator_id() -> str:
    return os.environ.get("BTQ_OPERATOR_ID") or OPERATOR_ID_GREG


def current_operator_identity() -> str:
    """Return the resolver-facing identity (an employee ``person_id``/``_id``).

    This is distinct from the document-stamping operator id.
    """
    operator_identity = os.environ.get("BTQ_OPERATOR_IDENTITY", "").strip()
    return operator_identity or current_operator_id()


# Operator step after deploy: set VOICE_MEMO_PERSON_ID=<this-value> in the
# ops-dashboard launch agent plist, then run `btq migrate-vault` to ingest
# People/Avery, Jordan.md into btq_vault CouchDB.
