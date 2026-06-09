from __future__ import annotations

from pathlib import Path

from btq_vault.entity_types import OPERATOR_ID_GREG
from event_pipeline.couchdb.migrate_vault import operator_bootstrap_doc, parse_vault_file, prepare_upserts


def write_note(vault_root: Path, relative_path: str, text: str) -> Path:
    path = vault_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_entity_doc_from_typed_markdown(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "Accounts/Summit/Visits/2026-01-15.md",
        "---\n"
        "type: visit\n"
        "site: Summit Wire\n"
        "date: 2026-01-15\n"
        "---\n"
        "\n"
        "Visited the site.\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["type"] == "visit"
    assert doc["operator"] == OPERATOR_ID_GREG
    assert "vault_path" not in doc
    assert doc["content"] == "Visited the site."
    assert doc["site"] == "Summit Wire"
    assert doc["date"] == "2026-01-15"


def test_type_drift_site_visit_resolved_to_visit(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "Visits/site-visit.md", "---\ntype: site_visit\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["type"] == "visit"


def test_type_drift_person_resolved_to_employee(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "People/Jordan.md", "---\ntype: person\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["type"] == "employee"


def test_dashboard_type_excluded(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "Dashboards/overview.md", "---\ntype: dashboard\n---\nBody\n")

    assert parse_vault_file(path, vault_root) is None


def test_freeform_file_becomes_note_doc(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "Journal/freeform.md", "No frontmatter here.\n\nSecond line.\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["type"] == "note"
    assert doc["content"] == "No frontmatter here.\n\nSecond line.\n"


def test_note_doc_content_is_full_text(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    text = "  leading space kept\n\ntrailing newline kept\n"
    path = write_note(vault_root, "Notes/raw.md", text)

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["content"] == text


def test_operator_stamped_on_entity_doc(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    entity_path = write_note(vault_root, "Visits/visit.md", "---\ntype: visit\n---\nBody\n")
    note_path = write_note(vault_root, "Notes/freeform.md", "Body\n")

    entity_doc = parse_vault_file(entity_path, vault_root)
    note_doc = parse_vault_file(note_path, vault_root)

    assert entity_doc is not None
    assert note_doc is not None
    assert entity_doc["operator"] == OPERATOR_ID_GREG
    assert note_doc["operator"] == OPERATOR_ID_GREG


def test_operator_record_has_no_operator_field() -> None:
    doc = operator_bootstrap_doc()

    assert doc["type"] == "operator"
    assert "operator" not in doc


def test_stable_id_for_employee_with_person_id(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "People/Jordan.md", "---\ntype: employee\nperson_id: prs_abc123\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["_id"] == "employee_prs_abc123"


def test_stable_id_for_location_with_site_id(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "Locations/Summit.md", "---\ntype: location\nsite_id: 7050\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["_id"] == "location_7050"


def test_stable_id_for_location_falls_back_to_job(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "Accounts/Contworks/Locations/7060 - Continental Metalworks/about.md",
        "---\ntype: location\njob: 7060\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["_id"] == "location_7060"


def test_stable_id_for_note_from_path(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "Journal/Daily Note.md", "Body\n")

    first = parse_vault_file(path, vault_root)
    second = parse_vault_file(path, vault_root)

    assert first is not None
    assert second is not None
    assert first["_id"] == "note_journal_daily_note"
    assert first["_id"] == second["_id"]


def test_parse_vault_file_employee_synthesizes_site_ids_from_job(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "People/Glen.md", "---\ntype: employee\njob: 7050\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["site_ids"] == ["7050"]
    assert doc["job"] == "7050"


def test_parse_vault_file_employee_job_as_yaml_list_single(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "People/Yaml List Single.md", "---\ntype: employee\njob: [7060]\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["site_ids"] == ["7060"]


def test_parse_vault_file_employee_job_as_yaml_list_multiple(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "People/Yaml List Multiple.md",
        "---\ntype: employee\njob: [7060, 7050]\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["site_ids"] == ["7060", "7050"]


def test_parse_vault_file_employee_job_yaml_list_plus_additional(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "People/Yaml List Additional.md",
        "---\ntype: employee\njob: [7060]\nadditional_jobs: [7050, 7091]\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["site_ids"] == ["7060", "7050", "7091"]


def test_parse_vault_file_employee_job_scalar_still_works(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "People/Scalar.md", "---\ntype: employee\njob: 7060\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["site_ids"] == ["7060"]


def test_parse_vault_file_employee_combines_job_and_additional_jobs(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "People/Multi.md",
        "---\ntype: employee\njob: 7030\nadditional_jobs: [7090, 172]\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["site_ids"] == ["7030", "7090", "172"]


def test_parse_vault_file_employee_falls_back_to_sites_field(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "People/Sites.md", "---\ntype: employee\nsites: [222, 241]\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["site_ids"] == ["222", "241"]


def test_parse_vault_file_employee_deduplicates_site_ids(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "People/Dedup.md",
        "---\ntype: employee\njob: 7050\nadditional_jobs: [7050, 7040]\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["site_ids"] == ["7050", "7040"]


def test_parse_vault_file_employee_with_no_site_ids(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "People/Unassigned.md", "---\ntype: employee\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["site_ids"] == []


def test_parse_vault_file_employee_name_uses_preferred_first_and_last(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "People/Smith, Robert.md",
        "---\ntype: employee\nfirst: Robert\nlast: Smith\npreferred_name: Bob\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["name"] == "Bob Smith"


def test_parse_vault_file_employee_name_falls_back_to_first_when_no_preferred(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "People/Mercer, Glen.md",
        "---\ntype: employee\nfirst: Glen\nlast: Mercer\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["name"] == "Glen Mercer"


def test_parse_vault_file_employee_name_handles_missing_last(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "People/Madonna.md", "---\ntype: employee\nfirst: Madonna\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["name"] == "Madonna"


def test_parse_vault_file_employee_name_handles_completely_empty(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "People/Unknown.md", "---\ntype: employee\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["name"] == ""


def test_parse_vault_file_opportunity_title_strips_leading_date_prefix(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "Accounts/X/Locations/1 - Y/Opportunities/2026-03-11-backpack-vacuums.md",
        "---\ntype: opportunity\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["title"] == "Backpack Vacuums"


def test_opportunity_title_uppercases_known_acronyms(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "Accounts/X/Locations/1 - Y/Opportunities/2026-05-01-mill-zone-ppe.md",
        "---\ntype: opportunity\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["title"] == "Mill Zone PPE"


def test_opportunity_title_preserves_non_acronym_words(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "Accounts/X/Locations/1 - Y/Opportunities/2026-05-01-backpack-vacuums.md",
        "---\ntype: opportunity\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["title"] == "Backpack Vacuums"


def test_opportunity_title_uppercases_acronym_at_any_position(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "Accounts/X/Locations/1 - Y/Opportunities/2026-05-01-ppe-compliance.md",
        "---\ntype: opportunity\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["title"] == "PPE Compliance"


def test_opportunity_title_handles_mixed_case_input(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "Accounts/X/Locations/1 - Y/Opportunities/2026-05-01-ft-anchor-proposal.md",
        "---\ntype: opportunity\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["title"] == "FT Anchor Proposal"


def test_parse_vault_file_opportunity_title_handles_underscore_date(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "Accounts/X/Locations/1 - Y/Opportunities/2026_03_11_water_intrusion.md",
        "---\ntype: opportunity\n---\nBody\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["title"] == "Water Intrusion"


def test_parse_vault_file_opportunity_title_no_date_prefix(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "Opportunities/legacy-leak.md", "---\ntype: opportunity\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["title"] == "Legacy Leak"


def test_parse_vault_file_non_employee_non_opportunity_unaffected(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "Locations/Summit.md", "---\ntype: location\njob: 7050\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert "name" not in doc
    assert "title" not in doc


def test_parse_vault_file_non_employee_unaffected(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(vault_root, "Locations/Summit.md", "---\ntype: location\njob: 7050\n---\nBody\n")

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert "site_ids" not in doc


def test_btq_job_ids_lifted_from_frontmatter(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = write_note(
        vault_root,
        "Visits/job-ids.md",
        "---\n"
        "type: visit\n"
        "btq_job_ids:\n"
        "  - job_abc\n"
        "  - job_xyz\n"
        "---\n"
        "Body\n",
    )

    doc = parse_vault_file(path, vault_root)

    assert doc is not None
    assert doc["btq_job_ids"] == ["job_abc", "job_xyz"]


def test_prepare_upserts_skips_existing_location_doc_when_markdown_drifts() -> None:
    desired = [{"_id": "location_7050", "type": "location", "account": "New Name"}]
    existing = {"location_7050": {"_id": "location_7050", "_rev": "1-abc",
                                   "type": "location", "account": "Old Name"}}
    writes, created, updated, unchanged, skipped_owned = prepare_upserts(desired, existing)
    assert writes == []
    assert skipped_owned == 1
    assert created == updated == unchanged == 0


def test_prepare_upserts_skips_existing_employee_doc_when_markdown_drifts() -> None:
    desired = [{"_id": "employee_jordan", "type": "employee", "first": "Jordan", "last": "X"}]
    existing = {"employee_jordan": {"_id": "employee_jordan", "_rev": "1-abc",
                                   "type": "employee", "first": "Jordan", "last": "Avery"}}
    writes, created, updated, unchanged, skipped_owned = prepare_upserts(desired, existing)
    assert writes == []
    assert skipped_owned == 1


def test_prepare_upserts_inserts_new_location_doc() -> None:
    desired = [{"_id": "location_7050", "type": "location", "account": "New Site"}]
    existing = {}
    writes, created, updated, unchanged, skipped_owned = prepare_upserts(desired, existing)
    assert len(writes) == 1
    assert writes[0]["_id"] == "location_7050"
    assert skipped_owned == 0
    assert created == 1


def test_prepare_upserts_still_updates_drifted_note_doc() -> None:
    desired = [{"_id": "note_abc", "type": "note", "content": "new text"}]
    existing = {"note_abc": {"_id": "note_abc", "_rev": "2-xyz", "type": "note", "content": "old text"}}
    writes, created, updated, unchanged, skipped_owned = prepare_upserts(desired, existing)
    assert len(writes) == 1
    assert writes[0]["content"] == "new text"
    assert skipped_owned == 0
    assert updated == 1


def test_prepare_upserts_still_updates_drifted_site_doc() -> None:
    desired = [{"_id": "site_7050", "type": "site", "location": "New"}]
    existing = {"site_7050": {"_id": "site_7050", "_rev": "3-qqq", "type": "site", "location": "Old"}}
    writes, created, updated, unchanged, skipped_owned = prepare_upserts(desired, existing)
    assert len(writes) == 1
    assert skipped_owned == 0
