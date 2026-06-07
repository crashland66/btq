from __future__ import annotations

import json
import logging
from pathlib import Path

import queue_spec
from field_capture.issue_routing import route_field_reported_issues


def write_intake(intake_dir: Path, name: str, payload: dict[str, object]) -> Path:
    intake_dir.mkdir(parents=True, exist_ok=True)
    path = intake_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def issue_capture(**overrides: object) -> dict[str, object]:
    capture: dict[str, object] = {
        "capture_id": "cap-photo-issue",
        "site_id": "7050",
        "site": "Summit Wire",
        "qc_category": "report_an_issue",
        "note": "drain backed up",
        "person_name": "Tom Walsh",
        "captured_at": "2026-05-27T10:15:00-04:00",
        "exported_at": "2026-05-27T10:16:00-04:00",
        "photos": [
            {
                "stored_path": "/srv/btq/runtime/uploads/2026-05-27/cap-photo-issue/drain.jpg",
                "upload_id": "2026-05-27/cap-photo-issue/drain.jpg",
            }
        ],
    }
    capture.update(overrides)
    return capture


def route(tmp_path: Path, *, limit: int | None = None) -> dict[str, int]:
    return route_field_reported_issues(
        tmp_path / "field_capture" / "intake",
        tmp_path / "queue",
        runtime_root=tmp_path,
        logger=logging.getLogger(f"test.issue_routing.{id(tmp_path)}"),
        limit=limit,
    )


def read_only_queue_job(queue_dir: Path) -> dict[str, object]:
    jobs = sorted(queue_dir.glob("log-site-issue-*.json"))
    assert len(jobs) == 1
    return json.loads(jobs[0].read_text(encoding="utf-8"))


def test_route_field_reported_issues_writes_queue_job_for_report_an_issue(tmp_path: Path) -> None:
    intake_dir = tmp_path / "field_capture" / "intake"
    write_intake(intake_dir, "cap-photo-issue.json", issue_capture())

    counts = route(tmp_path)

    assert counts == {"discovered": 1, "routed": 1, "skipped": 0, "failed": 0}
    job = read_only_queue_job(tmp_path / "queue")
    payload = job["payload"]
    assert job["job_type"] == "log_site_issue"
    assert payload["site_id"] == "7050"
    assert payload["title"] == "drain backed up"
    assert payload["reported_by"] == "Tom Walsh"
    assert payload["client_notified"] is False
    assert payload["resolution_trigger"] == "operator_resolves"
    assert payload["priority"] == "normal"
    assert payload["category"] == "other"
    assert payload["summary"] == "drain backed up"
    assert payload["source"] == "field_capture"
    assert payload["related_capture_ids"] == ["cap-photo-issue"]
    assert payload["related_media"] == ["/media/2026-05-27/cap-photo-issue/drain.jpg"]
    assert payload["source_artifacts"] == ["field_capture/intake/cap-photo-issue.json"]
    assert (intake_dir / "cap-photo-issue.issue_routed.json").exists()
    # Regression: the job must satisfy queue_spec so queue_processor accepts it at
    # consume time instead of quarantining it to runtime/failed/ (every field-reported
    # issue was silently dying before this passed).
    assert queue_spec.validate_job(job) is True


def test_route_field_reported_issues_skips_non_issue_captures(tmp_path: Path) -> None:
    write_intake(
        tmp_path / "field_capture" / "intake",
        "cap-photo-restrooms.json",
        issue_capture(capture_id="cap-photo-restrooms", qc_category="Restrooms"),
    )

    counts = route(tmp_path)

    assert counts == {"discovered": 0, "routed": 0, "skipped": 0, "failed": 0}
    assert not list((tmp_path / "queue").glob("*.json"))


def test_route_field_reported_issues_idempotent_on_marker(tmp_path: Path) -> None:
    write_intake(tmp_path / "field_capture" / "intake", "cap-photo-issue.json", issue_capture())

    first = route(tmp_path)
    second = route(tmp_path)

    assert first["routed"] == 1
    assert second == {"discovered": 1, "routed": 0, "skipped": 1, "failed": 0}
    assert len(list((tmp_path / "queue").glob("log-site-issue-*.json"))) == 1


def test_route_field_reported_issues_truncates_long_titles(tmp_path: Path) -> None:
    write_intake(tmp_path / "field_capture" / "intake", "cap-photo-issue.json", issue_capture(note="x" * 200))

    route(tmp_path)

    title = read_only_queue_job(tmp_path / "queue")["payload"]["title"]
    assert len(title) <= 60
    assert title.endswith("…")


def test_route_field_reported_issues_falls_back_title_when_note_blank(tmp_path: Path) -> None:
    write_intake(tmp_path / "field_capture" / "intake", "cap-photo-issue.json", issue_capture(note=""))

    route(tmp_path)

    title = read_only_queue_job(tmp_path / "queue")["payload"]["title"]
    assert title == "Field-reported issue at Summit Wire"


def test_route_field_reported_issues_skips_prospect_target_captures(tmp_path: Path, caplog) -> None:
    write_intake(
        tmp_path / "field_capture" / "intake",
        "cap-photo-prospect.json",
        issue_capture(capture_id="cap-photo-prospect", site_id="", target_type="prospect", target_id="kmf-birch-1"),
    )

    with caplog.at_level(logging.INFO):
        counts = route(tmp_path)

    assert counts == {"discovered": 1, "routed": 0, "skipped": 1, "failed": 0}
    assert "skip prospect-target capture=cap-photo-prospect target=prospect/kmf-birch-1" in caplog.text
    assert not list((tmp_path / "queue").glob("*.json"))


def test_route_field_reported_issues_honors_limit(tmp_path: Path) -> None:
    intake_dir = tmp_path / "field_capture" / "intake"
    for index in range(3):
        capture_id = f"cap-photo-issue-{index}"
        write_intake(intake_dir, f"{capture_id}.json", issue_capture(capture_id=capture_id))

    counts = route(tmp_path, limit=2)

    assert counts == {"discovered": 2, "routed": 2, "skipped": 0, "failed": 0}
    assert len(list((tmp_path / "queue").glob("log-site-issue-*.json"))) == 2


def test_route_field_reported_issues_handles_missing_site_id_for_location(tmp_path: Path) -> None:
    write_intake(
        tmp_path / "field_capture" / "intake",
        "cap-photo-location.json",
        issue_capture(capture_id="cap-photo-location", site_id="", target_type="location", target_id="7050"),
    )

    counts = route(tmp_path)

    assert counts["routed"] == 1
    assert read_only_queue_job(tmp_path / "queue")["payload"]["site_id"] == "7050"
