from __future__ import annotations

import json
import re
from pathlib import Path

from nightly_digest_builder import DigestPaths, build_digest, normalized_digest_for_hash


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def seed_digest_inputs(tmp_path: Path) -> DigestPaths:
    vault_root = tmp_path / "vault"
    local_root = tmp_path / "local"
    runtime_root = tmp_path / "runtime"
    logs_dir = tmp_path / "logs"

    journal_path = vault_root / "Journal" / "2026-04-20.md"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        "---\n"
        "type: journal\n"
        "date: 2026-04-20\n"
        "---\n\n"
        "# Daily Journal\n\n"
        "2026-04-20: Edwin Davis submitted resignation via WhatsApp.\n"
        "Damon Carver did not report to shift and did not respond.\n"
        "Damon Carver status: presumed resigned.\n"
        "Maria Hutton called off using unrecognized number 536-3552.\n"
        "Plumber identified slop sink hose as root cause of leak.\n",
        encoding="utf-8",
    )
    (vault_root / "Journal" / "2026-04-20-shift-report.md").write_text(
        "# Shift Report\n\n"
        "Damon Carver resigned.\n"
        "Summit Wire has 2 open position(s).\n"
        "Security badge access requires supervisor escort.\n",
        encoding="utf-8",
    )
    (vault_root / "state.md").write_text(
        "---\nupdated: 2026-04-19\n---\n\n# State\n",
        encoding="utf-8",
    )

    write_json(
        local_root / "events_valid" / "event-1.json",
        {
            "event_id": "event-1",
            "type": "employee_resigned",
            "site": "Summit Wire",
            "employee": "Edwin Davis",
            "details": "Edwin Davis submitted resignation via WhatsApp.",
            "timestamp": "2026-04-20T09:00:00Z",
        },
    )
    write_json(
        local_root / "events_valid" / "event-2.json",
        {
            "event_id": "event-2",
            "type": "employee_callout",
            "site": "Summit Wire",
            "employee": "Maria Hutton",
            "details": "Maria Hutton called off using unrecognized number 536-3552.",
            "timestamp": "2026-04-20T10:00:00Z",
        },
    )
    write_json(
        local_root / "events_valid" / "event-3.json",
        {
            "event_id": "event-3",
            "type": "staffing_risk",
            "site": "Summit Wire",
            "details": "Summit Wire critically short.",
            "timestamp": "2026-04-20T10:30:00Z",
            "open_positions": 2,
            "severity": "critical",
        },
    )
    write_json(
        local_root / "events_valid" / "event-4.json",
        {
            "event_id": "event-4",
            "type": "access_constraint",
            "site": "Summit Wire",
            "details": "Plumber identified slop sink hose as root cause of leak.",
            "timestamp": "2026-04-20T11:00:00Z",
        },
    )
    write_json(
        local_root / "events_failed" / "bad-event.json",
        {
            "event_id": "bad-event",
            "type": "access_constraint",
            "site": "unknown",
            "details": "Bad event.",
            "timestamp": "2026-04-20T12:20:00Z",
        },
    )
    write_json(
        runtime_root / "processed" / "job-1.json",
        {
            "job_id": "job-1",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-04-20.md",
                "content": "ACTION NEEDED - follow up with HR.",
                "destination": "journal",
            },
        },
    )
    write_json(
        runtime_root / "failed" / "job-2.json",
        {
            "job_id": "job-2",
            "job_type": "trigger_recruiting",
            "payload": {
                "site": "Unknown Site",
                "priority": "medium",
                "details": "Needs coverage.",
                "date": "2026-04-20",
            },
        },
    )

    unknown_path = vault_root / "Journal" / "2026-04-20-unknown.md"
    unknown_path.write_text(
        "---\n"
        "type: unknown_capture\n"
        "timestamp: 2026-04-20T01:00:00Z\n"
        "audio_file: sample.m4a\n"
        "status: unresolved\n"
        "retry_count: 1\n"
        "last_attempted: 2026-04-20T02:00:00Z\n"
        "---\n\n"
        "## Original Transcript\n"
        "Original text.\n\n"
        "## Normalized Transcript\n"
        "Unknown cleaner called from an unclear number.\n\n"
        "## Notes\n"
        "#unknown #needs-review\n",
        encoding="utf-8",
    )

    about_path = vault_root / "Accounts" / "Summit" / "Locations" / "7050 - Summit Wire" / "about.md"
    about_path.parent.mkdir(parents=True, exist_ok=True)
    about_path.write_text(
        "---\n"
        "type: location\n"
        "location: Summit Wire\n"
        "job: 7050\n"
        "---\n\n"
        "---\n"
        "type: visit_gap\n"
        "site: Summit Wire\n"
        "date: 2026-04-20\n"
        'reason: "event_without_visit"\n'
        "---\n",
        encoding="utf-8",
    )

    return DigestPaths(
        vault_root=vault_root,
        local_root=local_root,
        runtime_root=runtime_root,
        logs_dir=logs_dir,
    )


