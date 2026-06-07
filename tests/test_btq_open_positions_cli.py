from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import btq


def test_open_positions_cli_outputs_markdown(tmp_path: Path) -> None:
    write_site(tmp_path, triggers=["2026-04-20 — priority=high — Need coverage."])

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = btq.run(["open-positions", "--vault-root", str(tmp_path)])

    output = stdout.getvalue()
    assert exit_code == 0
    assert output.startswith("- ")
    assert "Western Gas Transmission (#7030)" in output
    assert "1 open position(s)" in output


def test_open_positions_cli_outputs_json(tmp_path: Path) -> None:
    write_site(tmp_path, triggers=["2026-04-20 — priority=high — Need coverage."])

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = btq.run(["open-positions", "--vault-root", str(tmp_path), "--json"])

    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert payload == [
        {
            "site_id": "7030",
            "site_name": "Western Gas Transmission",
            "account": "Wgtco",
            "trigger_count": 1,
            "close_count": 0,
            "net_open": 1,
            "oldest_trigger_date": "2026-04-20",
            "all_trigger_dates": ["2026-04-20"],
        }
    ]


def test_open_positions_cli_empty_vault_prints_no_open_positions(tmp_path: Path) -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = btq.run(["open-positions", "--vault-root", str(tmp_path)])

    assert exit_code == 0
    assert stdout.getvalue().strip() == "(no open positions)"


def write_site(vault_root: Path, *, triggers: list[str]) -> Path:
    about_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    about_path.parent.mkdir(parents=True, exist_ok=True)
    about_path.write_text(
        "\n".join(
            [
                "# Western Gas Transmission",
                "",
                "## Operational Notes",
                "",
                "### Recruiting Triggers",
                "",
                *triggers,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return about_path
