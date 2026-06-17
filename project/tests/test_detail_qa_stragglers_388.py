"""Independent verifier gating tests for prompt 388 (detail QA stragglers).

Covers two render-path fixes:

  Bug #6 — employee-detail Quick facts two-column overlap. The page must render
  quick-facts via the site-detail working pattern: a ``<section class="quick-facts">``
  wrapper using ``record_section`` output, NOT the old
  ``<section class="quick-facts grid">`` container that caused the two groups'
  text to overlap. Both Identity and Assignment groups must still be present.

  Bug #7 — imported-capture photo thumbnails blank. A capture photo whose media
  file EXISTS under the upload root must render a real ``<img src="/media/...">``.
  A photo whose media is MISSING (or whose path is unsafe / escapes the upload
  root) must render the muted "No safe image preview available" fallback and
  NEVER a dead ``<img>``.

Sandbox identity only (cap-sandbox-*, cap-import-sandbox-*, SANDBOX / Sandbox
Site, sandbox-user). Drives the real render functions in-process against a tmp
upload_root the test populates.

Invariant under check: media resolution MUST route through
``resolve_media_path`` / ``resolve_media_request`` (the path-security boundary in
field_capture/site_viewer.py); a traversal/absolute path that escapes the upload
root must NOT be served.

Mutation check (see verifier report): break the file-exists guard in
``captures._is_image_file`` so a missing-media photo emits an ``<img>`` again ->
test_missing_media_renders_fallback_no_img turns RED; restore byte-identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ops_dashboard.sections import captures
from ops_dashboard.sections import employee_detail as ed


# --------------------------------------------------------------------------
# Fixtures: a real upload_root populated with sandbox capture media.
#   layout: <root>/uploads/<date>/<capture_id>/<file>
# --------------------------------------------------------------------------

_DATE = "2026-06-16"
_IMPORT_CAPTURE = "cap-import-sandbox-0001"
_PLAIN_CAPTURE = "cap-sandbox-0001"

# 1x1 transparent PNG.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f5f0000000049454e44ae42"
    "6082"
)


def _seed_photo(root: Path, capture_id: str, filename: str) -> Path:
    capture_dir = root / "uploads" / _DATE / capture_id
    capture_dir.mkdir(parents=True, exist_ok=True)
    target = capture_dir / filename
    target.write_bytes(_PNG_BYTES)
    return target


# --------------------------------------------------------------------------
# Bug #7 — present media renders a real <img src="/media/...">
# --------------------------------------------------------------------------


def test_present_media_renders_real_img(tmp_path: Path) -> None:
    _seed_photo(tmp_path, _IMPORT_CAPTURE, "photo-a.png")
    record = {
        "capture_id": _IMPORT_CAPTURE,
        "photo_id": "photo-a.png",
        "image_media_url": f"/media/{_DATE}/{_IMPORT_CAPTURE}/photo-a.png",
        "source_image_path": str(
            tmp_path / "uploads" / _DATE / _IMPORT_CAPTURE / "photo-a.png"
        ),
        "area_guess": "Sandbox lobby",
    }
    out = captures.render_photo_preview(record, tmp_path)
    assert "<img" in out
    assert 'src="/media/' in out
    assert "photo-a.png" in out
    assert "No safe image preview available" not in out


def test_present_media_resolved_url_round_trips(tmp_path: Path) -> None:
    """Even when image_media_url is absent, a real on-disk file under the
    capture dir is discovered via the capture/filename path."""
    _seed_photo(tmp_path, _IMPORT_CAPTURE, "photo-b.png")
    record = {
        "capture_id": _IMPORT_CAPTURE,
        "photo_id": "photo-b.png",
        "image_media_url": "",  # divergent / missing url
        "source_image_path": "",
    }
    url = captures.resolved_photo_media_url(record, tmp_path)
    assert url.startswith("/media/")
    assert url.endswith("photo-b.png")


# --------------------------------------------------------------------------
# Bug #7 — missing media renders fallback, NEVER a dead <img>
# (this is the mutation-check anchor)
# --------------------------------------------------------------------------


def test_missing_media_renders_fallback_no_img(tmp_path: Path) -> None:
    # Upload root exists but this photo's file was never written.
    (tmp_path / "uploads" / _DATE / _IMPORT_CAPTURE).mkdir(parents=True, exist_ok=True)
    record = {
        "capture_id": _IMPORT_CAPTURE,
        "photo_id": "ghost.png",
        "image_media_url": f"/media/{_DATE}/{_IMPORT_CAPTURE}/ghost.png",
        "source_image_path": str(
            tmp_path / "uploads" / _DATE / _IMPORT_CAPTURE / "ghost.png"
        ),
        "area_guess": "Sandbox closet",
    }
    out = captures.render_photo_preview(record, tmp_path)
    assert "No safe image preview available" in out
    assert "<img" not in out


def test_missing_media_resolved_url_is_empty(tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    record = {
        "capture_id": _PLAIN_CAPTURE,
        "photo_id": "nope.png",
        "image_media_url": f"/media/{_DATE}/{_PLAIN_CAPTURE}/nope.png",
        "source_image_path": "",
    }
    assert captures.resolved_photo_media_url(record, tmp_path) == ""


def test_photo_card_missing_media_no_img(tmp_path: Path) -> None:
    """The card wrapper (the real call site in render_detail) must also not
    emit a dead <img> for a missing photo."""
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    record = {
        "capture_id": _IMPORT_CAPTURE,
        "photo_id": "missing-card.png",
        "image_media_url": f"/media/{_DATE}/{_IMPORT_CAPTURE}/missing-card.png",
        "source_image_path": "",
        "area_guess": "Sandbox dock",
    }
    out = captures.render_photo_card(record, tmp_path)
    assert "<img" not in out
    assert "No safe image preview available" in out


# --------------------------------------------------------------------------
# Bug #7 — security: a path escaping the upload root must NOT be served.
# --------------------------------------------------------------------------


def test_traversal_path_not_served(tmp_path: Path) -> None:
    # A real secret outside the upload root.
    secret = tmp_path / "etc-passwd-sandbox.png"
    secret.write_bytes(_PNG_BYTES)
    (tmp_path / "uploads" / _DATE / _IMPORT_CAPTURE).mkdir(parents=True, exist_ok=True)
    record = {
        "capture_id": _IMPORT_CAPTURE,
        "photo_id": "../../../etc-passwd-sandbox.png",
        "image_media_url": f"/media/{_DATE}/{_IMPORT_CAPTURE}/../../../../etc-passwd-sandbox.png",
        "source_image_path": str(secret),
    }
    url = captures.resolved_photo_media_url(record, tmp_path)
    assert url == ""
    out = captures.render_photo_preview(record, tmp_path)
    assert "<img" not in out
    assert "etc-passwd-sandbox.png" not in out
    assert "No safe image preview available" in out


def test_absolute_outside_root_not_served(tmp_path: Path) -> None:
    outside = tmp_path / "outside-sandbox.png"
    outside.write_bytes(_PNG_BYTES)
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    record = {
        "capture_id": _PLAIN_CAPTURE,
        "photo_id": "",
        "image_media_url": "",
        "source_image_path": str(outside),  # absolute, outside upload root
    }
    assert captures.resolved_photo_media_url(record, tmp_path) == ""


def test_security_boundary_routes_through_resolver(monkeypatch, tmp_path: Path) -> None:
    """Confirm media resolution actually calls resolve_media_path /
    resolve_media_request (the path-security boundary), not some ad-hoc bypass."""
    _seed_photo(tmp_path, _IMPORT_CAPTURE, "boundary.png")
    calls: dict[str, int] = {"path": 0, "request": 0}
    real_path = captures.resolve_media_path
    real_request = captures.resolve_media_request

    def spy_path(*a, **k):
        calls["path"] += 1
        return real_path(*a, **k)

    def spy_request(*a, **k):
        calls["request"] += 1
        return real_request(*a, **k)

    monkeypatch.setattr(captures, "resolve_media_path", spy_path)
    monkeypatch.setattr(captures, "resolve_media_request", spy_request)
    record = {
        "capture_id": _IMPORT_CAPTURE,
        "photo_id": "boundary.png",
        "image_media_url": f"/media/{_DATE}/{_IMPORT_CAPTURE}/boundary.png",
        "source_image_path": "",
    }
    captures.resolved_photo_media_url(record, tmp_path)
    assert calls["path"] + calls["request"] > 0, "media never routed through the security boundary"


# --------------------------------------------------------------------------
# Bug #7 — first-render stability: a racing/bad best-effort scan must not raise.
# --------------------------------------------------------------------------


def test_detail_render_does_not_raise_on_scan_error(monkeypatch, tmp_path: Path) -> None:
    """photo_card_records_for_capture must swallow a discover_photo_assets blow-up
    (the previously-unguarded best-effort path) and still return."""

    def boom(*_a, **_k):
        raise RuntimeError("filesystem race during best-effort media scan")

    monkeypatch.setattr(
        captures.field_photo_vision, "discover_photo_assets", boom
    )
    # Must not raise.
    records = captures.photo_card_records_for_capture(tmp_path, _IMPORT_CAPTURE)
    assert isinstance(records, list)


# --------------------------------------------------------------------------
# Bug #6 — employee quick-facts uses the working .quick-facts / record_section
# pattern, not the overlap-causing .quick-facts.grid container.
# --------------------------------------------------------------------------


def _sandbox_doc() -> dict[str, object]:
    return {
        "_id": "employee_sandbox-user",
        "type": "employee",
        "name": "Sandy Sandbox",
        "person_id": "sandbox-user",
        "status": "active",
        "role": "Cleaner",
        "employee_id": "EHUB-SANDBOX",
        "phone": "555-0100",
        "email": "sandbox@example.com",
    }


def test_quick_facts_uses_working_container_not_grid() -> None:
    out = ed._quick_facts(
        _sandbox_doc(),
        person_id="sandbox-user",
        primary_site_id="SANDBOX",
        assigned_site_ids=["SANDBOX"],
        site_names={"SANDBOX": "Sandbox Site"},
    )
    # Working pattern: bare .quick-facts wrapper (site-detail style).
    assert 'class="quick-facts"' in out
    # The overlap-causing grid container must be gone.
    assert 'class="quick-facts grid"' not in out
    assert "grid-column" not in out


def test_quick_facts_has_both_groups_via_record_section() -> None:
    out = ed._quick_facts(
        _sandbox_doc(),
        person_id="sandbox-user",
        primary_site_id="SANDBOX",
        assigned_site_ids=["SANDBOX"],
        site_names={"SANDBOX": "Sandbox Site"},
    )
    # Both groups present (record_section emits <h3>title</h3>).
    assert "<h3>Identity</h3>" in out
    assert "<h3>Assignment</h3>" in out
    # record_section's dl_class is carried through.
    assert 'class="fields summary-fields"' in out
    # Representative fields from each group survive.
    assert "Cleaner" in out  # role (Identity)
    assert "Sandbox Site" in out  # primary site resolved name (Assignment)
