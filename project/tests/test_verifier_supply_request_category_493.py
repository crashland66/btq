"""INDEPENDENT VERIFIER gate for prompt 493 — "Supply Request" capture category.

Written by the verifier, not the executor. Every fixture here is synthetic and
public-safe: site "SANDBOX" / "Sandbox Site", person "person-public-493",
generic janitorial consumables. No real people, sites, vendors, or prices.

Sections
  A. Category contract + namespace collision safety (incl. ios-1.3.1 decode)
  B. The Supply Request lane
  C. Regression: everything NOT in the lane
  D. Defect probes — these encode acceptance criteria the change does not meet
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from field_capture.action_candidates import (
    candidate_list_item,
    structured_payloads_from_semantic,
)
from field_capture.display_categories import (
    BUILTIN_FALLBACK_CATEGORIES,
    OPERATOR_ONLY_CANONICALS,
    OPERATOR_ONLY_CATEGORIES,
    OPERATOR_ONLY_LABELS,
    SUPPLY_REQUEST_CAPTURE_CATEGORY,
    SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION,
    SUPPLY_REQUEST_CAPTURE_CATEGORY_LABEL,
    apply_role_category_filter,
    canonicalize_qc_category,
    resolve_display_categories,
)
from field_capture.site_status_export import classify_review_item
from ops_dashboard.sections.candidates import render_candidate_card
from ops_dashboard.sections.inbox import candidate_group_line
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    LocalModelCaptureEngine,
    RuleCaptureEngine,
    clean_supply_item_phrase,
    create_supply_request_payload,
    is_supply_request_capture_category,
    split_supply_request_item_names,
    supply_item_name_from_text,
    supply_request_items_from_text,
)
from queue_spec import (
    CREATE_SUPPLY_REQUEST_ALLOWED_PAYLOAD_FIELDS,
    JOB_CREATE_SUPPLY_REQUEST,
    JOB_LOG_EQUIPMENT_REQUEST,
    JOB_LOG_SUPPLY_NEED,
    SUPPLY_REQUEST_ITEM_FIELDS,
    validate_job,
)

SITE_ID = "SANDBOX"
SITE_LABEL = "Sandbox Site"
PERSON_ID = "person-public-493"
CAPTURE_ID = "capture-public-493"
CAPTURED_AT = "2026-07-22T09:15:00-04:00"

# The existing review_type namespace string. Must never be the capture category.
EXISTING_REVIEW_TYPE = "supply_request"

FINANCIAL_KEY_FRAGMENTS = (
    "price",
    "cost",
    "budget",
    "sku",
    "vendor",
    "amount",
    "total",
    "subtotal",
    "tax",
    "dollar",
    "usd",
    "invoice",
    "spend",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class StubModelClient:
    """Deterministic stand-in for the local model client."""

    provider = "verifier"
    model = "public-fixture"

    def __init__(self, response: object) -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> object:
        self.prompts.append(prompt)
        return self.response


def capture(
    text: str,
    *,
    area: str = SUPPLY_REQUEST_CAPTURE_CATEGORY,
    capture_id: str = CAPTURE_ID,
    captured_at: str = CAPTURED_AT,
    source_kind: str = "ops_dashboard_text",
) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id=capture_id,
        source_kind=source_kind,
        source_text=text,
        site_id=SITE_ID,
        site_label=SITE_LABEL,
        submitter_person_id=PERSON_ID,
        captured_at=captured_at,
        area=area,
    )


def rule_actions(source: CaptureSemanticInput) -> list:
    return list(RuleCaptureEngine()(source).extracted_actions or [])


def model_actions(source: CaptureSemanticInput, response: object) -> list:
    engine = LocalModelCaptureEngine(StubModelClient(response))
    return list(engine(source).extracted_actions or [])


def sole_request(source: CaptureSemanticInput) -> dict:
    """Assert exactly one create_supply_request draft and return its queue job."""
    actions = rule_actions(source)
    assert len(actions) == 1, f"expected exactly one draft, got {[a.job_type for a in actions]}"
    action = actions[0]
    assert action.job_type == JOB_CREATE_SUPPLY_REQUEST
    proposed = action.proposed_queue_job
    assert isinstance(proposed, dict), f"no proposed queue job: {action.proposed_queue_job_error!r}"
    return proposed


def item_names(proposed: dict) -> list[str]:
    return [item["item_name"] for item in proposed["payload"]["items"]]


def all_keys(value: object) -> set[str]:
    """Every dict key anywhere in a nested structure."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            found.add(str(key))
            found |= all_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found |= all_keys(nested)
    return found


def model_supply_request_response(
    items: list[str],
    *,
    source_excerpt: str,
    **payload_overrides: object,
) -> dict:
    payload_fields: dict[str, object] = {"items": [{"item_name": name} for name in items]}
    payload_fields.update(payload_overrides)
    return {
        "extracted_actions": [
            {
                "action_key": "create_supply_request",
                "candidate_type": "field_capture_follow_up",
                "job_type": JOB_CREATE_SUPPLY_REQUEST,
                "target_type": "site",
                "target_label": SITE_LABEL,
                "summary": "Supply request.",
                "rationale": "Worker listed supplies.",
                "confidence": "normal",
                "source_excerpt": source_excerpt,
                "evidence_terms": ["supply"],
                "payload_fields": payload_fields,
            }
        ]
    }


# --- a faithful port of ios-1.3.1 BTQDisplayCategory.init(from:) -----------
# Verified against `git show ios-1.3.1:.../Models/BTQModels.swift` (lines 583-654).