def extract_section(text: str, heading: str) -> str:
    pattern = re.compile(rf"## {re.escape(heading)}\n(.*?)(?=\n## |\Z)", re.DOTALL)
    match = pattern.search(text)
    assert match is not None
    return match.group(1)


def test_build_digest_is_deterministic_except_dynamic_meta(tmp_path: Path) -> None:
    paths = seed_digest_inputs(tmp_path)
    digest_one = build_digest("2026-04-20", paths)
    digest_two = build_digest("2026-04-20", paths)

    assert digest_one != digest_two
    assert normalized_digest_for_hash(digest_one) == normalized_digest_for_hash(digest_two)

    hash_one = re.search(r"deterministic_hash: ([0-9a-f]{64})", digest_one)
    hash_two = re.search(r"deterministic_hash: ([0-9a-f]{64})", digest_two)
    assert hash_one is not None
    assert hash_two is not None
    assert hash_one.group(1) == hash_two.group(1)


def test_build_digest_event_count_and_validation_are_consistent(tmp_path: Path) -> None:
    paths = seed_digest_inputs(tmp_path)
    digest = build_digest("2026-04-20", paths)

    assert "events_detected: 11" in digest
    assert "events_included: 7" in digest
    validation = extract_section(digest, "Validation")
    assert "- events_detected: 11" in validation
    assert "- merged_event_count: 11" in validation
    assert "- events_included: 7" in validation
    assert "- deduplicated_event_count: 7" in validation
    assert "- journal: 4" in validation
    assert "- report: 3" in validation
    assert "- system: 4" in validation
    assert "duplicate_fact" in validation
    assert "uncertain_status_claim" in validation
    assert "state_checksum:" in validation


def test_build_digest_separates_event_log_and_derived_signals(tmp_path: Path) -> None:
    paths = seed_digest_inputs(tmp_path)
    digest = build_digest("2026-04-20", paths)

    event_log = extract_section(digest, "Event Log (chronological)")
    derived = extract_section(digest, "Derived Signals")

    assert "source=system | 2026-04-20T09:00:00Z | Edwin Davis submitted resignation via WhatsApp." in event_log
    assert "source=journal | Damon Carver did not report to shift and did not respond." in event_log
    assert "Summit Wire has 2 open position(s)" in event_log
    assert "source=report | Security badge access requires supervisor escort." in event_log
    assert "source=report | Damon Carver resigned." in event_log
    assert "Summit Wire critically short" not in event_log
    assert "presumed resigned" not in event_log

    assert "Summit Wire staffing gap active" in derived
    assert "Call-out protocol unclear" in derived


def test_build_digest_preserves_cross_source_conflicts(tmp_path: Path) -> None:
    paths = seed_digest_inputs(tmp_path)
    digest = build_digest("2026-04-20", paths)

    ambiguities = extract_section(digest, "Ambiguities / Unresolved")

    assert "Status differs for Damon Carver" in ambiguities
    assert "journal = Damon Carver status: presumed resigned." in ambiguities
    assert "report = Damon Carver resigned." in ambiguities


def test_build_digest_checksum_is_stable(tmp_path: Path) -> None:
    paths = seed_digest_inputs(tmp_path)
    digest_one = build_digest("2026-04-20", paths)
    digest_two = build_digest("2026-04-20", paths)

    checksum_one = re.search(r"state_checksum: ([0-9a-f]{64})", digest_one)
    checksum_two = re.search(r"state_checksum: ([0-9a-f]{64})", digest_two)
    assert checksum_one is not None
    assert checksum_two is not None
    assert checksum_one.group(1) == checksum_two.group(1)
