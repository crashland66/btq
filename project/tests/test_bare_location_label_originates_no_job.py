"""Gating contract for prompt 523: a bare LOCATION LABEL originates no interpretive job.

Authored by the INDEPENDENT VERIFIER (not the executor). Generalizes 404's
supply/equipment-only need guard across every job type.

The bug (live, four occurrences at one site between 2026-08-05 and 2026-08-10,
three of them APPROVED into real jobs): a field worker photographs the offices
in the plant's Melting department and types the room label "Melting offices".
The model reads "melting" as a verb, inverts the noun phrase into the sentence
"Offices are melting.", and emits a ``log_site_issue``. Nothing was melting. The
sibling notes were "Melting offices and bathroom", "Melting offices and
bathrooms" and "Melting control", all in the QC category
"Offices / Classrooms / Exam Rooms".

404 required an expressed NEED before a supply/equipment job. That guard is keyed
to two job types, so an invented ``log_site_issue`` walked through it. 523 makes
the guard cross-job-type and deterministic: when a short note carries no
expressed content AND the worker selected a room/area documentation category, no
interpretive action survives, whatever job type the model proposed.

THE TWO CRITICAL OVER-SUPPRESSION GATES, both drawn from live APPROVED drafts:
  * "Floor issue" (category report_an_issue) -> still one log_site_issue;
  * "Electrical room locked" (category Report an Issue) -> still one access flag.
A guard keyed on shortness alone kills both. The discriminator is absence of
expressed content in a documentation category -- never room/label words, never
length by itself.

These tests drive the REAL paths, not hand-built candidate dicts:
  * model boundary: hand-built MODEL PAYLOAD -> ``extracted_actions_from_model_payload``
    -> ``structured_payloads_from_semantic`` (the 401/403 split), so the asserted
    candidates are whatever production actually emits;
  * end to end: ``run_text_semantic_pipeline`` driven by the real
    ``LocalModelCaptureEngine`` over a stub client that returns the live model
    payload -> ``job_drafts_from_semantic``, i.e. capture text in, job drafts out.

Sandbox identity throughout (SANDBOX / Sandbox Site / Sandy Sandbox); the live
worker, client and site names stay out of the suite.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from field_capture.action_candidates import structured_payloads_from_semantic
from field_capture.job_draft_emission import job_drafts_from_semantic
from field_capture.text_semantics import run_text_semantic_pipeline
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    LocalModelCaptureEngine,
    extracted_actions_from_model_payload,
    note_is_bare_location_label,
)


# The live capture category on all four melting captures.
OFFICES = "Offices / Classrooms / Exam Rooms"
REPORT_ISSUE = "report_an_issue"
REPORT_ISSUE_LABEL = "Report an Issue"

# The exact live notes.
LIVE_LABELS = (
    "Melting offices",
    "Melting offices and bathroom",
    "Melting offices and bathrooms",
    "Melting control",
)

# Every job type an action can carry that ASSERTS something about the world.
# ``append_to_note`` and the empty job type are documentation, not assertions.
INTERPRETIVE_JOB_TYPES = frozenset(
    {
        "log_site_issue",
        "log_supply_need",
        "log_equipment_request",
        "flag_access_constraint",
        "log_personnel_event",
        "visit_create",
        "trigger_recruiting",
        "flag_retention_risk",
        "set_entity_status",
        "update_site_equipment",
        "remove_from_schedule",
        "add_person",
        "create_supply_request",
    }
)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def _input(note: str, area: str = OFFICES, capture_id: str = "cap-523") -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id=capture_id,
        source_kind="ops_dashboard_text",
        source_text=note,
        site_id="SANDBOX",
        site_label="Sandbox Site",
        area=area,
    )


def _action(job_type: str, *, summary: str, excerpt: str, payload_fields: dict | None = None) -> dict:
    return {
        "action_key": "k",
        "candidate_type": "field_capture_follow_up",
        "target_type": "site",
        "target_label": "Sandbox Site",
        "summary": summary,
        "source_excerpt": excerpt,
        "job_type": job_type,
        "payload_fields": payload_fields if payload_fields is not None else {"summary": summary},
    }


def _melting_site_issue(note: str = "Melting offices") -> dict:
    """The live model output, verbatim in shape: an inverted predicate."""
    return _action(
        "log_site_issue",
        summary="Offices are melting.",
        excerpt=note,
        payload_fields={"summary": note},
    )


def _job_types_from_model(note: str, model_actions: list[dict], *, area: str = OFFICES) -> list[str]:
    """Model payload -> real finalize path -> real candidate split -> job types."""
    source = _input(note, area=area)
    actions = extracted_actions_from_model_payload({"extracted_actions": model_actions}, source)
    artifact = json.loads(
        json.dumps(
            {
                "capture_id": source.capture_id,
                "upload_id": source.capture_id,
                "extracted_actions": [asdict(action) for action in actions],
            }
        )
    )
    candidates = structured_payloads_from_semantic(Path(f"{source.capture_id}.json"), artifact)
    return [
        ((candidate.get("channel_metadata") or {}).get("job_type") or "")
        for candidate in candidates
    ]


def _interpretive(job_types: list[str]) -> list[str]:
    return [job_type for job_type in job_types if job_type in INTERPRETIVE_JOB_TYPES]


class _StubModelClient:
    """Stands in for the local model: returns a fixed extraction payload."""

    provider = "stub"
    model = "stub"

    def __init__(self, actions: list[dict]) -> None:
        self._actions = actions
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        return {"extracted_actions": self._actions}


def _drafts_end_to_end(note: str, model_actions: list[dict], *, area: str = OFFICES, capture_id: str = "cap-523-e2e") -> list[dict]:
    """Capture text in, job drafts out, through the real engine and emitter."""
    artifact = run_text_semantic_pipeline(
        note,
        site_id="SANDBOX",
        upload_id=capture_id,
        area=area,
        person_id="sandy_sandbox",
        engine=LocalModelCaptureEngine(_StubModelClient(model_actions)),
    )
    artifact = json.loads(json.dumps(artifact))
    return job_drafts_from_semantic(Path(f"{capture_id}.json"), artifact)


# =========================================================================== #
# CRITERION 1 -- THE LIVE CASE. The model proposes the inverted site issue; the
# capture originates zero interpretive jobs. This is what the mutation check
# flips RED.
# =========================================================================== #
def test_live_melting_offices_model_site_issue_is_suppressed() -> None:
    job_types = _job_types_from_model("Melting offices", [_melting_site_issue()])
    assert _interpretive(job_types) == [], (
        "the inverted 'Offices are melting.' site issue survived: " f"{job_types}"
    )


@pytest.mark.parametrize("note", LIVE_LABELS)
def test_every_live_melting_label_originates_no_interpretive_job(note: str) -> None:
    job_types = _job_types_from_model(note, [_melting_site_issue(note)])
    assert _interpretive(job_types) == [], f"{note!r} leaked {job_types}"


def test_live_melting_offices_end_to_end_emits_no_interpretive_draft() -> None:
    drafts = _drafts_end_to_end("Melting offices", [_melting_site_issue()])
    leaked = [draft for draft in drafts if draft["job_type"] in INTERPRETIVE_JOB_TYPES]
    assert leaked == [], f"interpretive draft emitted from a bare label: {leaked}"
    # The words the model invented must not reach a draft message either.
    assert not any("melting." in str(draft.get("message", "")).lower() for draft in drafts)


# =========================================================================== #
# CRITERION 2 -- CROSS-JOB-TYPE. 404 guarded two job types; the label must
# originate nothing whichever job the model picks.
# =========================================================================== #
@pytest.mark.parametrize(
    "job_type, payload_fields",
    [
        ("log_site_issue", {"summary": "Offices are melting."}),
        ("log_supply_need", {"item_name": "offices"}),
        ("log_equipment_request", {"equipment_name": "offices"}),
        ("flag_access_constraint", {"constraint": "offices"}),
        ("log_personnel_event", {"event_type": "other"}),
        ("visit_create", {"visit_type": "follow_up"}),
    ],
)
def test_bare_label_suppresses_whatever_job_type_the_model_proposes(job_type: str, payload_fields: dict) -> None:
    job_types = _job_types_from_model(
        "Melting offices",
        [_action(job_type, summary="Offices are melting.", excerpt="Melting offices", payload_fields=payload_fields)],
    )
    assert _interpretive(job_types) == [], f"{job_type} survived a bare label: {job_types}"


def test_bare_label_suppresses_several_proposed_actions_at_once() -> None:
    job_types = _job_types_from_model(
        "Melting offices",
        [
            _melting_site_issue(),
            _action("log_supply_need", summary="Supplies for offices.", excerpt="Melting offices", payload_fields={"item_name": "offices"}),
        ],
    )
    assert _interpretive(job_types) == []


# =========================================================================== #
# CRITERION 3 -- OVER-SUPPRESSION. Both cases below are live APPROVED drafts.
# A guard keyed on shortness alone kills them.
# =========================================================================== #
def test_live_floor_issue_in_report_an_issue_category_still_fires() -> None:
    job_types = _job_types_from_model(
        "Floor issue",
        [_action("log_site_issue", summary="Floor issue reported at site", excerpt="Floor issue")],
        area=REPORT_ISSUE,
    )
    assert "log_site_issue" in job_types, f"live approved 'Floor issue' was suppressed: {job_types}"


def test_live_electrical_room_locked_still_fires() -> None:
    job_types = _job_types_from_model(
        "Electrical room locked",
        [
            _action(
                "flag_access_constraint",
                summary="Electrical room is locked.",
                excerpt="Electrical room locked",
                payload_fields={"constraint": "electrical room locked"},
            )
        ],
        area=REPORT_ISSUE_LABEL,
    )
    assert "flag_access_constraint" in job_types, f"live approved access flag was suppressed: {job_types}"


def test_a_condition_term_defeats_the_guard_even_in_a_documentation_category() -> None:
    # Same category as the live bug, but the note states a condition.
    job_types = _job_types_from_model(
        "Melting offices locked",
        [
            _action(
                "flag_access_constraint",
                summary="Melting offices are locked.",
                excerpt="Melting offices locked",
                payload_fields={"constraint": "melting offices locked"},
            )
        ],
    )
    assert "flag_access_constraint" in job_types


@pytest.mark.parametrize(
    "note, job_type, payload_fields",
    [
        ("There is water leaking in the Melting offices", "log_site_issue", {"summary": "Water leaking in the Melting offices."}),
        ("A vacuum is missing from the Melting offices", "log_site_issue", {"summary": "A vacuum is missing from the Melting offices."}),
        ("Need paper towels in the Melting offices", "log_supply_need", {"item_name": "paper towels"}),
        ("Please fix the door in the Melting offices", "log_site_issue", {"summary": "Door needs repair in the Melting offices."}),
    ],
)
def test_an_expressed_condition_that_names_the_same_room_still_fires(note: str, job_type: str, payload_fields: dict) -> None:
    job_types = _job_types_from_model(note, [_action(job_type, summary=note, excerpt=note, payload_fields=payload_fields)])
    assert job_type in job_types, f"{note!r} was over-suppressed: {job_types}"


@pytest.mark.parametrize(
    "note, job_type, payload_fields",
    [
        ("Bruce got a client compliment.", "log_personnel_event", {"event_type": "recognition"}),
        ("Set Maria inactive.", "set_entity_status", {"status": "inactive"}),
        # A bare LABEL with no capture category: still not the guard's business.
        ("Front lobby glass", "log_site_issue", {"summary": "Front lobby glass."}),
    ],
)
def test_a_capture_with_no_category_is_never_guarded(note: str, job_type: str, payload_fields: dict) -> None:
    # Ops-dashboard text and voice memos carry no capture category. The guard
    # must not reach them, however terse they are.
    job_types = _job_types_from_model(note, [_action(job_type, summary=note, excerpt=note, payload_fields=payload_fields)], area="")
    assert job_type in job_types, f"{note!r} with no category was suppressed: {job_types}"


def test_an_unknown_custom_category_is_never_guarded() -> None:
    # A site-defined category the code does not know: fail open, never suppress.
    job_types = _job_types_from_model("Melting offices", [_melting_site_issue()], area="Client Custom Area 7")
    assert "log_site_issue" in job_types


def test_longer_free_text_is_untouched_by_the_guard() -> None:
    note = "walked the melting offices and the adjacent bathrooms this evening with the shift lead"
    job_types = _job_types_from_model(note, [_action("log_site_issue", summary=note, excerpt=note)])
    assert "log_site_issue" in job_types


# =========================================================================== #
# CRITERION 4 -- THE CAPTURE SURVIVES AS DOCUMENTATION. Suppressing the action
# must not suppress the capture, its words, or its documentation draft.
# =========================================================================== #
def test_the_bare_label_capture_emits_no_draft_at_all_but_keeps_its_review_candidate() -> None:
    # Suppressing the invented job leaves the capture with nothing to execute --
    # and the generic review candidate, the documentation lane, is untouched.
    drafts = _drafts_end_to_end("Melting offices", [_melting_site_issue()], capture_id="cap-523-doc")
    assert drafts == [], f"a bare label still produced drafts: {[d['job_type'] for d in drafts]}"

    artifact = run_text_semantic_pipeline(
        "Melting offices",
        site_id="SANDBOX",
        upload_id="cap-523-doc2",
        area=OFFICES,
        person_id="sandy_sandbox",
        engine=LocalModelCaptureEngine(_StubModelClient([_melting_site_issue()])),
    )
    assert artifact["action_candidates"], "the generic review/documentation candidate was lost"


def test_the_semantic_artifact_still_carries_the_human_note_verbatim() -> None:
    artifact = run_text_semantic_pipeline(
        "Melting offices",
        site_id="SANDBOX",
        upload_id="cap-523-artifact",
        area=OFFICES,
        person_id="sandy_sandbox",
        engine=LocalModelCaptureEngine(_StubModelClient([_melting_site_issue()])),
    )
    assert artifact["source_text"] == "Melting offices"
    assert artifact["area"] == OFFICES
    assert artifact["status"] == "complete"


# =========================================================================== #
# CRITERION 5 -- the rule engine (no model available) cannot re-create the job,
# and the model prompt itself carries the anti-inversion instruction.
# =========================================================================== #
def test_rule_engine_path_also_originates_no_interpretive_job() -> None:
    artifact = run_text_semantic_pipeline(
        "Melting offices",
        site_id="SANDBOX",
        upload_id="cap-523-rule",
        area=OFFICES,
        person_id="sandy_sandbox",
    )  # no engine -> the real default/rule path
    artifact = json.loads(json.dumps(artifact))
    drafts = job_drafts_from_semantic(Path("cap-523-rule.json"), artifact)
    assert [d for d in drafts if d["job_type"] in INTERPRETIVE_JOB_TYPES] == []


def test_the_model_prompt_forbids_inventing_a_predicate() -> None:
    client = _StubModelClient([_melting_site_issue()])
    LocalModelCaptureEngine(client)(_input("Melting offices"))
    assert client.prompts, "the model was never called"
    prompt = client.prompts[0].lower()
    assert "never invent a predicate" in prompt
    assert "label" in prompt


# =========================================================================== #
# CRITERION 6 -- the recognizer in isolation.
# =========================================================================== #
@pytest.mark.parametrize("note", LIVE_LABELS)
def test_recognizer_accepts_the_live_labels(note: str) -> None:
    assert note_is_bare_location_label(note, OFFICES) is True


@pytest.mark.parametrize(
    "note, area",
    [
        ("Floor issue", REPORT_ISSUE),                       # intent category
        ("Electrical room locked", REPORT_ISSUE_LABEL),      # intent category
        ("Melting offices locked", OFFICES),                 # expressed condition
        ("Melting offices are dirty", OFFICES),              # copula + condition
        ("Need towels in Melting offices", OFFICES),         # expressed need
        ("Melting offices 3rd door", OFFICES),               # a digit
        ("Melting offices?", OFFICES),                       # a question
        ("Melting offices", ""),                             # no category
        ("Melting offices", "Client Custom Area 7"),         # unknown category
        ("", OFFICES),                                       # nothing at all
        ("walked the melting offices and the adjacent bathrooms with the lead", OFFICES),  # too long
    ],
)
def test_recognizer_rejects_everything_that_is_not_a_bare_label(note: str, area: str) -> None:
    assert note_is_bare_location_label(note, area) is False