_IOS_LABEL_KEYS = ("label", "name", "value", "canonical", "slug", "id", "key")
_IOS_VALUE_KEYS = ("canonical", "value", "slug", "id", "key", "name", "label")


class IOSDecodingError(Exception):
    pass


def ios_1_3_1_decode_category(raw: object) -> tuple[str, str]:
    """Return (value, label) exactly as the shipped App Store build would."""
    if isinstance(raw, str):
        trimmed = raw.strip()
        return trimmed, trimmed
    if not isinstance(raw, dict):
        raise IOSDecodingError("not a string or object")

    def first_present(keys: tuple[str, ...]) -> str | None:
        for key in keys:
            candidate = raw.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    label_candidate = first_present(_IOS_LABEL_KEYS)
    value_candidate = first_present(_IOS_VALUE_KEYS)
    resolved_value = value_candidate if value_candidate is not None else label_candidate
    if resolved_value is None:
        raise IOSDecodingError(
            "Display category requires a value, canonical, slug, id, key, name, or label."
        )
    return resolved_value, (label_candidate if label_candidate is not None else resolved_value)


# ==========================================================================
# A. Category contract + namespace collision safety
# ==========================================================================


def test_a1_definition_is_exactly_label_and_canonical() -> None:
    assert set(SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION) == {"label", "canonical"}
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION["label"] == "Supply Request"
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION["canonical"] == "supply_request_capture"
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY == "supply_request_capture"
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY_LABEL == "Supply Request"


def test_a2_every_served_category_object_has_exactly_two_keys() -> None:
    for entry in list(BUILTIN_FALLBACK_CATEGORIES) + list(OPERATOR_ONLY_CATEGORIES):
        assert set(entry) == {"label", "canonical"}, entry
        assert isinstance(entry["label"], str) and entry["label"].strip()
        assert isinstance(entry["canonical"], str) and entry["canonical"].strip()


def test_a3_category_is_worker_facing_not_operator_only() -> None:
    canonicals = [entry["canonical"] for entry in BUILTIN_FALLBACK_CATEGORIES]
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY in canonicals
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY not in OPERATOR_ONLY_CANONICALS
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY_LABEL not in OPERATOR_ONLY_LABELS
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION not in OPERATOR_ONLY_CATEGORIES


def test_a4_served_payload_for_a_worker_token_includes_the_category() -> None:
    # Mirrors unified_capture/server.py: resolve_display_categories -> apply_role_category_filter.
    served = apply_role_category_filter(resolve_display_categories(None, None), "capture")
    canonicals = [entry["canonical"] for entry in served]
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY in canonicals
    assert not (set(canonicals) & set(OPERATOR_ONLY_CANONICALS))
    for entry in served:
        assert set(entry) == {"label", "canonical"}, entry


def test_a5_served_payload_for_site_admin_keeps_the_two_key_contract() -> None:
    served = apply_role_category_filter(resolve_display_categories(None, None), "site_admin")
    canonicals = [entry["canonical"] for entry in served]
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY in canonicals
    assert set(OPERATOR_ONLY_CANONICALS) <= set(canonicals)
    for entry in served:
        assert set(entry) == {"label", "canonical"}, entry


def test_a6_ios_1_3_1_decodes_the_new_category() -> None:
    value, label = ios_1_3_1_decode_category(dict(SUPPLY_REQUEST_CAPTURE_CATEGORY_DEFINITION))
    assert value == "supply_request_capture"
    assert label == "Supply Request"


def test_a7_ios_1_3_1_decodes_a_full_site_payload_shaped_as_the_field_build_receives_it() -> None:
    site_payload = {
        "site_id": SITE_ID,
        "label": SITE_LABEL,
        "capture_guidance": "",
        "display_categories": apply_role_category_filter(
            resolve_display_categories(None, None), "capture"
        ),
    }
    # Round-trip through JSON: this is literally what the device gets over the wire.
    decoded = [
        ios_1_3_1_decode_category(entry)
        for entry in json.loads(json.dumps(site_payload))["display_categories"]
    ]
    assert (SUPPLY_REQUEST_CAPTURE_CATEGORY, SUPPLY_REQUEST_CAPTURE_CATEGORY_LABEL) in decoded
    # No entry may decode to an empty value/label, and none may throw.
    for value, label in decoded:
        assert value and label


def test_a8_ios_1_3_1_bare_string_form_still_decodes() -> None:
    assert ios_1_3_1_decode_category(SUPPLY_REQUEST_CAPTURE_CATEGORY) == (
        "supply_request_capture",
        "supply_request_capture",
    )
    with pytest.raises(IOSDecodingError):
        ios_1_3_1_decode_category({"unrelated": "x"})


def test_a9_capture_category_is_not_the_existing_supply_request_namespace() -> None:
    assert SUPPLY_REQUEST_CAPTURE_CATEGORY != EXISTING_REVIEW_TYPE
    assert EXISTING_REVIEW_TYPE not in {e["canonical"] for e in BUILTIN_FALLBACK_CATEGORIES}
    assert EXISTING_REVIEW_TYPE not in {e["canonical"] for e in OPERATOR_ONLY_CATEGORIES}
    # The bare review_type / canonical doc-type string must NOT open the lane.
    assert is_supply_request_capture_category(EXISTING_REVIEW_TYPE) is False
    assert is_supply_request_capture_category("supply_requests") is False
    assert is_supply_request_capture_category("supply-request") is False
    # The genuine category strings must.
    assert is_supply_request_capture_category(SUPPLY_REQUEST_CAPTURE_CATEGORY) is True
    assert is_supply_request_capture_category(SUPPLY_REQUEST_CAPTURE_CATEGORY_LABEL) is True
    assert is_supply_request_capture_category("  supply   request  ") is True


