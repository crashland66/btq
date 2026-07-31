from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import pytest

import queue_processor.main as queue_main
from btq_vault.operational_calendar import (
    OperationalCalendarError,
    normalize_operational_calendar,
    normalize_operational_calendars,
    operational_calendars_from_doc,
)
from ops_dashboard.common import KNOWN_JOB_SUMMARY_TYPES, render_job_summary
from queue_processor.handlers import _shared
from queue_processor.handlers import site_operational_calendar
from queue_processor.registry import JOB_HANDLERS
from queue_spec import (
    ALLOWED_JOB_TYPES,
    JOB_SET_SITE_OPERATIONAL_CALENDAR,
    SITE_ROUTABILITY_JOB_TYPES,
    validate_job,
)

from test_queue_processor_couchdb_write import RecordingRmwVaultStore


SANDBOX_SITE_ID = "SANDBOX"
SANDBOX_DOC_ID = "location_SANDBOX"
SANDBOX_ACTOR = "Sandbox Operator"


def _event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": "first-student-day",
        "start_date": "2026-08-24",
        "end_date": "2026-08-24",
        "kind": "first_student_day",
        "label": "First student day",
        "student_status": "in_session",
        "facility_status": "open",
        "bt_service_impact": "normal",
    }
    event.update(overrides)
    return event


def _calendar(calendar_id: str = "sandbox-2026-2027", **overrides: Any) -> dict[str, Any]:
    calendar: dict[str, Any] = {
        "schema_version": 1,
        "calendar_id": calendar_id,
        "label": "Sandbox 2026-2027 operational calendar",
        "timezone": "America/New_York",
        "status": "verified",
        "valid_from": "2026-08-01",
        "valid_through": "2027-06-30",
        "last_verified_at": "2026-07-30T14:00:00-04:00",
        "last_verified_by": SANDBOX_ACTOR,
        "source": {
            "kind": "sandbox_document",
            "title": "Public-safe synthetic calendar",
            "retrieved_at": "2026-07-30T13:30:00-04:00",
            "document_url": "https://calendar.example.invalid/sandbox/calendar.pdf",
        },
        "events": [_event()],
    }
    calendar.update(overrides)
    return calendar


def _payload(
    *,
    action: str = "upsert",
    calendar_id: str = "sandbox-2026-2027",
    calendar: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "site_id": SANDBOX_SITE_ID,
        "action": action,
        "calendar_id": calendar_id,
        "actor": SANDBOX_ACTOR,
        "source": "sandbox_verifier",
    }
    if action == "upsert":
        payload["calendar"] = calendar if calendar is not None else _calendar(calendar_id)
    payload.update(overrides)
    return payload


def _job_dict(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "job_id": "sandbox-calendar-job",
        "job_type": JOB_SET_SITE_OPERATIONAL_CALENDAR,
        "payload": payload if payload is not None else _payload(),
    }


def _queue_job(payload: dict[str, Any], *, job_id: str = "sandbox-calendar-job") -> _shared.QueueJob:
    return _shared.QueueJob(
        job_id=job_id,
        job_type=JOB_SET_SITE_OPERATIONAL_CALENDAR,
        payload=payload,
        metadata={},
        intent={},
    )


def _context(tmp_path: Path, *, dry_run: bool = False) -> _shared.RunContext:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    return _shared.RunContext(
        project_root=tmp_path,
        runtime_root=runtime_root,
        log_path=runtime_root / "queue.log",
        dry_run=dry_run,
        valid_site_ids={SANDBOX_SITE_ID},
        site_id_to_opportunities_dir={},
    )


def _queue_file(context: _shared.RunContext, name: str) -> Path:
    path = context.runtime_root / name
    path.write_text("{}\n", encoding="utf-8")
    return path


