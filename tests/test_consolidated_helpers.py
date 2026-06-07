from __future__ import annotations

from pathlib import Path

import pytest

from epistemic import slugify as epistemic_slugify
from event_pipeline.schema import slugify as event_slugify
from ops_dashboard.common import slugify_status
from processing_core.slugs import lower_dash_slug
from processing_core.time import utc_now
from queue_processor.governance import slugify as review_slugify
from queue_processor.handlers._shared import slugify_issue_component
from queue_processor.handlers.unknowns import slugify_unknown_id
from vault_markdown import read_typed_markdown_note, slugify_identifier
from vault_sync.parsing import slugify as vault_sync_slugify


@pytest.mark.parametrize(
    ("value", "fallback", "expected"),
    [
        ("Restroom drain backup", "fallback", "restroom-drain-backup"),
        ("  ACME & Sons / Unit #2  ", "fallback", "acme-sons-unit-2"),
        ("", "fallback", "fallback"),
        ("---", "fallback", "fallback"),
    ],
)
def test_lower_dash_slug_core_matches_existing_lowercase_dash_behavior(value: str, fallback: str, expected: str) -> None:
    assert lower_dash_slug(value, fallback=fallback) == expected


def test_slugify_wrappers_preserve_distinct_fallbacks() -> None:
    assert epistemic_slugify("") == "contradiction"
    assert review_slugify("") == "review"
    assert event_slugify("") == "event"
    assert slugify_issue_component("") == "site-issue"
    assert slugify_unknown_id("") == "unknown-capture"
    assert slugify_identifier("") == "unknown"
    assert slugify_issue_component("Floor 2 / Bath") == "floor-2-bath"


def test_behaviorally_distinct_slugifiers_stay_distinct() -> None:
    assert vault_sync_slugify("Café déjà vu") == "cafe-deja-vu"
    assert epistemic_slugify("Café déjà vu") == "caf-d-j-vu"
    assert vault_sync_slugify("") == ""
    assert slugify_status("Needs_Review + Open") == "needs_reviewopen"


def test_shared_utc_now_core_keeps_aware_iso_string_shape() -> None:
    value = utc_now()

    assert value.endswith("+00:00")
    assert "T" in value


def test_consolidated_utc_now_definitions_are_removed_from_call_sites() -> None:
    root = Path(__file__).resolve().parents[1]
    consolidated_files = [
        root / "project" / "epistemic.py",
        root / "project" / "queue_processor" / "structured_log.py",
        root / "project" / "queue_processor" / "manifest.py",
        root / "project" / "queue_processor" / "replay.py",
        root / "project" / "queue_processor" / "governance.py",
        root / "project" / "queue_processor" / "evidence.py",
    ]

    assert "def utc_now() -> str:" in (root / "project" / "processing_core" / "time.py").read_text(encoding="utf-8")
    for path in consolidated_files:
        assert "def utc_now() -> str:" not in path.read_text(encoding="utf-8")


def test_read_typed_markdown_note_preserves_warning_and_skip_shapes(tmp_path: Path) -> None:
    good = tmp_path / "good.md"
    good.write_text("---\ntype: site_issue\ntitle: Drain\n---\n## Summary\nWater.\n", encoding="utf-8")
    missing = tmp_path / "missing.md"
    missing.write_text("# No frontmatter\n", encoding="utf-8")
    wrong = tmp_path / "wrong.md"
    wrong.write_text("---\ntype: supply_need\n---\n", encoding="utf-8")

    frontmatter, body, warning = read_typed_markdown_note(good, "site_issue")
    assert frontmatter == {"type": "site_issue", "title": "Drain"}
    assert "Water." in body
    assert warning is None
    assert read_typed_markdown_note(missing, "site_issue") == (None, "", {"path": str(missing), "reason": "missing_frontmatter"})
    assert read_typed_markdown_note(wrong, "site_issue") == (None, "", None)