def test_a10_no_other_category_opens_the_lane() -> None:
    for entry in list(BUILTIN_FALLBACK_CATEGORIES) + list(OPERATOR_ONLY_CATEGORIES):
        if entry["canonical"] == SUPPLY_REQUEST_CAPTURE_CATEGORY:
            continue
        assert is_supply_request_capture_category(entry["canonical"]) is False, entry
        assert is_supply_request_capture_category(entry["label"]) is False, entry
    assert is_supply_request_capture_category("") is False


def test_a11_review_type_classifier_is_a_separate_namespace() -> None:
    # classify_review_item derives review_type from candidate TEXT, never from the
    # capture category, so the two namespaces cannot coerce into each other.
    assert classify_review_item({"summary": "Need paper towels"}) == EXISTING_REVIEW_TYPE
    assert classify_review_item({"summary": "Need paper towels"}) != SUPPLY_REQUEST_CAPTURE_CATEGORY
    # A capture whose only text is the new canonical string does not become the
    # review_type by way of the category name.
    assert classify_review_item({"summary": SUPPLY_REQUEST_CAPTURE_CATEGORY}) != (
        SUPPLY_REQUEST_CAPTURE_CATEGORY
    )


def test_a12_worker_can_submit_the_category_through_the_submit_gate() -> None:
    # unified_capture/server.py canonicalizes the submitted category then rejects
    # it when it is operator-only and the token is not site_admin.
    served = apply_role_category_filter(resolve_display_categories(None, None), "capture")
    for submitted in (SUPPLY_REQUEST_CAPTURE_CATEGORY, SUPPLY_REQUEST_CAPTURE_CATEGORY_LABEL):
        canonical = canonicalize_qc_category(submitted, served)
        assert canonical == SUPPLY_REQUEST_CAPTURE_CATEGORY
        assert canonical not in OPERATOR_ONLY_CANONICALS
        # And the canonicalized value is what actually opens the lane.
        assert is_supply_request_capture_category(canonical) is True


# ==========================================================================
# B. The Supply Request lane
# ==========================================================================


def test_b1_three_spoken_items_become_one_draft_with_three_ordered_line_items() -> None:
    proposed = sole_request(capture("Need trash liners, paper towels, and hand soap."))
    assert proposed["job_type"] == JOB_CREATE_SUPPLY_REQUEST
    # Spoken order, deliberately NOT alphabetical, so a sort() mutation is caught.
    assert item_names(proposed) == ["trash liners", "paper towels", "hand soap"]


def test_b2_the_draft_would_actually_enqueue() -> None:
    proposed = sole_request(capture("Need trash liners, paper towels, and hand soap."))
    assert validate_job(proposed) is True
    payload = proposed["payload"]
    assert payload["site_id"] == SITE_ID
    assert payload["requested_by"] == PERSON_ID
    assert payload["observed_at"] == CAPTURED_AT
    assert isinstance(payload["items"], list) and payload["items"]
    for item in payload["items"]:
        assert set(item) <= SUPPLY_REQUEST_ITEM_FIELDS
        assert isinstance(item["item_name"], str) and item["item_name"].strip()
    assert set(payload) <= CREATE_SUPPLY_REQUEST_ALLOWED_PAYLOAD_FIELDS


def test_b3_photos_attach_as_evidence_via_related_capture_ids() -> None:
    proposed = sole_request(
        capture("Need trash liners and hand soap.", capture_id="capture-public-evidence")
    )
    assert proposed["payload"]["related_capture_ids"] == ["capture-public-evidence"]


def test_b4_duplicate_sounding_lines_are_preserved() -> None:
    proposed = sole_request(capture("Need trash liners, trash liners, and hand soap."))
    assert item_names(proposed) == ["trash liners", "trash liners", "hand soap"]
    assert len(item_names(proposed)) == 3


def test_b5_no_discernible_items_produces_no_action_and_does_not_raise() -> None:
    for text in (
        "",
        "   ",
        "Everything is stocked.",
        "No supplies needed.",
        "Need supplies.",
        "Need some things.",
    ):
        assert rule_actions(capture(text)) == [], text
    # And through the model engine, with a model that also finds nothing.
    assert model_actions(capture("No supplies needed."), {"extracted_actions": []}) == []


def test_b6_an_empty_items_request_is_never_emitted() -> None:
    for text in ("Everything is stocked.", "Need supplies.", ""):
        for action in rule_actions(capture(text)):
            if action.job_type == JOB_CREATE_SUPPLY_REQUEST:
                pytest.fail(f"emitted a supply request for {text!r}")
        response = model_supply_request_response(["supplies"], source_excerpt=text or "note")
        for action in model_actions(capture(text), response):
            proposed = action.proposed_queue_job
            if isinstance(proposed, dict) and proposed["job_type"] == JOB_CREATE_SUPPLY_REQUEST:
                assert proposed["payload"]["items"], "empty-items request emitted"


def test_b7_observed_at_preserves_the_captures_own_utc_offset_verbatim() -> None:
    for captured_at in (
        "2026-07-22T09:15:00-04:00",
        "2026-01-15T23:45:00-05:00",
        "2026-07-22T09:15:00+00:00",
        "2026-07-22T09:15:00Z",
        "2026-12-31T18:00:00+09:30",
    ):
        proposed = sole_request(capture("Need trash liners.", captured_at=captured_at))
        assert proposed["payload"]["observed_at"] == captured_at, captured_at


