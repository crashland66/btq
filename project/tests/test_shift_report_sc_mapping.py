from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from btq_cli import shift_report_to_sc as cli
from btq_cli.shift_report_sc_mapping import (
    ANSWER_CLEANING_COUNT,
    ANSWER_QC_COUNT,
    ANSWER_YES_NO_QUITS,
    ANSWER_YES_NO_SHARED,
    ITEM_ACCOUNTS_COMPLETED_QC,
    ITEM_ADDITIONAL_PAYROLL_HR_NOTES,
    ITEM_CLEANING_FILL_IN_COUNT,
    ITEM_CUSTOMER_INTERACTIONS,
    ITEM_EHUB_WINTEAM_ALERTS,
    ITEM_EMPLOYEES_VISITED,
    ITEM_EXCUSED_MISSED_SHIFTS,
    ITEM_INTERVIEWS_HIRES,
    ITEM_INTERVIEWS_HIRES_DETAIL,
    ITEM_NIGHTLY_SUMMARY,
    ITEM_NOTES_FOR_HR,
    ITEM_NOTES_FOR_OPERATIONS_MANAGER,
    ITEM_OPEN_POSITIONS_DETAIL,
    ITEM_OPEN_POSITIONS_FULL_AREA,
    ITEM_OVER_HOURS,
    ITEM_QC_VISITS_COUNT,
    ITEM_QUITS_TERMINATIONS,
    ITEM_QUITS_TERMINATIONS_DETAIL,
    ITEM_REPORT_DATE,
    ITEM_TEAM_MEMBER_CONNECTION,
    ITEM_TEAM_MEMBERS_COUNT,
    TEMPLATE_ID,
    build_prefill_payload,
    count_bucket,
    is_no_body,
    parse_shift_report,
    yes_no_detail,
)


SAMPLE = """# Area Manager Shift Report — 2026-06-24

## Nightly Summary
Altoona loop: QC at two sites, supply delivery, coaching.

## eHub / WinTeam Alert Updates Needed
None noted.

## Over-Hours Accounts
None noted.

## Accounts Visited for QC / Issue Ticket Follow-Up (how many)
2 accounts.

## Accounts Completed QC In
- Interfuse (222)
- PHN Altoona (592)

## Team Members Visited (Safety, QC, Training) (how many)
2 team members.

## Employees Visited, Trained, Etc. (Who and Account)
- Megan Greenwood (592): coached on flat-mop method.

## Team Member Connection (First and Last Name)
- Megan Greenwood

## Accounts Worked Strictly for Cleaning Fill-In (how many)
0.

## Customer Interactions (In-Person, Who and Account)
- Jackie (592): floor concern.

## Open Positions (Full Area)
- Mobile cleaner — area-wide

## Open Positions — Account, Days, Hours, Pay
- Mobile cleaner, area-wide — ~$17/hr.

## Interviews Conducted or Hires Made Today
No interviews or hires noted.

## Voluntary / Involuntary Quits or Terminations Today
No quits or terminations noted.

## Excused / Approved / Unexcused Missed Shifts
None noted.

## Notes for Operations Manager
- Interfuse (222): order bulk rags, trace missing delivery.
"""

CLOSEDAY_SAMPLE = """---
date: 2026-07-08
operator: greg
---
# Area Manager Shift Report — 2026-07-08

## Title Page
**Nightly Summary:**
Public-safe nightly summary with QC follow-up, staffing review, and client communication.

## Operations
**eHub / WinTeam Alert Updates Needed:**
- Account 101: confirm schedule exception was cleared.

**Over-Hours Accounts:**
- Account 202: reviewed 1.5 over-hours and set correction plan.

**Accounts Visited for QC / Issue Ticket Follow-Up (how many):**
1-2 — 1 account

**Accounts Completed QC In:**
- Account 101

**Team Members Visited (Safety, QC, Training) (how many):**
2

**Employees Visited, Trained, Etc. (Who and Account):**
- Sample Employee at Account 101: reviewed floor detail.

**Team Member Connection (First and Last Name):**
Sample Employee

**Accounts Worked Strictly for Cleaning Fill-In (how many):**
1-2 — 1 account

**Customer Interactions (In-Person, Who and Account):**
- Sample Customer at Account 202: reviewed service status.

## Human Resources
**Open Positions (Full Area):**
2 open positions across the area.

**Open Positions — Account, Days, Hours, Pay:**
- Account 101 — Mon/Wed/Fri — 6:00 PM-8:00 PM — $16/hr
- Account 202 — Tue/Thu — 7:00 PM-9:30 PM — $17/hr

**Interviews Conducted or Hires Made Today:** No

**Voluntary / Involuntary Quits or Terminations Today:** Yes
- Sample Employee resigned from Account 303.

**Excused / Approved / Unexcused Missed Shifts:**
- Account 101: one approved absence covered.

**Additional Payroll / HR Notes:**
- Correct one synthetic timecard entry.

## Requests-Notes
**Notes for HR:**
- Synthetic recruiting note for HR.

**Notes for Operations Manager:**
- Synthetic operations note that should publish.
"""


