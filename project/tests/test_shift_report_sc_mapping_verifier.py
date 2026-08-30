"""Independent acceptance tests for quits/terminations SafetyCulture mapping."""

from __future__ import annotations

import pytest

from btq_cli import shift_report_sc_mapping as mapping


QUITS_FIELD = "Voluntary / Involuntary Quits or Terminations Today"
QUITS_CHOICE_ID = "0eff9059-bf4a-4160-9f80-f717f94cdf7d"
QUITS_DETAIL_ID = "5d8a8e88-74c2-4e05-a1b4-23765bd40d32"
QUITS_YES_ID = "be5d6d1e-d779-41d8-abae-0966eff42ce0"
QUITS_NO_ID = "8a62fda4-3146-4af2-9bc5-43d4a608892f"


@pytest.fixture(autouse=True)
def _synthetic_prepared_by(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mapping, "PREPARED_BY", "Synthetic Operator")


def _item(payload: dict, item_id: str) -> dict | None:
    return next((item for item in payload["items"] if item["item_id"] == item_id), None)


def _choice_id(payload: dict) -> str:
    item = _item(payload, QUITS_CHOICE_ID)
    assert item is not None
    return item["responses"]["selected"][0]["id"]


def test_positive_standalone_yes_keeps_choice_and_emits_detail_without_marker() -> None:
    payload = mapping.build_prefill_payload(
        {QUITS_FIELD: "Yes\nSynthetic evening assignment ended after a documented handoff."},
        "2026-02-03",
    )

    assert _choice_id(payload) == QUITS_YES_ID
    assert _item(payload, QUITS_DETAIL_ID) == {
        "item_id": QUITS_DETAIL_ID,
        "type": "text",
        "responses": {"text": "Synthetic evening assignment ended after a documented handoff."},
    }


def test_markdown_field_flows_through_parser_to_detail_item() -> None:
    sections = mapping.parse_shift_report(
        "**Voluntary / Involuntary Quits or Terminations Today:** Yes\n"
        "Synthetic overnight assignment ended after review."
    )
    payload = mapping.build_prefill_payload(sections, "2026-02-03")

    assert _choice_id(payload) == QUITS_YES_ID
    assert _item(payload, QUITS_DETAIL_ID)["responses"]["text"] == (
        "Synthetic overnight assignment ended after review."
    )


@pytest.mark.parametrize("body", ["", "No", "None noted.", "N/A", "Yes", "Yes:"])
def test_non_detail_inputs_omit_detail_item(body: str) -> None:
    payload = mapping.build_prefill_payload({QUITS_FIELD: body}, "2026-02-03")

    assert _item(payload, QUITS_DETAIL_ID) is None
    assert _choice_id(payload) == (QUITS_YES_ID if body.lower().startswith("yes") else QUITS_NO_ID)


def test_direct_substantive_detail_is_preserved_exactly() -> None:
    detail = "Synthetic contractor assignment concluded.\nDocumentation retained verbatim."
    payload = mapping.build_prefill_payload({QUITS_FIELD: detail}, "2026-02-03")

    assert _choice_id(payload) == QUITS_YES_ID
    assert _item(payload, QUITS_DETAIL_ID)["responses"]["text"] == detail


@pytest.mark.parametrize(
    "detail",
    [
        "No-call/no-show led to the end of a synthetic assignment.",
        "No-show led to the end of a synthetic assignment.",
    ],
)
def test_direct_no_show_detail_is_not_mistaken_for_a_negative_marker(detail: str) -> None:
    payload = mapping.build_prefill_payload({QUITS_FIELD: detail}, "2026-02-03")

    assert _choice_id(payload) == QUITS_YES_ID
    assert _item(payload, QUITS_DETAIL_ID)["responses"]["text"] == detail


