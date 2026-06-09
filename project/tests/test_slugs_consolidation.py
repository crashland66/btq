from __future__ import annotations

from pathlib import Path

import epistemic
from event_pipeline import schema
from event_pipeline.couchdb.migrate_vault import slug_value
from field_capture.person_slugs import employee_slug_candidates, last_first_person_slug, person_slug
from ops_dashboard.common import slugify_status
from processing_core.slugs import ascii_lower_dash_slug, lower_dash_slug
from queue_processor import governance
from queue_processor.handlers._shared import slugify_issue_component
from queue_processor.handlers.unknowns import derive_source_unknown_id, slugify_unknown_id
from vault_markdown import slugify_identifier


def test_core_lower_dash_slug_unchanged() -> None:
    cases = [
        ("Hello, World!", "fallback", "hello-world"),
        ("  A__B  ", "fallback", "a-b"),
        ("José Reyes", "fallback", "jos-reyes"),
        ("", "fallback", "fallback"),
        ("!!!", "fallback", "fallback"),
    ]

    for value, fallback, expected in cases:
        assert lower_dash_slug(value, fallback=fallback) == expected


def test_event_schema_slugify_output_matches_pre_consolidation() -> None:
    assert schema.slugify("Memo 001, Site A") == "memo-001-site-a"
    assert schema.slugify("!!!") == "event"


def test_ascii_lower_dash_slug_output_matches_pre_consolidation() -> None:
    assert ascii_lower_dash_slug("José Reyes Castillo") == "jose-reyes-castillo"
    assert ascii_lower_dash_slug("  A__B  ") == "a-b"
    assert ascii_lower_dash_slug(None) == ""


def test_slugify_status_output_matches_pre_consolidation() -> None:
    assert slugify_status("In Progress!") == "inprogress"
    assert slugify_status("Needs_review-2") == "needs_review-2"
    assert slugify_status("   ") == ""


def test_slug_value_output_matches_pre_consolidation() -> None:
    assert slug_value("People/Jane Doe.md") == "people_jane_doe"
    assert slug_value("A--B.md") == "a_b"
    assert slug_value("!!!.md") == "unknown"


def test_epistemic_slugify_output_matches_pre_consolidation() -> None:
    assert epistemic.slugify("Conflicting Site Note") == "conflicting-site-note"
    assert epistemic.slugify("!!!") == "contradiction"


def test_slugify_identifier_output_matches_pre_consolidation() -> None:
    assert slugify_identifier("Jordan / BTQ") == "jordan-btq"
    assert slugify_identifier("") == "unknown"


def test_governance_slugify_output_matches_pre_consolidation() -> None:
    assert governance.slugify("Review: Access Constraint") == "review-access-constraint"
    assert governance.slugify("!!!") == "review"


def test_person_slug_output_matches_pre_consolidation() -> None:
    assert person_slug("Hutton, Maria") == "hutton-maria"
    assert person_slug("") == "unknown"


def test_slugify_issue_component_output_matches_pre_consolidation() -> None:
    assert slugify_issue_component("Gate / Badge Reader #2") == "gate-badge-reader-2"
    assert slugify_issue_component("!!!") == "site-issue"


def test_slugify_unknown_id_output_matches_pre_consolidation() -> None:
    assert (
        slugify_unknown_id("2026-05-08T10:00:00+00:00 memo-a.m4a")
        == "2026-05-08t10-00-00-00-00-memo-a-m4a"
    )
    assert slugify_unknown_id("!!!") == "unknown-capture"


def test_person_slug_both_orders_preserved() -> None:
    assert last_first_person_slug(first="Maria", last="Hutton") == "hutton-maria"
    assert employee_slug_candidates(first="Maria", last="Hutton") == {
        "hutton-maria",
        "maria-hutton",
    }
    assert employee_slug_candidates(filename_stem="Hutton, Maria L") == {
        "hutton-maria-l",
        "maria-l-hutton",
        "hutton-maria",
        "maria-hutton",
    }


def test_canonical_id_and_lookup_slugs_stay_consistent() -> None:
    person_frontmatter = {"first": "Maria", "last": "Hutton", "job": "7060"}
    person_id = last_first_person_slug(first=str(person_frontmatter["first"]), last=str(person_frontmatter["last"]))
    site_id = str(person_frontmatter["job"])
    issue_slug = slugify_issue_component("Gate Badge Reader")

    assert person_id == "hutton-maria"
    assert person_slug(person_id) == person_id
    assert person_id in employee_slug_candidates(first="Maria", last="Hutton")
    assert site_id == "7060"
    assert f"issue_{site_id}_{issue_slug}" == "issue_7060_gate-badge-reader"


def test_derive_source_unknown_id_output_matches_pre_consolidation() -> None:
    assert (
        derive_source_unknown_id(
            path=Path("Journal/2026-05-08 Unknown.md"),
            timestamp="2026-05-08T10:00:00+00:00",
            audio_file="memo-a.m4a",
        )
        == "2026-05-08-unknown-2026-05-08t10-00-00-00-00-memo-a-m4a"
    )
