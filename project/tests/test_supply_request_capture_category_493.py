from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from field_capture.action_candidates import structured_payloads_from_semantic
from field_capture.display_categories import (
    BUILTIN_FALLBACK_CATEGORIES,
    OPERATOR_ONLY_CATEGORIES,
    SUPPLY_REQUEST_CAPTURE_CATEGORY,
    SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION,
)
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    LocalModelCaptureEngine,
    RuleCaptureEngine,
    is_supply_request_capture_category,
)
from queue_spec import (
    JOB_CREATE_SUPPLY_REQUEST,
    JOB_LOG_EQUIPMENT_REQUEST,
    JOB_LOG_SUPPLY_NEED,
    validate_job,
)


class FakeModelClient:
    provider = "test"
    model = "public-fixture"

    def __init__(self, response: object) -> None:
        self.response = response

    def generate_json(self, _prompt: str) -> object:
        return self.response


def capture_input(
    text: str,
    *,
    area: str = SUPPLY_REQUEST_CAPTURE_CATEGORY,
    capture_id: str = "capture-public-493",
) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id=capture_id,
        source_kind="ops_dashboard_text",
        source_text=text,
        site_id="SANDBOX",
        site_label="Sandbox Site",
        submitter_person_id="person-public-493",
        captured_at="2026-07-22T09:15:00-04:00",
        area=area,
    )


def request_action(text: str):
    actions = RuleCaptureEngine()(capture_input(text)).extracted_actions or []
    assert len(actions) == 1
    return actions[0]


def recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for nested in value.values() for key in recursive_keys(nested)}
    if isinstance(value, list):
        return {key for nested in value for key in recursive_keys(nested)}
    return set()


def test_worker_category_contract_is_distinct_from_supply_request_namespaces() -> None:
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION == {
        "label": "Supply Request",
        "canonical": "supply_request_capture",
    }
    assert set(SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION) == {"label", "canonical"}
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION in BUILTIN_FALLBACK_CATEGORIES
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION not in OPERATOR_ONLY_CATEGORIES
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY != "supply_request"
    assert is_supply_request_capture_category("supply_request") is False
    assert is_supply_request_capture_category("supply_request_capture") is True

    # ios-1.3.1 decodes object categories by taking the first present supported key.
    decoded = next(
        str(SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION[key])
        for key in ("label", "name", "value", "canonical", "slug", "id", "key")
        if SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION.get(key)
    )
    assert decoded == "Supply Request"


def test_three_spoken_items_make_one_valid_ordered_request_draft() -> None:
    action = request_action("Need paper towels, hand soap, and trash liners.")

    assert action.job_type == JOB_CREATE_SUPPLY_REQUEST
    proposed = action.proposed_queue_job
    assert proposed is not None
    assert validate_job(proposed) is True
    payload = proposed["payload"]
    assert payload["items"] == [
        {"item_name": "paper towels"},
        {"item_name": "hand soap"},
        {"item_name": "trash liners"},
    ]
    assert payload["site_id"] == "SANDBOX"
    assert payload["requested_by"] == "person-public-493"
    assert payload["observed_at"] == "2026-07-22T09:15:00-04:00"
    assert payload["related_capture_ids"] == ["capture-public-493"]
    assert not recursive_keys(proposed) & {"price", "cost", "budget"}


def test_multi_sentence_and_pronoun_clauses_keep_every_item() -> None:
    for text in (
        "We need paper towels. Also need hand soap and trash liners.",
        "Need paper towels, hand soap, and we are out of trash liners.",
    ):
        action = request_action(text)
        assert action.proposed_queue_job["payload"]["items"] == [
            {"item_name": "paper towels"},
            {"item_name": "hand soap"},
            {"item_name": "trash liners"},
        ]


def test_request_stays_one_review_candidate_instead_of_splitting_per_item() -> None:
    action = request_action("Need paper towels, hand soap, and trash liners.")
    semantic = {
        "capture_id": "capture-public-493",
        "upload_id": "capture-public-493",
        "source_kind": "ops_dashboard_text",
        "site_id": "SANDBOX",
        "area": SUPPLY_REQUEST_CAPTURE_CATEGORY,
        "extracted_actions": [asdict(action)],
    }
    semantic = json.loads(json.dumps(semantic))

    candidates = structured_payloads_from_semantic(Path("capture-public-493.json"), semantic)

    assert len(candidates) == 1
    assert candidates[0]["status"] == "pending_review"
    assert candidates[0]["approval_metadata"]["proposed_queue_job"]["job_type"] == JOB_CREATE_SUPPLY_REQUEST


def test_duplicate_sounding_lines_are_preserved_in_spoken_order() -> None:
    action = request_action("Need paper towels, paper towels, and hand soap.")
    assert action.proposed_queue_job["payload"]["items"] == [
        {"item_name": "paper towels"},
        {"item_name": "paper towels"},
        {"item_name": "hand soap"},
    ]


def test_no_discernible_items_emits_no_empty_request_and_does_not_raise() -> None:
    for text in ("", "Everything is stocked.", "No supplies needed.", "Need supplies."):
        assert (RuleCaptureEngine()(capture_input(text)).extracted_actions or []) == []

    model = LocalModelCaptureEngine(FakeModelClient({"extracted_actions": []}))
    assert (model(capture_input("No supplies needed.")).extracted_actions or []) == []