def test_b8_model_output_cannot_override_capture_identity() -> None:
    text = "Need trash liners and hand soap."
    response = model_supply_request_response(
        ["trash liners", "hand soap"],
        source_excerpt=text,
        site_id="not-the-site",
        requested_by="not-the-person",
        observed_at="2099-01-01T00:00:00+00:00",
        related_capture_ids=["not-the-capture"],
        request_id="attacker-chosen-id",
    )
    actions = model_actions(capture(text), response)
    assert len(actions) == 1
    payload = actions[0].proposed_queue_job["payload"]
    assert payload["site_id"] == SITE_ID
    assert payload["requested_by"] == PERSON_ID
    assert payload["observed_at"] == CAPTURED_AT
    assert payload["related_capture_ids"] == [CAPTURE_ID]
    assert "request_id" not in payload


def test_b8b_the_payload_builder_itself_refuses_model_supplied_identity() -> None:
    """B8 only proves the lane throws model actions away. This pins the builder's
    own contract, which is also what the (leaky) non-lane path reaches."""
    source = capture("Need trash liners and hand soap.")
    hostile: dict[str, object] = {
        "items": [{"item_name": "trash liners"}, {"item_name": "hand soap"}],
        "site_id": "not-the-site",
        "requested_by": "not-the-person",
        "observed_at": "2099-01-01T00:00:00+00:00",
        "related_capture_ids": ["not-the-capture"],
        "request_id": "attacker-chosen-id",
        "notes": "model supplied note",
        "source": "model-supplied-source",
        "unit_price": 9.99,
        "estimated_cost": 42,
    }
    action = rule_actions(source)[0]
    payload = create_supply_request_payload(action, source, hostile)
    assert payload["site_id"] == SITE_ID
    assert payload["requested_by"] == PERSON_ID
    assert payload["observed_at"] == CAPTURED_AT
    assert payload["related_capture_ids"] == [CAPTURE_ID]
    assert "request_id" not in payload
    assert payload["notes"] == source.source_text
    assert payload["source"] == source.source_kind
    assert set(payload) <= CREATE_SUPPLY_REQUEST_ALLOWED_PAYLOAD_FIELDS
    keys = {k.lower() for k in all_keys(payload)}
    assert not {k for k in keys if any(f in k for f in FINANCIAL_KEY_FRAGMENTS)}
    assert validate_job({"job_type": JOB_CREATE_SUPPLY_REQUEST, "payload": payload}) is True


def test_b9_no_financial_data_anywhere_in_the_draft() -> None:
    proposed = sole_request(
        capture("Need trash liners, paper towels, and hand soap.")
    )
    keys = {key.lower() for key in all_keys(proposed)}
    leaked = {
        key
        for key in keys
        if any(fragment in key for fragment in FINANCIAL_KEY_FRAGMENTS)
    }
    assert leaked == set(), f"financial keys leaked into a worker-submitted draft: {leaked}"
    # Also guard the allowed-field surface the payload is filtered against.
    allowed = {field.lower() for field in CREATE_SUPPLY_REQUEST_ALLOWED_PAYLOAD_FIELDS}
    assert not {
        field for field in allowed if any(f in field for f in FINANCIAL_KEY_FRAGMENTS)
    }


def test_b10_lane_produces_a_draft_for_review_not_a_canonical_write() -> None:
    actions = rule_actions(capture("Need trash liners, paper towels, and hand soap."))
    semantic = json.loads(
        json.dumps(
            {
                "capture_id": CAPTURE_ID,
                "upload_id": CAPTURE_ID,
                "source_kind": "ops_dashboard_text",
                "site_id": SITE_ID,
                "area": SUPPLY_REQUEST_CAPTURE_CATEGORY,
                "extracted_actions": [asdict(action) for action in actions],
            },
            default=str,
        )
    )
    candidates = structured_payloads_from_semantic(Path(f"{CAPTURE_ID}.json"), semantic)
    assert len(candidates) == 1, "one capture must yield one review candidate"
    candidate = candidates[0]
    assert candidate["status"] == "pending_review"
    assert candidate["status"] != "approved"
    assert (
        candidate["approval_metadata"]["proposed_queue_job"]["job_type"]
        == JOB_CREATE_SUPPLY_REQUEST
    )
    # A proposal only: nothing marks it auto-approved or already written.
    assert not candidate.get("approved_at")
    assert not candidate.get("canonical_write")


def test_b11_explicit_operator_supply_declaration_still_wins_inside_the_lane() -> None:
    actions = rule_actions(capture("supply need: squeegee"))
    assert len(actions) == 1
    action = actions[0]
    assert action.job_type == JOB_LOG_SUPPLY_NEED
    assert action.proposed_queue_job["job_type"] == JOB_LOG_SUPPLY_NEED
    assert action.proposed_queue_job["payload"]["item_name"] == "squeegee"
    assert "items" not in action.proposed_queue_job["payload"]
    assert validate_job(action.proposed_queue_job) is True


def test_b12_explicit_operator_equipment_declaration_still_wins_inside_the_lane() -> None:
    actions = rule_actions(capture("equipment: backpack vacuum"))
    assert len(actions) == 1
    action = actions[0]
    assert action.job_type == JOB_LOG_EQUIPMENT_REQUEST
    assert action.proposed_queue_job["payload"]["equipment_name"] == "backpack vacuum"
    assert validate_job(action.proposed_queue_job) is True


def test_b13_lane_opens_from_the_label_form_too() -> None:
    # The PWA may post the human label rather than the canonical.
    proposed = sole_request(
        capture(
            "Need trash liners, paper towels, and hand soap.",
            area=SUPPLY_REQUEST_CAPTURE_CATEGORY_LABEL,
        )
    )
    assert item_names(proposed) == ["trash liners", "paper towels", "hand soap"]


