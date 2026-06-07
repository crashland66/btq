from __future__ import annotations

from pathlib import Path

from site_visits import discover_site_visits, load_person_first_names


def write_visit(
    vault_root: Path,
    *,
    account: str = "Summit Wire",
    site_dir: str = "Summit Wire",
    file_date: str = "2026-05-14",
    site_id: str = "7050",
    note_type: str = "visit",
    confidence: str = "provisional",
    evidence: str = "All done.",
    visited_by: str = "",
) -> Path:
    path = vault_root / "Accounts" / account / "Locations" / site_dir / "Visits" / f"{file_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    visited_by_line = f"visited_by: {visited_by}\n" if visited_by else ""
    path.write_text(
        f"""---
type: {note_type}
timestamp: {file_date}T20:00:00+00:00
site: "{site_id}"
date: {file_date}
visit_key: "Summit Wire:{file_date}"
source: audio-abc123
confidence: {confidence}
evidence: {evidence}
{visited_by_line}---
""",
        encoding="utf-8",
    )
    return path


def test_discover_site_visits_returns_empty_when_no_accounts_dir(tmp_path: Path) -> None:
    assert discover_site_visits(tmp_path, site_id="7050") == {"visits": [], "warnings": []}


def test_discover_site_visits_reads_visit_from_frontmatter(tmp_path: Path) -> None:
    write_visit(tmp_path)

    result = discover_site_visits(tmp_path, site_id="7050")
    visits = result["visits"]

    assert len(visits) == 1
    assert visits[0]["date"] == "2026-05-14"
    assert visits[0]["confidence"] == "provisional"
    assert visits[0]["evidence"] == "All done."


def test_discover_site_visits_includes_visited_by_field(tmp_path: Path) -> None:
    write_visit(tmp_path, visited_by="per_test001")

    result = discover_site_visits(tmp_path, site_id="7050")
    visit = result["visits"][0]

    assert visit["visited_by"] == "per_test001"


def test_discover_site_visits_visited_by_empty_when_absent(tmp_path: Path) -> None:
    write_visit(tmp_path)

    result = discover_site_visits(tmp_path, site_id="7050")
    visit = result["visits"][0]

    assert visit["visited_by"] == ""


def test_discover_site_visits_resolves_visited_by_first_name_from_people_registry(tmp_path: Path) -> None:
    people_dir = tmp_path / "People"
    people_dir.mkdir()
    (people_dir / "jordan-avery.md").write_text(
        """---
person_id: per_test002
name: "Avery, Jordan"
---
""",
        encoding="utf-8",
    )
    write_visit(tmp_path, visited_by="per_test002")

    result = discover_site_visits(tmp_path, site_id="7050")
    visit = result["visits"][0]

    assert visit["visited_by_first_name"] == "Jordan"


def test_discover_site_visits_visited_by_first_name_empty_when_person_not_found(tmp_path: Path) -> None:
    write_visit(tmp_path, visited_by="per_unknown")

    result = discover_site_visits(tmp_path, site_id="7050")
    visit = result["visits"][0]

    assert visit["visited_by_first_name"] == ""


def test_load_person_first_names_handles_missing_people_dir(tmp_path: Path) -> None:
    assert load_person_first_names(tmp_path) == {}


def test_discover_site_visits_filters_by_site_id(tmp_path: Path) -> None:
    write_visit(tmp_path, file_date="2026-05-14", site_id="7050")
    write_visit(tmp_path, account="Contworks", site_dir="Contworks", file_date="2026-05-15", site_id="7060")

    result = discover_site_visits(tmp_path, site_id="7050")

    assert [visit["site_id"] for visit in result["visits"]] == ["7050"]


def test_discover_site_visits_sorts_newest_first(tmp_path: Path) -> None:
    write_visit(tmp_path, file_date="2026-05-10")
    write_visit(tmp_path, file_date="2026-05-12")
    write_visit(tmp_path, file_date="2026-05-14")

    result = discover_site_visits(tmp_path, site_id="7050")

    assert [visit["date"] for visit in result["visits"]] == ["2026-05-14", "2026-05-12", "2026-05-10"]


def test_discover_site_visits_skips_non_visit_type_files(tmp_path: Path) -> None:
    write_visit(tmp_path, note_type="about")

    result = discover_site_visits(tmp_path, site_id="7050")

    assert result["visits"] == []


def test_discover_site_visits_respects_limit(tmp_path: Path) -> None:
    for day in range(10, 16):
        write_visit(tmp_path, file_date=f"2026-05-{day}")

    result = discover_site_visits(tmp_path, site_id="7050", limit=3)

    assert [visit["date"] for visit in result["visits"]] == ["2026-05-15", "2026-05-14", "2026-05-13"]
