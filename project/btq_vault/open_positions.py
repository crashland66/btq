from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple


LOGGER = logging.getLogger(__name__)
CLOSING_OUTCOMES = {"filled", "cancelled", "superseded"}

DATE_RE = re.compile(r"^\s*(?:-\s*)?(?P<date>\d{4}-\d{2}-\d{2})\b")
OUTCOME_RE = re.compile(r"\boutcome=(?P<outcome>[A-Za-z_-]+)\b")


class OpenPosition(NamedTuple):
    site_id: str
    site_name: str
    account: str
    trigger_count: int
    close_count: int
    net_open: int
    oldest_trigger_date: str | None
    all_trigger_dates: list[str]


def compute_open_positions(vault_root: Path | None = None) -> list[OpenPosition]:
    """Walk vault site about.md files and identify net-open recruiting triggers."""
    if vault_root is None:
        from config import get_config

        vault_root = get_config().vault_dir

    accounts_root = Path(vault_root) / "Accounts"
    positions: list[OpenPosition] = []
    for about_path in sorted(accounts_root.glob("*/Locations/*/about.md")):
        try:
            position = _position_for_about(about_path)
        except Exception as exc:
            LOGGER.warning("Skipping unparseable site about.md %s: %s", about_path, exc)
            continue
        if position is not None:
            positions.append(position)
    return sorted(positions, key=lambda p: (-p.net_open, p.oldest_trigger_date or "9999-99-99"))


def _position_for_about(about_path: Path) -> OpenPosition | None:
    text = about_path.read_text(encoding="utf-8")
    operational_notes = _heading_body(text, "## Operational Notes")
    if operational_notes is None:
        raise ValueError("missing ## Operational Notes section")

    trigger_lines = _dated_lines(_heading_body(operational_notes, "### Recruiting Triggers") or "")
    close_lines = _dated_lines(_heading_body(operational_notes, "### Recruiting Closed") or "")
    trigger_dates = [_entry_date(line) for line in trigger_lines]
    trigger_dates = [date for date in trigger_dates if date is not None]
    closing_dates = [
        date
        for line in close_lines
        if _closing_outcome(line)
        for date in [_entry_date(line)]
        if date is not None
    ]

    trigger_count = len(trigger_dates)
    close_count = len(closing_dates)
    net_open = trigger_count - close_count
    if net_open <= 0:
        return None

    account, site_id, site_name = _site_identity(about_path, text)
    return OpenPosition(
        site_id=site_id,
        site_name=site_name,
        account=account,
        trigger_count=trigger_count,
        close_count=close_count,
        net_open=net_open,
        oldest_trigger_date=min(trigger_dates) if trigger_dates else None,
        all_trigger_dates=trigger_dates,
    )


def _heading_body(text: str, heading: str) -> str | None:
    lines = text.splitlines()
    target_level = _heading_level(heading)
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        return None

    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        level = _heading_level(line)
        if level is not None and level <= target_level:
            end = index
            break
    return "\n".join(lines[start:end])


def _heading_level(line: str) -> int | None:
    stripped = line.strip()
    if not stripped.startswith("#"):
        return None
    marker = stripped.split(" ", 1)[0]
    if marker and set(marker) == {"#"}:
        return len(marker)
    return None


def _dated_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if _entry_date(line)]


def _entry_date(line: str) -> str | None:
    match = DATE_RE.match(line)
    if not match:
        return None
    return match.group("date")


def _closing_outcome(line: str) -> bool:
    match = OUTCOME_RE.search(line)
    return bool(match and match.group("outcome").lower() in CLOSING_OUTCOMES)


def _site_identity(about_path: Path, text: str) -> tuple[str, str, str]:
    account = about_path.parents[2].name
    site_dir = about_path.parent.name
    if " - " in site_dir:
        site_id, site_name = site_dir.split(" - ", 1)
    else:
        site_id = site_dir
        site_name = _title_from_markdown(text) or site_dir
    return account, site_id.strip(), site_name.strip()


def _title_from_markdown(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None