def test_b14_the_model_is_only_offered_create_supply_request_inside_the_lane() -> None:
    client = StubModelClient({"extracted_actions": []})
    LocalModelCaptureEngine(client)(capture("Need trash liners."))
    assert any(JOB_CREATE_SUPPLY_REQUEST in prompt for prompt in client.prompts)

    other = StubModelClient({"extracted_actions": []})
    LocalModelCaptureEngine(other)(capture("Need trash liners.", area="Supply Levels"))
    assert all(JOB_CREATE_SUPPLY_REQUEST not in prompt for prompt in other.prompts)


# ==========================================================================
# C. Regression — the highest-risk part of this change
# ==========================================================================

NON_LANE_AREAS = (
    "Supply Levels",
    "Restrooms",
    "Janitorial Closet",
    "Chemicals / Safety / PPE / SDS",
    "Break Rooms / Kitchens / Cafes",
    "Other",
    "report_an_issue",
    "",
    EXISTING_REVIEW_TYPE,  # the *review_type* string must not open the lane
    "Supply",
)

LEGACY_SUPPLY_JOB = {
    "job_type": JOB_LOG_SUPPLY_NEED,
    "payload": {
        "site_id": SITE_ID,
        "item_name": "mop heads, neutralizer",
        "requested_by": PERSON_ID,
        "observed_at": CAPTURED_AT,
        "source": "ops_dashboard_text",
        "notes": "We're low on mop heads and neutralizer.",
        "related_capture_ids": [CAPTURE_ID],
    },
}


def test_c1_log_supply_need_is_live_unchanged_and_single_item() -> None:
    source = capture("We're low on mop heads and neutralizer.", area="Supply Levels")
    actions = rule_actions(source)
    assert len(actions) == 1
    action = actions[0]
    assert action.job_type == JOB_LOG_SUPPLY_NEED
    # Byte-for-byte the pre-493 shape: one item_name string, no items list.
    assert action.proposed_queue_job == LEGACY_SUPPLY_JOB
    assert "items" not in action.proposed_queue_job["payload"]
    assert validate_job(action.proposed_queue_job) is True


def test_c2_no_non_lane_area_ever_emits_a_supply_request_draft() -> None:
    texts = (
        "We're low on mop heads and neutralizer.",
        "Need trash liners, paper towels, and hand soap.",
        "Out of trash liners.",
        "Please order paper towels and hand soap, thanks.",
        "There is a leak under the sink in the restroom.",
        "Everything is stocked.",
    )
    for area in NON_LANE_AREAS:
        for text in texts:
            for source_kind in ("ops_dashboard_text", "voice_memo", "field_capture_audio"):
                source = capture(text, area=area, source_kind=source_kind)
                for action in rule_actions(source):
                    assert action.job_type != JOB_CREATE_SUPPLY_REQUEST, (area, text, source_kind)


def test_c3_the_existing_review_type_string_as_area_keeps_legacy_routing() -> None:
    # area == "supply_request" is the pre-existing review_type namespace. It still
    # contains "supply", so it must fall through to the LEGACY single-item lane.
    source = capture("We're low on mop heads and neutralizer.", area=EXISTING_REVIEW_TYPE)
    actions = rule_actions(source)
    assert len(actions) == 1
    assert actions[0].job_type == JOB_LOG_SUPPLY_NEED
    assert "items" not in actions[0].proposed_queue_job["payload"]


def test_c4_non_lane_supply_area_still_suppresses_needless_supply_drafts() -> None:
    # Pre-493 behavior for a supply area with no expressed need: no supply job.
    source = capture("The mop bucket walked off again.", area="Supply Levels")
    for action in rule_actions(source):
        assert action.job_type not in {JOB_LOG_SUPPLY_NEED, JOB_CREATE_SUPPLY_REQUEST}


def test_c5_operator_declarations_outside_the_lane_route_exactly_as_before() -> None:
    """Pinned against a checkout of base fcea4b5 — these are the pre-493 outputs,
    warts and all. The new lane must not have perturbed them."""
    baseline = {
        ("Janitorial Closet", "equipment: backpack vacuum"): ("", None),
        ("Janitorial Closet", "supply need: squeegee"): ("", None),
        ("Supply Levels", "equipment: backpack vacuum"): (
            JOB_LOG_SUPPLY_NEED,
            {
                "job_type": JOB_LOG_SUPPLY_NEED,
                "payload": {
                    "item_name": "Equipment: backpack vacuum",
                    "notes": "Equipment: backpack vacuum.",
                    "site_id": SITE_ID,
                    "requested_by": PERSON_ID,
                    "source": "ops_dashboard_text",
                    "related_capture_ids": [CAPTURE_ID],
                    "observed_at": CAPTURED_AT,
                },
            },
        ),
        ("Supply Levels", "supply need: squeegee"): (
            JOB_LOG_SUPPLY_NEED,
            {
                "job_type": JOB_LOG_SUPPLY_NEED,
                "payload": {
                    "item_name": "Supply need: squeegee",
                    "notes": "Supply need: squeegee.",
                    "site_id": SITE_ID,
                    "requested_by": PERSON_ID,
                    "source": "ops_dashboard_text",
                    "related_capture_ids": [CAPTURE_ID],
                    "observed_at": CAPTURED_AT,
                },
            },
        ),
    }
    for (area, text), expected in baseline.items():
        actions = rule_actions(capture(text, area=area))
        assert len(actions) == 1, (area, text)
        assert (actions[0].job_type, actions[0].proposed_queue_job) == expected, (area, text)


