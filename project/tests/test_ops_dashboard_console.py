from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.common import SectionContext
from ops_dashboard.sections import console, inbox, swipe
from tests.test_ops_dashboard import request_text


def section_ctx(runtime_root: Path, vault_root: Path | None = None) -> SectionContext:
    vault = vault_root or runtime_root / "vault"
    return SectionContext(runtime_root, lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))


def install_console_counts(monkeypatch: pytest.MonkeyPatch, counts: dict[str, int]) -> None:
    monkeypatch.setattr(inbox, "console_counts", lambda _ctx: counts)
    monkeypatch.setattr(
        inbox,
        "console_review_queue_counts",
        lambda _ctx: {
            "pending_review": counts["review"],
            "approved": 0,
            "rejected": 2,
            "failed": 1,
        },
    )
    monkeypatch.setattr(
        inbox,
        "console_cards",
        lambda _ctx: {
            "issues": {
                "id": "open_site_issues",
                "title": "Open site issues",
                "count": counts["issues"],
                "top": [{"site": "7050", "submitter": "Greg", "summary": "Fix sink", "age_seconds": 0, "deep_link": "/field-capture/issues"}],
                "see_all": "/field-capture/issues",
                "shape": inbox.INBOX_SHAPE_STRUCTURED,
            },
            "supplies": {
                "id": "open_supply_needs",
                "title": "Open supply needs",
                "count": counts["supplies"],
                "top": [],
                "see_all": "/supplies?status=open",
                "shape": inbox.INBOX_SHAPE_STRUCTURED,
            },
            "equipment": {
                "id": "open_equipment_requests",
                "title": "Open equipment requests",
                "count": counts["equipment"],
                "top": [],
                "see_all": "/equipment?status=open",
                "shape": inbox.INBOX_SHAPE_STRUCTURED,
            },
        },
    )
    monkeypatch.setattr(swipe, "collect_cards", lambda _runtime_root: [])


def test_console_tab_bar_renders_live_badges_from_shared_counts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    counts = {"review": 4, "issues": 11, "supplies": 3, "equipment": 2}
    install_console_counts(monkeypatch, counts)

    body = console.render_console(section_ctx(tmp_path / "runtime"))

    assert 'class="console-tab is-active" href="/?tab=review"' in body
    for label, count in (("Review", 4), ("Issues", 11), ("Supplies", 3), ("Equipment", 2)):
        assert f"<span>{label}</span><span class=\"console-tab-badge\">{count}</span>" in body


def test_console_default_landing_renders_review_panel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_console_counts(monkeypatch, {"review": 0, "issues": 0, "supplies": 0, "equipment": 0})

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'data-console-tab="review"' in body
    assert "One proposed job at a time" in body
    assert "needs approval" in body
    assert "Nothing waiting for approval." in body
    assert "<kbd>U</kbd> mark unknown" in body


def test_console_issues_tab_renders_preview_card_and_active_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_console_counts(monkeypatch, {"review": 0, "issues": 11, "supplies": 0, "equipment": 0})

    body = console.render_console(section_ctx(tmp_path / "runtime"))
    ctx = section_ctx(tmp_path / "runtime")
    ctx.query = {"tab": ["issues"]}
    issues_body = console.render_console(ctx)

    assert 'data-console-tab="issues"' in issues_body
    assert 'href="/?tab=issues" role="tab" aria-current="page" aria-selected="true"' in issues_body
    assert 'data-card-id="open_site_issues"' in issues_body
    assert '<p class="count" data-count-bucket="warn">11</p>' in issues_body
    assert "Fix sink" in issues_body
    assert 'data-console-tab="review"' in body


def test_home_query_tab_renders_issues_panel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_console_counts(monkeypatch, {"review": 0, "issues": 11, "supplies": 0, "equipment": 0})

    status, _content_type, body = request_text("GET", "/?tab=issues", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'data-console-tab="issues"' in body
    assert 'data-card-id="open_site_issues"' in body
    assert "Fix sink" in body


def test_swipe_script_handles_u_as_unknown_reject_action() -> None:
    assert "e.key === 'u' || e.key === 'U'" in swipe._SWIPE_SCRIPT
    assert "decide(c, 'unknown', null)" in swipe._SWIPE_SCRIPT
    assert "action === 'unknown' ? 'mark unknown' : ''" in swipe._SWIPE_SCRIPT