def _location_doc(
    *,
    calendars: list[dict[str, Any]] | None = None,
    job_ids: list[str] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": SANDBOX_DOC_ID,
        "_rev": "7-synthetic",
        "type": "location",
        "site_id": SANDBOX_SITE_ID,
        "location": "Sandbox Site",
        "account": "Sandbox Account",
        "content": "Preserve this synthetic operational note.",
        "facility_hours": {"raw": "unrelated sandbox field"},
        "nested_extension": {"preserve": ["exactly", {"including": "shape"}]},
        "btq_job_ids": list(job_ids or []),
    }
    if calendars is not None:
        doc["operational_calendars"] = calendars
    return doc


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: RecordingRmwVaultStore,
    payload: dict[str, Any],
    *,
    job_id: str = "sandbox-calendar-job",
    source_name: str = "sandbox-calendar-job.json",
    dry_run: bool = False,
) -> tuple[Path, Path]:
    context = _context(tmp_path, dry_run=dry_run)
    monkeypatch.setattr(_shared, "_VAULT_STORE", store)
    source = _queue_file(context, source_name)
    processed = context.runtime_root / "processed"
    site_operational_calendar.process_set_site_operational_calendar_job(
        source,
        _queue_job(payload, job_id=job_id),
        context,
        processed,
    )
    return source, processed


def test_complete_calendar_normalizes_to_exact_schema_v1_shape() -> None:
    raw = _calendar(
        label="  Sandbox   operational calendar  ",
        last_verified_by="  Sandbox   Operator  ",
        note="  Preserve   source ambiguity.  ",
        source={
            "kind": "  sandbox_document ",
            "title": " Public-safe   synthetic calendar ",
            "retrieved_at": " 2026-07-30T13:30:00-04:00 ",
            "page_url": " https://calendar.example.invalid/sandbox ",
            "document_url": "https://calendar.example.invalid/sandbox/calendar.pdf",
        },
        events=[
            _event(
                label="  First   student day ",
                dismissal_time="15:05",
                note="  Synthetic   note. ",
            )
        ],
    )

    normalized = normalize_operational_calendar(raw)

    assert normalized == {
        "schema_version": 1,
        "calendar_id": "sandbox-2026-2027",
        "label": "Sandbox operational calendar",
        "timezone": "America/New_York",
        "status": "verified",
        "valid_from": "2026-08-01",
        "valid_through": "2027-06-30",
        "last_verified_at": "2026-07-30T14:00:00-04:00",
        "last_verified_by": SANDBOX_ACTOR,
        "source": {
            "kind": "sandbox_document",
            "title": "Public-safe synthetic calendar",
            "retrieved_at": "2026-07-30T13:30:00-04:00",
            "page_url": "https://calendar.example.invalid/sandbox",
            "document_url": "https://calendar.example.invalid/sandbox/calendar.pdf",
        },
        "events": [
            {
                "event_id": "first-student-day",
                "start_date": "2026-08-24",
                "end_date": "2026-08-24",
                "kind": "first_student_day",
                "label": "First student day",
                "student_status": "in_session",
                "facility_status": "open",
                "bt_service_impact": "normal",
                "dismissal_time": "15:05",
                "note": "Synthetic note.",
            }
        ],
        "note": "Preserve source ambiguity.",
    }
    assert validate_job(_job_dict(_payload(calendar=raw))) is True


@pytest.mark.parametrize(
    "missing_field",
    [
        "schema_version",
        "calendar_id",
        "label",
        "timezone",
        "status",
        "valid_from",
        "valid_through",
        "last_verified_at",
        "last_verified_by",
        "source",
        "events",
    ],
)
def test_calendar_rejects_every_missing_required_field(missing_field: str) -> None:
    raw = _calendar()
    del raw[missing_field]

    with pytest.raises(OperationalCalendarError, match="missing required fields"):
        normalize_operational_calendar(raw)
    assert validate_job(_job_dict(_payload(calendar=raw))) is False


@pytest.mark.parametrize("missing_field", ["kind", "title", "retrieved_at"])
def test_source_rejects_every_missing_required_field(missing_field: str) -> None:
    raw = _calendar()
    del raw["source"][missing_field]

    with pytest.raises(OperationalCalendarError, match="source is missing required fields"):
        normalize_operational_calendar(raw)
    assert validate_job(_job_dict(_payload(calendar=raw))) is False