# ==========================================================================
# D. Defect probes
#
# Each of these encodes an explicit acceptance criterion of prompt 493.
# They are expected to FAIL against the change under verification.
# ==========================================================================


def test_d1_multi_sentence_transcript_keeps_every_spoken_item() -> None:
    """DEFECT: a natural multi-sentence voice note loses everything after the
    first sentence. `supply_request_items_from_text` reuses the single-item
    extractor `supply_item_name_from_text`, whose `_truncate_supply_sentence_tail`
    deliberately cuts at the first sentence boundary."""
    proposed = sole_request(
        capture("We need paper towels. Also need hand soap and trash liners.")
    )
    assert item_names(proposed) == ["paper towels", "hand soap", "trash liners"]


def test_d2_a_clause_with_a_pronoun_does_not_truncate_the_item_list() -> None:
    """DEFECT: `_strip_supply_commentary_tail` drops everything from the first
    'we'/'i'/'so'/'because' onward, silently discarding later spoken items."""
    proposed = sole_request(
        capture("Need paper towels, hand soap, and we are out of trash liners.")
    )
    assert item_names(proposed) == ["paper towels", "hand soap", "trash liners"]


def test_d3_model_items_are_used_when_the_transcript_parser_under_extracts() -> None:
    """DEFECT: the transcript path wins as soon as it yields ONE item, so a
    correct 3-item model extraction is thrown away in favour of a 1-item parse."""
    text = "We need paper towels. Also need hand soap and trash liners."
    actions = model_actions(
        capture(text),
        model_supply_request_response(
            ["paper towels", "hand soap", "trash liners"], source_excerpt=text
        ),
    )
    assert len(actions) == 1
    assert item_names(actions[0].proposed_queue_job) == [
        "paper towels",
        "hand soap",
        "trash liners",
    ]


def test_d4_a_non_lane_capture_never_gains_a_create_supply_request_queue_job() -> None:
    """DEFECT: JOB_CREATE_SUPPLY_REQUEST was added to OPERATOR_PROPOSED_JOB_TYPES
    unconditionally, so a model-emitted create_supply_request on a capture in ANY
    other category now builds an enqueueable job where pre-493 it was inert
    (proposed_queue_job is None). On area='Supply Levels' this yields TWO drafts
    for one capture — the exact duplication 493 exists to eliminate."""
    text = "Need trash liners, paper towels, and hand soap."
    response = model_supply_request_response(
        ["trash liners", "paper towels", "hand soap"], source_excerpt=text
    )
    for area in ("Restrooms", "Supply Levels"):
        for action in model_actions(capture(text, area=area), response):
            if action.job_type == JOB_CREATE_SUPPLY_REQUEST:
                assert action.proposed_queue_job is None, (
                    f"area={area!r} gained an enqueueable create_supply_request job"
                )


# ==========================================================================
# E. Transcript/model reconciliation + the operator-visible disagreement
#    warning. Added in round 2: this behavior is new, is not specified by
#    prompt 493, and was entirely ungated.
# ==========================================================================

WARNING_FRAGMENT = "extraction sources disagreed on the item count"
THREE_ITEM_TEXT = "Need trash liners, paper towels, and hand soap."
THREE_ITEMS = ["trash liners", "paper towels", "hand soap"]


def lane_request_action(source: CaptureSemanticInput, response: object):
    """The single create_supply_request action from the model engine."""
    matches = [
        action
        for action in model_actions(source, response)
        if action.job_type == JOB_CREATE_SUPPLY_REQUEST
    ]
    assert len(matches) == 1, f"expected one request, got {len(matches)}"
    return matches[0]


def review_candidate(action) -> dict:
    semantic = json.loads(
        json.dumps(
            {
                "capture_id": CAPTURE_ID,
                "upload_id": CAPTURE_ID,
                "source_kind": "ops_dashboard_text",
                "site_id": SITE_ID,
                "area": SUPPLY_REQUEST_CAPTURE_CATEGORY,
                "extracted_actions": [asdict(action)],
            },
            default=str,
        )
    )
    candidates = structured_payloads_from_semantic(Path(f"{CAPTURE_ID}.json"), semantic)
    assert len(candidates) == 1
    return candidates[0]


def test_e1_warning_appears_when_transcript_and_model_item_counts_disagree() -> None:
    action = lane_request_action(
        capture(THREE_ITEM_TEXT),
        model_supply_request_response(
            ["trash liners", "paper towels"], source_excerpt=THREE_ITEM_TEXT
        ),
    )
    assert WARNING_FRAGMENT in action.summary
    assert "Verify all line items" in action.summary
    # The rationale must show its work: both counts, so the operator can judge.
    assert "3 item(s)" in action.rationale
    assert "2 transcript-supported item(s)" in action.rationale


def test_e2_no_warning_when_the_two_sources_agree() -> None:
    action = lane_request_action(
        capture(THREE_ITEM_TEXT),
        model_supply_request_response(THREE_ITEMS, source_excerpt=THREE_ITEM_TEXT),
    )
    assert WARNING_FRAGMENT not in action.summary
    assert "Verify all line items" not in action.summary
    assert action.summary == "Supply request with 3 line items."
    assert action.rationale == "Worker selected the dedicated Supply Request capture category."


def test_e3_no_warning_on_the_rule_path_where_no_model_participated() -> None:
    # The rule engine has no model extraction to disagree with; warning here
    # would be permanent noise on every deterministic capture.
    for text in (THREE_ITEM_TEXT, "Need trash liners.", "Out of hand soap."):
        for action in rule_actions(capture(text)):
            assert WARNING_FRAGMENT not in action.summary, text
            assert "Verify all line items" not in action.summary, text


