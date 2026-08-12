"""Independent gates for the read-only media-coverage audit (prompt 526).

Authored by the verifier from the 525 design contract (§2.7, §3.10). The audit
must argue from canonical capture metadata and R2 existence, count honestly,
disclose orphans in both directions, and perform zero writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import btq
from btq_cli import media_coverage


UPLOAD_ROOT = Path("/srv/example/uploads")


class RecordingStore:
    """A read-only store double that records every attribute access."""

    def __init__(self, present: set[str]) -> None:
        self.present = present
        self.exists_calls: list[str] = []

    def exists(self, key: str) -> bool:
        self.exists_calls.append(key)
        return key in self.present

    def __getattr__(self, name: str):  # pragma: no cover - failure path
        raise AssertionError(f"audit touched non-read method/attribute: {name}")


def _fixture() -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]], RecordingStore]:
    captures_by_site = {
        "site_a": [
            {
                "capture_id": "cap_1",
                "site_id": "site_a",
                "captured_at": "2026-06-10T09:00:00-04:00",
                "category": "site",
                "photos": [
                    {"filename": "a1.jpg", "upload_id": "2026-06-10/cap_1/a1.jpg"},
                    {"filename": "a2.jpg", "upload_id": "2026-06-10/cap_1/a2.jpg"},
                ],
            }
        ],
        "site_b": [
            {
                "capture_id": "cap_2",
                "site_id": "site_b",
                "captured_at": "2026-07-04T10:00:00-04:00",
                "category": "qc",
                "photos": [
                    {"filename": "escape.jpg", "stored_path": "../../etc/shadow"},
                    {"filename": "b1.jpg", "upload_id": "2026-07-04/cap_2/b1.jpg"},
                ],
            }
        ],
    }
    vision_docs = [
        {
            "_id": "v-a1",
            "capture_id": "cap_1",
            "photo_id": "2026-06-10/cap_1/a1.jpg",
            "description": "clean",
        },
        # Orphan: names a capture that does not exist canonically.
        {
            "_id": "v-orphan",
            "capture_id": "cap_ghost",
            "photo_id": "2026-06-01/cap_ghost/x.jpg",
        },
        # Known capture, but no photo carries this identity.
        {
            "_id": "v-dangling",
            "capture_id": "cap_2",
            "photo_id": "2026-07-04/cap_2/never-uploaded.jpg",
        },
    ]
    store = RecordingStore(present={"2026-06-10/cap_1/a1.jpg", "2026-06-10/cap_1/a2.jpg"})
    return captures_by_site, vision_docs, store


def test_audit_counts_are_exact_and_honest() -> None:
    captures, vision, store = _fixture()
    report = media_coverage.audit_media_coverage(
        captures_by_site_id=captures,
        vision_docs=vision,
        store=store,
        upload_root=UPLOAD_ROOT,
    )
    assert report.total_referenced_photos == 4
    assert report.valid_media_keys == 3
    assert report.invalid_media_keys == 1
    assert report.r2_present == 2
    assert report.r2_absent == 1
    assert report.earliest_capture_date_overall == "2026-06-10"
    assert report.latest_capture_date_overall == "2026-07-04"
    assert report.earliest_capture_date_with_media == "2026-06-10"
    assert report.latest_capture_date_with_media == "2026-06-10"
    assert dict(report.missing_by_month) == {"2026-07": 1}
    assert dict(report.missing_by_site_id) == {"site_b": 1}
    assert report.orphan_vision_documents == 1
    assert report.vision_without_photos >= 1
    # a2.jpg has no vision doc; the audit must say so.
    assert report.photos_without_vision >= 1


def test_audit_is_read_only_and_leaks_no_urls() -> None:
    captures, vision, store = _fixture()
    report = media_coverage.audit_media_coverage(
        captures_by_site_id=captures,
        vision_docs=vision,
        store=store,
        upload_root=UPLOAD_ROOT,
    )
    # Only exists() was permitted; RecordingStore raises on anything else.
    assert set(store.exists_calls) == {
        "2026-06-10/cap_1/a1.jpg",
        "2026-06-10/cap_1/a2.jpg",
        "2026-07-04/cap_2/b1.jpg",
    }
    payload = json.dumps(report.as_dict())
    assert "http" not in payload
    assert "X-Amz" not in payload
    assert "secret" not in payload.lower()


def test_cli_registers_audit_media_coverage() -> None:
    args = btq.parse_args(["audit-media-coverage", "--json"])
    assert args.command == "audit-media-coverage"
    assert args.json is True
    assert callable(args.func)