@pytest.mark.parametrize(
    "missing_field",
    [
        "event_id",
        "start_date",
        "end_date",
        "kind",
        "label",
        "student_status",
        "facility_status",
        "bt_service_impact",
    ],
)
def test_event_rejects_every_missing_required_field(missing_field: str) -> None:
    raw = _calendar()
    del raw["events"][0][missing_field]

    with pytest.raises(OperationalCalendarError, match="missing required fields"):
        normalize_operational_calendar(raw)
    assert validate_job(_job_dict(_payload(calendar=raw))) is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("calendar", [], "operational calendar must be an object"),
        ("source", [], "source must be an object"),
        ("events", {}, "events must be a list"),
        ("event", "not-an-object", r"events\[0\] must be an object"),
    ],
)
def test_nested_container_types_fail_closed(
    field: str,
    value: object,
    message: str,
) -> None:
    if field == "calendar":
        raw: object = value
    else:
        raw = _calendar()
        if field == "event":
            raw["events"] = [value]
        else:
            raw[field] = value

    with pytest.raises(OperationalCalendarError, match=message):
        normalize_operational_calendar(raw)


@pytest.mark.parametrize("schema_version", [True, False, 0, 2, 1.0, "1", None])
def test_schema_version_is_exact_integer_one(schema_version: object) -> None:
    raw = _calendar(schema_version=schema_version)

    with pytest.raises(OperationalCalendarError, match="schema_version"):
        normalize_operational_calendar(raw)
    assert validate_job(_job_dict(_payload(calendar=raw))) is False


@pytest.mark.parametrize(
    ("level", "extra"),
    [
        ("payload", {"surprise": True}),
        ("calendar", {"client_hours": "not part of this contract"}),
        ("source", {"refresh_policy": "automatic"}),
        ("event", {"service_assumed_from_students": True}),
    ],
)
def test_unknown_keys_are_rejected_at_every_contract_level(
    level: str,
    extra: dict[str, Any],
) -> None:
    payload = _payload()
    if level == "payload":
        payload.update(extra)
    elif level == "calendar":
        payload["calendar"].update(extra)
    elif level == "source":
        payload["calendar"]["source"].update(extra)
    else:
        payload["calendar"]["events"][0].update(extra)

    assert validate_job(_job_dict(payload)) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "trusted"),
        ("kind", "weather_closure"),
        ("student_status", "closed"),
        ("facility_status", "no_students"),
        ("bt_service_impact", "implied_no_service"),
    ],
)
def test_unknown_enums_are_rejected_without_cross_field_inference(field: str, value: str) -> None:
    raw = _calendar()
    if field == "status":
        raw[field] = value
    else:
        raw["events"][0][field] = value

    with pytest.raises(OperationalCalendarError):
        normalize_operational_calendar(raw)
    assert validate_job(_job_dict(_payload(calendar=raw))) is False


def test_all_declared_enums_accept_and_independent_statuses_survive() -> None:
    calendar_statuses = ("verified", "reference", "stale")
    event_kinds = (
        "first_student_day",
        "final_student_day",
        "no_student_day",
        "school_break",
        "teacher_in_service",
        "early_dismissal",
        "holiday_dismissal",
        "snow_makeup_reserved",
        "flexible_instruction_reserved",
        "informational",
    )
    student_statuses = ("in_session", "no_students", "early_dismissal", "unknown")
    facility_statuses = ("open", "closed", "unknown")
    service_impacts = ("normal", "no_service", "modified", "confirm", "unknown")

    for value in calendar_statuses:
        assert normalize_operational_calendar(_calendar(status=value))["status"] == value
    for value in event_kinds:
        assert normalize_operational_calendar(
            _calendar(events=[_event(kind=value)])
        )["events"][0]["kind"] == value
    for value in student_statuses:
        assert normalize_operational_calendar(
            _calendar(events=[_event(student_status=value)])
        )["events"][0]["student_status"] == value
    for value in facility_statuses:
        assert normalize_operational_calendar(
            _calendar(events=[_event(facility_status=value)])
        )["events"][0]["facility_status"] == value
    for value in service_impacts:
        assert normalize_operational_calendar(
            _calendar(events=[_event(bt_service_impact=value)])
        )["events"][0]["bt_service_impact"] == value

    independent = normalize_operational_calendar(
        _calendar(
            events=[
                _event(
                    student_status="no_students",
                    facility_status="open",
                    bt_service_impact="confirm",
                )
            ]
        )
    )
    assert independent["events"][0] == {
        **_event(),
        "student_status": "no_students",
        "facility_status": "open",
        "bt_service_impact": "confirm",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("valid_from", "2026-02-30"),
        ("valid_from", "2026-8-01"),
        ("valid_from", "20260801"),
        ("valid_through", "2027-13-01"),
        ("start_date", "2026-09-31"),
        ("start_date", "2026-08-1"),
        ("end_date", "not-a-date"),
    ],
)
def test_coverage_and_event_dates_are_strict_real_iso_dates(field: str, value: str) -> None:
    raw = _calendar()
    if field in {"valid_from", "valid_through"}:
        raw[field] = value
    else:
        raw["events"][0][field] = value

    with pytest.raises(OperationalCalendarError):
        normalize_operational_calendar(raw)


