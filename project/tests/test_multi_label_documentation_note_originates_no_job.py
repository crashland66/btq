"""Gating contract for prompt 533: a MULTI-LABEL documentation note originates no job.

Authored by the INDEPENDENT VERIFIER (not the executor). Extends prompt 523's
bare-location-label guard to notes that list SEVERAL photographed rooms/areas
("Plant pictures. North cafeteria, upper offices and lower offices and locker
room") — too long for 523's whole-note recognizer, but every delimited segment
is still just a room label. The new mechanism is deterministic, post-model,
PER ACTION: an interpretive action whose ``source_excerpt`` is grounded only in
bare-label segments of a documentation-category note is dropped; everything
else survives.

The over-suppression gates: a segment carrying an expressed condition protects
every action grounded in it — including an action whose excerpt names only the
room ("North cafeteria" inside "North cafeteria dirty"); intent categories
(Report an Issue, Supply Request), empty/unknown categories, ungrounded
excerpts, ``append_to_note`` and empty job types are never touched. When in
doubt (excerpt spans a delimiter, excerpt matches an expressed segment), the
guard must NOT suppress.

These tests drive the REAL paths, mirroring the 523 harness:
  * model boundary: model payload -> ``extracted_actions_from_model_payload``
    -> ``structured_payloads_from_semantic``;
  * end to end: ``run_text_semantic_pipeline`` with ``LocalModelCaptureEngine``
    over a stub client -> ``job_drafts_from_semantic``.

Sandbox identity throughout (SANDBOX / Sandbox Site / sandy_sandbox); only
public-safe phrases appear in notes.
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
    bare_location_label_documentation_actions,
    extracted_actions_from_model_payload,
)
from processing_core.extracted_actions import ExtractedAction


OFFICES = "Offices / Classrooms / Exam Rooms"
REPORT_ISSUE = "report_an_issue"
REPORT_ISSUE_LABEL = "Report an Issue"
SUPPLY_REQUEST_LABEL = "Supply Request"
SUPPLY_REQUEST_CANONICAL = "supply_request_capture"

# The reference multi-label shape from the acceptance criteria: a preamble plus
# four bare location labels. Eleven tokens -- beyond 523's whole-note guard.
MULTI_LABEL = "Plant pictures. North cafeteria, upper offices and lower offices and locker room"
MULTI_LABELS = ("North cafeteria", "upper offices", "lower offices", "locker room")

# The mixed shape: one segment carries an expressed condition, the rest are labels.
MIXED = "Plant pictures. North cafeteria dirty, upper offices and locker room"

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
# Harness (mirrors the committed 523 file)
# --------------------------------------------------------------------------- #
def _input(note: str, area: str = OFFICES, capture_id: str = "cap-533") -> CaptureSemanticInput:
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


def _label_issue(excerpt: str) -> dict:
    """A well-formed site issue whose excerpt is one label, verbatim."""
    return _action(
        "log_site_issue",
        summary=f"{excerpt} needs attention.",
        excerpt=excerpt,
        payload_fields={"summary": excerpt},
    )


def _job_types_from_model(note: str, model_actions: list[dict], *, area: str = OFFICES) -> list[str]:
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
    provider = "stub"
    model = "stub"

    def __init__(self, actions: list[dict]) -> None:
        self._actions = actions
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        return {"extracted_actions": self._actions}


def _drafts_end_to_end(note: str, model_actions: list[dict], *, area: str = OFFICES, capture_id: str = "cap-533-e2e") -> list[dict]:
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


def _extracted_action(job_type: str, excerpt: str) -> ExtractedAction:
    return ExtractedAction(
        action_key="k",
        candidate_type="field_capture_follow_up",
        target_type="site",
        target_id="SANDBOX",
        target_label="Sandbox Site",
        summary=f"{excerpt} follow up.",
        rationale="",
        confidence="high",
        source_excerpt=excerpt,
        job_type=job_type,
        payload_fields={"summary": excerpt},
    )


# =========================================================================== #
# CRITERION 1 -- THE MULTI-LABEL CASE. The model proposes one well-formed site
# issue per label; the capture originates zero interpretive actions. This is
# what the mutation check flips RED.
# =========================================================================== #
def test_multi_label_note_suppresses_every_per_label_site_issue() -> None:
    job_types = _job_types_from_model(MULTI_LABEL, [_label_issue(label) for label in MULTI_LABELS])
    assert _interpretive(job_types) == [], (
        f"per-label site issues survived the multi-label documentation note: {job_types}"
    )


def test_multi_label_note_end_to_end_emits_no_interpretive_draft() -> None:
    drafts = _drafts_end_to_end(MULTI_LABEL, [_label_issue(label) for label in MULTI_LABELS])
    leaked = [draft for draft in drafts if draft["job_type"] in INTERPRETIVE_JOB_TYPES]
    assert leaked == [], f"interpretive draft emitted from a multi-label note: {leaked}"


# =========================================================================== #
# CRITERION 2 -- CROSS-JOB-TYPE, and the rule-engine (no model) path.
# =========================================================================== #
@pytest.mark.parametrize(
    "job_type, payload_fields",
    [
        ("log_site_issue", {"summary": "Upper offices need attention."}),
        ("log_supply_need", {"item_name": "upper offices"}),
        ("log_equipment_request", {"equipment_name": "upper offices"}),
        ("flag_access_constraint", {"constraint": "upper offices"}),
        ("log_personnel_event", {"event_type": "other"}),
        ("visit_create", {"visit_type": "follow_up"}),
    ],
)
def test_label_grounded_action_is_suppressed_whatever_job_type_the_model_picks(job_type: str, payload_fields: dict) -> None:
    job_types = _job_types_from_model(
        MULTI_LABEL,
        [_action(job_type, summary="Upper offices.", excerpt="upper offices", payload_fields=payload_fields)],
    )
    assert _interpretive(job_types) == [], f"{job_type} survived a bare label segment: {job_types}"


def test_rule_engine_path_originates_no_interpretive_job_from_the_multi_label_note() -> None:
    artifact = run_text_semantic_pipeline(
        MULTI_LABEL,
        site_id="SANDBOX",
        upload_id="cap-533-rule",
        area=OFFICES,
        person_id="sandy_sandbox",
    )  # no engine -> the real default/rule path
    artifact = json.loads(json.dumps(artifact))
    drafts = job_drafts_from_semantic(Path("cap-533-rule.json"), artifact)
    assert [d for d in drafts if d["job_type"] in INTERPRETIVE_JOB_TYPES] == []


# =========================================================================== #
# CRITERION 3 -- MIXED NOTES FILTER PER ACTION. The expressed segment keeps its
# action; the sibling labels file nothing.
# =========================================================================== #
def test_mixed_note_keeps_exactly_the_expressed_condition_action() -> None:
    job_types = _job_types_from_model(
        MIXED,
        [
            _action("log_site_issue", summary="North cafeteria is dirty.", excerpt="North cafeteria dirty"),
            _label_issue("upper offices"),
            _label_issue("locker room"),
        ],
    )
    assert _interpretive(job_types) == ["log_site_issue"], (
        f"expected exactly the dirty-cafeteria issue to survive, got {job_types}"
    )


def test_mixed_note_protects_a_retained_action_whose_excerpt_is_only_the_room_name() -> None:
    # The model grounds the retained issue in "North cafeteria" alone; the
    # segment's expressed condition ("dirty") must still protect it.
    job_types = _job_types_from_model(
        MIXED,
        [
            _action("log_site_issue", summary="North cafeteria is dirty.", excerpt="North cafeteria"),
            _label_issue("upper offices"),
            _label_issue("locker room"),
        ],
    )
    assert _interpretive(job_types) == ["log_site_issue"], (
        f"room-name excerpt inside an expressed segment was mishandled: {job_types}"
    )


# =========================================================================== #
# CRITERION 4/5 -- expressed needs and intent categories are never guarded.
# =========================================================================== #
def test_documentation_category_supply_need_with_expressed_need_still_fires() -> None:
    note = "Need paper towels in the north cafeteria"
    job_types = _job_types_from_model(
        note,
        [_action("log_supply_need", summary=note, excerpt=note, payload_fields={"item_name": "paper towels"})],
    )
    assert "log_supply_need" in job_types, f"expressed need was over-suppressed: {job_types}"


@pytest.mark.parametrize(
    "note, area, job_type, payload_fields",
    [
        ("Electrical room locked", REPORT_ISSUE_LABEL, "flag_access_constraint", {"constraint": "electrical room locked"}),
        ("Floor issue", REPORT_ISSUE, "log_site_issue", {"summary": "Floor issue reported at site"}),
    ],
)
def test_report_an_issue_category_keeps_its_action(note: str, area: str, job_type: str, payload_fields: dict) -> None:
    job_types = _job_types_from_model(
        note,
        [_action(job_type, summary=note, excerpt=note, payload_fields=payload_fields)],
        area=area,
    )
    assert job_type in job_types, f"{note!r} in {area!r} was suppressed: {job_types}"


@pytest.mark.parametrize("area", [SUPPLY_REQUEST_LABEL, SUPPLY_REQUEST_CANONICAL])
def test_supply_request_capture_category_is_never_touched_by_the_guard(area: str) -> None:
    # Unit level on the changed function itself: the Supply Request category is
    # not a documentation category, so the guard must return the actions as-is
    # (the category's own dedicated pipeline handles them downstream).
    actions = [_extracted_action("log_site_issue", "upper offices")]
    survived = bare_location_label_documentation_actions(actions, _input(MULTI_LABEL, area=area))
    assert survived == actions


# =========================================================================== #
# CRITERION 6 -- empty/unknown/custom categories stay fail-open.
# =========================================================================== #
@pytest.mark.parametrize("area", ["", "Client Custom Area 7"])
def test_multi_label_note_outside_documentation_categories_is_never_suppressed(area: str) -> None:
    job_types = _job_types_from_model(MULTI_LABEL, [_label_issue("upper offices")], area=area)
    assert "log_site_issue" in job_types, f"guard leaked into area {area!r}: {job_types}"


# =========================================================================== #
# CRITERION 7 -- an excerpt that does not occur in the note decides nothing.
# =========================================================================== #
def test_ungrounded_excerpt_is_never_suppressed_by_the_per_action_path() -> None:
    job_types = _job_types_from_model(MULTI_LABEL, [_label_issue("south stairwell")])
    assert "log_site_issue" in job_types, (
        f"an excerpt absent from the human note was suppressed: {job_types}"
    )


# =========================================================================== #
# CRITERION 9 -- the capture survives as documentation; append/empty job types
# are never suppressed.
# =========================================================================== #
def test_the_semantic_artifact_keeps_the_note_verbatim_and_the_review_candidate() -> None:
    artifact = run_text_semantic_pipeline(
        MULTI_LABEL,
        site_id="SANDBOX",
        upload_id="cap-533-artifact",
        area=OFFICES,
        person_id="sandy_sandbox",
        engine=LocalModelCaptureEngine(_StubModelClient([_label_issue(label) for label in MULTI_LABELS])),
    )
    assert artifact["source_text"] == MULTI_LABEL
    assert artifact["area"] == OFFICES
    assert artifact["status"] == "complete"
    assert artifact["action_candidates"], "the generic review/documentation candidate was lost"


def test_append_to_note_actions_survive_the_multi_label_guard() -> None:
    source = _input(MULTI_LABEL)
    actions = extracted_actions_from_model_payload(
        {
            "extracted_actions": [
                _action("append_to_note", summary="Photographed rooms.", excerpt="upper offices"),
                _label_issue("upper offices"),
            ]
        },
        source,
    )
    job_types = [action.job_type for action in actions]
    assert "append_to_note" in job_types, f"append_to_note was suppressed: {job_types}"
    assert "log_site_issue" not in job_types


def test_empty_job_type_actions_survive_the_multi_label_guard() -> None:
    keep = _extracted_action("", "upper offices")
    drop = _extracted_action("log_site_issue", "lower offices")
    survived = bare_location_label_documentation_actions([keep, drop], _input(MULTI_LABEL))
    assert survived == [keep]


# =========================================================================== #
# CRITERION 10 -- the model prompt mentions multi-room notes.
# =========================================================================== #
def test_the_model_prompt_mentions_multi_room_notes() -> None:
    client = _StubModelClient([])
    LocalModelCaptureEngine(client)(_input(MULTI_LABEL))
    assert client.prompts, "the model was never called"
    prompt = client.prompts[0].lower()
    assert "several photographed rooms or areas" in prompt
    assert "never invent a predicate" in prompt  # 523's instruction must remain


# =========================================================================== #
# PROBES BEYOND THE CONTRACT. Principle: suppress only interpretive actions
# grounded purely in bare-label segments of a documentation note; when in
# doubt, do not suppress.
# =========================================================================== #
def test_probe_excerpt_spanning_a_delimiter_is_left_alone() -> None:
    # "upper offices and lower offices" occurs in the note but in no single
    # segment. The guard cannot attribute it -> conservative fail-open: keep.
    job_types = _job_types_from_model(MULTI_LABEL, [_label_issue("upper offices and lower offices")])
    assert "log_site_issue" in job_types, (
        f"a delimiter-spanning excerpt was suppressed despite being unattributable: {job_types}"
    )


def test_probe_excerpt_matching_both_an_expressed_and_a_bare_segment_is_kept() -> None:
    # "North cafeteria" occurs in the expressed segment "North cafeteria dirty"
    # AND in the bare segment "north cafeteria floor mats"... both contain it,
    # and one is expressed -> not ALL bare -> keep (do not suppress on doubt).
    note = "Plant pictures. North cafeteria dirty, north cafeteria floor mats and locker room"
    job_types = _job_types_from_model(
        note,
        [_action("log_site_issue", summary="North cafeteria is dirty.", excerpt="North cafeteria")],
    )
    assert "log_site_issue" in job_types, f"ambiguously grounded action was suppressed: {job_types}"


def test_probe_expressed_segment_last_in_the_list_is_still_protected() -> None:
    note = "Plant pictures. Upper offices, locker room and north cafeteria dirty"
    job_types = _job_types_from_model(
        note,
        [
            _label_issue("Upper offices"),
            _label_issue("locker room"),
            _action("log_site_issue", summary="North cafeteria is dirty.", excerpt="north cafeteria dirty"),
        ],
    )
    assert _interpretive(job_types) == ["log_site_issue"], (
        f"position of the expressed segment changed the outcome: {job_types}"
    )


def test_probe_substring_of_a_label_word_is_still_label_grounded_and_suppressed() -> None:
    # Excerpt "office" occurs only inside the bare labels "upper offices" /
    # "lower offices": still grounded purely in bare-label material -> suppress.
    job_types = _job_types_from_model(MULTI_LABEL, [_label_issue("office")])
    assert _interpretive(job_types) == [], f"substring-grounded label action leaked: {job_types}"


def test_probe_punctuation_variants_still_segment_and_suppress() -> None:
    note = "Plant pictures! North cafeteria; upper offices and the locker room"
    job_types = _job_types_from_model(
        note,
        [_label_issue("North cafeteria"), _label_issue("upper offices"), _label_issue("the locker room")],
    )
    assert _interpretive(job_types) == [], f"punctuation variant defeated the guard: {job_types}"


def test_probe_emoji_in_a_label_does_not_defeat_the_guard() -> None:
    note = "Plant pictures. North cafeteria \U0001f33f, upper offices and locker room"
    job_types = _job_types_from_model(
        note,
        [_label_issue("North cafeteria \U0001f33f"), _label_issue("upper offices")],
    )
    assert _interpretive(job_types) == [], f"an emoji in the label leaked an action: {job_types}"


def test_probe_condition_term_embedded_inside_a_word_does_not_count_as_expressed() -> None:
    # "Outside patio" contains the letters of the condition term "out"; word
    # boundaries must keep it a bare label -> its action is suppressed.
    note = "Plant pictures. Outside patio, upper offices and locker room"
    job_types = _job_types_from_model(note, [_label_issue("Outside patio")])
    assert _interpretive(job_types) == [], (
        f"'out' matched inside 'Outside' and wrongly protected a bare label: {job_types}"
    )