def _sections():
    return parse_shift_report(SAMPLE)


def _closeday_sections():
    return parse_shift_report(CLOSEDAY_SAMPLE)


def test_parse_splits_sections_on_h2():
    sections = _sections()
    assert sections["Accounts Visited for QC / Issue Ticket Follow-Up (how many)"] == "2 accounts."
    assert sections["Accounts Worked Strictly for Cleaning Fill-In (how many)"] == "0."
    assert "order bulk rags" in sections["Notes for Operations Manager"]


def test_parse_extracts_bold_label_blocks_from_closeday_shape():
    sections = _closeday_sections()
    assert sections["Nightly Summary"].startswith("Public-safe nightly summary")
    assert sections["Interviews Conducted or Hires Made Today"] == "No"
    assert sections["Voluntary / Involuntary Quits or Terminations Today"].startswith("Yes")
    assert "Account 101" in sections["Open Positions — Account, Days, Hours, Pay"]
    assert "operations note" in sections["Notes for Operations Manager"]


def test_title_page_fields_go_in_header_items_not_items():
    payload = build_prefill_payload(_sections(), "2026-06-24")
    assert payload["template_id"] == TEMPLATE_ID
    header_ids = {i["item_id"] for i in payload["header_items"]}
    item_ids = {i["item_id"] for i in payload["items"]}
    # Prepared By / Date / Nightly Summary are header items
    assert ITEM_NIGHTLY_SUMMARY in header_ids
    assert ITEM_NIGHTLY_SUMMARY not in item_ids
    assert len(payload["header_items"]) == 3


def test_date_is_datetime_typed():
    payload = build_prefill_payload(_sections(), "2026-06-24")
    date_item = next(i for i in payload["header_items"] if i["item_id"] == ITEM_REPORT_DATE)
    assert date_item["type"] == "datetime"
    assert date_item["responses"]["datetime"] == "2026-06-24T12:00:00Z"


def test_nightly_summary_uses_entry_field_not_the_static_label():
    payload = build_prefill_payload(_sections(), "2026-06-24")
    all_ids = {i["item_id"] for i in payload["header_items"] + payload["items"]}
    # 0639daca is a static LABEL in the template — text sent to it is silently dropped
    assert "0639daca-7836-4b88-b1e4-b4b90d04391d" not in all_ids
    summary = next(i for i in payload["header_items"] if i["item_id"] == ITEM_NIGHTLY_SUMMARY)
    assert "Altoona loop" in summary["responses"]["text"]


def test_closeday_label_fields_prefill_safetyculture_text_and_choices():
    payload = build_prefill_payload(_closeday_sections(), "2026-07-08")
    text_items = {
        i["item_id"]: i["responses"]["text"]
        for i in payload["header_items"] + payload["items"]
        if "text" in i.get("responses", {})
    }
    choice_items = {
        i["item_id"]: i["responses"]["selected"][0]["id"]
        for i in payload["items"]
        if "selected" in i.get("responses", {})
    }

    expected_text = {
        ITEM_NIGHTLY_SUMMARY: "Public-safe nightly summary",
        ITEM_EHUB_WINTEAM_ALERTS: "schedule exception",
        ITEM_OVER_HOURS: "1.5 over-hours",
        ITEM_ACCOUNTS_COMPLETED_QC: "Account 101",
        ITEM_EMPLOYEES_VISITED: "Sample Employee at Account 101",
        ITEM_TEAM_MEMBER_CONNECTION: "Sample Employee",
        ITEM_CUSTOMER_INTERACTIONS: "Sample Customer",
        ITEM_OPEN_POSITIONS_FULL_AREA: "2 open positions",
        ITEM_OPEN_POSITIONS_DETAIL: "$16/hr",
        ITEM_QUITS_TERMINATIONS_DETAIL: "Sample Employee resigned",
        ITEM_EXCUSED_MISSED_SHIFTS: "approved absence",
        ITEM_ADDITIONAL_PAYROLL_HR_NOTES: "synthetic timecard",
        ITEM_NOTES_FOR_HR: "recruiting note",
        ITEM_NOTES_FOR_OPERATIONS_MANAGER: "operations note",
    }
    for item_id, expected in expected_text.items():
        assert text_items[item_id]
        assert expected in text_items[item_id]
        assert text_items[item_id] != "None noted."

    assert choice_items[ITEM_QC_VISITS_COUNT] == ANSWER_QC_COUNT["1-2"]
    assert choice_items[ITEM_TEAM_MEMBERS_COUNT] == ANSWER_QC_COUNT["1-2"]
    assert choice_items[ITEM_CLEANING_FILL_IN_COUNT] == ANSWER_CLEANING_COUNT["1-2"]
    assert choice_items[ITEM_INTERVIEWS_HIRES] == ANSWER_YES_NO_SHARED["No"]
    assert choice_items[ITEM_QUITS_TERMINATIONS] == ANSWER_YES_NO_QUITS["Yes"]
    employees_item = next(i for i in payload["items"] if i["item_id"] == ITEM_EMPLOYEES_VISITED)
    assert employees_item["type"] == "question"
    assert "Internal note" not in json.dumps(payload)