def test_coverage_event_and_provenance_ordering_is_enforced_across_offsets() -> None:
    with pytest.raises(OperationalCalendarError, match="valid_through"):
        normalize_operational_calendar(
            _calendar(valid_from="2027-01-01", valid_through="2026-12-31")
        )
    with pytest.raises(OperationalCalendarError, match="end_date"):
        normalize_operational_calendar(
            _calendar(
                events=[
                    _event(start_date="2026-08-25", end_date="2026-08-24")
                ]
            )
        )
    with pytest.raises(OperationalCalendarError, match="outside"):
        normalize_operational_calendar(
            _calendar(
                events=[
                    _event(start_date="2026-07-31", end_date="2026-08-24")
                ]
            )
        )
    with pytest.raises(OperationalCalendarError, match="outside"):
        normalize_operational_calendar(
            _calendar(
                events=[
                    _event(start_date="2027-06-30", end_date="2027-07-01")
                ]
            )
        )
    with pytest.raises(OperationalCalendarError, match="retrieved_at.*after.*last_verified_at"):
        normalize_operational_calendar(
            _calendar(
                last_verified_at="2026-07-30T09:00:00-04:00",
                source={
                    **_calendar()["source"],
                    "retrieved_at": "2026-07-30T13:30:01Z",
                },
            )
        )

    normalized = normalize_operational_calendar(
        _calendar(
            last_verified_at="2026-07-30T09:30:00-04:00",
            source={
                **_calendar()["source"],
                "retrieved_at": "2026-07-30T13:30:00Z",
            },
        )
    )
    assert normalized["source"]["retrieved_at"] == "2026-07-30T13:30:00Z"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("last_verified_at", "not-a-timestamp"),
        ("last_verified_at", "2026-02-30T09:00:00-05:00"),
        ("last_verified_at", "2026-07-30"),
        ("last_verified_at", "2026-07-30T14:00:00"),
        ("last_verified_at", "2026-07-30 14:00:00-04:00"),
        ("last_verified_at", "20260730T140000-04:00"),
        ("retrieved_at", "not-a-timestamp"),
        ("retrieved_at", "2026-02-30T08:00:00-05:00"),
        ("retrieved_at", "2026-07-30"),
        ("retrieved_at", "2026-07-30T13:30:00"),
        ("retrieved_at", "2026-07-30 13:30:00-04:00"),
    ],
)
def test_verification_and_source_dates_are_strict_real_timezone_aware_iso_datetimes(
    field: str,
    value: str,
) -> None:
    raw = _calendar()
    if field == "last_verified_at":
        raw[field] = value
    else:
        raw["source"][field] = value

    with pytest.raises(OperationalCalendarError, match=field):
        normalize_operational_calendar(raw)
    assert validate_job(_job_dict(_payload(calendar=raw))) is False


def test_installed_iana_timezone_is_required() -> None:
    assert normalize_operational_calendar(_calendar(timezone="America/New_York"))[
        "timezone"
    ] == "America/New_York"

    for invalid in ("Mars/Olympus", "EST+05:00", "../America/New_York", ""):
        with pytest.raises(OperationalCalendarError, match="timezone"):
            normalize_operational_calendar(_calendar(timezone=invalid))


