from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import pytest

from ops_dashboard.layout import html_page
from tests.test_ops_dashboard import request_text
from tests.test_ops_dashboard_captures import seed_capture
from tests.test_ops_dashboard_home import install_full_home


def lightbox_definition_count(body: str) -> int:
    return body.count("function openLb(")


def test_html_page_includes_lightbox_chrome() -> None:
    body = html_page("Test", "<main>content</main>", active_section="home")

    assert 'id="lb"' in body
    assert "function openLb(" in body


def test_home_page_includes_lightbox_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_full_home(monkeypatch)
    monkeypatch.setattr(
        "ops_dashboard.sections.home._field_photos_mod.latest_photo_cards",
        lambda _runtime_root, limit: (
            '<a href="#" onclick="openLb(&quot;/media/home.jpg&quot;);return false">photo</a>',
            False,
        ),
    )

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'onclick="openLb(' in body
    assert lightbox_definition_count(body) == 1
    assert body.count('id="lb"') == 1


def test_capture_detail_lightbox_still_included_once(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_lightbox", photo=True)

    status, _content_type, body = request_text("GET", "/captures?capture_id=cap_lightbox", runtime_root)

    assert status == HTTPStatus.OK
    assert 'onclick="openLb(' in body
    assert lightbox_definition_count(body) == 1
    assert body.count('id="lb"') == 1