def test_e4_the_warning_reaches_the_operator_review_candidate() -> None:
    """Requirement: the warning must reach the review surface, not only a log."""
    action = lane_request_action(
        capture(THREE_ITEM_TEXT),
        model_supply_request_response(
            ["trash liners", "paper towels"], source_excerpt=THREE_ITEM_TEXT
        ),
    )
    candidate = review_candidate(action)
    assert WARNING_FRAGMENT in candidate["summary"]
    assert "3 item(s)" in candidate["rationale"]
    assert candidate["status"] == "pending_review"


def test_e5_the_warning_renders_on_the_surface_the_operator_approves_from() -> None:
    """Not merely present in the doc — actually rendered in the approve card and
    carried by the review-list projection. A log line would not satisfy this."""
    action = lane_request_action(
        capture(THREE_ITEM_TEXT),
        model_supply_request_response(
            ["trash liners", "paper towels"], source_excerpt=THREE_ITEM_TEXT
        ),
    )
    candidate = review_candidate(action)

    # 1) the review-list projection keeps summary and rationale verbatim
    listed = candidate_list_item(Path(f"{CAPTURE_ID}.json"), candidate)
    assert WARNING_FRAGMENT in listed["summary"]
    assert "3 item(s)" in listed["rationale"]

    # 2) the grouped approve-set checklist line shows the summary
    group_line = candidate_group_line(
        {
            "candidate_id": candidate["candidate_id"],
            "draft_id": candidate["candidate_id"],
            "summary": candidate["summary"],
            "source_text": candidate["source_text"],
            "job_type": JOB_CREATE_SUPPLY_REQUEST,
        }
    )
    assert WARNING_FRAGMENT in group_line["summary"]

    # 3) the actual approve-decision HTML
    card = {
        "candidate_id": candidate["candidate_id"],
        "draft_id": candidate["candidate_id"],
        "status": "pending_approval",
        "_rev": "1-verifier",
        "summary": candidate["summary"],
        "rationale": candidate["rationale"],
        "source_text": candidate["source_text"],
        "confidence": candidate["confidence"],
        "candidate_type": candidate["candidate_type"],
        "site_id": SITE_ID,
        "area": SUPPLY_REQUEST_CAPTURE_CATEGORY,
        "proposed_job_type": JOB_CREATE_SUPPLY_REQUEST,
        "proposed_payload": candidate["approval_metadata"]["proposed_queue_job"]["payload"],
        "review_history": [],
        "vision_items": [],
        "client_notification": {},
        "resolution": {},
        "archived": False,
        "visit_proposed": False,
        "source_is_human": True,
        "artifact_path": f"{CAPTURE_ID}.json",
    }
    for key in (
        "archived_at", "archived_by", "audio_asset_id", "capture_id", "captured_at",
        "proposed_job_error", "review_rationale", "reviewed_at", "reviewer",
        "semantic_artifact_path", "source_context", "source_missing_message",
        "source_transcript_path", "submitter_id", "submitter_name", "visit_type",
    ):
        card.setdefault(key, "")
    html = render_candidate_card(card)
    assert WARNING_FRAGMENT in html, "warning is invisible on the approve card"
    assert "3 item(s)" in html, "count breakdown is invisible on the approve card"


def test_e6_the_warning_is_advisory_and_does_not_degrade_the_draft() -> None:
    action = lane_request_action(
        capture(THREE_ITEM_TEXT),
        model_supply_request_response(
            ["trash liners", "paper towels"], source_excerpt=THREE_ITEM_TEXT
        ),
    )
    proposed = action.proposed_queue_job
    assert validate_job(proposed) is True
    # The richer (transcript) list is what ships, not the thinner model list.
    assert item_names(proposed) == THREE_ITEMS
    candidate = review_candidate(action)
    assert candidate["status"] == "pending_review"
    assert not candidate.get("approved_at")


# A phrasing the deterministic parser genuinely under-extracts (it has no
# separator the splitter recognises), so the model-wins branch of the
# reconciliation is actually exercised rather than passing vacuously.
UNDER_EXTRACTED_TEXT = "Need paper towels/hand soap/trash liners."


def test_e7_reconciliation_takes_the_richer_supported_set_in_both_directions() -> None:
    """The D1/D3 regression guard: a thin parse must never veto a richer one."""
    # Precondition: the transcript parser really does under-extract here.
    assert len(supply_request_items_from_text(UNDER_EXTRACTED_TEXT)) == 1

    # transcript thin (1) vs model rich (3) -> model wins
    action = lane_request_action(
        capture(UNDER_EXTRACTED_TEXT),
        model_supply_request_response(
            ["paper towels", "hand soap", "trash liners"],
            source_excerpt=UNDER_EXTRACTED_TEXT,
        ),
    )
    assert item_names(action.proposed_queue_job) == [
        "paper towels", "hand soap", "trash liners",
    ]

    # transcript rich (3) vs model thin (1) -> transcript wins
    action = lane_request_action(
        capture(THREE_ITEM_TEXT),
        model_supply_request_response(["trash liners"], source_excerpt=THREE_ITEM_TEXT),
    )
    assert item_names(action.proposed_queue_job) == THREE_ITEMS


def test_e8_reconciliation_never_lets_the_model_invent_an_unspoken_item() -> None:
    action = lane_request_action(
        capture(THREE_ITEM_TEXT),
        model_supply_request_response(
            THREE_ITEMS + ["floor wax", "buffer pads"], source_excerpt=THREE_ITEM_TEXT
        ),
    )
    names = item_names(action.proposed_queue_job)
    assert names == THREE_ITEMS
    assert "floor wax" not in names
    assert "buffer pads" not in names