@pytest.mark.parametrize(
    "source",
    [
        {
            "kind": "sandbox_page",
            "title": "Synthetic page",
            "retrieved_at": "2026-07-30T13:30:00Z",
            "page_url": "https://calendar.example.invalid/path?year=2026",
        },
        {
            "kind": "sandbox_document",
            "title": "Synthetic document",
            "retrieved_at": "2026-07-30T13:30:00+00:00",
            "document_url": "http://calendar.example.invalid:8080/calendar.pdf",
        },
    ],
)
def test_source_accepts_either_absolute_http_or_https_url(source: dict[str, str]) -> None:
    assert normalize_operational_calendar(_calendar(source=source))["source"] == source


@pytest.mark.parametrize(
    "url",
    [
        "calendar.example.invalid/calendar.pdf",
        "/sandbox/calendar.pdf",
        "ftp://calendar.example.invalid/calendar.pdf",
        "javascript:alert(1)",
        "https://user@calendar.example.invalid/calendar.pdf",
        "https://user:secret@calendar.example.invalid/calendar.pdf",
        "https://calendar.example.invalid/calendar.pdf#section",
        "https://calendar.example.invalid/calendar file.pdf",
        "https:///calendar.pdf",
        "https://calendar.example.invalid:bad/calendar.pdf",
    ],
)
def test_source_url_must_be_absolute_safe_http_s_without_credentials_or_fragment(
    url: str,
) -> None:
    raw = _calendar(
        source={
            **_calendar()["source"],
            "document_url": url,
        }
    )

    with pytest.raises(OperationalCalendarError, match=r"HTTP\(S\) URL"):
        normalize_operational_calendar(raw)


def test_source_requires_at_least_one_url_and_rejects_blank_second_url() -> None:
    source_without_url = deepcopy(_calendar()["source"])
    del source_without_url["document_url"]
    with pytest.raises(OperationalCalendarError, match="requires page_url or document_url"):
        normalize_operational_calendar(_calendar(source=source_without_url))

    with pytest.raises(OperationalCalendarError, match="source.page_url"):
        normalize_operational_calendar(
            _calendar(source={**_calendar()["source"], "page_url": "  "})
        )


@pytest.mark.parametrize("value", ["00:00", "07:05", "15:30", "23:59"])
def test_optional_dismissal_time_accepts_strict_hh_mm(value: str) -> None:
    with_time = normalize_operational_calendar(
        _calendar(events=[_event(dismissal_time=value)])
    )
    without_time = normalize_operational_calendar(_calendar())

    assert with_time["events"][0]["dismissal_time"] == value
    assert "dismissal_time" not in without_time["events"][0]


@pytest.mark.parametrize(
    "value",
    ["0:00", "7:05", "24:00", "23:60", "12:00:00", "12:00 PM", "", 1500, None],
)
def test_optional_dismissal_time_rejects_non_hh_mm(value: object) -> None:
    with pytest.raises(OperationalCalendarError, match="dismissal_time"):
        normalize_operational_calendar(
            _calendar(events=[_event(dismissal_time=value)])
        )


def test_duplicate_event_and_calendar_ids_are_rejected() -> None:
    duplicate_events = _calendar(
        events=[
            _event(),
            _event(
                start_date="2026-09-01",
                end_date="2026-09-01",
                label="Duplicate identifier",
            ),
        ]
    )
    with pytest.raises(OperationalCalendarError, match="duplicate event_id"):
        normalize_operational_calendar(duplicate_events)

    with pytest.raises(OperationalCalendarError, match="duplicate calendar_id"):
        normalize_operational_calendars(
            [_calendar(), _calendar(label="Duplicate calendar")]
        )


@pytest.mark.parametrize(
    "calendar_id",
    [
        "",
        "Sandbox-2026",
        "sandbox 2026",
        "sandbox--2026",
        "sandbox__2026",
        "-sandbox",
        "sandbox-",
        "sandbox.2026",
    ],
)
def test_calendar_and_event_ids_are_strict_stable_slugs(calendar_id: str) -> None:
    assert validate_job(
        _job_dict(_payload(calendar_id=calendar_id, calendar=_calendar(calendar_id)))
    ) is False

    with pytest.raises(OperationalCalendarError, match="event_id"):
        normalize_operational_calendar(
            _calendar(events=[_event(event_id=calendar_id)])
        )


