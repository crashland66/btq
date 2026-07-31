"""Independent behavioral verifier coverage for the site calendar dashboard.

All identities and calendar content are synthetic.  POST tests stage only
inside pytest-provided temporary runtime roots and fail if a CouchDB seam is
called.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import html
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from ops_dashboard import post_routes
from ops_dashboard.common import SectionContext
from ops_dashboard.sections import site_detail
from queue_spec import validate_job


SITE_ID = "synthetic-site-497"
CALENDAR_ID = "synthetic-calendar-497"
ACTOR = "Synthetic Verifier"


class RecordingContext(SimpleNamespace):
    def __init__(
        self,
        tmp_path: Path,
        query: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(runtime_root=tmp_path / "runtime", query=query or {})
        self.audit_entries: list[tuple[str, dict[str, object], str]] = []

    def audit(
        self,
        route: str,
        payload: dict[str, object],
        result: str,
    ) -> None:
        self.audit_entries.append((route, payload, result))

    def flash(self, query: dict[str, list[str]] | None = None) -> str:
        return SectionContext.flash(self, query)


def _event(
    event_id: str,
    *,
    start_date: str,
    end_date: str | None = None,
    kind: str = "informational",
    label: str = "Synthetic event",
    student_status: str = "unknown",
    facility_status: str = "unknown",
    bt_service_impact: str = "unknown",
    **extra: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": event_id,
        "start_date": start_date,
        "end_date": end_date or start_date,
        "kind": kind,
        "label": label,
        "student_status": student_status,
        "facility_status": facility_status,
        "bt_service_impact": bt_service_impact,
    }
    event.update(extra)
    return event


def _calendar(
    *,
    calendar_id: str = CALENDAR_ID,
    events: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    today = date.today()
    calendar: dict[str, Any] = {
        "schema_version": 1,
        "calendar_id": calendar_id,
        "label": "Synthetic operational calendar",
        "timezone": "America/New_York",
        "status": "verified",
        "valid_from": (today - timedelta(days=365)).isoformat(),
        "valid_through": (today + timedelta(days=365)).isoformat(),
        "last_verified_at": "2026-07-30T12:00:00-04:00",
        "last_verified_by": ACTOR,
        "source": {
            "kind": "synthetic_public_document",
            "title": "Synthetic public calendar",
            "retrieved_at": "2026-07-30T11:30:00-04:00",
            "document_url": (
                "https://calendar.example.invalid/synthetic-calendar.pdf"
            ),
        },
        "events": list(events or []),
    }
    calendar.update(overrides)
    return calendar


def _location(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": f"location_{SITE_ID}",
        "_rev": "1-synthetic",
        "type": "location",
        "site_id": SITE_ID,
        "location": "Synthetic Calendar Test Site",
        "operator": "synthetic-operator",
    }
    doc.update(overrides)
    return doc


def _stub_site_detail_reads(
    monkeypatch: pytest.MonkeyPatch,
    doc: dict[str, Any],
) -> None:
    monkeypatch.setattr(site_detail, "_load_location", lambda _site_id: doc)
    monkeypatch.setattr(
        site_detail,
        "_load_account_doc_for_location",
        lambda _doc: None,
    )
    monkeypatch.setattr(
        site_detail,
        "_related_data",
        lambda _site_id: {
            "notes": [],
            "employee_rows": [],
            "coverage_gap_rows": [],
            "opportunity_rows": [],
            "visit_rows": [],
            "recent_visits": [],
        },
    )
    monkeypatch.setattr(site_detail, "_related_sections", lambda _data: [])
    monkeypatch.setattr(
        site_detail,
        "_site_capture_records",
        lambda _ctx, _site_id: ([], False, 0),
    )
    monkeypatch.setattr(
        site_detail,
        "_site_capture_processing_counts",
        lambda _site_id: None,
    )
    monkeypatch.setattr(
        site_detail,
        "submitters_by_capture",
        lambda _runtime_root: {},
    )


def _post_calendar(
    ctx: RecordingContext,
    fields: dict[str, str],
) -> tuple:
    return site_detail.handle_site_operational_calendar_post(
        ctx,
        SITE_ID,
        urlencode(fields).encode(),
    )


def _queue_files(ctx: RecordingContext) -> list[Path]:
    queue_dir = ctx.runtime_root / "queue"
    if not queue_dir.exists():
        return []
    return sorted(path for path in queue_dir.rglob("*") if path.is_file())


def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("calendar dashboard POST must not read or write CouchDB")


def test_valid_calendar_renders_safe_provenance_timeline_and_independent_states() -> None:
    today = date.today()
    past = (today - timedelta(days=20)).isoformat()
    current_start = (today - timedelta(days=1)).isoformat()
    current_end = (today + timedelta(days=1)).isoformat()
    upcoming = (today + timedelta(days=20)).isoformat()
    page_url = (
        "https://calendar.example.invalid/official"
        "?edition=497&view=public"
    )
    document_url = (
        "https://calendar.example.invalid/download/calendar-497.pdf"
    )
    calendar = _calendar(
        label="Synthetic <District & Calendar>",
        note='Calendar note <script>alert("calendar")</script>',
        source={
            "kind": "synthetic_public_document",
            "title": "Synthetic <Official & Source>",
            "retrieved_at": "2026-07-30T11:30:00-04:00",
            "page_url": page_url,
            "document_url": document_url,
        },
        events=[
            _event(
                "past-no-student-open-normal",
                start_date=past,
                kind="no_student_day",
                label="Past no-student day",
                student_status="no_students",
                facility_status="open",
                bt_service_impact="normal",
            ),
            _event(
                "current-early-dismissal",
                start_date=current_start,
                end_date=current_end,
                kind="early_dismissal",
                label="Current <Early & Dismissal>",
                student_status="early_dismissal",
                facility_status="unknown",
                bt_service_impact="confirm",
                dismissal_time="13:15",
                note='Verify <service> & "routing".',
            ),
            _event(
                "upcoming-no-student-open-normal",
                start_date=upcoming,
                kind="teacher_in_service",
                label="Upcoming no-student, facility-open day",
                student_status="no_students",
                facility_status="open",
                bt_service_impact="normal",
            ),
        ],
    )

    rendered = site_detail._operational_calendar_section(
        {"operational_calendars": [calendar]},
        SITE_ID,
    )

    assert "Synthetic &lt;District &amp; Calendar&gt;" in rendered
    assert '<span class="pill status-verified">Verified</span>' in rendered
    assert (
        f"<strong>Inclusive coverage:</strong> "
        f"{calendar['valid_from']} through {calendar['valid_through']}"
    ) in rendered
    assert (
        "<strong>Last verification:</strong> "
        "2026-07-30T12:00:00-04:00 by Synthetic Verifier"
    ) in rendered
    assert (
        f'<a href="{html.escape(page_url, quote=True)}" '
        'target="_blank" rel="noreferrer noopener">'
        "Synthetic &lt;Official &amp; Source&gt;</a>"
    ) in rendered
    assert "provenance only; not automatically monitored" in rendered
    assert f'<a href="{html.escape(document_url, quote=True)}"' not in rendered

    current_heading = rendered.index("Current and upcoming events")
    current_event = rendered.index("Current &lt;Early &amp; Dismissal&gt;")
    upcoming_event = rendered.index("Upcoming no-student, facility-open day")
    past_disclosure = rendered.index(
        "<details><summary>Past events (1)</summary>"
    )
    past_event = rendered.index("Past no-student day")
    assert (
        current_heading
        < current_event
        < upcoming_event
        < past_disclosure
        < past_event
    )
    assert "<details open" not in rendered
    assert "Dismissal time: 13:15" in rendered

    current_item = rendered[current_event : rendered.index("</li>", current_event)]
    assert "<strong>Student:</strong>" in current_item
    assert "Early Dismissal" in current_item
    assert "<strong>Facility:</strong>" in current_item
    assert "Unknown" in current_item
    assert "<strong>B&amp;T service:</strong>" in current_item
    assert "Service impact needs confirmation." in current_item

    no_student_item = rendered[
        upcoming_event : rendered.index("</li>", upcoming_event)
    ]
    assert "No Students" in no_student_item
    assert "<strong>Facility:</strong>" in no_student_item
    assert "Open" in no_student_item
    assert "<strong>B&amp;T service:</strong>" in no_student_item
    assert "Normal" in no_student_item
    assert "Closed" not in no_student_item
    assert "No Service" not in no_student_item

    assert "<District & Calendar>" not in rendered
    assert "<Official & Source>" not in rendered
    assert "<script>" not in rendered
    assert "<service>" not in rendered
    assert "Verify &lt;service&gt; &amp; &quot;routing&quot;." in rendered


def test_no_calendar_renders_bounded_empty_state_without_degrading_site_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_site_detail_reads(monkeypatch, _location())
    ctx = RecordingContext(tmp_path)

    rendered = site_detail.render(ctx, SITE_ID)

    assert "Synthetic Calendar Test Site" in rendered
    assert "<h2>Operational calendar</h2>" in rendered
    assert "No operational calendars are recorded for this site." in rendered
    assert "<summary>Add operational calendar</summary>" in rendered
    assert '<section class="error"><p>' not in rendered


def test_malformed_and_duplicate_stored_calendars_are_bounded_and_escaped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    valid = _calendar(label="One safe synthetic calendar")
    malformed = {
        "calendar_id": "malformed-497",
        "label": '<script id="stored-secret">bad</script>',
    }
    doc = _location(
        operational_calendars=[valid, deepcopy(valid), malformed],
    )
    _stub_site_detail_reads(monkeypatch, doc)
    ctx = RecordingContext(tmp_path)

    rendered = site_detail.render(ctx, SITE_ID)

    assert "Synthetic Calendar Test Site" in rendered
    assert "One safe synthetic calendar" in rendered
    assert "Operational calendar warning." in rendered
    assert "2 malformed stored entries could not be displayed." in rendered
    assert "No operational calendars are recorded for this site." not in rendered
    assert '<script id="stored-secret">' not in rendered
    assert "Site not found" not in rendered
    assert '<section class="error"><p>' not in rendered


def test_operational_calendar_and_facility_hours_post_routes_remain_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx = RecordingContext(tmp_path)
    calls: list[tuple[str, str, bytes]] = []

    def calendar_handler(_ctx: object, site_id: str, body: bytes) -> tuple:
        calls.append(("calendar", site_id, body))
        return 303, "text/html", b"calendar", {"Location": "/calendar"}

    def hours_handler(_ctx: object, site_id: str, body: bytes) -> tuple:
        calls.append(("hours", site_id, body))
        return 303, "text/html", b"hours", {"Location": "/hours"}

    monkeypatch.setattr(
        post_routes.site_detail,
        "handle_site_operational_calendar_post",
        calendar_handler,
    )
    monkeypatch.setattr(
        post_routes.site_detail,
        "handle_site_hours_post",
        hours_handler,
    )

    calendar_result = post_routes.dispatch_post_route(
        f"/sites/{SITE_ID}/operational-calendar",
        ctx,
        b"calendar-body",
        "application/x-www-form-urlencoded",
        {},
    )
    hours_result = post_routes.dispatch_post_route(
        f"/sites/{SITE_ID}/facility-hours",
        ctx,
        b"hours-body",
        "application/x-www-form-urlencoded",
        {},
    )
    invalid_nested_result = post_routes.dispatch_post_route(
        f"/sites/{SITE_ID}/nested/operational-calendar",
        ctx,
        b"must-not-dispatch",
        "application/x-www-form-urlencoded",
        {},
    )

    assert calendar_result is not None
    assert calendar_result[2] == b"calendar"
    assert hours_result is not None
    assert hours_result[2] == b"hours"
    assert invalid_nested_result is None
    assert calls == [
        ("calendar", SITE_ID, b"calendar-body"),
        ("hours", SITE_ID, b"hours-body"),
    ]


def test_upsert_normalizes_validates_and_atomically_stages_one_queue_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(site_detail, "_load_location", _fail_if_called)
    monkeypatch.setattr(site_detail.sites, "request_json", _fail_if_called)
    monkeypatch.setattr(site_detail.urlrequest, "urlopen", _fail_if_called)
    ctx = RecordingContext(tmp_path)
    raw = _calendar(
        label="  Synthetic   operational calendar  ",
        last_verified_by="  Synthetic   Verifier  ",
        source={
            "kind": "  synthetic_document ",
            "title": " Synthetic   source ",
            "retrieved_at": " 2026-07-30T11:30:00-04:00 ",
            "page_url": " https://calendar.example.invalid/source ",
        },
        events=[
            _event(
                "normalized-event",
                start_date=date.today().isoformat(),
                label="  Normalized   event label ",
                student_status="in_session",
                facility_status="open",
                bt_service_impact="normal",
            )
        ],
    )

    status, content_type, response_body, headers = _post_calendar(
        ctx,
        {
            "action": "upsert",
            "calendar_id": CALENDAR_ID,
            "actor": ACTOR,
            "calendar_json": json.dumps(raw),
        },
    )

    assert status == 303
    assert content_type == "text/html; charset=utf-8"
    assert headers["Location"] == (
        f"/sites/{SITE_ID}"
        "?message=Operational%20calendar%20update%20queued."
    )
    assert b"Operational%20calendar%20update%20queued." in response_body
    files = _queue_files(ctx)
    assert len(files) == 1
    assert files[0].name.startswith("set-site-operational-calendar-")
    assert not files[0].name.startswith(".")
    job = json.loads(files[0].read_text(encoding="utf-8"))
    assert validate_job(job) is True
    assert job["job_type"] == "set_site_operational_calendar"
    assert set(job) == {"job_id", "job_type", "payload"}
    payload = job["payload"]
    assert payload == {
        "site_id": SITE_ID,
        "action": "upsert",
        "calendar_id": CALENDAR_ID,
        "actor": ACTOR,
        "source": "ops_dashboard_site_detail",
        "calendar": {
            **raw,
            "label": "Synthetic operational calendar",
            "last_verified_by": ACTOR,
            "source": {
                "kind": "synthetic_document",
                "title": "Synthetic source",
                "retrieved_at": "2026-07-30T11:30:00-04:00",
                "page_url": "https://calendar.example.invalid/source",
            },
            "events": [
                {
                    **raw["events"][0],
                    "label": "Normalized event label",
                }
            ],
        },
    }
    assert list((ctx.runtime_root / "queue").glob(".*.tmp")) == []


def test_confirmed_remove_stages_no_calendar_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(site_detail, "_load_location", _fail_if_called)
    monkeypatch.setattr(site_detail.sites, "request_json", _fail_if_called)
    monkeypatch.setattr(site_detail.urlrequest, "urlopen", _fail_if_called)
    ctx = RecordingContext(tmp_path)

    status, _content_type, _body, headers = _post_calendar(
        ctx,
        {
            "action": "remove",
            "calendar_id": CALENDAR_ID,
            "actor": ACTOR,
            "confirm": "1",
            "calendar_json": '{"sensitive":"must be ignored for remove"}',
        },
    )

    assert status == 303
    assert headers["Location"] == (
        f"/sites/{SITE_ID}"
        "?message=Operational%20calendar%20removal%20queued."
    )
    files = _queue_files(ctx)
    assert len(files) == 1
    job = json.loads(files[0].read_text(encoding="utf-8"))
    assert validate_job(job) is True
    assert job["payload"] == {
        "site_id": SITE_ID,
        "action": "remove",
        "calendar_id": CALENDAR_ID,
        "actor": ACTOR,
        "source": "ops_dashboard_site_detail",
    }
    assert "calendar" not in job["payload"]


@pytest.mark.parametrize(
    "case",
    [
        "invalid_json",
        "unknown_calendar_field",
        "calendar_id_mismatch",
        "invalid_event_contract",
        "invalid_action",
    ],
)
def test_invalid_json_or_contract_stages_no_queue_files(
    tmp_path: Path,
    case: str,
) -> None:
    ctx = RecordingContext(tmp_path / case)
    raw = _calendar()
    fields = {
        "action": "upsert",
        "calendar_id": CALENDAR_ID,
        "actor": ACTOR,
        "calendar_json": json.dumps(raw),
    }
    if case == "invalid_json":
        fields["calendar_json"] = '{"schema_version": 1'
    elif case == "unknown_calendar_field":
        raw["unknown_field"] = "rejected"
        fields["calendar_json"] = json.dumps(raw)
    elif case == "calendar_id_mismatch":
        raw["calendar_id"] = "different-calendar-497"
        fields["calendar_json"] = json.dumps(raw)
    elif case == "invalid_event_contract":
        raw["events"] = [
            _event(
                "bad-event",
                start_date=date.today().isoformat(),
                student_status="not-a-status",
            )
        ]
        fields["calendar_json"] = json.dumps(raw)
    else:
        fields["action"] = "publish"

    status, _content_type, _body, headers = _post_calendar(ctx, fields)

    assert status == 303
    assert urlsplit(headers["Location"]).path == f"/sites/{SITE_ID}"
    error = parse_qs(urlsplit(headers["Location"]).query)["error"][0]
    assert error
    assert _queue_files(ctx) == []
    assert ctx.audit_entries[-1][2].startswith("failed:")


def test_unconfirmed_remove_stages_no_files_and_reports_confirmation_needed(
    tmp_path: Path,
) -> None:
    ctx = RecordingContext(tmp_path)

    status, _content_type, body, headers = _post_calendar(
        ctx,
        {
            "action": "remove",
            "calendar_id": CALENDAR_ID,
            "actor": ACTOR,
        },
    )

    assert status == 303
    assert headers["Location"] == (
        f"/sites/{SITE_ID}"
        "?error=Confirm%20calendar%20removal%20before%20continuing."
    )
    assert (
        b"Confirm%20calendar%20removal%20before%20continuing."
        in body
    )
    assert _queue_files(ctx) == []
    assert ctx.audit_entries == [
        (
            f"/sites/{SITE_ID}/operational-calendar",
            {"action": "remove", "calendar_id": CALENDAR_ID},
            "failed: confirm_required",
        )
    ]


def test_audit_never_contains_submitted_calendar_json_actor_or_text(
    tmp_path: Path,
) -> None:
    ctx = RecordingContext(tmp_path)
    submitted_marker = "private-marker"
    raw = _calendar(
        label=submitted_marker,
        note=f"{submitted_marker}-note",
        last_verified_by=f"{submitted_marker}-actor-in-json",
    )

    success = _post_calendar(
        ctx,
        {
            "action": "upsert",
            "calendar_id": CALENDAR_ID,
            "actor": f"{submitted_marker}-submitter",
            "calendar_json": json.dumps(raw),
        },
    )
    failure = _post_calendar(
        ctx,
        {
            "action": "upsert",
            "calendar_id": CALENDAR_ID,
            "actor": f"{submitted_marker}-submitter",
            "calendar_json": f'{{"broken":"{submitted_marker}"',
        },
    )

    assert success[0] == 303
    assert failure[0] == 303
    assert len(ctx.audit_entries) == 2
    serialized_audit = json.dumps(ctx.audit_entries)
    assert submitted_marker not in serialized_audit
    for route, payload, result in ctx.audit_entries:
        assert route == f"/sites/{SITE_ID}/operational-calendar"
        assert payload == {
            "action": "upsert",
            "calendar_id": CALENDAR_ID,
        }
        assert "calendar_json" not in result


def test_failure_redirect_is_descriptive_percent_encoded_and_html_safe(
    tmp_path: Path,
) -> None:
    ctx = RecordingContext(tmp_path)
    raw = _calendar()
    raw['unknown<&"field'] = "rejected"

    status, _content_type, body, headers = _post_calendar(
        ctx,
        {
            "action": "upsert",
            "calendar_id": CALENDAR_ID,
            "actor": ACTOR,
            "calendar_json": json.dumps(raw),
        },
    )

    assert status == 303
    location = headers["Location"]
    assert " " not in location
    assert "<" not in location
    assert "&field" not in location
    error = parse_qs(urlsplit(location).query)["error"][0]
    assert "unsupported fields" in error
    assert 'unknown<&"field' in error
    assert body == (
        f'<a href="{html.escape(location, quote=True)}">Return</a>'.encode()
    )
    assert _queue_files(ctx) == []


def test_site_detail_renders_success_and_failure_flash_state_escaped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_site_detail_reads(monkeypatch, _location())
    unsafe_error = 'Rejected <img src=x onerror="secret"> & retry'
    ctx = RecordingContext(
        tmp_path,
        {
            "message": ["Operational calendar update queued."],
            "error": [unsafe_error],
        },
    )

    rendered = site_detail.render(ctx, SITE_ID)

    assert (
        '<section class="notice success"><p>'
        "Operational calendar update queued.</p></section>"
    ) in rendered
    assert (
        '<section class="error"><p>'
        "Rejected &lt;img src=x onerror=&quot;secret&quot;&gt; "
        "&amp; retry</p></section>"
    ) in rendered
    assert unsafe_error not in rendered
    assert "<img src=x" not in rendered