def test_model_items_are_consolidated_without_dedup_and_capture_identity_wins() -> None:
    response = {
        "extracted_actions": [
            {
                "action_key": "request",
                "candidate_type": "field_capture_follow_up",
                "job_type": JOB_CREATE_SUPPLY_REQUEST,
                "target_type": "site",
                "target_label": "Wrong Site",
                "summary": "Three items.",
                "source_excerpt": "Need soap, liners, and soap.",
                "payload_fields": {
                    "site_id": "wrong-site",
                    "requested_by": "wrong-person",
                    "observed_at": "2099-01-01T00:00:00+00:00",
                    "related_capture_ids": ["wrong-capture"],
                    "items": [
                        {"item_name": "soap"},
                        {"item_name": "soap"},
                        {"item_name": "liners"},
                    ],
                },
            }
        ]
    }
    [action] = LocalModelCaptureEngine(FakeModelClient(response))(
        capture_input("Need soap, liners, and soap.")
    ).extracted_actions or []
    payload = action.proposed_queue_job["payload"]

    assert payload["items"] == [
        {"item_name": "soap"},
        {"item_name": "liners"},
        {"item_name": "soap"},
    ]
    assert payload["site_id"] == "SANDBOX"
    assert payload["requested_by"] == "person-public-493"
    assert payload["observed_at"] == "2026-07-22T09:15:00-04:00"
    assert payload["related_capture_ids"] == ["capture-public-493"]
    assert "request_id" not in payload
    assert validate_job(action.proposed_queue_job) is True


def test_richer_supported_model_parse_is_visible_at_review() -> None:
    text = "Need paper towels and hand soap."
    response = {
        "extracted_actions": [
            {
                "action_key": "request",
                "candidate_type": "field_capture_follow_up",
                "job_type": JOB_CREATE_SUPPLY_REQUEST,
                "target_type": "site",
                "target_label": "Sandbox Site",
                "summary": "Two items.",
                "source_excerpt": text,
                "payload_fields": {
                    "items": [
                        {"item_name": "paper"},
                        {"item_name": "towels"},
                        {"item_name": "hand soap"},
                    ]
                },
            }
        ]
    }
    [action] = LocalModelCaptureEngine(FakeModelClient(response))(capture_input(text)).extracted_actions or []

    assert [item["item_name"] for item in action.proposed_queue_job["payload"]["items"]] == [
        "paper",
        "towels",
        "hand soap",
    ]
    assert "extraction sources disagreed" in action.summary
    assert "Transcript parsing found 2 item(s)" in action.rationale

    semantic = json.loads(
        json.dumps(
            {
                "capture_id": "capture-public-493",
                "upload_id": "capture-public-493",
                "source_kind": "ops_dashboard_text",
                "site_id": "SANDBOX",
                "area": SUPPLY_REQUEST_CAPTURE_CATEGORY,
                "extracted_actions": [asdict(action)],
            }
        )
    )
    [candidate] = structured_payloads_from_semantic(Path("capture-public-493.json"), semantic)
    assert "extraction sources disagreed" in candidate["summary"]
    assert "Transcript parsing found 2 item(s)" in candidate["rationale"]


def test_explicit_operator_classification_overrides_new_lane() -> None:
    supply = request_action("supply need: squeegee")
    equipment = request_action("equipment: backpack vacuum")

    assert supply.job_type == JOB_LOG_SUPPLY_NEED
    assert supply.payload_fields == {"item_name": "squeegee"}
    assert validate_job(supply.proposed_queue_job) is True
    assert equipment.job_type == JOB_LOG_EQUIPMENT_REQUEST
    assert equipment.payload_fields == {"equipment_name": "backpack vacuum"}
    assert validate_job(equipment.proposed_queue_job) is True


def test_non_request_supply_category_keeps_legacy_single_item_job_shape() -> None:
    source = capture_input(
        "We're low on mop heads and neutralizer.",
        area="Supply Levels",
        capture_id="capture-legacy-supply",
    )
    [action] = RuleCaptureEngine()(source).extracted_actions or []

    assert action.job_type == JOB_LOG_SUPPLY_NEED
    assert action.payload_fields == {
        "item_name": "mop heads, neutralizer",
        "notes": "We're low on mop heads and neutralizer.",
    }
    assert action.proposed_queue_job == {
        "job_type": JOB_LOG_SUPPLY_NEED,
        "payload": {
            "site_id": "SANDBOX",
            "item_name": "mop heads, neutralizer",
            "requested_by": "person-public-493",
            "observed_at": "2026-07-22T09:15:00-04:00",
            "source": "ops_dashboard_text",
            "notes": "We're low on mop heads and neutralizer.",
            "related_capture_ids": ["capture-legacy-supply"],
        },
    }
    assert "items" not in action.proposed_queue_job["payload"]
    assert validate_job(action.proposed_queue_job) is True


def test_non_lane_model_request_action_stays_inert() -> None:
    text = "Need paper towels, hand soap, and trash liners."
    response = {
        "extracted_actions": [
            {
                "action_key": "create_supply_request",
                "candidate_type": "field_capture_follow_up",
                "job_type": JOB_CREATE_SUPPLY_REQUEST,
                "target_type": "site",
                "target_label": "Sandbox Site",
                "summary": "Supply request.",
                "source_excerpt": text,
                "payload_fields": {
                    "items": [
                        {"item_name": "paper towels"},
                        {"item_name": "hand soap"},
                        {"item_name": "trash liners"},
                    ]
                },
            }
        ]
    }
    for area in ("Restrooms", "Supply Levels"):
        actions = LocalModelCaptureEngine(FakeModelClient(response))(capture_input(text, area=area)).extracted_actions or []
        for action in actions:
            if action.job_type == JOB_CREATE_SUPPLY_REQUEST:
                assert action.proposed_queue_job is None