def test_payload_action_calendar_pairing_and_matching_id_are_strict() -> None:
    assert validate_job(_job_dict(_payload())) is True
    assert validate_job(
        _job_dict(_payload(action="remove", calendar_id="sandbox-2026-2027"))
    ) is True
    assert validate_job(
        _job_dict(
            {
                **_payload(action="remove"),
                "calendar": _calendar(),
            }
        )
    ) is False
    assert validate_job(
        _job_dict(_payload(action="replace"))
    ) is False
    assert validate_job(
        _job_dict(
            _payload(
                calendar_id="sandbox-2026-2027",
                calendar=_calendar("sandbox-other"),
            )
        )
    ) is False


def test_absent_operational_calendars_reads_as_empty() -> None:
    assert operational_calendars_from_doc(None) == []
    assert operational_calendars_from_doc(_location_doc()) == []


def test_upsert_replaces_only_matching_calendar_and_preserves_raw_siblings_and_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matching = _calendar(
        "sandbox-current",
        label="Old matching calendar",
        status="reference",
    )
    raw_unrelated = _calendar(
        "sandbox-history",
        label="  Raw   historical label  ",
        status="stale",
        note="  Preserve   raw spacing.  ",
    )
    original = _location_doc(calendars=[matching, raw_unrelated])
    store = RecordingRmwVaultStore([original])
    replacement = _calendar(
        "sandbox-current",
        label="Replacement calendar",
        events=[
            _event(
                event_id="sandbox-closure",
                kind="no_student_day",
                label="Sandbox closure",
                student_status="no_students",
                facility_status="unknown",
                bt_service_impact="confirm",
            )
        ],
    )

    source, processed = _run(
        tmp_path,
        monkeypatch,
        store,
        _payload(calendar_id="sandbox-current", calendar=replacement),
        job_id="sandbox-upsert",
    )

    stored = store.get_optional(SANDBOX_DOC_ID)
    assert stored is not None
    assert stored["operational_calendars"] == [replacement, raw_unrelated]
    for key in (
        "_rev",
        "location",
        "account",
        "content",
        "facility_hours",
        "nested_extension",
    ):
        assert stored[key] == original[key]
    assert stored["btq_job_ids"] == ["sandbox-upsert"]
    assert store.update_doc_calls == [SANDBOX_DOC_ID]
    assert not source.exists()
    assert (processed / source.name).exists()


def test_upsert_appends_to_missing_list_and_remove_targets_only_one_calendar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingRmwVaultStore([_location_doc()])
    added = _calendar("sandbox-added")

    _run(
        tmp_path / "add",
        monkeypatch,
        store,
        _payload(calendar_id="sandbox-added", calendar=added),
        job_id="sandbox-add",
    )
    after_add = store.get_optional(SANDBOX_DOC_ID)
    assert after_add is not None
    assert after_add["operational_calendars"] == [added]

    history = _calendar("sandbox-history", status="stale")
    after_add["operational_calendars"].append(history)
    store = RecordingRmwVaultStore([after_add])
    _run(
        tmp_path / "remove",
        monkeypatch,
        store,
        _payload(action="remove", calendar_id="sandbox-added"),
        job_id="sandbox-remove",
    )

    after_remove = store.get_optional(SANDBOX_DOC_ID)
    assert after_remove is not None
    assert after_remove["operational_calendars"] == [history]
    assert after_remove["btq_job_ids"] == ["sandbox-add", "sandbox-remove"]
    assert after_remove["content"] == "Preserve this synthetic operational note."


class _ConflictStore(RecordingRmwVaultStore):
    def update_doc(
        self,
        doc_id: str,
        transform: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        *,
        create: Callable[[], dict[str, Any]] | None = None,
        require_existing: bool = True,
        max_conflict_retries: int = 1,
    ) -> dict[str, Any]:
        raise RuntimeError(f"synthetic conflict for {doc_id}")


