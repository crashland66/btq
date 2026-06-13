from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from ops_dashboard.app import route_response_with_headers
from ops_dashboard.sections import prospect_detail
from queue_spec import JOB_PROMOTE_PROSPECT


def prospect_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "prospect_kmf-birch",
        "doc_type": "prospect",
        "type": "prospect",
        "prospect_id": "kmf-birch",
        "name": "KMF Birch",
        "address": "1 Demo Way",
        "account": "KMF",
        "area_manager": "Casey",
        "lead_source": "walk",
        "status": "open",
        "created_at": "2026-05-27T10:00:00Z",
        "promoted_to_site_id": None,
        "promoted_at": None,
        "notes": "",
    }
    doc.update(overrides)
    return doc


def render_with_doc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, doc: dict[str, object]) -> str:
    monkeypatch.setattr(prospect_detail, "_load_prospect", lambda prospect_id: doc)
    monkeypatch.setattr(prospect_detail, "_site_options", lambda: ([("7040", "KMF Main")], True))
    monkeypatch.setattr(
        prospect_detail.field_photos,
        "latest_photo_cards",
        lambda ctx, limit, *, target_type="", target_id="", site_id="": ("", False),
    )
    return prospect_detail.render(SimpleNamespace(runtime_root=tmp_path / "runtime", query={}), "kmf-birch")


def test_prospect_detail_renders_summary_for_active_prospect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(
        monkeypatch,
        tmp_path,
        prospect_doc(promoted_to_site_id="7040", promoted_at="2026-05-27T12:00:00Z"),
    )

    headings = ["<h3>Identity</h3>", "<h3>Lifecycle</h3>", "<h3>Promotion</h3>"]
    assert all(heading in html for heading in headings)
    assert [html.index(heading) for heading in headings] == sorted(html.index(heading) for heading in headings)


def test_prospect_detail_renders_about_section_when_notes_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(monkeypatch, tmp_path, prospect_doc(notes="## Walk Notes\n- bring samples"))

    assert "<h2>About</h2>" in html
    assert "<h2>Walk Notes</h2>" in html
    assert "<li>bring samples</li>" in html


def test_prospect_detail_renders_promote_form_for_open_prospect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(monkeypatch, tmp_path, prospect_doc(status="open"))

    assert 'method="POST" action="/prospects/kmf-birch/promote"' in html
    assert 'name="site_id"' in html
    assert 'name="actor"' in html
    assert 'name="confirm"' in html


def test_prospect_detail_omits_promote_form_for_terminal_prospect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(
        monkeypatch,
        tmp_path,
        prospect_doc(status="won", promoted_to_site_id="7040", promoted_at="2026-05-27T12:00:00Z"),
    )

    assert 'action="/prospects/kmf-birch/promote"' not in html
    # Prompt 364: the promotion note now leads with the canonical site NAME
    # (resolved via _site_options), not the raw <code>site_id</code>.
    assert "Promoted to site KMF Main on 2026-05-27T12:00:00Z." in html
    assert "<code>7040</code>" not in html


def test_prospect_detail_renders_photos_panel_with_prospect_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_latest(ctx: object, limit: int, *, target_type: str = "", target_id: str = "", site_id: str = "") -> tuple[str, bool]:
        calls.append({"runtime_root": Path(getattr(ctx, "runtime_root")).resolve(strict=False), "limit": limit, "target_type": target_type, "target_id": target_id})
        return "<article>photo</article>", False

    monkeypatch.setattr(prospect_detail, "_load_prospect", lambda prospect_id: prospect_doc())
    monkeypatch.setattr(prospect_detail, "_site_options", lambda: ([("7040", "KMF Main")], True))
    monkeypatch.setattr(prospect_detail.field_photos, "latest_photo_cards", fake_latest)

    html = prospect_detail.render(SimpleNamespace(runtime_root=tmp_path / "runtime", query={}), "kmf-birch")

    assert calls == [
        {
            "runtime_root": (tmp_path / "runtime").resolve(strict=False),
            "limit": 4,
            "target_type": "prospect",
            "target_id": "kmf-birch",
        }
    ]
    assert "<article>photo</article>" in html


def test_prospect_detail_render_returns_not_found_for_missing_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(prospect_detail, "_load_prospect", lambda prospect_id: None)

    html = prospect_detail.render(SimpleNamespace(runtime_root=tmp_path / "runtime", query={}), "missing")

    assert "not found" in html.lower()
    assert "Prospect not found" in html


def test_prospect_detail_render_returns_not_found_for_non_prospect_doc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(prospect_detail, "_load_prospect", lambda prospect_id: {"doc_type": "location"})

    html = prospect_detail.render(SimpleNamespace(runtime_root=tmp_path / "runtime", query={}), "7040")

    assert "not found" in html.lower()


def test_prospect_detail_route_dispatches_to_render(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(prospect_detail, "render", lambda ctx, prospect_id: f"<html>detail {prospect_id}</html>")

    status, _content_type, body, _headers = route_response_with_headers("GET", "/prospects/kmf-birch", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "detail kmf-birch" in body.decode("utf-8")


def test_prospects_admin_route_still_handles_exact_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(prospect_detail, "render", lambda ctx, prospect_id: pytest.fail("prospect_detail should not handle exact /prospects"))

    status, _content_type, body, _headers = route_response_with_headers("GET", "/prospects", tmp_path / "runtime")

    assert status == HTTPStatus.NOT_FOUND
    assert "Not Found" in body.decode("utf-8")


def test_prospect_promote_post_stages_queue_job(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    form = "site_id=7040&actor=Jordan&confirm=1"

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/prospects/x/promote",
        runtime_root,
        form.encode("utf-8"),
    )

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/prospects/x?message=staged&site_id=7040"
    queue_files = list((runtime_root / "queue").glob("promote-prospect-*.json"))
    assert len(queue_files) == 1
    payload = json.loads(queue_files[0].read_text(encoding="utf-8"))
    assert payload["job_type"] == JOB_PROMOTE_PROSPECT
    assert payload["payload"] == {"prospect_id": "x", "site_id": "7040", "actor": "Jordan"}


def test_prospect_promote_post_rejects_missing_confirm(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/prospects/x/promote",
        runtime_root,
        b"site_id=7040&actor=Jordan",
    )

    assert status == HTTPStatus.SEE_OTHER
    assert parse_qs(headers["Location"].split("?", 1)[1]) == {"error": ["confirm_required"]}
    assert not (runtime_root / "queue").exists()


def test_prospect_promote_post_rejects_missing_site_id(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/prospects/x/promote",
        runtime_root,
        b"site_id=&actor=Jordan&confirm=1",
    )

    assert status == HTTPStatus.SEE_OTHER
    assert parse_qs(headers["Location"].split("?", 1)[1]) == {"error": ["missing_field"]}
    assert not (runtime_root / "queue").exists()
