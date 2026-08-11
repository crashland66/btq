"""Gating contract for prompt 524: a site issue is filed as what was reported.

Authored by the INDEPENDENT VERIFIER (not the executor).

The bug (live, measured 2026-08-10): ``site_issue_payload`` hardcoded three
loss-shaped defaults -- title "Equipment or supply loss reported at <site>",
category ``supply``, resolution trigger "operator confirms item recovered or
replaced" -- and the model is never asked for a title, so EVERY site issue from
the model lane took all three. Of 27 live log_site_issue drafts, 15 carried that
title; 14 of the 15 were approved. Not one was a loss: a flood from bad plumbing,
a failed door lock, a urinal that would not drain, cracked tile, a section washed
out by rain, an IT room closed with no key.

The messages asserted below are the real live draft messages (sandbox site
identity substituted). Each drives the REAL path -- model payload ->
``extracted_actions_from_model_payload`` -> ``proposed_queue_job`` -- so the
asserted payload is whatever production actually builds, and every payload is
checked against the queue contract with ``validate_job``.

Judgment pinned here, deliberately: damaged hardware is ``maintenance`` even when
it mentions a lock or a door ("locking mechanism has failed"), while a lock/key
report with nothing broken is ``access`` ("no key available"). The operator's
next action differs -- repair versus restore access -- and so does the trigger.
"""
from __future__ import annotations

import pytest

from processing_core.capture_semantics import (
    CaptureSemanticInput,
    extracted_actions_from_model_payload,
)
from queue_spec import SITE_ISSUE_CATEGORIES, validate_job


OLD_LOSS_TITLE = "Equipment or supply loss reported"
OLD_LOSS_TRIGGER = "operator confirms item recovered or replaced"


def _input(note: str) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id="cap-524",
        source_kind="ops_dashboard_text",
        source_text=note,
        site_id="SANDBOX",
        site_label="Sandbox Site",
    )


def _payload(message: str, *, note: str | None = None, payload_fields: dict | None = None) -> dict:
    """Real path: model action -> finalize -> proposed queue job payload."""
    source_note = note if note is not None else message
    actions = extracted_actions_from_model_payload(
        {
            "extracted_actions": [
                {
                    "action_key": "k",
                    "candidate_type": "field_capture_follow_up",
                    "target_type": "site",
                    "target_label": "Sandbox Site",
                    "summary": message,
                    "source_excerpt": source_note,
                    "job_type": "log_site_issue",
                    "payload_fields": payload_fields if payload_fields is not None else {},
                }
            ]
        },
        _input(source_note),
    )
    assert len(actions) == 1, actions
    proposed = actions[0].proposed_queue_job
    assert isinstance(proposed, dict), actions[0].proposed_queue_job_error
    assert proposed["job_type"] == "log_site_issue"
    payload = proposed["payload"]
    # Every payload this prompt produces must remain contract-valid.
    assert validate_job(proposed), payload
    assert payload["category"] in SITE_ISSUE_CATEGORIES
    assert payload["title"].strip()
    assert payload["resolution_trigger"].strip()
    return payload


# =========================================================================== #
# CRITERION 1 -- THE LIVE MISFILINGS. Each was filed as a supply loss.
# =========================================================================== #
LIVE_MAINTENANCE_MESSAGES = [
    "A flood occurred in the plating offices due to poor plumbing.",
    "The external door G402-A locking mechanism has failed.",
    "Side admin entry door is broken from the turn lock.",
    "Urinal in Restrooms is not draining properly",
    "Cracked and missing tile in the lunch room floor needs attention.",
    "The middle section of the fuel department is washed out due to rain.",
]


@pytest.mark.parametrize("message", LIVE_MAINTENANCE_MESSAGES)
def test_live_maintenance_reports_are_filed_as_maintenance(message: str) -> None:
    payload = _payload(message)
    assert payload["category"] == "maintenance", (message, payload["category"])
    assert OLD_LOSS_TITLE not in payload["title"], payload["title"]
    assert payload["resolution_trigger"] != OLD_LOSS_TRIGGER, message


@pytest.mark.parametrize("message", LIVE_MAINTENANCE_MESSAGES)
def test_live_maintenance_titles_state_what_was_reported(message: str) -> None:
    payload = _payload(message)
    title = payload["title"]
    assert len(title) <= 80
    # The title carries the report's own words, not a manufactured allegation.
    assert title.split()[0].lower() in message.lower()


def test_live_flood_resolution_trigger_describes_the_repair_not_a_recovery() -> None:
    payload = _payload("A flood occurred in the plating offices due to poor plumbing.")
    trigger = payload["resolution_trigger"].lower()
    assert "recovered" not in trigger and "replaced" not in trigger
    assert "repair" in trigger or "dry" in trigger