@pytest.mark.parametrize(
    "body,bucket",
    [
        ("0.", "0"),
        ("1 account", "1-2"),
        ("1-2 — 1 account", "1-2"),
        ("2 accounts.", "1-2"),
        ("3 sites", "3 or more"),
        ("7", "3 or more"),
        ("", "0"),
    ],
)
def test_count_buckets(body, bucket):
    assert count_bucket(body) == bucket


def test_qc_and_cleaning_use_distinct_answer_sets():
    # Same bucket label, different answer ids per question family.
    assert ANSWER_QC_COUNT["0"] != ANSWER_CLEANING_COUNT["0"]
    assert ANSWER_QC_COUNT["1-2"] != ANSWER_CLEANING_COUNT["1-2"]


def test_choice_answers_resolve_to_expected_ids():
    payload = build_prefill_payload(_sections(), "2026-06-24")
    by_id = {i["item_id"]: i for i in payload["items"]}
    qc = by_id["d493e3ee-e331-43b7-9b9c-f4e7f6f449ce"]
    assert qc["type"] == "question"
    assert qc["responses"]["selected"][0]["id"] == ANSWER_QC_COUNT["1-2"]
    cleaning = by_id["77fcb7f3-8bdc-4b7b-854c-1e7ded415831"]
    assert cleaning["responses"]["selected"][0]["id"] == ANSWER_CLEANING_COUNT["0"]


@pytest.mark.parametrize("body,expected", [("None noted.", True), ("No interviews.", True), ("N/A", True), ("", True), ("- a hire", False)])
def test_is_no_body(body, expected):
    assert is_no_body(body) is expected


def test_quits_uses_its_own_yes_no_answer_set():
    payload = build_prefill_payload(_sections(), "2026-06-24")
    quits = next(i for i in payload["items"] if i["item_id"] == ITEM_QUITS_TERMINATIONS)
    assert quits["responses"]["selected"][0]["id"] == ANSWER_YES_NO_QUITS["No"]
    assert ANSWER_YES_NO_QUITS["No"] != ANSWER_YES_NO_SHARED["No"]


def test_quits_detail_prefills_conditional_text_field_from_closeday_report():
    report = """## Human Resources
**Voluntary / Involuntary Quits or Terminations Today:** Yes
Sandy Sandbox — Sandbox Site (900), voluntary resignation effective after the July 17 shift; the synthetic employee agreed to finish the week and is eligible for rehire.
"""
    payload = build_prefill_payload(parse_shift_report(report), "2026-07-14")
    by_id = {item["item_id"]: item for item in payload["items"]}

    assert by_id[ITEM_QUITS_TERMINATIONS]["responses"]["selected"][0]["id"] == ANSWER_YES_NO_QUITS["Yes"]
    assert by_id[ITEM_QUITS_TERMINATIONS_DETAIL]["responses"]["text"].startswith("Sandy Sandbox")
    assert not by_id[ITEM_QUITS_TERMINATIONS_DETAIL]["responses"]["text"].startswith("Yes")


def test_no_quits_omits_conditional_detail_field():
    payload = build_prefill_payload(_sections(), "2026-06-24")
    item_ids = {item["item_id"] for item in payload["items"]}
    assert ITEM_QUITS_TERMINATIONS_DETAIL not in item_ids


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Yes\nEmployee resigned.", "Employee resigned."),
        ("- Employee resigned.", "- Employee resigned."),
        ("No quits noted.", ""),
        ("Yes", ""),
    ],
)
def test_yes_no_detail(body, expected):
    assert yes_no_detail(body) == expected


def test_all_closing_note_fields_are_included():
    payload = build_prefill_payload(_sections(), "2026-06-24")
    by_id = {item["item_id"]: item for item in payload["items"]}
    assert by_id[ITEM_NOTES_FOR_OPERATIONS_MANAGER]["responses"]["text"] == (
        "- Interfuse (222): order bulk rags, trace missing delivery."
    )
    assert by_id[ITEM_ADDITIONAL_PAYROLL_HR_NOTES]["responses"]["text"] == "None noted."
    assert by_id[ITEM_NOTES_FOR_HR]["responses"]["text"] == "None noted."


