from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.sections import site_detail


class DummyContext(SimpleNamespace):
    def __init__(self, tmp_path: Path, query: dict[str, list[str]] | None = None) -> None:
        super().__init__(runtime_root=tmp_path / "runtime", query=query or {})


def _stub_downstream(monkeypatch: pytest.MonkeyPatch) -> None:
    # _related_sections needs BTQ_COUCHDB_URL and _photos_section needs a real
    # runtime_root + field_photos.latest_photo_cards; in a bare test both throw,
    # tipping render into its _degraded except-handler and masking the real page.
    # Neutralize them so we exercise the genuine non-degraded render path.
    monkeypatch.setattr(site_detail, "_related_sections", lambda *a, **k: [])
    monkeypatch.setattr(site_detail, "_photos_section", lambda *a, **k: "")


def test_sandbox_renders_full_non_degraded_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: None)
    _stub_downstream(monkeypatch)
    ctx = DummyContext(tmp_path, {})

    html = site_detail.render(ctx, "SANDBOX")

    # The full, non-degraded H1 proves the builtin-location fallback path:
    # canonical_name(builtin) -> "Sandbox Site". Prompt 364 made the H1 the
    # canonical name only (no trailing " · {site_id}").
    assert "<h1>Sandbox Site</h1>" in html
    assert "Sandbox Site · SANDBOX" not in html
    assert "Sandbox Site" in html
    # The site_id still appears on the page (e.g. the admin-metadata link).
    assert "SANDBOX" in html
    # Not the not_found page.
    assert "Site not found" not in html
    # Not the _degraded page (its bare header is "<h1>Site SANDBOX</h1>").
    assert "<h1>Site SANDBOX</h1>" not in html


def test_builtin_location_doc_sandbox_and_unknown() -> None:
    doc = site_detail._builtin_location_doc("SANDBOX")
    assert isinstance(doc, dict)
    assert doc["type"] == "location"
    assert doc["site_id"] == "SANDBOX"
    from ops_dashboard.sections import sites

    assert sites.canonical_name(doc) == "Sandbox Site"
    assert (doc.get("location") or doc.get("account")) == "Sandbox Site"

    assert site_detail._builtin_location_doc("1337") is None
    assert site_detail._builtin_location_doc("totally-unknown") is None


def test_real_location_doc_wins_over_builtin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = {
        "type": "location",
        "location": "Real Co",
        "account": "Real Co",
        "site_id": "SANDBOX",
    }
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: real)
    _stub_downstream(monkeypatch)
    ctx = DummyContext(tmp_path, {})

    html = site_detail.render(ctx, "SANDBOX")

    # Prompt 364: H1 is the canonical name only (no trailing " · {site_id}").
    assert "<h1>Real Co</h1>" in html
    assert "Real Co · SANDBOX" not in html
    # The real site_id still appears on the page (e.g. links).
    assert "SANDBOX" in html
    # The builtin's name must NOT have been consulted.
    assert "Sandbox Site" not in html
    assert "Site not found" not in html
    assert "<h1>Site SANDBOX</h1>" not in html


def test_unknown_site_still_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: None)
    _stub_downstream(monkeypatch)
    ctx = DummyContext(tmp_path, {})

    html = site_detail.render(ctx, "nope-xyz")

    assert "Site not found" in html
