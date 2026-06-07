from __future__ import annotations

import json
from pathlib import Path

from processing_core.approved_job_drafts import write_approved_job_draft
from processing_core.draft_staging import stage_approved_drafts, stage_approved_drafts_report


def write_named_draft(runtime_root: Path, draft_id: str) -> dict[str, object]:
    draft = {
        "type": "approved_queue_job_draft",
        "draft_id": draft_id,
        "status": "approved_draft",
        "candidate_id": f"ac_{draft_id}",
        "proposed_job_type": "append_to_note",
        "proposed_payload": {"path": "Accounts/Test/Site.md", "destination": "site_note", "content": f"{draft_id}\n"},
    }
    write_approved_job_draft(runtime_root / "reviews" / "approved_job_drafts" / "field_capture", draft)
    return draft


def seed_three_drafts(runtime_root: Path) -> list[dict[str, object]]:
    return [write_named_draft(runtime_root, f"ajd_filter_{index}") for index in range(3)]


def test_stage_approved_drafts_draft_ids_filter_stages_only_named(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    drafts = seed_three_drafts(runtime_root)

    counts = stage_approved_drafts(runtime_root / "reviews" / "approved_job_drafts" / "field_capture", runtime_root=runtime_root, draft_ids={str(drafts[0]["draft_id"])})

    queue_files = sorted((runtime_root / "queue").glob("*.json"))
    assert counts == {"discovered": 3, "skipped": 2, "completed": 1, "failed": 0}
    assert len(queue_files) == 1
    assert json.loads(queue_files[0].read_text(encoding="utf-8"))["metadata"]["draft_id"] == drafts[0]["draft_id"]


def test_stage_approved_drafts_draft_ids_filter_skips_others(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    drafts = seed_three_drafts(runtime_root)

    stage_approved_drafts(runtime_root / "reviews" / "approved_job_drafts" / "field_capture", runtime_root=runtime_root, draft_ids={str(drafts[1]["draft_id"])})

    staged_ids = [json.loads(path.read_text(encoding="utf-8"))["metadata"]["draft_id"] for path in (runtime_root / "queue").glob("*.json")]
    assert staged_ids == [drafts[1]["draft_id"]]
    assert drafts[0]["draft_id"] not in staged_ids
    assert drafts[2]["draft_id"] not in staged_ids


def test_stage_approved_drafts_report_draft_ids_filter_dry_run(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    drafts = seed_three_drafts(runtime_root)

    report = stage_approved_drafts_report(
        runtime_root / "reviews" / "approved_job_drafts" / "field_capture",
        runtime_root=runtime_root,
        dry_run=True,
        draft_ids={str(drafts[2]["draft_id"])},
    )

    assert report["dry_run"] is True
    assert report["counts"] == {"discovered": 3, "skipped": 2, "completed": 1, "failed": 0}
    assert [item["draft_id"] for item in report["results"]] == [drafts[2]["draft_id"]]
    assert not (runtime_root / "queue").exists()