def test_full_payload_is_unchanged_except_detail_inserted_after_quits_choice() -> None:
    sections = {
        "Nightly Summary": "Synthetic summary.",
        "eHub / WinTeam Alert Updates Needed": "Synthetic alert.",
        "Over-Hours Accounts": "Synthetic over-hours note.",
        "Accounts Visited for QC / Issue Ticket Follow-Up (how many)": "2 - synthetic visits",
        "Accounts Completed QC In": "Synthetic Site Alpha.",
        "Team Members Visited (Safety, QC, Training) (how many)": "3 synthetic visits",
        "Employees Visited, Trained, Etc. (Who and Account)": "Employee Z at Site Alpha.",
        "Team Member Connection (First and Last Name)": "Synthetic Person.",
        "Accounts Worked Strictly for Cleaning Fill-In (how many)": "1 synthetic fill-in",
        "Customer Interactions (In-Person, Who and Account)": "Customer Z at Site Beta.",
        "Open Positions (Full Area)": "One synthetic opening.",
        "Open Positions — Account, Days, Hours, Pay": "Site Gamma; weekdays; synthetic hours/pay.",
        "Interviews Conducted or Hires Made Today": "Yes - synthetic interview completed.",
        QUITS_FIELD: "Yes\nSynthetic role ended; handoff documented.",
        "Excused / Approved / Unexcused Missed Shifts": "Synthetic approved absence.",
    }

    assert mapping.build_prefill_payload(sections, "2026-02-03") == {
        "template_id": "template_cbea032fa8414d7f81c107d75758ad67",
        "header_items": [
            {
                "item_id": "f3245d43-ea77-11e1-aff1-0800200c9a66",
                "type": "text",
                "responses": {"text": "Synthetic Operator"},
            },
            {
                "item_id": "5fa172c2-ab90-4426-9565-b9a1a956ccf9",
                "type": "datetime",
                "responses": {"datetime": "2026-02-03T12:00:00Z"},
            },
            {
                "item_id": "b796be8c-4981-4404-ac38-927be1ba8375",
                "type": "text",
                "responses": {"text": "Synthetic summary."},
            },
        ],
        "items": [
            {
                "item_id": "76be27fd-7665-42e6-a03b-bf6b0d81ff53",
                "type": "text",
                "responses": {"text": "Synthetic alert."},
            },
            {
                "item_id": "45e184eb-7a78-43fc-abec-94ac62456a64",
                "type": "text",
                "responses": {"text": "Synthetic over-hours note."},
            },
            {
                "item_id": "d493e3ee-e331-43b7-9b9c-f4e7f6f449ce",
                "type": "question",
                "responses": {"selected": [{"id": "d573660b-553e-428e-97da-aa7e0a12e238"}]},
            },
            {
                "item_id": "15c7a502-7c66-4369-b9c1-4c615281b33c",
                "type": "text",
                "responses": {"text": "Synthetic Site Alpha."},
            },
            {
                "item_id": "2d95f9e6-61f5-471d-84e6-913758cf5dd0",
                "type": "question",
                "responses": {"selected": [{"id": "9c01128b-a444-44d5-a5b2-0c01dbb83023"}]},
            },
            {
                "item_id": "8ddf707e-f811-46ad-ab3e-33535b8d1873",
                "type": "question",
                "responses": {"text": "Employee Z at Site Alpha."},
            },
            {
                "item_id": "b4cf664a-5c86-408d-8696-a7ca6f3da844",
                "type": "text",
                "responses": {"text": "Synthetic Person."},
            },
            {
                "item_id": "77fcb7f3-8bdc-4b7b-854c-1e7ded415831",
                "type": "question",
                "responses": {"selected": [{"id": "a4218146-45f0-4e82-96a2-0bcfe1244b6f"}]},
            },
            {
                "item_id": "fa04b2a8-4ed8-4660-9181-3e219ac4292b",
                "type": "question",
                "responses": {"selected": [{"id": "060ace00-1f61-468e-9c42-918fa93badcf"}]},
            },
            {
                "item_id": "5454f983-510b-4de9-baa6-612ee3a39c88",
                "type": "text",
                "responses": {"text": "Customer Z at Site Beta."},
            },
            {
                "item_id": "867d2e67-6ecc-4f28-b7b4-d02fb61f8122",
                "type": "text",
                "responses": {"text": "One synthetic opening."},
            },
            {
                "item_id": "309cbb25-106a-47fa-b1eb-4d2fb32e411b",
                "type": "text",
                "responses": {"text": "Site Gamma; weekdays; synthetic hours/pay."},
            },
            {
                "item_id": "5bf7cdfd-76f3-480b-bbb8-2cc0e95c8e2d",
                "type": "question",
                "responses": {"selected": [{"id": "060ace00-1f61-468e-9c42-918fa93badcf"}]},
            },
            {
                "item_id": QUITS_CHOICE_ID,
                "type": "question",
                "responses": {"selected": [{"id": QUITS_YES_ID}]},
            },
            {
                "item_id": QUITS_DETAIL_ID,
                "type": "text",
                "responses": {"text": "Synthetic role ended; handoff documented."},
            },
            {
                "item_id": "fa3c326d-650c-4874-b150-bc29e78be13f",
                "type": "text",
                "responses": {"text": "Synthetic approved absence."},
            },
        ],
    }
