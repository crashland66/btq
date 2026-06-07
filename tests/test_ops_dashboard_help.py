from __future__ import annotations

import html
from http import HTTPStatus
from pathlib import Path

from tests.test_ops_dashboard import request_text


HELP_PATH = Path("project/ops_dashboard/HELP.md")
REQUIRED_HEADINGS = (
    "Approve a candidate",
    "Reject a candidate",
    "Mark a candidate's client informed",
    "Generate an approved draft from a candidate",
    "Stage an approved draft into the queue",
    "Retry a failed photo-vision sidecar",
    "Browse captures by site and date",
    "Inspect a failed queue job",
)


def test_help_route_renders_from_help_md(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/help", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "Ops Dashboard Help" in body
    assert "Approve a candidate" in body


def test_help_md_contains_one_section_per_supported_action() -> None:
    markdown = HELP_PATH.read_text(encoding="utf-8")

    for heading in REQUIRED_HEADINGS:
        assert markdown.count(f"## {heading}") == 1


def test_help_route_html_renders_headings_and_code_blocks(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/help", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    for heading in REQUIRED_HEADINGS:
        assert f"<h2>{html.escape(heading)}</h2>" in body
    assert "<pre><code>" in body


def test_help_nav_entry_present_and_active_on_help_page(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/help", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'href="/help" title="Help" aria-current="page">' in body
    assert '<span class="nav-glyph" aria-hidden="true">?</span><span class="nav-label">Help</span>' in body
