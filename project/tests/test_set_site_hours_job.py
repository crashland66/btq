from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import queue_processor.main as qp
from btq_vault.entity_types import OPERATOR_ID_GREG
from btq_vault.facility_hours import facility_hours_from_doc, facility_open_state_dict
from queue_processor.canonical_rmw import resolve_site_context
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import site_hours
from queue_processor.registry import JOB_HANDLERS
from queue_spec import JOB_SET_SITE_HOURS, validate_job

from test_queue_processor_couchdb_write import RecordingRmwVaultStore, context_for, job, make_queue_file


def _phn_hours(**overrides: Any) -> dict[str, Any]:
    hours: dict[str, Any] = {
        "status": "verified",
        "last_verified_at": "2026-06-26",
        "last_verified_by": "Greg",
        "source": "operator_verified",
        "note": "Public-safe synthetic fixture.",
        "weekly": {
            "mon": [{"open": "08:30", "close": "17:00"}],
            "tue": [{"open": "08:30", "close": "17:00"}],
            "wed": [{"open": "08:30", "close": "17:00"}],
            "thu": [{"open": "08:30", "close": "17:00"}],
            "fri": [{"open": "08:30", "close": "15:00"}],
            "sat": [],
            "sun": [],
        },
        "exceptions": [
            {
                "rule": "nth_weekday",
                "weekday": "tue",
                "ordinals": [2, 4],
                "hours": [{"open": "10:00", "close": "19:00"}],
                "note": "Second and fourth Tuesday",
            },
            {
                "rule": "date",
                "date": "2026-12-25",
                "hours": [],
                "note": "Closed",
            },
        ],
    }
    hours.update(overrides)
    return hours


def _location_doc(
    *, facility_hours: dict[str, Any] | None = None, job_ids: list[str] | None = None
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "location_7060",
        "type": "location",
        "operator": OPERATOR_ID_GREG,
        "site_id": "7060",
        "location": "Continental Metalworks",
        "account": "Contworks",
        "content": "Keep the operational notes intact.",
        "btq_job_ids": list(job_ids or []),
    }
    if facility_hours is not None:
        doc["facility_hours"] = facility_hours
    return doc


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "site_id": "7060",
        "action": "set",
        "facility_hours": _phn_hours(),
        "actor": "Greg",
        "source": "ops_dashboard_site_detail",
    }
    payload.update(overrides)
    return payload


def _validatable(payload: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": "job-one", "job_type": JOB_SET_SITE_HOURS, "payload": payload}


def _run(
    store: RecordingRmwVaultStore,
    context,
    payload: dict[str, Any],
    job_id: str,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, job_id)
    processed_dir = context.runtime_root / "processed"
    qp.process_set_site_hours_job(queue_file, job(JOB_SET_SITE_HOURS, payload, job_id=job_id), context, processed_dir)
    return queue_file, processed_dir


def test_registry_dispatch_points_at_handler() -> None:
    assert JOB_HANDLERS[JOB_SET_SITE_HOURS] is site_hours.process_set_site_hours_job


def test_absent_facility_hours_reads_as_unknown() -> None:
    hours = facility_hours_from_doc(_location_doc())

    assert hours["status"] == "unknown"
    assert hours["weekly"]["mon"] == []
    assert hours["exceptions"] == []


def test_validate_accepts_set_and_clear() -> None:
    assert validate_job(_validatable(_payload())) is True
    assert validate_job(_validatable({"site_id": "7060", "action": "clear", "actor": "Greg"})) is True


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_payload(facility_hours=_phn_hours(status="trusted")), id="bad-status"),
        pytest.param(_payload(facility_hours=_phn_hours(weekly={"monday": []})), id="bad-weekday"),
        pytest.param(_payload(facility_hours=_phn_hours(weekly={"mon": [{"open": "8:30", "close": "17:00"}]})), id="bad-time"),
        pytest.param(
            _payload(
                facility_hours=_phn_hours(
                    exceptions=[
                        {
                            "rule": "nth_weekday",
                            "weekday": "tue",
                            "ordinals": [6],
                            "hours": [{"open": "10:00", "close": "19:00"}],
                        }
                    ]
                )
            ),
            id="bad-ordinal",
        ),
        pytest.param(
            _payload(
                facility_hours=_phn_hours(
                    exceptions=[
                        {
                            "rule": "holiday",
                            "date": "2026-12-25",
                            "hours": [],
                        }
                    ]
                )
            ),
            id="bad-rule",
        ),
        pytest.param(_payload(actor=""), id="blank-actor"),
    ],
)
def test_validate_rejects_invalid_payload(payload: dict[str, Any]) -> None:
    assert validate_job(_validatable(payload)) is False


def test_set_via_job_populates_valid_structure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_location_doc()])

    queue_file, processed_dir = _run(store, context, _payload(), "hours-set", monkeypatch)

    doc = store.get_optional("location_7060")
    assert doc is not None
    assert doc["facility_hours"]["status"] == "verified"
    assert doc["facility_hours"]["weekly"]["fri"] == [{"open": "08:30", "close": "15:00"}]
    assert doc["content"] == "Keep the operational notes intact."
    assert doc["btq_job_ids"] == ["hours-set"]
    assert store.update_doc_calls == ["location_7060"]
    assert (processed_dir / queue_file.name).exists()


def test_clear_via_job_removes_facility_hours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_location_doc(facility_hours=_phn_hours())])

    _run(store, context, {"site_id": "7060", "action": "clear", "actor": "Greg"}, "hours-clear", monkeypatch)

    doc = store.get_optional("location_7060")
    assert doc is not None
    assert "facility_hours" not in doc
    assert doc["btq_job_ids"] == ["hours-clear"]


def test_resolver_output_includes_facility_hours() -> None:
    store = RecordingRmwVaultStore([_location_doc(facility_hours=_phn_hours())])

    ctx = resolve_site_context(store, "7060")

    assert ctx.facility_hours["status"] == "verified"
    assert ctx.facility_hours["weekly"]["mon"] == [{"open": "08:30", "close": "17:00"}]


def test_is_open_helper_regular_nth_weekday_date_closure_and_weekend() -> None:
    tz = ZoneInfo("America/New_York")
    hours = _phn_hours()

    regular_monday = facility_open_state_dict(hours, datetime(2026, 6, 8, 9, 0, tzinfo=tz))
    assert regular_monday["is_open"] is True
    assert regular_monday["next_close"] == "2026-06-08T17:00:00-04:00"

    second_tuesday_before_special_open = facility_open_state_dict(hours, datetime(2026, 6, 9, 9, 0, tzinfo=tz))
    assert second_tuesday_before_special_open["is_open"] is False
    assert second_tuesday_before_special_open["next_open"] == "2026-06-09T10:00:00-04:00"

    normal_tuesday = facility_open_state_dict(hours, datetime(2026, 6, 16, 9, 0, tzinfo=tz))
    assert normal_tuesday["is_open"] is True
    assert normal_tuesday["next_close"] == "2026-06-16T17:00:00-04:00"

    date_closure = facility_open_state_dict(hours, datetime(2026, 12, 25, 10, 0, tzinfo=tz))
    assert date_closure["is_open"] is False
    assert date_closure["next_open"] == "2026-12-28T08:30:00-05:00"

    weekend = facility_open_state_dict(hours, datetime(2026, 6, 13, 10, 0, tzinfo=tz))
    assert weekend["is_open"] is False
    assert weekend["next_open"] == "2026-06-15T08:30:00-04:00"