def test_interview_detail_prefills_conditional_text_field():
    report = """## Human Resources
**Interviews Conducted or Hires Made Today:** Yes
Synthetic Candidate — interviewed for Account 101 at $16/hour; start date pending.
"""
    payload = build_prefill_payload(parse_shift_report(report), "2026-07-14")
    by_id = {item["item_id"]: item for item in payload["items"]}

    assert by_id[ITEM_INTERVIEWS_HIRES]["responses"]["selected"][0]["id"] == ANSWER_YES_NO_SHARED["Yes"]
    assert by_id[ITEM_INTERVIEWS_HIRES_DETAIL]["responses"]["text"].startswith("Synthetic Candidate")


def _args(**kw):
    base = dict(report_path=None, date=None, dry_run=False, force=False, state_path=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_dry_run_makes_no_network_call_and_needs_no_token(tmp_path, monkeypatch, capsys):
    report = tmp_path / "2026-06-24-shift-report.md"
    report.write_text(SAMPLE, encoding="utf-8")
    # Any attempt to load a token or submit during --dry-run is a failure.
    monkeypatch.setattr(cli, "load_safetyculture_token", lambda *a, **k: pytest.fail("token loaded in dry-run"))
    monkeypatch.setattr(cli, "submit_prefill_payload", lambda *a, **k: pytest.fail("submit called in dry-run"))
    rc = cli.handle_shift_report_to_sc(_args(report_path=report, dry_run=True))
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["template_id"] == TEMPLATE_ID


def test_idempotent_skip_when_draft_already_recorded(tmp_path, monkeypatch, capsys):
    report = tmp_path / "2026-06-24-shift-report.md"
    report.write_text(SAMPLE, encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"drafts": {"2026-06-24": {"audit_id": "audit_EXISTING"}}}), encoding="utf-8")
    monkeypatch.setattr(cli, "load_safetyculture_token", lambda *a, **k: pytest.fail("token loaded on idempotent skip"))
    monkeypatch.setattr(cli, "submit_prefill_payload", lambda *a, **k: pytest.fail("submit called on idempotent skip"))
    rc = cli.handle_shift_report_to_sc(_args(report_path=report, state_path=state))
    assert rc == 0
    assert "audit_EXISTING" in capsys.readouterr().out


def test_submit_extracts_audit_id_via_injected_client():
    calls = {}

    class FakeClient:
        def post_json(self, url, payload, headers):
            calls["url"] = url
            calls["auth"] = headers.get("Authorization")
            return 201, {"audit_id": "audit_NEW123"}

    audit_id = cli.submit_prefill_payload({"template_id": TEMPLATE_ID}, "tok", http_client=FakeClient())
    assert audit_id == "audit_NEW123"
    assert calls["url"].endswith("/audits")
    assert calls["auth"] == "Bearer tok"


@pytest.mark.parametrize("status", [200, 201])
def test_submit_accepts_documented_and_legacy_success_statuses(status):
    class FakeClient:
        def post_json(self, url, payload, headers):
            return status, {"audit_id": "audit_NEW123"}

    assert cli.submit_prefill_payload({}, "tok", http_client=FakeClient()) == "audit_NEW123"


def test_submit_repairs_and_verifies_silently_omitted_prefill_responses():
    summary = {
        "item_id": ITEM_NIGHTLY_SUMMARY,
        "type": "text",
        "responses": {"text": "Nightly summary"},
    }
    employees = {
        "item_id": ITEM_EMPLOYEES_VISITED,
        "type": "question",
        "responses": {"text": "Sample Employee — Account 101"},
    }
    payload = {"template_id": TEMPLATE_ID, "header_items": [summary], "items": [employees]}

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.audit = {"audit_id": "audit_REPAIRED", "header_items": [], "items": []}

        def post_json(self, url, payload, headers):
            self.calls.append("POST")
            return 200, self.audit

        def put_json(self, url, payload, headers):
            self.calls.append("PUT")
            self.audit["header_items"] = payload["header_items"]
            self.audit["items"] = payload["items"]
            return 200, {}

        def get_json(self, url, headers):
            self.calls.append("GET")
            return 200, self.audit

    client = FakeClient()
    assert cli.submit_prefill_payload(payload, "tok", http_client=client) == "audit_REPAIRED"
    assert client.calls == ["POST", "PUT", "GET"]


def test_submit_raises_on_non_success_status():
    class FakeClient:
        def post_json(self, url, payload, headers):
            return 400, {"error": "bad"}

    with pytest.raises(cli.SafetyCultureSubmitError):
        cli.submit_prefill_payload({}, "tok", http_client=FakeClient())
