from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.common import SectionContext
from ops_dashboard.sections import console, inbox, swipe
from tests.test_ops_dashboard import request_text, write_vault_site_issue
from tests.test_ops_dashboard_admin import write_vault_equipment_request, write_vault_supply_need
from tests._couch_vault_stub import couch_vault_stub  # noqa: F401 - autouse fixture


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


def test_console_default_landing_links_to_each_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Redesign (372): the landing no longer embeds the inline console. The four
    # console queues are now promoted to the "Needs you" metric-card row, each
    # card LINKING to its queue (don't-lose-info — every queue stays reachable).
    # The inline console swipe panel itself is exercised by the direct
    # console.render_console tests above and lives on /swipe.
    install_console_counts(monkeypatch, {"review": 0, "issues": 0, "supplies": 0, "equipment": 0})

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    # "Needs you" metric cards, each an <a> linking to its queue surface.
    assert "Needs you" in body
    assert 'class="metric-grid"' in body
    assert 'href="/candidates?status=pending_approval"' in body  # review queue
    assert 'href="/field-capture/issues"' in body                # issues queue
    assert 'href="/supplies?status=open"' in body                # supplies queue
    assert 'href="/equipment?status=open"' in body               # equipment queue


def test_console_issues_tab_renders_full_list_and_active_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_console_counts(monkeypatch, {"review": 0, "issues": 11, "supplies": 0, "equipment": 0})
    vault = tmp_path / "vault"
    write_vault_site_issue(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))

    body = console.render_console(section_ctx(tmp_path / "runtime"))
    ctx = section_ctx(tmp_path / "runtime", vault)
    ctx.query = {"tab": ["issues"], "site_id": ["7050"], "sort": ["site"]}
    issues_body = console.render_console(ctx)

    assert 'data-console-tab="issues"' in issues_body
    assert 'href="/?tab=issues&amp;site_id=7050&amp;sort=site" role="tab" aria-current="page" aria-selected="true"' in issues_body
    assert '<h1>Site Issues for 7050</h1>' in issues_body
    assert '<form method="get" action="/" data-submit-on-change>' in issues_body
    assert 'name="tab" value="issues"' in issues_body
    assert 'name="site_id" value="7050"' in issues_body
    assert '<option value="site" selected>Site</option>' in issues_body
    assert "Restroom drain backup" in issues_body
    assert 'data-card-id="open_site_issues"' not in issues_body
    assert "See all" not in issues_body
    assert 'data-console-tab="review"' in body


def test_issues_queue_renders_site_filtered_panel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Redesign (372): the landing's old "/?tab=issues" inline console view moved
    # to the dedicated issues queue at /field-capture/issues, which the "Open
    # issues" metric card links to. The site-filtered issue list is unchanged and
    # still reachable (don't-lose-info) — just at its own route now.
    install_console_counts(monkeypatch, {"review": 0, "issues": 11, "supplies": 0, "equipment": 0})
    vault = tmp_path / "vault"
    write_vault_site_issue(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))

    status, _content_type, body = request_text("GET", "/field-capture/issues?site_id=7050", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert '<h1>Site Issues for 7050</h1>' in body
    assert "Restroom drain backup" in body


def test_console_supplies_tab_renders_open_full_list_with_filters_and_actions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_console_counts(monkeypatch, {"review": 0, "issues": 0, "supplies": 2, "equipment": 0})
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, supply_id="sup_open", status="open", item_name="BrightWash cleaner")
    write_vault_supply_need(vault, supply_id="sup_ordered", status="ordered", item_name="Mop heads")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))
    ctx = section_ctx(tmp_path / "runtime", vault)
    ctx.query = {"tab": ["supplies"]}

    body = console.render_console(ctx)

    assert 'data-console-tab="supplies"' in body
    assert '<h1>Supply Needs with status open</h1>' in body
    assert '<form method="get" action="/" data-submit-on-change>' in body
    assert 'name="tab" value="supplies"' in body
    assert '<option value="open" selected>Open (1)</option>' in body
    assert "BrightWash cleaner" in body
    assert "Mop heads" not in body
    # 349: supplies list is now a responsive card GRID, not a <table>. Actions live
    # in each card's <footer>, not an "Actions" column header. Assert the card markup
    # and the per-card actions footer instead of the old ">Actions</th>" column header.
    assert "<article" in body  # card markup present (was a table row before)
    assert "minmax(260px,1fr)" in body  # responsive card grid wrapper
    assert "<footer" in body  # actions rendered inside the card footer
    assert "Mark ordered" in body
    assert "Mark no action needed" in body
    assert "See all" not in body


def test_console_equipment_tab_renders_open_full_list_with_filters_and_actions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_console_counts(monkeypatch, {"review": 0, "issues": 0, "supplies": 0, "equipment": 2})
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault, equipment_id="eqr_open", status="open", equipment_name="vacuum")
    write_vault_equipment_request(vault, equipment_id="eqr_approved", status="approved", equipment_name="floor buffer")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))
    ctx = section_ctx(tmp_path / "runtime", vault)
    ctx.query = {"tab": ["equipment"]}

    body = console.render_console(ctx)

    assert 'data-console-tab="equipment"' in body
    assert '<h1>Equipment Requests with status open</h1>' in body
    assert '<form method="get" action="/" data-submit-on-change>' in body
    assert 'name="tab" value="equipment"' in body
    assert '<option value="open" selected>Open (1)</option>' in body
    assert "vacuum" in body
    assert "floor buffer" not in body
    # 352: equipment list is now a responsive card GRID, not a <table>. Actions live
    # in each card's <footer>, not an "Actions" column header. Assert the card markup
    # and the per-card actions footer instead of the old ">Actions</th>" column header.
    assert "<article" in body  # card markup present (was a table row before)
    assert "minmax(260px,1fr)" in body  # responsive card grid wrapper
    assert "<footer" in body  # actions rendered inside the card footer
    assert "Mark approved" in body
    assert "Mark denied" in body
    assert "See all" not in body


def test_console_state_filters_round_trip_in_tab_urls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_console_counts(monkeypatch, {"review": 0, "issues": 0, "supplies": 2, "equipment": 0})
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, supply_id="sup_7050", site_id="7050", status="ordered", item_name="Mop heads")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))
    ctx = section_ctx(tmp_path / "runtime", vault)
    ctx.query = {"tab": ["supplies"], "site_id": ["7050"], "status": ["ordered"], "sort": ["site"]}

    body = console.render_console(ctx)

    assert 'href="/?tab=issues&amp;site_id=7050&amp;sort=site" role="tab" aria-selected="false"' in body
    assert 'href="/?tab=supplies&amp;site_id=7050&amp;sort=site&amp;status=ordered" role="tab" aria-current="page" aria-selected="true"' in body
    assert 'href="/?tab=equipment&amp;site_id=7050&amp;sort=site&amp;status=ordered" role="tab" aria-selected="false"' in body
    assert 'name="site_id" value="7050"' in body
    assert '<option value="ordered" selected>Ordered (1)</option>' in body
    assert '<option value="site" selected>Site</option>' in body


def test_swipe_script_handles_u_as_unknown_reject_action() -> None:
    assert "e.key === 'u' || e.key === 'U'" in swipe._SWIPE_SCRIPT
    assert "decide(c, 'unknown', null)" in swipe._SWIPE_SCRIPT
    assert "action === 'unknown' ? 'mark unknown' : ''" in swipe._SWIPE_SCRIPT