def test_e9_model_items_are_normalized_into_spoken_order() -> None:
    # Must use a text the parser under-extracts, otherwise the transcript list
    # wins outright and this asserts nothing about the model's ordering.
    assert len(supply_request_items_from_text(UNDER_EXTRACTED_TEXT)) == 1
    action = lane_request_action(
        capture(UNDER_EXTRACTED_TEXT),
        model_supply_request_response(
            ["trash liners", "paper towels", "hand soap"],
            source_excerpt=UNDER_EXTRACTED_TEXT,
        ),
    )
    # Model returned them out of order; transcript position is authoritative.
    assert item_names(action.proposed_queue_job) == [
        "paper towels", "hand soap", "trash liners",
    ]


# ==========================================================================
# F. The rewritten multi-item transcript parser (highest-risk new code)
# ==========================================================================


def test_f1_parser_splits_across_sentences_commas_and_coordinators() -> None:
    cases = {
        "We need paper towels. Also need hand soap and trash liners.":
            ["paper towels", "hand soap", "trash liners"],
        "Need paper towels, hand soap, and we are out of trash liners.":
            ["paper towels", "hand soap", "trash liners"],
        "Need trash liners; paper towels; hand soap":
            ["trash liners", "paper towels", "hand soap"],
        "paper towels\nhand soap\ntrash liners":
            ["paper towels", "hand soap", "trash liners"],
        "Need paper towels plus hand soap.":
            ["paper towels", "hand soap"],
        "We need paper towels... also hand soap!!! And trash liners?":
            ["paper towels", "hand soap", "trash liners"],
        "Need paper towels and hand soap and trash liners and glass cleaner and mop heads.":
            ["paper towels", "hand soap", "trash liners", "glass cleaner", "mop heads"],
    }
    for text, expected in cases.items():
        assert split_supply_request_item_names(text) == expected, text
        assert item_names(sole_request(capture(text))) == expected, text


def test_f2_the_final_spoken_item_is_never_dropped() -> None:
    for text, last in (
        ("Need trash liners, paper towels, and hand soap.", "hand soap"),
        ("Need paper towels. Need hand soap. Need mop heads.", "mop heads"),
        ("paper towels\nhand soap\nglass cleaner", "glass cleaner"),
        ("Need trash liners and glass cleaner", "glass cleaner"),
    ):
        names = item_names(sole_request(capture(text)))
        assert names[-1] == last, (text, names)


def test_f3_parser_neither_dedupes_nor_reorders() -> None:
    text = "Need trash liners, paper towels, trash liners, and hand soap."
    names = item_names(sole_request(capture(text)))
    assert names == ["trash liners", "paper towels", "trash liners", "hand soap"]
    assert len(names) == 4


def test_f4_quantities_and_units_stay_attached_to_their_item() -> None:
    names = item_names(
        sole_request(capture("Need 3 cases of paper towels and 2 gallons of floor cleaner."))
    )
    assert names == ["3 cases of paper towels", "2 gallons of floor cleaner"]


def test_f5_single_item_helpers_are_untouched_by_the_new_parser() -> None:
    """The legacy single-item extractor must keep its old semantics — it still
    backs log_supply_need, whose payload c1 pins byte-for-byte."""
    assert supply_item_name_from_text("We're low on mop heads and neutralizer.") == (
        "mop heads, neutralizer"
    )
    assert clean_supply_item_phrase("Need paper towels and hand soap") == (
        "paper towels, hand soap"
    )
    # And the sentence-truncation behavior the lane deliberately no longer uses.
    assert supply_item_name_from_text("Need paper towels. Also need hand soap.") == (
        "paper towels"
    )


def test_f6_degenerate_and_negative_notes_still_produce_nothing() -> None:
    for text in (
        "", "   ", "Everything is stocked.", "No supplies needed.", "Need supplies.",
        "Need some things.", "Thanks!", "Hello.", "Nothing is needed.",
        "We don't need anything.",
    ):
        assert rule_actions(capture(text)) == [], text


# ==========================================================================
# G. Round-2 defect probes — real, out-of-scope-for-493, non-silent.
#    strict xfail: they document current behavior and will fail loudly
#    (as unexpectedly-passing) once fixed, prompting removal.
# ==========================================================================


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT G1: in the lane, list SHAPE alone (a comma) is a sufficient "
    "gate, so non-supply speech is fabricated into supply line items.",
)
def test_g1_non_supply_speech_in_the_lane_does_not_fabricate_line_items() -> None:
    for text in (
        "The floor was wet, someone slipped.",
        "Met with the client, everything looked fine.",
        "The vacuum broke, I put it in the closet.",
    ):
        assert rule_actions(capture(text)) == [], text


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT G2: the sentence split fires on abbreviation periods, "
    "shattering one item into a fabricated fragment pair.",
)
def test_g2_abbreviation_periods_do_not_shatter_an_item() -> None:
    assert split_supply_request_item_names("Need 5 gal. of floor stripper.") == [
        "5 gal. of floor stripper"
    ]
    assert split_supply_request_item_names("Need 12 oz. bottles of hand soap.") == [
        "12 oz. bottles of hand soap"
    ]


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT G3: ' and ' is always a separator, so a product whose name "
    "contains a conjunction is split into two line items. Inherent ambiguity — "
    "unfixable without a product lexicon; over-splitting is at least visible.",
)
def test_g3_a_conjunction_inside_one_product_name_does_not_split_it() -> None:
    assert split_supply_request_item_names("Need a mop and bucket set.") == [
        "mop and bucket set"
    ]
