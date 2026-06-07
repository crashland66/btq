from __future__ import annotations

import logging
from pathlib import Path

from btq_vault.open_positions import CLOSING_OUTCOMES, compute_open_positions


def test_compute_open_positions_returns_empty_for_empty_vault(tmp_path: Path) -> None:
    assert compute_open_positions(tmp_path) == []


def test_compute_open_positions_finds_site_with_trigger_no_close(tmp_path: Path) -> None:
    write_site(tmp_path, triggers=["2026-04-20 — priority=high | open_positions=1 — Need coverage."])

    result = compute_open_positions(tmp_path)

    assert len(result) == 1
    assert result[0].site_id == "7030"
    assert result[0].site_name == "Western Gas Transmission"
    assert result[0].account == "Wgtco"
    assert result[0].trigger_count == 1
    assert result[0].close_count == 0
    assert result[0].net_open == 1
    assert result[0].oldest_trigger_date == "2026-04-20"
    assert result[0].all_trigger_dates == ["2026-04-20"]


def test_compute_open_positions_excludes_filled_site(tmp_path: Path) -> None:
    write_site(
        tmp_path,
        triggers=["2026-04-20 — priority=high — Need coverage."],
        closes=["2026-05-26 — outcome=filled — filled_by=David Pearson"],
    )

    assert compute_open_positions(tmp_path) == []
    assert "filled" in CLOSING_OUTCOMES


def test_compute_open_positions_excludes_cancelled_site(tmp_path: Path) -> None:
    write_site(
        tmp_path,
        triggers=["2026-04-20 — priority=high — Need coverage."],
        closes=["2026-05-26 — outcome=cancelled — not needed"],
    )

    assert compute_open_positions(tmp_path) == []


def test_compute_open_positions_excludes_superseded_site(tmp_path: Path) -> None:
    write_site(
        tmp_path,
        triggers=["2026-04-20 — priority=high — Need coverage."],
        closes=["2026-05-26 — outcome=superseded — replaced by new plan"],
    )

    assert compute_open_positions(tmp_path) == []


def test_compute_open_positions_keeps_withdrawn_site_open(tmp_path: Path) -> None:
    write_site(
        tmp_path,
        triggers=["2026-04-20 — priority=high — Need coverage."],
        closes=["2026-05-26 — outcome=withdrawn — recruiting paused"],
    )

    result = compute_open_positions(tmp_path)

    assert len(result) == 1
    assert result[0].net_open == 1
    assert result[0].close_count == 0


def test_compute_open_positions_partial_close_keeps_site_open(tmp_path: Path) -> None:
    write_site(
        tmp_path,
        triggers=[
            "2026-04-20 — priority=high — Need coverage.",
            "2026-04-21 — priority=normal — Need coverage.",
            "2026-04-22 — priority=normal — Need coverage.",
        ],
        closes=[
            "2026-05-01 — outcome=filled — filled_by=Alice",
            "2026-05-02 — outcome=filled — filled_by=Bob",
        ],
    )

    result = compute_open_positions(tmp_path)

    assert len(result) == 1
    assert result[0].trigger_count == 3
    assert result[0].close_count == 2
    assert result[0].net_open == 1


def test_compute_open_positions_oldest_date_is_min_trigger_date(tmp_path: Path) -> None:
    write_site(
        tmp_path,
        triggers=[
            "2026-05-10 — priority=normal — Need coverage.",
            "2026-04-20 — priority=high — Need coverage.",
            "2026-05-01 — priority=normal — Need coverage.",
        ],
    )

    result = compute_open_positions(tmp_path)

    assert result[0].oldest_trigger_date == "2026-04-20"


def test_compute_open_positions_sorts_by_net_open_desc_then_age_asc(tmp_path: Path) -> None:
    write_site(
        tmp_path,
        account="Alpha",
        site_dir="100 - Alpha Site",
        title="Alpha Site",
        triggers=[
            "2026-04-20 — priority=normal — Need coverage.",
            "2026-04-21 — priority=normal — Need coverage.",
        ],
    )
    write_site(
        tmp_path,
        account="Beta",
        site_dir="200 - Beta Site",
        title="Beta Site",
        triggers=["2026-03-15 — priority=normal — Need coverage."],
    )

    result = compute_open_positions(tmp_path)

    assert [position.site_id for position in result] == ["100", "200"]


def test_compute_open_positions_skips_unparseable_about(tmp_path: Path, caplog) -> None:
    about_path = tmp_path / "Accounts" / "Bad" / "Locations" / "999 - Bad Site" / "about.md"
    about_path.parent.mkdir(parents=True)
    about_path.write_text("# Bad Site\n\nNo operational notes.\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = compute_open_positions(tmp_path)

    assert result == []
    assert "Skipping unparseable site about.md" in caplog.text


def write_site(
    vault_root: Path,
    *,
    account: str = "Wgtco",
    site_dir: str = "7030 - Western Gas Transmission",
    title: str = "Western Gas Transmission",
    triggers: list[str] | None = None,
    closes: list[str] | None = None,
) -> Path:
    about_path = vault_root / "Accounts" / account / "Locations" / site_dir / "about.md"
    about_path.parent.mkdir(parents=True, exist_ok=True)
    sections = [f"# {title}", "", "## Operational Notes", ""]
    if triggers is not None:
        sections.extend(["### Recruiting Triggers", "", *triggers, ""])
    if closes is not None:
        sections.extend(["### Recruiting Closed", "", *closes, ""])
    about_path.write_text("\n".join(sections), encoding="utf-8")
    return about_path