@pytest.mark.parametrize(
    ("site_id", "store", "message"),
    [
        ("UNKNOWN-SANDBOX-SITE", RecordingRmwVaultStore([]), "Could not resolve"),
        (SANDBOX_SITE_ID, RecordingRmwVaultStore([]), "required document not found"),
        (
            SANDBOX_SITE_ID,
            _ConflictStore([_location_doc()]),
            "synthetic conflict",
        ),
    ],
)
def test_unknown_or_missing_site_and_final_conflict_fail_without_moving_queue_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site_id: str,
    store: RecordingRmwVaultStore,
    message: str,
) -> None:
    context = _context(tmp_path)
    monkeypatch.setattr(_shared, "_VAULT_STORE", store)
    source = _queue_file(context, "sandbox-failure.json")

    with pytest.raises(_shared.QueueJobError, match=message):
        site_operational_calendar.process_set_site_operational_calendar_job(
            source,
            _queue_job(_payload(site_id=site_id), job_id="sandbox-failure"),
            context,
            context.runtime_root / "processed",
        )

    assert source.exists()
    assert not (context.runtime_root / "processed" / source.name).exists()


def test_dry_run_is_inert_no_canonical_store_access_or_queue_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path, dry_run=True)
    source = _queue_file(context, "sandbox-dry-run.json")

    def fail_if_store_is_requested() -> None:
        raise AssertionError("dry-run must not request the canonical store")

    monkeypatch.setattr(_shared, "_vault_store", fail_if_store_is_requested)
    site_operational_calendar.process_set_site_operational_calendar_job(
        source,
        _queue_job(_payload(), job_id="sandbox-dry-run"),
        context,
        context.runtime_root / "processed",
    )

    assert source.read_text(encoding="utf-8") == "{}\n"
    assert not (context.runtime_root / "processed").exists()
    assert not (context.runtime_root / "mutation_evidence").exists()


def test_replay_is_idempotent_by_job_marker_and_moves_replayed_queue_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingRmwVaultStore([_location_doc()])
    context = _context(tmp_path)
    monkeypatch.setattr(_shared, "_VAULT_STORE", store)
    processed = context.runtime_root / "processed"
    job = _queue_job(_payload(), job_id="sandbox-replay")

    first = _queue_file(context, "sandbox-replay-first.json")
    site_operational_calendar.process_set_site_operational_calendar_job(
        first, job, context, processed
    )
    after_first = deepcopy(store.get_optional(SANDBOX_DOC_ID))

    second = _queue_file(context, "sandbox-replay-second.json")
    site_operational_calendar.process_set_site_operational_calendar_job(
        second, job, context, processed
    )

    assert store.get_optional(SANDBOX_DOC_ID) == after_first
    assert store.update_doc_calls == [SANDBOX_DOC_ID]
    assert after_first is not None
    assert after_first["btq_job_ids"] == ["sandbox-replay"]
    assert len(after_first["operational_calendars"]) == 1
    assert (processed / first.name).exists()
    assert (processed / second.name).exists()


def test_registry_target_hint_and_dashboard_summary_are_wired() -> None:
    payload = _payload()
    job = _queue_job(payload)
    context = _shared.RunContext(
        project_root=Path("/tmp"),
        runtime_root=Path("/tmp"),
        log_path=Path("/tmp/sandbox-calendar-queue.log"),
        dry_run=True,
        valid_site_ids={SANDBOX_SITE_ID},
        site_id_to_opportunities_dir={},
    )

    assert JOB_SET_SITE_OPERATIONAL_CALENDAR in ALLOWED_JOB_TYPES
    assert JOB_SET_SITE_OPERATIONAL_CALENDAR in SITE_ROUTABILITY_JOB_TYPES
    assert (
        JOB_HANDLERS[JOB_SET_SITE_OPERATIONAL_CALENDAR]
        is site_operational_calendar.process_set_site_operational_calendar_job
    )
    assert (
        queue_main.process_set_site_operational_calendar_job
        is site_operational_calendar.process_set_site_operational_calendar_job
    )
    assert queue_main.target_path_hint(job, context) == SANDBOX_DOC_ID
    assert JOB_SET_SITE_OPERATIONAL_CALENDAR in KNOWN_JOB_SUMMARY_TYPES

    summary = render_job_summary(JOB_SET_SITE_OPERATIONAL_CALENDAR, payload)
    assert "Set operational calendar" in summary
    assert "SANDBOX" in summary
    assert "upsert" in summary
    assert "sandbox-2026-2027" in summary
