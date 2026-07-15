from __future__ import annotations

import re
from datetime import date as date_cls, datetime
from typing import Any


SC_BASE = "https://api.safetyculture.io"
TEMPLATE_ID = "template_cbea032fa8414d7f81c107d75758ad67"
PREPARED_BY = "Gregory Stoltz"

ITEM_PREPARED_BY = "f3245d43-ea77-11e1-aff1-0800200c9a66"
ITEM_REPORT_DATE = "5fa172c2-ab90-4426-9565-b9a1a956ccf9"
ITEM_NIGHTLY_SUMMARY = "b796be8c-4981-4404-ac38-927be1ba8375"

ITEM_EHUB_WINTEAM_ALERTS = "76be27fd-7665-42e6-a03b-bf6b0d81ff53"
ITEM_OVER_HOURS = "45e184eb-7a78-43fc-abec-94ac62456a64"
ITEM_QC_VISITS_COUNT = "d493e3ee-e331-43b7-9b9c-f4e7f6f449ce"
ITEM_ACCOUNTS_COMPLETED_QC = "15c7a502-7c66-4369-b9c1-4c615281b33c"
ITEM_TEAM_MEMBERS_COUNT = "2d95f9e6-61f5-471d-84e6-913758cf5dd0"
ITEM_EMPLOYEES_VISITED = "8ddf707e-f811-46ad-ab3e-33535b8d1873"
ITEM_TEAM_MEMBER_CONNECTION = "b4cf664a-5c86-408d-8696-a7ca6f3da844"
ITEM_CLEANING_FILL_IN_COUNT = "77fcb7f3-8bdc-4b7b-854c-1e7ded415831"
ITEM_CLEANED_ACCOUNTS_YN = "fa04b2a8-4ed8-4660-9181-3e219ac4292b"
ITEM_CUSTOMER_INTERACTIONS = "5454f983-510b-4de9-baa6-612ee3a39c88"
ITEM_OPEN_POSITIONS_FULL_AREA = "867d2e67-6ecc-4f28-b7b4-d02fb61f8122"
ITEM_OPEN_POSITIONS_DETAIL = "309cbb25-106a-47fa-b1eb-4d2fb32e411b"
ITEM_INTERVIEWS_HIRES = "5bf7cdfd-76f3-480b-bbb8-2cc0e95c8e2d"
ITEM_QUITS_TERMINATIONS = "0eff9059-bf4a-4160-9f80-f717f94cdf7d"
ITEM_QUITS_TERMINATIONS_DETAIL = "5d8a8e88-74c2-4e05-a1b4-23765bd40d32"
ITEM_EXCUSED_MISSED_SHIFTS = "fa3c326d-650c-4874-b150-bc29e78be13f"

ANSWER_QC_COUNT = {
    "0": "62707c36-b9b8-4535-bc5a-b079c9e562b0",
    "1-2": "d573660b-553e-428e-97da-aa7e0a12e238",
    "3 or more": "9c01128b-a444-44d5-a5b2-0c01dbb83023",
}
ANSWER_CLEANING_COUNT = {
    "0": "c4063cf4-02a2-4823-b0ce-776b7355c952",
    "1-2": "a4218146-45f0-4e82-96a2-0bcfe1244b6f",
    "3 or more": "d8a5a5f1-f274-482f-b422-b96af7a5d393",
}
ANSWER_YES_NO_SHARED = {
    "Yes": "060ace00-1f61-468e-9c42-918fa93badcf",
    "No": "060ace01-1f61-468e-9c42-918fa93badcf",
    "N/A": "060ace02-1f61-468e-9c42-918fa93badcf",
}
ANSWER_YES_NO_QUITS = {
    "Yes": "be5d6d1e-d779-41d8-abae-0966eff42ce0",
    "No": "8a62fda4-3146-4af2-9bc5-43d4a608892f",
}

_SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_FIELD_LABEL_RE = re.compile(r"^\s*\*\*(?P<label>.+?):\*\*\s*(?P<body>.*)$")
_LEADING_COUNT_RE = re.compile(r"^\s*(\d+)(?:\s*[-\u2013\u2014]\s*(\d+))?")
_NEGATIVE_BODY_RE = re.compile(r"^\s*(?:none|no|n/a)(?:\b|[\s.,;:!-]|$)", re.IGNORECASE)
_NO_CALL_OR_SHOW_DETAIL_RE = re.compile(
    r"^\s*(?:no[- ]call(?:\s*/\s*no[- ]show)?|no[- ]show)\b",
    re.IGNORECASE,
)


def parse_shift_report(md_text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    heading_name: str | None = None
    heading_lines: list[str] = []
    field_name: str | None = None
    field_lines: list[str] = []

    def flush_heading() -> None:
        nonlocal heading_name, heading_lines
        if heading_name is not None:
            sections[heading_name] = heading_lines
        heading_name = None
        heading_lines = []

    def flush_field() -> None:
        nonlocal field_name, field_lines
        if field_name is not None:
            sections[field_name] = field_lines
        field_name = None
        field_lines = []

    for line in md_text.splitlines():
        if line.strip() == "---":
            flush_field()
            continue

        field_match = _FIELD_LABEL_RE.match(line)
        heading_match = _SECTION_HEADING_RE.match(line)

        if field_match:
            flush_field()
            field_name = _clean_heading(field_match.group("label"))
            body = field_match.group("body").strip()
            field_lines = [body] if body else []
            if heading_name is not None:
                heading_lines.append(line)
            continue

        if heading_match:
            flush_field()
            flush_heading()
            heading_name = _clean_heading(heading_match.group(1))
            heading_lines = []
            continue

        if field_name is not None:
            field_lines.append(line)
        if heading_name is not None:
            heading_lines.append(line)

    flush_field()
    flush_heading()

    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def build_prefill_payload(sections: dict[str, str], date: str | date_cls | datetime) -> dict[str, Any]:
    report_day = _coerce_report_date(date)
    nightly_summary = _section_body(sections, "Nightly Summary")
    qc_count_bucket = count_bucket(_section_body(sections, "Accounts Visited for QC / Issue Ticket Follow-Up (how many)"))
    team_count_bucket = count_bucket(_section_body(sections, "Team Members Visited (Safety, QC, Training) (how many)"))
    cleaning_count_bucket = count_bucket(_section_body(sections, "Accounts Worked Strictly for Cleaning Fill-In (how many)"))
    quits_terminations = _section_body(sections, "Voluntary / Involuntary Quits or Terminations Today")

    items = [
        text_item(ITEM_EHUB_WINTEAM_ALERTS, _section_body(sections, "eHub / WinTeam Alert Updates Needed", default="None noted.")),
        text_item(ITEM_OVER_HOURS, _section_body(sections, "Over-Hours Accounts")),
        choice_item(ITEM_QC_VISITS_COUNT, ANSWER_QC_COUNT[qc_count_bucket]),
        text_item(ITEM_ACCOUNTS_COMPLETED_QC, _section_body(sections, "Accounts Completed QC In")),
        choice_item(ITEM_TEAM_MEMBERS_COUNT, ANSWER_QC_COUNT[team_count_bucket]),
        text_item(ITEM_EMPLOYEES_VISITED, _section_body(sections, "Employees Visited, Trained, Etc. (Who and Account)")),
        text_item(ITEM_TEAM_MEMBER_CONNECTION, _section_body(sections, "Team Member Connection (First and Last Name)")),
        choice_item(ITEM_CLEANING_FILL_IN_COUNT, ANSWER_CLEANING_COUNT[cleaning_count_bucket]),
        choice_item(ITEM_CLEANED_ACCOUNTS_YN, ANSWER_YES_NO_SHARED["No" if cleaning_count_bucket == "0" else "Yes"]),
        text_item(ITEM_CUSTOMER_INTERACTIONS, _section_body(sections, "Customer Interactions (In-Person, Who and Account)")),
        text_item(ITEM_OPEN_POSITIONS_FULL_AREA, _section_body(sections, "Open Positions (Full Area)")),
        text_item(ITEM_OPEN_POSITIONS_DETAIL, _section_body(sections, "Open Positions \u2014 Account, Days, Hours, Pay")),
        yes_no_item(ITEM_INTERVIEWS_HIRES, _section_body(sections, "Interviews Conducted or Hires Made Today")),
        choice_item(
            ITEM_QUITS_TERMINATIONS,
            ANSWER_YES_NO_QUITS["No" if is_no_quits_body(quits_terminations) else "Yes"],
        ),
    ]
    quits_terminations_detail = yes_no_detail(quits_terminations)
    if quits_terminations_detail:
        items.append(text_item(ITEM_QUITS_TERMINATIONS_DETAIL, quits_terminations_detail))
    items.append(
        text_item(ITEM_EXCUSED_MISSED_SHIFTS, _section_body(sections, "Excused / Approved / Unexcused Missed Shifts", default="None noted."))
    )

    return {
        "template_id": TEMPLATE_ID,
        "header_items": [
            text_item(ITEM_PREPARED_BY, PREPARED_BY),
            datetime_item(ITEM_REPORT_DATE, f"{report_day.isoformat()}T12:00:00Z"),
            text_item(ITEM_NIGHTLY_SUMMARY, nightly_summary),
        ],
        "items": items,
    }


def text_item(item_id: str, text: str) -> dict[str, Any]:
    return {"item_id": item_id, "type": "text", "responses": {"text": text}}


def datetime_item(item_id: str, value: str) -> dict[str, Any]:
    return {"item_id": item_id, "type": "datetime", "responses": {"datetime": value}}


def choice_item(item_id: str, answer_id: str) -> dict[str, Any]:
    return {"item_id": item_id, "type": "question", "responses": {"selected": [{"id": answer_id}]}}


def yes_no_item(item_id: str, body: str, *, answers: dict[str, str] = ANSWER_YES_NO_SHARED) -> dict[str, Any]:
    value = "No" if is_no_body(body) else "Yes"
    return choice_item(item_id, answers[value])


def count_bucket(body: str) -> str:
    match = _LEADING_COUNT_RE.match(body)
    count = int(match.group(2) or match.group(1)) if match else 0
    if count <= 0:
        return "0"
    if count <= 2:
        return "1-2"
    return "3 or more"


def is_no_body(body: str) -> bool:
    stripped = body.strip()
    return not stripped or bool(_NEGATIVE_BODY_RE.match(stripped))


def is_no_quits_body(body: str) -> bool:
    return not _NO_CALL_OR_SHOW_DETAIL_RE.match(body) and is_no_body(body)


def yes_no_detail(body: str) -> str:
    if is_no_quits_body(body):
        return ""
    lines = body.strip().splitlines()
    if lines and lines[0].strip().rstrip(".:;- ").lower() == "yes":
        lines = lines[1:]
    return "\n".join(lines).strip()


def _coerce_report_date(value: str | date_cls | datetime) -> date_cls:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_cls):
        return value
    try:
        return date_cls.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"report date must be YYYY-MM-DD: {value}") from exc


def _section_body(sections: dict[str, str], name: str, *, default: str = "") -> str:
    normalized_name = _normalize_section_name(name)
    for section_name, body in sections.items():
        if _normalize_section_name(section_name) == normalized_name:
            return body.strip() or default
    return default


def _clean_heading(value: str) -> str:
    return value.strip().strip("#").strip()


def _normalize_section_name(value: str) -> str:
    normalized = value.replace("\u2013", "-").replace("\u2014", "-")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized.strip().lower())
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized
