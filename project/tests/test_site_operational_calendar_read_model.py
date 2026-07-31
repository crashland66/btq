"""Independent verifier coverage for the operator-scoped site calendar read model.

Every identity and document in this file is synthetic.  Tests use the public
injection seams or replace the shared read client, so no live CouchDB is read.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from event_pipeline import couchdb_config
from event_pipeline import site_operational_calendar as read_model
from event_pipeline.btq_client import BTQClientError
from event_pipeline.site_operational_calendar import (
    SiteOperationalCalendarError,
    site_calendar_report,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "btq-site-calendar"
WINDOW_START = "2026-08-10"
WINDOW_END = "2026-08-20"


def _event(
    event_id: str = "district-closure",
    *,
    start_date: str = "2026-08-12",
    end_date: str | None = None,
    kind: str = "no_student_day",
    label: str = "District closure",
    student_status: str = "no_students",
    facility_status: str = "closed",
    bt_service_impact: str = "confirm",
    **extra: Any,
) -> dict[str, Any]:
    value = {
        "event_id": event_id,
        "start_date": start_date,
        "end_date": end_date or start_date,
        "kind": kind,
        "label": label,
        "student_status": student_status,
        "facility_status": facility_status,
        "bt_service_impact": bt_service_impact,
    }
    value.update(extra)
    return value


def _calendar(
    calendar_id: str = "synthetic-district-2026",
    *,
    events: list[dict[str, Any]] | None = None,
    status: str = "verified",
    valid_from: str = "2026-01-01",
    valid_through: str = "2026-12-31",
    label: str = "Synthetic district calendar",
    **extra: Any,
) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "calendar_id": calendar_id,
        "label": label,
        "timezone": "America/New_York",
        "status": status,
        "valid_from": valid_from,
        "valid_through": valid_through,
        "last_verified_at": "2026-07-30T12:00:00-04:00",
        "last_verified_by": "Synthetic Verifier",
        "source": {
            "kind": "synthetic_document",
            "title": "Synthetic public calendar",
            "retrieved_at": "2026-07-30T11:30:00-04:00",
            "document_url": "https://calendar.example.invalid/synthetic.pdf",
        },
        "events": list(events if events is not None else [_event()]),
    }
    value.update(extra)
    return value


def _account(site_id: str, name: str | None = None, **extra: Any) -> dict[str, Any]:
    value = {
        "kind": "account",
        "job_number": site_id,
        "canonical_name": name or f"Synthetic Site {site_id}",
        "status": "active",
    }
    value.update(extra)
    return value


def _snapshot(*site_ids: str, operator: str = "Synthetic Operator") -> dict[str, Any]:
    return {
        "operator": operator,
        "accounts": [_account(site_id) for site_id in site_ids],
        "people": [],
        "generated_at": "2026-07-30T16:00:00+00:00",
    }


def _location(
    site_id: str,
    *,
    calendars: Any = None,
    status: str | None = None,
    active: bool | None = None,
    doc_type: str = "location",
    name: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "_id": f"location_{site_id}",
        "_rev": "1-synthetic",
        "type": doc_type,
        "site_id": site_id,
        "location": name or f"Synthetic Location {site_id}",
    }
    if calendars is not None:
        value["operational_calendars"] = calendars
    if status is not None:
        value["status"] = status
    if active is not None:
        value["active"] = active
    return value


def _load_cli() -> Any:
    loader = importlib.machinery.SourceFileLoader(
        "btq_site_calendar_verifier_cli",
        str(SCRIPT_PATH),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli() -> Any:
    return _load_cli()


def _run_cli(cli: Any, argv: list[str], *, builder: Any) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = cli.main(argv, report_builder=builder)
    return result, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from_date", "2026-8-10"),
        ("from_date", "2026-08-10 "),
        ("from_date", "2026-02-30"),
        ("from_date", "2026-08-10T00:00:00"),
        ("from_date", 20260810),
        ("through_date", "2026-8-20"),
        ("through_date", "2026-08-20Z"),
        ("through_date", "2026-13-01"),
        ("through_date", None),
    ],
)
def test_dates_are_strict_real_iso_dates(field: str, value: object) -> None:
    kwargs = {"from_date": WINDOW_START, "through_date": WINDOW_END}
    kwargs[field] = value

    with pytest.raises(SiteOperationalCalendarError, match=field):
        site_calendar_report(
            "Synthetic Operator",
            kwargs["from_date"],
            kwargs["through_date"],
            snapshot=_snapshot(),
            docs=[],
        )


def test_reversed_window_fails_before_scope_or_store_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("invalid dates must fail before any live resolution")

    monkeypatch.setattr(read_model, "operator_context_snapshot", unexpected)

    with pytest.raises(
        SiteOperationalCalendarError,
        match="through_date must be on or after from_date",
    ):
        site_calendar_report("Synthetic Operator", WINDOW_END, WINDOW_START)


def test_scope_is_derived_by_operator_context_snapshot_from_injected_canonical_docs() -> None:
    accounts = [
        {
            "_id": "site_100",
            "type": "site",
            "site_id": "100",
            "account": "Synthetic North",
            "status": "active",
        },
        {
            "_id": "site_200",
            "type": "site",
            "site_id": "200",
            "account": "Synthetic South",
            "status": "active",
        },
    ]
    people = [
        {
            "_id": "employee_operator",
            "type": "employee",
            "name": "Synthetic Operator",
            "site_ids": ["100"],
        }
    ]
    docs = {
        "accounts": accounts,
        "people": people,
        "locations": [
            _location("100", calendars=[_calendar()]),
            _location(
                "200",
                calendars=[
                    _calendar(
                        events=[_event("out-of-scope", label="Must not leak")]
                    )
                ],
            ),
        ],
    }

    report = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        docs=docs,
    )

    assert report["operator"] == "Synthetic Operator"
    assert [site["site_id"] for site in report["events"][0]["affected_sites"]] == [
        "100"
    ]
    assert "Must not leak" not in json.dumps(report)


def test_only_in_scope_active_canonical_location_documents_contribute() -> None:
    docs = [
        _location("100", calendars=[_calendar()], name="Included"),
        _location(
            "200",
            calendars=[_calendar(events=[_event("inactive", label="Inactive")])],
            status="closed",
        ),
        _location(
            "300",
            calendars=[_calendar(events=[_event("disabled", label="Disabled")])],
            active=False,
        ),
        _location(
            "400",
            calendars=[_calendar(events=[_event("wrong-type", label="Wrong type")])],
            doc_type="site",
        ),
        _location(
            "999",
            calendars=[_calendar(events=[_event("outside", label="Out of scope")])],
        ),
    ]

    report = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot("100", "200", "300", "400"),
        docs=docs,
    )

    assert len(report["events"]) == 1
    assert report["events"][0]["affected_sites"] == [
        {"site_id": "100", "site_name": "Included"}
    ]
    assert report["diagnostics"] == []


def test_injected_snapshot_and_docs_are_a_pure_core_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("pure injected path attempted a live read")

    monkeypatch.setattr(read_model, "operator_context_snapshot", unexpected)
    monkeypatch.setattr(read_model.btq_client, "find", unexpected)

    report = site_calendar_report(
        "ignored query",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot("100", operator="Canonical Synthetic Operator"),
        docs=[_location("100", calendars=[_calendar()])],
    )

    assert report["operator"] == "Canonical Synthetic Operator"
    assert len(report["events"]) == 1


def test_live_path_reuses_shared_resolver_config_and_btq_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], int]] = []

    monkeypatch.setattr(
        read_model,
        "operator_context_snapshot",
        lambda operator, *, config=None: _snapshot("100", operator=str(operator)),
    )

    def fake_find(
        database: str,
        selector: dict[str, Any],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        calls.append((database, selector, limit))
        return [_location("100", calendars=[_calendar()])]

    monkeypatch.setattr(read_model.btq_client, "find", fake_find)

    report = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
    )

    assert len(report["events"]) == 1
    assert calls == [
        (
            couchdb_config.vault_database(),
            {
                "type": "location",
                "_id": {"$in": ["location_100"]},
            },
            1,
        )
    ]

    source = Path(read_model.__file__).read_text(encoding="utf-8")
    imports = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(name == "urllib" or name.startswith("urllib.") for name in imports)


def test_overlap_is_inclusive_and_events_and_sites_are_deterministically_ordered() -> None:
    events = [
        _event(
            "after",
            start_date="2026-08-21",
            label="After window",
        ),
        _event(
            "through-edge",
            start_date=WINDOW_END,
            label="Through edge",
            bt_service_impact="normal",
            student_status="in_session",
            facility_status="open",
        ),
        _event(
            "before",
            start_date="2026-08-09",
            label="Before window",
        ),
        _event(
            "from-edge-overlap",
            start_date="2026-08-08",
            end_date=WINDOW_START,
            label="From edge overlap",
        ),
        _event(
            "middle",
            start_date="2026-08-15",
            label="Middle",
        ),
    ]
    docs = [
        _location("20", calendars=[_calendar(events=copy.deepcopy(events))]),
        _location("3", calendars=[_calendar(events=copy.deepcopy(events))]),
    ]

    first = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot("20", "3"),
        docs=docs,
    )
    second = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot("3", "20"),
        docs=list(reversed(docs)),
    )

    assert first == second
    assert [event["event_id"] for event in first["events"]] == [
        "from-edge-overlap",
        "middle",
        "through-edge",
    ]
    assert first["events"][0]["days_until_start"] == -2
    assert first["events"][0]["affected_sites"] == [
        {"site_id": "3", "site_name": "Synthetic Location 3"},
        {"site_id": "20", "site_name": "Synthetic Location 20"},
    ]


def test_grouping_uses_full_normalized_event_meaning_and_exact_service_impact() -> None:
    base = _event(
        label="  Shared   closure ",
        bt_service_impact="confirm",
    )
    same_after_normalization = _event(
        label="Shared closure",
        bt_service_impact="confirm",
    )
    different_meaning = _event(
        label="Different closure meaning",
        bt_service_impact="confirm",
    )
    different_impact = _event(
        label="Shared closure",
        bt_service_impact="no_service",
    )
    docs = [
        _location("40", calendars=[_calendar(events=[different_impact])]),
        _location("30", calendars=[_calendar(events=[different_meaning])]),
        _location("20", calendars=[_calendar(events=[same_after_normalization])]),
        _location("10", calendars=[_calendar(events=[base])]),
    ]

    report = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot("10", "20", "30", "40"),
        docs=docs,
    )

    assert len(report["events"]) == 3
    by_meaning = {
        (event["label"], event["bt_service_impact"]): event
        for event in report["events"]
    }
    assert by_meaning[("Shared closure", "confirm")]["affected_sites"] == [
        {"site_id": "10", "site_name": "Synthetic Location 10"},
        {"site_id": "20", "site_name": "Synthetic Location 20"},
    ]
    assert by_meaning[("Different closure meaning", "confirm")][
        "affected_sites"
    ] == [{"site_id": "30", "site_name": "Synthetic Location 30"}]
    assert by_meaning[("Shared closure", "no_service")]["affected_sites"] == [
        {"site_id": "40", "site_name": "Synthetic Location 40"}
    ]
    assert {event["bt_service_impact"] for event in report["events"]} == {
        "confirm",
        "no_service",
    }


def test_service_impact_is_preserved_without_inference_from_other_statuses() -> None:
    events = [
        _event(
            "closed-but-normal",
            start_date="2026-08-11",
            label="Closed but explicitly normal",
            student_status="no_students",
            facility_status="closed",
            bt_service_impact="normal",
        ),
        _event(
            "open-but-unknown",
            start_date="2026-08-12",
            label="Open but explicitly unknown",
            student_status="in_session",
            facility_status="open",
            bt_service_impact="unknown",
        ),
        _event(
            "open-but-modified",
            start_date="2026-08-13",
            label="Open but explicitly modified",
            student_status="in_session",
            facility_status="open",
            bt_service_impact="modified",
        ),
    ]

    report = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot("100"),
        docs=[_location("100", calendars=[_calendar(events=events)])],
    )

    assert {
        event["event_id"]: event["bt_service_impact"] for event in report["events"]
    } == {
        "closed-but-normal": "normal",
        "open-but-unknown": "unknown",
        "open-but-modified": "modified",
    }


def test_diagnostics_cover_all_failure_states_only_for_in_scope_active_sites() -> None:
    stale_and_expired = _calendar(
        "stale-expired",
        status="stale",
        valid_from="2025-01-01",
        valid_through="2026-08-01",
        events=[],
    )
    missing_verification = _calendar("missing-verification", events=[])
    del missing_verification["last_verified_by"]
    malformed = _calendar("malformed", events=[])
    malformed["timezone"] = "Not/A_Timezone"
    problem_calendars = [
        stale_and_expired,
        missing_verification,
        malformed,
    ]
    docs = [
        _location("100", calendars=copy.deepcopy(problem_calendars)),
        _location(
            "200",
            calendars=copy.deepcopy(problem_calendars),
            status="inactive",
        ),
        _location("999", calendars=copy.deepcopy(problem_calendars)),
        _location(
            "300",
            calendars=copy.deepcopy(problem_calendars),
            doc_type="site",
        ),
    ]

    report = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot("100", "200", "300"),
        docs=docs,
    )

    assert report["events"] == []
    assert [(row["calendar_id"], row["kind"]) for row in report["diagnostics"]] == [
        ("malformed", "malformed"),
        ("missing-verification", "missing_verification"),
        ("stale-expired", "expired"),
        ("stale-expired", "stale"),
    ]
    assert {row["site_id"] for row in report["diagnostics"]} == {"100"}
    assert all(row["site_name"] == "Synthetic Location 100" for row in report["diagnostics"])


def test_malformed_calendar_container_is_diagnostic_only_in_active_scope() -> None:
    report = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot("100", "200"),
        docs=[
            _location("100", calendars={"not": "a list"}),
            _location("200", calendars={"not": "a list"}, active=False),
            _location("999", calendars={"not": "a list"}),
        ],
    )

    assert report["events"] == []
    assert report["diagnostics"] == [
        {
            "site_id": "100",
            "site_name": "Synthetic Location 100",
            "calendar_id": None,
            "calendar_label": None,
            "calendar_status": None,
            "kind": "malformed",
            "effective_calendar_state": "malformed",
            "message": "operational_calendars must be a list",
        }
    ]


def test_clean_empty_scope_and_empty_calendars_return_clean_stable_results() -> None:
    empty_scope = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot(),
        docs=[],
    )
    empty_calendars = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot("100"),
        docs=[_location("100", calendars=[])],
    )

    expected = {
        "operator": "Synthetic Operator",
        "from_date": WINDOW_START,
        "through_date": WINDOW_END,
        "events": [],
        "diagnostics": [],
    }
    assert empty_scope == expected
    assert empty_calendars == expected


def test_cli_json_and_text_are_byte_deterministic(cli: Any) -> None:
    report = site_calendar_report(
        "Synthetic Operator",
        WINDOW_START,
        WINDOW_END,
        snapshot=_snapshot("100"),
        docs=[_location("100", calendars=[_calendar()])],
    )

    def builder(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(report)

    json_args = [
        "--operator",
        "ignored",
        "--from",
        WINDOW_START,
        "--through",
        WINDOW_END,
        "--format",
        "json",
    ]
    rc_one, out_one, err_one = _run_cli(cli, json_args, builder=builder)
    rc_two, out_two, err_two = _run_cli(cli, json_args, builder=builder)
    assert (rc_one, rc_two, err_one, err_two) == (0, 0, "", "")
    assert out_one == out_two == json.dumps(report, indent=2, sort_keys=True) + "\n"

    text_args = [*json_args[:-1], "text"]
    rc_text, out_text, err_text = _run_cli(cli, text_args, builder=builder)
    assert (rc_text, err_text) == (0, "")
    assert out_text == (
        "Operator: Synthetic Operator\n"
        f"Window: {WINDOW_START} through {WINDOW_END}\n"
        "Events: 1\n"
        "- 2026-08-12 | District closure | confirm | "
        "Synthetic Location 100 (100)\n"
        "Diagnostics: 0\n"
    )


@pytest.mark.parametrize(
    ("error", "expected_rc", "expected_message"),
    [
        (
            SiteOperationalCalendarError("synthetic invalid window"),
            2,
            "btq-site-calendar: synthetic invalid window\n",
        ),
        (
            BTQClientError("couchdb_unavailable", "synthetic CouchDB outage"),
            1,
            "btq-site-calendar: synthetic CouchDB outage\n",
        ),
    ],
)
def test_cli_errors_use_stderr_and_nonzero_exit(
    cli: Any,
    error: Exception,
    expected_rc: int,
    expected_message: str,
) -> None:
    def builder(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise error

    rc, stdout, stderr = _run_cli(
        cli,
        [
            "--operator",
            "Synthetic Operator",
            "--from",
            WINDOW_START,
            "--through",
            WINDOW_END,
        ],
        builder=builder,
    )

    assert rc == expected_rc
    assert stdout == ""
    assert stderr == expected_message


def test_cli_is_repo_root_independent_and_invalid_dates_fail_on_stderr(
    tmp_path: Path,
) -> None:
    help_result = subprocess.run(
        [str(SCRIPT_PATH), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "operational-calendar events" in help_result.stdout
    assert help_result.stderr == ""

    invalid_result = subprocess.run(
        [
            str(SCRIPT_PATH),
            "--operator",
            "Synthetic Operator",
            "--from",
            "2026-8-10",
            "--through",
            WINDOW_END,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert invalid_result.returncode == 2
    assert invalid_result.stdout == ""
    assert invalid_result.stderr == (
        "btq-site-calendar: from_date must use strict ISO YYYY-MM-DD format\n"
    )