def test_live_urinal_drain_resolution_trigger_describes_the_drain() -> None:
    payload = _payload("Urinal in Restrooms is not draining properly")
    assert "drain" in payload["resolution_trigger"].lower()


# =========================================================================== #
# CRITERION 2 -- ACCESS vs MAINTENANCE. A lock with nothing broken is access;
# broken hardware is a repair even when it mentions a lock.
# =========================================================================== #
def test_live_it_closed_with_no_key_is_access() -> None:
    payload = _payload("IT closed and no key available")
    assert payload["category"] == "access"
    assert "access" in payload["resolution_trigger"].lower()


def test_live_electrical_room_locked_is_access() -> None:
    payload = _payload("Electrical room is locked.")
    assert payload["category"] == "access"


@pytest.mark.parametrize(
    "message",
    [
        "The external door G402-A locking mechanism has failed.",
        "Side admin entry door is broken from the turn lock.",
    ],
)
def test_damaged_hardware_outranks_the_lock_vocabulary(message: str) -> None:
    payload = _payload(message)
    assert payload["category"] == "maintenance", payload["category"]
    assert "access is restored" not in payload["resolution_trigger"].lower()


# =========================================================================== #
# CRITERION 3 -- A GENUINE LOSS STILL FILES AS ONE. The fix must not overshoot.
# =========================================================================== #
@pytest.mark.parametrize(
    "message",
    [
        "A backpack vacuum is missing from the janitor closet.",
        "Someone walked off with the extractor over the weekend.",
    ],
)
def test_a_real_loss_still_files_as_supply_with_a_recovery_trigger(message: str) -> None:
    payload = _payload(message)
    assert payload["category"] == "supply", payload["category"]
    trigger = payload["resolution_trigger"].lower()
    assert "recover" in trigger or "replace" in trigger


def test_a_restock_report_files_as_supply_with_a_restock_trigger() -> None:
    payload = _payload("The paper towel stock in the janitor closet needs restocked.")
    assert payload["category"] == "supply"
    assert "restock" in payload["resolution_trigger"].lower()


# =========================================================================== #
# CRITERION 4 -- THE HONEST FALLBACK. Nothing indicated -> other, never supply.
# =========================================================================== #
def test_an_unclassifiable_report_falls_back_to_other_not_supply() -> None:
    payload = _payload("The front lobby seems off tonight.")
    assert payload["category"] == "other", payload["category"]
    assert OLD_LOSS_TITLE not in payload["title"]
    assert payload["resolution_trigger"] == "operator confirms the reported condition is resolved"


def test_a_title_is_always_produced_and_bounded() -> None:
    long_message = (
        "The second floor east wing corridor carpet has a large stain running from the "
        "elevator lobby all the way to the north stairwell door and it needs attention"
    )
    payload = _payload(long_message)
    assert 0 < len(payload["title"]) <= 80


def test_a_report_with_no_words_still_yields_a_non_empty_title() -> None:
    # Summary blank, excerpt blank: the neutral site fallback, not a loss claim.
    actions = extracted_actions_from_model_payload(
        {
            "extracted_actions": [
                {
                    "action_key": "k",
                    "candidate_type": "field_capture_follow_up",
                    "target_type": "site",
                    "target_label": "Sandbox Site",
                    "summary": "Something happened",
                    "source_excerpt": "Something happened",
                    "job_type": "log_site_issue",
                    "payload_fields": {},
                }
            ]
        },
        _input("Something happened"),
    )
    payload = actions[0].proposed_queue_job["payload"]
    assert payload["title"].strip()
    assert OLD_LOSS_TITLE not in payload["title"]


# =========================================================================== #
# CRITERION 5 -- MODEL-SUPPLIED VALUES STILL WIN.
# =========================================================================== #
def test_explicit_model_values_are_not_overwritten() -> None:
    payload = _payload(
        "A flood occurred in the plating offices due to poor plumbing.",
        payload_fields={
            "title": "Plating offices flood",
            "category": "safety",
            "resolution_trigger": "operator confirms the floor is dry and marked",
        },
    )
    assert payload["title"] == "Plating offices flood"
    assert payload["category"] == "safety"
    assert payload["resolution_trigger"] == "operator confirms the floor is dry and marked"


# =========================================================================== #
# CRITERION 6 -- the retired constants are gone from the produced payloads.
# =========================================================================== #
def test_no_live_message_can_still_produce_the_loss_boilerplate() -> None:
    messages = [
        *LIVE_MAINTENANCE_MESSAGES,
        "IT closed and no key available",
        "The front lobby seems off tonight.",
    ]
    for message in messages:
        payload = _payload(message)
        assert not payload["title"].startswith(OLD_LOSS_TITLE), message
        assert payload["category"] != "supply", message
