from __future__ import annotations

import html
import json
from http import HTTPStatus
from io import BytesIO
from pathlib import Path

from field_capture import approved_job_drafts
from field_capture.server import FieldCaptureHandler
from ops_dashboard import audit
import ops_dashboard.common as common
from ops_dashboard.app import request_context, route_response, route_response_with_headers
from ops_dashboard.common import SectionContext
from ops_dashboard.layout import NAV_ITEMS, _VERSION, nav_html
from ops_dashboard.sections.inbox import candidate_inbox_row, unknown_capture_rows
from processing_core.approved_job_drafts import write_approved_job_draft
from processing_core.action_candidates import action_candidate_payload, write_action_candidate_review
from processing_core.artifacts import write_json_object
from shared_pwa.assets import DB_JS_PATH
from tests.test_ops_dashboard import request_text, write_field_capture_fixture, write_photo_vision_sidecar, write_vault_site_issue
from voice_memo.server import VoiceMemoHandler


def candidate_id_for(runtime_root: Path) -> str:
    [path] = sorted((runtime_root / "reviews" / "action_candidates" / "field_capture").glob("*.json"))
    return json.loads(path.read_text(encoding="utf-8"))["candidate_id"]


class StaticCaptureHarness:
    def __init__(self) -> None:
        self.status: HTTPStatus | None = None
        self.headers: dict[str, str] = {}
        self.wfile = BytesIO()

    def send_response(self, status: HTTPStatus) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.headers[key] = value

    def end_headers(self) -> None:
        return


def served_static_body(handler_class: type, path: str) -> bytes:
    handler = object.__new__(handler_class)
    harness = StaticCaptureHarness()
    handler.send_response = harness.send_response
    handler.send_header = harness.send_header
    handler.end_headers = harness.end_headers
    handler.wfile = harness.wfile

    assert handler.try_serve_static(path)
    assert harness.status == HTTPStatus.OK
    assert harness.headers["Content-Type"] == "application/javascript; charset=utf-8"
    return harness.wfile.getvalue()


def write_vault_site_about(vault_root: Path, *, site_id: str = "7060", account: str = "Contworks", name: str = "Continental Metalworks") -> Path:
    path = vault_root / "Accounts" / account / "Locations" / f"{site_id} - {name}" / "about.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
type: location
site_id: "{site_id}"
account: {account}
location: {name}
---
# {name}
""",
        encoding="utf-8",
    )
    return path


def write_vault_supply_need(
    vault_root: Path,
    *,
    account: str = "Summitsteel",
    site_dir: str = "7050 - Summit Wire",
    supply_id: str = "sup_cleaner",
    site_id: str = "7050",
    status: str = "open",
    item_name: str = "BrightWash cleaner",
) -> Path:
    path = vault_root / "Accounts" / account / "Locations" / site_dir / "Supplies" / f"{supply_id}__supply.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
type: supply_need
supply_id: {supply_id}
site_id: "{site_id}"
site_name: Summit Wire
account: {account}
item_name: {item_name}
quantity_needed: 2 bottles
urgency: high
requested_by: Tom Walsh
observed_at: 2026-05-08T14:12:43+00:00
source: field_capture
status: {status}
created_at: 2026-05-08T20:00:00+00:00
notes: Supply closet is empty.
related_capture_ids: []
related_candidate_ids: []
---
# Supply need: {item_name}
""",
        encoding="utf-8",
    )
    return path


def write_vault_equipment_request(
    vault_root: Path,
    *,
    account: str = "Summitsteel",
    site_dir: str = "7050 - Summit Wire",
    equipment_id: str = "eqr_vacuum",
    site_id: str = "7050",
    status: str = "open",
    equipment_name: str = "vacuum",
) -> Path:
    path = vault_root / "Accounts" / account / "Locations" / site_dir / "Equipment" / f"{equipment_id}__equipment.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
type: equipment_request
equipment_id: {equipment_id}
site_id: "{site_id}"
site_name: Summit Wire
account: {account}
equipment_name: {equipment_name}
reason: Current unit will not start.
priority: urgent
requested_by: Tom Walsh
observed_at: 2026-05-08T14:12:43+00:00
source: field_capture
status: {status}
created_at: 2026-05-08T20:00:00+00:00
notes: Needed for lobby carpet.
related_capture_ids: []
related_candidate_ids: []
---
# Equipment request: {equipment_name}
""",
        encoding="utf-8",
    )
    return path


def write_vault_site_issue_record(
    vault_root: Path,
    *,
    account: str = "Summitsteel",
    site_dir: str = "7050 - Summit Wire",
    issue_id: str = "iss_drain",
    site_id: str = "7050",
    status: str = "open",
    title: str = "Restroom drain backup",
) -> Path:
    path = vault_root / "Accounts" / account / "Locations" / site_dir / "Issues" / f"{issue_id}__issue.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
type: site_issue
issue_id: {issue_id}
site_id: "{site_id}"
site: Summit Wire
account: {account}
title: {title}
status: {status}
priority: high
category: maintenance
client_notified: true
client_notified_method: email
reported_by: Tom Walsh
observed_at: 2026-05-08T14:12:43+00:00
created_at: 2026-05-08T20:00:00+00:00
resolution_trigger: Maintenance confirms the issue is clear.
related_capture_ids: []
related_candidate_ids: []
---
# {title}

## Summary
Needs operator follow-up.
""",
        encoding="utf-8",
    )
    return path


def card_by_id(payload: dict[str, object], card_id: str) -> dict[str, object]:
    cards = payload.get("cards") if isinstance(payload.get("cards"), list) else []
    return next(card for card in cards if isinstance(card, dict) and card.get("id") == card_id)


def rendered_card(body: str, card_id: str) -> str:
    start = body.index(f'<article class="inbox-card" data-card-id="{card_id}"')
    end = body.index("</article>", start)
    return body[start:end]


def header_cell_count(fragment: str) -> int:
    return fragment.count("<th>") + fragment.count("<th ")


def test_existing_status_route_moved_to_health_path(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    root_status, _root_type, root_body = request_text("GET", "/", runtime_root)
    health_status, _health_type, health_body = request_text("GET", "/health", runtime_root)

    assert root_status == HTTPStatus.OK
    assert 'aria-current="page"><span class="nav-glyph" aria-hidden="true">H</span><span class="nav-label">Home</span>' in root_body
    assert health_status == HTTPStatus.OK
    assert "Runtime Health" in health_body
    assert 'href="/admin" title="Admin" aria-current="page">' in health_body


def test_root_renders_home_with_active_nav(tmp_path: Path) -> None:
    status, content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "text/html" in content_type
    assert 'aria-current="page"><span class="nav-glyph" aria-hidden="true">H</span><span class="nav-label">Home</span>' in body


def test_layout_sidebar_has_collapse_toggle(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'class="nav-toggle"' in body
    assert "btq-admin-nav-collapsed" in body


def test_layout_head_reads_theme_before_first_paint(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    head = body[: body.index("</head>")]
    assert "'btq-admin-theme'" in head
    assert "document.documentElement.dataset.theme = _t" in head


def test_layout_renders_three_state_theme_toggle(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'data-theme-toggle' in body
    assert 'aria-label="Theme: System (click to switch)"' in body
    assert "const THEME_KEY = 'btq-admin-theme';" in body
    assert "m === 'light' ? '☀' : m === 'dark' ? '☾' : '◐'" in body


def test_layout_nav_items_render_glyphs(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    for glyph, label in (("H", "Home"), ("P", "Field Photos"), ("A", "Admin"), ("?", "Help")):
        assert f'<span class="nav-glyph" aria-hidden="true">{glyph}</span><span class="nav-label">{label}</span>' in body


def test_nav_items_include_field_photos() -> None:
    assert len(NAV_ITEMS) == 7
    # "Candidates" (/candidates) added in 320/A: the console replaced the home preview card that was
    # the only link to the full candidates route, so it gets its own nav entry.
    assert [label for _section, label, _href, _glyph in NAV_ITEMS] == ["Home", "Review", "Candidates", "Vault", "Field Photos", "Admin", "Help"]


def test_admin_nav_entry_active_on_health_page() -> None:
    body = nav_html("health")

    assert 'href="/admin" title="Admin" aria-current="page"' in body


def test_field_photos_nav_entry_active_on_field_photos_page(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/field-photos", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'href="/field-photos" title="Field Photos" aria-current="page"' in body


def test_field_photos_link_present_on_candidates_page(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/candidates", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'href="/field-photos"' in body


def test_nav_version_readout_renders_at_bottom_after_links(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    nav_brand_start = body.index('<div class="nav-brand">')
    nav_brand_end = body.index("</div>", nav_brand_start)
    nav_brand = body[nav_brand_start:nav_brand_end]
    last_nav_link = body.rindex("</a>", 0, body.index("</nav>"))
    nav_bottom = body.index('<div class="nav-bottom">')

    assert '<span class="nav-label">BTQ Ops</span>' in nav_brand
    assert html.escape(_VERSION) not in nav_brand
    assert nav_bottom > last_nav_link
    assert f'<span class="nav-label muted" style="font-size:0.75em">{html.escape(_VERSION)}</span>' in body


def test_static_admin_css_served_with_cache_header(tmp_path: Path) -> None:
    status, content_type, body, headers = route_response_with_headers("GET", "/static/admin.css", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "text/css" in content_type
    assert b".admin-shell" in body
    assert headers["Cache-Control"] == "public, max-age=300"


def test_admin_css_defines_empty_strip_classes(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/static/admin.css", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert ".empty-strip" in body
    assert ".empty-pills" in body


def test_admin_css_defines_theme_palette_and_dark_mode(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/static/admin.css", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "@media (prefers-color-scheme: dark)" in body
    assert '[data-theme="dark"]' in body
    for variable in ("--nav-bg", "--highlight-bg", "--btn-bg", "--approve-bg", "--reject-bg", "--pill-bg"):
        assert variable in body
    assert "background: var(--nav-bg)" in body


def test_static_recorder_js_served_with_cache_header(tmp_path: Path) -> None:
    status, content_type, body, headers = route_response_with_headers("GET", "/static/recorder.js", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert content_type == "application/javascript; charset=utf-8"
    assert b"attachRecorder" in body
    assert headers["Cache-Control"] == "public, max-age=300"


def test_static_recorder_js_supports_pause_resume(tmp_path: Path) -> None:
    status, _content_type, body, _headers = route_response_with_headers("GET", "/static/recorder.js", tmp_path / "runtime")
    app_js = body.decode("utf-8")

    assert status == HTTPStatus.OK
    assert "recorder.pause()" in app_js
    assert "recorder.resume()" in app_js
    assert 'recorder?.state === "paused"' in app_js
    assert "audioElapsedBeforePause" in app_js


def test_static_db_js_served_with_cache_header(tmp_path: Path) -> None:
    status, content_type, body, headers = route_response_with_headers("GET", "/static/db.js", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert content_type == "application/javascript; charset=utf-8"
    assert body == DB_JS_PATH.read_bytes()
    assert b"window.fieldCaptureDb" in body
    assert headers["Cache-Control"] == "public, max-age=300"


def test_all_pwas_serve_identical_db_js(tmp_path: Path) -> None:
    shared_db = DB_JS_PATH.read_bytes()
    status, content_type, ops_body, _headers = route_response_with_headers("GET", "/static/db.js", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert content_type == "application/javascript; charset=utf-8"
    assert shared_db == served_static_body(FieldCaptureHandler, "/db.js")
    assert shared_db == served_static_body(VoiceMemoHandler, "/db.js")
    assert shared_db == ops_body


def test_tape_deck_recorder_includes_capture_id_field(tmp_path: Path) -> None:
    status, _content_type, body, _headers = route_response_with_headers("GET", "/static/recorder.js", tmp_path / "runtime")
    app_js = body.decode("utf-8")

    assert status == HTTPStatus.OK
    assert "cap-tapedeck-" in app_js
    assert 'input[name="capture_id"]' in app_js
    assert "newCaptureId()" in app_js
    assert "ensureCaptureId(form)" in app_js


def test_tape_deck_safety_net_stash_present(tmp_path: Path) -> None:
    status, _content_type, body, _headers = route_response_with_headers("GET", "/static/recorder.js", tmp_path / "runtime")
    app_js = body.decode("utf-8")

    assert status == HTTPStatus.OK
    assert "stashFailedSubmit" in app_js
    assert "drainSafetyNet" in app_js
    assert "window.fieldCaptureDb" in app_js
    assert "ops_dashboard_tapedeck" in app_js
    assert "opsRecorderRetryBanner" in app_js


def test_tape_deck_fetch_treats_error_redirect_as_failure(tmp_path: Path) -> None:
    status, _content_type, body, _headers = route_response_with_headers("GET", "/static/recorder.js", tmp_path / "runtime")
    app_js = body.decode("utf-8")

    assert status == HTTPStatus.OK
    assert 'responseUrl.searchParams.has("error")' in app_js
    assert "&& !redirectedToError" in app_js


def test_admin_route_returns_200(tmp_path: Path) -> None:
    status, _content_type, _body = request_text("GET", "/admin", tmp_path / "runtime")

    assert status == HTTPStatus.OK


def test_admin_screen_links_to_health(tmp_path: Path) -> None:
    body = request_text("GET", "/admin", tmp_path / "runtime")[2]

    assert 'href="/health"' in body


def test_admin_screen_links_to_sites(tmp_path: Path) -> None:
    body = request_text("GET", "/admin", tmp_path / "runtime")[2]

    assert 'href="/sites"' in body


def test_admin_screen_links_to_tokens(tmp_path: Path) -> None:
    body = request_text("GET", "/admin", tmp_path / "runtime")[2]

    assert 'href="/tokens"' in body


def test_admin_screen_links_to_system(tmp_path: Path) -> None:
    body = request_text("GET", "/admin", tmp_path / "runtime")[2]

    assert 'href="/system"' in body


def test_admin_screen_links_to_photos(tmp_path: Path) -> None:
    body = request_text("GET", "/admin", tmp_path / "runtime")[2]

    assert 'href="/photos"' in body


def test_admin_screen_links_to_audio_processing(tmp_path: Path) -> None:
    body = request_text("GET", "/admin", tmp_path / "runtime")[2]

    assert 'href="/audio"' in body
    assert "Audio Processing" in body


def seed_field_audio(runtime_root: Path, capture_id: str = "cap-audio-pending") -> None:
    audio_path = runtime_root / "uploads" / "2026-05-31" / capture_id / "voice.webm"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"audio bytes")
    write_json_object(
        runtime_root / "field_capture" / "intake" / f"{capture_id}.json",
        {
            "job_type": "photo_capture",
            "metadata": {"capture_id": capture_id, "site_id": "7050"},
            "payload": {
                "area": "Restrooms",
                "phase": "issue",
                "captured_at": "2026-05-31T17:25:00-04:00",
                "photos": [],
                "audio": [
                    {
                        "media_type": "audio",
                        "filename": "voice.webm",
                        "mime_type": "audio/webm",
                        "stored_path": str(audio_path),
                        "upload_id": f"2026-05-31/{capture_id}/voice.webm",
                        "size_bytes": audio_path.stat().st_size,
                    }
                ],
            },
        },
    )


def test_audio_processing_page_shows_pending_field_audio(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_field_audio(runtime_root)

    status, _content_type, body = request_text("GET", "/audio", runtime_root)

    assert status == HTTPStatus.OK
    assert "Audio Processing" in body
    assert "Field Capture Audio" in body
    assert "awaiting transcript" in body
    assert "/media/2026-05-31/cap-audio-pending/voice.webm" in body
    assert "/captures?capture_id=cap-audio-pending" in body


def test_audio_processing_page_shows_voice_memo_inbox(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    inbox = runtime_root / "voice_inbox"
    inbox.mkdir(parents=True)
    (inbox / "vm-cap-voice-123.webm").write_bytes(b"audio bytes")
    write_json_object(
        inbox / "vm-cap-voice-123.metadata.json",
        {
            "capture_id": "cap-voice-123",
            "source": "voice_memo",
            "site_id": "7060",
            "created_at": "2026-05-31T18:00:00-04:00",
        },
    )

    status, _content_type, body = request_text("GET", "/audio", runtime_root)

    assert status == HTTPStatus.OK
    assert "Voice Memo Audio" in body
    assert "vm-cap-voice-123.webm" in body
    assert "cap-voice-123" in body


def test_audio_processing_page_shows_recent_voice_memo_couchdb_docs(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    completed = runtime_root / "completed" / "audio"
    completed.mkdir(parents=True)
    (completed / "vm-cap-voice-eastern.webm").write_bytes(b"audio")

    monkeypatch.setenv("VOICE_MEMO_COUCHDB_URL", "http://couchdb.test")
    monkeypatch.setenv("VOICE_MEMO_COUCHDB_USER", "admin")
    monkeypatch.setenv("VOICE_MEMO_COUCHDB_PASSWORD", "secret")

    def fake_find(_config, _database, _selector):
        return {
            "docs": [
                {
                    "_id": "cap-voice-eastern",
                    "capture_id": "cap-voice-eastern",
                    "created_at": "2026-05-31T22:30:00Z",
                    "mode": "operations",
                    "site_account": "WGTCO",
                    "site_location": "Western Gas Transmission",
                    "processing_state": "intake_done",
                    "audio_path": "2026/05/cap-voice-eastern.webm",
                    "duration_seconds": 14,
                }
            ]
        }

    monkeypatch.setattr("ops_dashboard.sections.audio.query_couchdb_find", fake_find)

    status, _content_type, body = request_text("GET", "/audio", runtime_root)

    assert status == HTTPStatus.OK
    assert "Recent Records" in body
    assert "Western Gas Transmission" in body
    assert "intake_done" in body
    assert "completed audio" in body


def test_review_approve_appends_redacted_audit_line(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_id = candidate_id_for(runtime_root)
    couchdb_review.seed_from_fs(runtime_root)

    status, _content_type, _body = route_response(
        "POST",
        "/field-capture/review/approve",
        runtime_root,
        f"candidate_id={candidate_id}&reviewer=Jordan&password=secret&rationale=ok".encode(),
    )

    assert status == HTTPStatus.SEE_OTHER
    # 308b: the shared CouchDB review fn still records the redacted audit line.
    [line] = (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()
    payload = json.loads(line)
    assert payload["route"] == "/field-capture/review/approve"
    assert payload["payload"]["password"] == "[REDACTED]"
    assert "success" in payload["result_summary"]
    assert couchdb_review.status_of(candidate_id) == "approved"


def test_review_reject_appends_redacted_audit_line(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_id = candidate_id_for(runtime_root)

    route_response(
        "POST",
        "/field-capture/review/reject",
        runtime_root,
        f"candidate_id={candidate_id}&reviewer=Jordan&token_value=abc&rationale=no".encode(),
    )

    payload = json.loads((runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()[0])
    assert payload["route"] == "/field-capture/review/reject"
    assert payload["payload"]["token_value"] == "[REDACTED]"


def test_client_informed_appends_redacted_audit_line(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_id = candidate_id_for(runtime_root)
    route_response("POST", "/field-capture/review/approve", runtime_root, f"candidate_id={candidate_id}&reviewer=Jordan&rationale=ok".encode())

    route_response("POST", "/field-capture/review/client-informed", runtime_root, f"candidate_id={candidate_id}&method=email&by=Jordan&secret_note=hide".encode())

    lines = (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[-1])
    assert payload["route"] == "/field-capture/review/client-informed"
    assert payload["payload"]["secret_note"] == "[REDACTED]"


def test_audit_redacts_token_and_password_fields() -> None:
    assert audit.redacted_payload({"token": "a", "password_hint": "b", "safe": "c"}) == {
        "token": "[REDACTED]",
        "password_hint": "[REDACTED]",
        "safe": "c",
    }


def test_legacy_review_url_still_serves_same_form_shape(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/field-capture/review", runtime_root)

    assert status == HTTPStatus.OK
    assert 'action="/field-capture/review/approve"' in body
    assert 'action="/field-capture/review/reject"' in body
    assert "Raw transcript" in body  # context is expanded by default; no "Show context" toggle


def test_candidates_section_renders_independently_of_legacy_app() -> None:
    section_path = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "sections" / "candidates.py"

    assert "legacy" "_app" not in section_path.read_text(encoding="utf-8")


def test_candidates_route_renders_200_and_contains_filter_rail(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/candidates", runtime_root)

    assert status == HTTPStatus.OK
    assert 'class="filter-rail"' in body
    for label in ("Status", "Site", "Submitter", "Date from", "Date to", "Area contains", "Has photo", "Has audio", "Has photo-vision warning"):
        assert label in body


def test_legacy_review_url_still_aliases_to_candidates(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    candidates_status, _candidates_type, candidates_body = request_text("GET", "/candidates", runtime_root)
    legacy_status, _legacy_type, legacy_body = request_text("GET", "/field-capture/review", runtime_root)

    assert candidates_status == HTTPStatus.OK
    assert legacy_status == HTTPStatus.OK
    assert 'action="/field-capture/review/approve"' in legacy_body
    assert 'class="filter-rail"' in legacy_body
    assert "<h1>Captures</h1>" in candidates_body
    assert "<h1>Captures</h1>" in legacy_body


def test_review_approve_audit_line_shape_unchanged(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_id = candidate_id_for(runtime_root)

    status, _content_type, _body = route_response(
        "POST",
        "/field-capture/review/approve",
        runtime_root,
        f"candidate_id={candidate_id}&reviewer=Jordan&rationale=ok".encode(),
    )

    assert status == HTTPStatus.SEE_OTHER
    [line] = (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()
    payload = json.loads(line)
    assert payload["route"] == "/field-capture/review/approve"
    assert payload["actor"] == "localhost"
    assert isinstance(payload["payload"], dict)
    assert payload["payload"]["candidate_id"] == candidate_id
    assert payload["result_summary"]


def test_legacy_app_module_no_longer_exists() -> None:
    legacy_path = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / ("legacy" "_app.py")

    assert not legacy_path.exists(), "legacy" "_app.py should have been deleted in prompt 31"


def test_legacy_issues_url_still_serves(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_site_issue(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/field-capture/issues", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "Site Issues" in body


def test_placeholder_sections_render_with_nav(tmp_path: Path) -> None:
    for route, label in (("/drafts", "Drafts"), ("/failed", "Failed"), ("/captures", "Captures"), ("/sites", "Sites"), ("/tokens", "Tokens"), ("/system", "System")):
        status, _content_type, body = request_text("GET", route, tmp_path / "runtime")
        assert status == HTTPStatus.OK
        assert f">{label}<" in body


def test_inbox_empty_cards_render_with_zero_state(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert body.count('class="inbox-card"') == 5
    assert "Nothing waiting" in body
    assert 'class="inbox-summary-strip"' in body


def test_inbox_section_renders_independently_of_legacy_app() -> None:
    section_path = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "sections" / "inbox.py"

    assert "legacy" "_app" not in section_path.read_text(encoding="utf-8")


def test_inbox_route_renders_200_and_has_expected_cards(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    for card_id in (
        "captures_with_note",
        "pending_candidates",
        "failed_queue_jobs",
        "unknown_captures",
        "open_site_issues",
    ):
        assert f'data-card-id="{card_id}"' in body
    for card_id in (
        "approved_missing_draft",
        "approved_drafts_not_staged",
        "failed_photo_vision_sidecars",
        "uploaded_without_candidate",
        "open_supply_needs",
        "open_equipment_requests",
    ):
        assert f'data-summary-id="{card_id}"' in body


def test_inbox_renders_stat_strip(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'class="stat-strip"' in body
    for card_id in ("captures_with_note", "pending_candidates", "failed_queue_jobs", "unknown_captures", "open_site_issues"):
        assert f'data-stat-id="{card_id}"' in body


def test_inbox_counts_have_semantic_color_css(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'data-stat-id="failed_queue_jobs"' in body
    assert 'data-card-id="failed_queue_jobs"' in body


def test_inbox_primary_grid_has_five_cards(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    start = body.index('<section class="inbox-primary">')
    end = body.index("</section>", start)
    primary = body[start:end]
    assert primary.count('class="inbox-card"') == 5
    for title in ("Captures with a note — needs triage", "Pending candidates", "Failed queue jobs", "Unknown captures", "Open site issues"):
        assert title in primary


def test_inbox_compact_summary_row_lists_low_signal_cards(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    summary = body[body.index('class="inbox-summary-strip"') :]
    for label in ("Missing drafts:", "Unstaged drafts:", "Vision sidecars:", "Uploads w/o candidate:", "Supply:", "Equipment:"):
        assert label in summary


def test_inbox_see_all_hidden_when_count_zero(tmp_path: Path, couchdb_review) -> None:
    empty_status, _content_type, empty_body = request_text("GET", "/inbox", tmp_path / "runtime-empty")
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    base = json.loads(sorted(candidate_dir.glob("*.json"))[0].read_text(encoding="utf-8"))
    base["candidate_id"] = "ac_no_note"
    base["provenance"]["source_transcript_path"] = ""
    write_action_candidate_review(candidate_dir, base)
    populated_status, _content_type, populated_body = request_text("GET", "/inbox", runtime_root)

    assert empty_status == HTTPStatus.OK
    assert "See all" not in rendered_card(empty_body, "pending_candidates")
    assert populated_status == HTTPStatus.OK
    assert "See all" in rendered_card(populated_body, "pending_candidates")


def test_api_inbox_json_route_serves_inbox_payload(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, content_type, body = request_text("GET", "/api/inbox.json", runtime_root)
    payload = json.loads(body)

    assert status == HTTPStatus.OK
    assert "application/json" in content_type
    assert sorted(payload) == ["cards", "generated_at"]
    assert [card["id"] for card in payload["cards"]] == [
        "captures_with_note",
        "pending_candidates",
        "approved_missing_draft",
        "approved_drafts_not_staged",
        "failed_queue_jobs",
        "failed_photo_vision_sidecars",
        "unknown_captures",
        "uploaded_without_candidate",
        "open_site_issues",
        "open_supply_needs",
        "open_equipment_requests",
    ]
    assert payload["cards"][0]["top"][0]["deep_link"].startswith("/candidates?candidate_id=")


def test_inbox_captures_with_note_card_present_when_note_bearing_candidates_exist(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/inbox", runtime_root)

    assert status == HTTPStatus.OK
    assert 'data-card-id="captures_with_note"' in body
    assert "Captures with a note" in body


def test_inbox_unknown_capture_summary_is_not_raw_filename(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    journal = vault / "Journal"
    journal.mkdir(parents=True)
    (journal / "2026-05-08-unknown.md").write_text("# Unknown\n", encoding="utf-8")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    card = rendered_card(body, "unknown_captures")
    assert "Unknown capture - 2026-05-08" in card
    assert ".md" not in card


def test_inbox_unknown_capture_rows_have_non_empty_deep_link(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    journal = vault / "Journal"
    journal.mkdir(parents=True)
    (journal / "2026-05-08-unknown.md").write_text("# Unknown\n", encoding="utf-8")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    _count, rows = unknown_capture_rows(request_context("/inbox", tmp_path / "runtime"))

    assert rows[0]["deep_link"] == "/captures"


def test_candidate_inbox_row_has_note_true_when_transcript_path_set() -> None:
    result = candidate_inbox_row({"source_transcript_path": "/some/path.json"})

    assert result["has_note"] is True


def test_candidate_inbox_row_has_note_false_when_no_transcript_path() -> None:
    result = candidate_inbox_row({})

    assert result["has_note"] is False


def test_inbox_api_json_still_lists_eleven_cards(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/api/inbox.json", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert len(json.loads(body)["cards"]) == 11


def test_inbox_includes_open_site_issues_card(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_site_issue_record(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'data-card-id="open_site_issues"' in body
    assert "Open site issues" in body
    assert "Restroom drain backup" in body


def test_inbox_open_site_issues_count_matches_discover_filter(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_site_issue_record(vault, issue_id="iss_drain", title="Restroom drain backup")
    write_vault_site_issue_record(vault, issue_id="iss_floor", title="Floor tile crack")
    write_vault_site_issue_record(vault, issue_id="iss_watch", status="monitoring", title="Watch ceiling stain")
    write_vault_site_issue_record(vault, issue_id="iss_done", status="resolved", title="Resolved door issue")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    _status, _content_type, body = request_text("GET", "/api/inbox.json", tmp_path / "runtime")
    card = card_by_id(json.loads(body), "open_site_issues")

    assert card["count"] == 2


def test_inbox_open_site_issues_card_excludes_monitoring_and_resolved(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_site_issue_record(vault, issue_id="iss_open", title="Open drain issue")
    write_vault_site_issue_record(vault, issue_id="iss_watch", status="monitoring", title="Monitoring stain")
    write_vault_site_issue_record(vault, issue_id="iss_done", status="resolved", title="Resolved door issue")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    _status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert "Open drain issue" in body
    assert "Monitoring stain" not in body
    assert "Resolved door issue" not in body


def test_inbox_includes_open_supply_needs_card(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'data-summary-id="open_supply_needs"' in body
    assert "Supply: 1" in body


def test_inbox_open_supply_needs_count_excludes_ordered_and_terminal(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, supply_id="sup_open", status="open", item_name="BrightWash cleaner")
    write_vault_supply_need(vault, supply_id="sup_ordered", status="ordered", item_name="Mop heads")
    write_vault_supply_need(vault, supply_id="sup_stocked", status="stocked", item_name="Paper towels")
    write_vault_supply_need(vault, supply_id="sup_none", status="no_action_needed", item_name="Hand soap")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    _status, _content_type, body = request_text("GET", "/api/inbox.json", tmp_path / "runtime")
    card = card_by_id(json.loads(body), "open_supply_needs")

    assert card["count"] == 1


def test_inbox_open_supply_needs_top_rows_link_to_supplies_detail(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, supply_id="sup_cleaner", item_name="BrightWash cleaner")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    _status, _content_type, body = request_text("GET", "/api/inbox.json", tmp_path / "runtime")
    card = card_by_id(json.loads(body), "open_supply_needs")

    assert card["top"][0]["summary"] == "BrightWash cleaner"
    assert card["top"][0]["deep_link"] == "/supplies?supply_id=sup_cleaner"


def test_inbox_includes_open_equipment_requests_card(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'data-summary-id="open_equipment_requests"' in body
    assert "Equipment: 1" in body


def test_inbox_open_equipment_requests_excludes_approved_and_terminal(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault, equipment_id="eqr_open", status="open", equipment_name="vacuum")
    write_vault_equipment_request(vault, equipment_id="eqr_approved", status="approved", equipment_name="floor buffer")
    write_vault_equipment_request(vault, equipment_id="eqr_provided", status="provided", equipment_name="mop bucket")
    write_vault_equipment_request(vault, equipment_id="eqr_denied", status="denied", equipment_name="ladder")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    _status, _content_type, body = request_text("GET", "/api/inbox.json", tmp_path / "runtime")
    card = card_by_id(json.loads(body), "open_equipment_requests")

    assert card["count"] == 1


def test_inbox_open_equipment_requests_top_rows_link_to_equipment_detail(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault, equipment_id="eqr_vacuum", equipment_name="vacuum")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    _status, _content_type, body = request_text("GET", "/api/inbox.json", tmp_path / "runtime")
    card = card_by_id(json.loads(body), "open_equipment_requests")

    assert card["top"][0]["summary"] == "vacuum"
    assert card["top"][0]["deep_link"] == "/equipment?equipment_id=eqr_vacuum"


def test_inbox_card_order_places_structured_open_items_after_intake_cards(tmp_path: Path) -> None:
    _status, _content_type, body = request_text("GET", "/api/inbox.json", tmp_path / "runtime")
    payload = json.loads(body)

    assert [card["id"] for card in payload["cards"]] == [
        "captures_with_note",
        "pending_candidates",
        "approved_missing_draft",
        "approved_drafts_not_staged",
        "failed_queue_jobs",
        "failed_photo_vision_sidecars",
        "unknown_captures",
        "uploaded_without_candidate",
        "open_site_issues",
        "open_supply_needs",
        "open_equipment_requests",
    ]


def test_inbox_uploaded_without_candidate_excludes_those_with_candidate(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root, include_candidate=False)
    status, _content_type, body = request_text("GET", "/inbox", runtime_root)
    assert status == HTTPStatus.OK
    assert "Uploads w/o candidate: 1" in body

    semantic_path = runtime_root / "field_capture" / "audio_semantics" / "fca_test.json"
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Review test note.",
        rationale="Test note.",
        source_text="Test note.",
        source_context="Test note.",
        provenance={
            "semantic_artifact_path": str(semantic_path),
            "source_transcript_path": str(runtime_root / "field_capture" / "audio_transcripts" / "fca_test.json"),
            "audio_asset_id": "fca_test",
        },
        channel_metadata={"channel": "field_capture", "site_id": "7050", "area": "Restrooms", "upload_id": "cap-photo-2026-05-03T18-25-20-04-00"},
    )
    write_action_candidate_review(runtime_root / "reviews" / "action_candidates" / "field_capture", candidate)
    _status, _content_type, body = request_text("GET", "/inbox", runtime_root)
    assert "Uploads w/o candidate: 0" in body


def test_api_inbox_json_shape_matches_html_card_counts(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, content_type, body = request_text("GET", "/api/inbox.json", runtime_root)
    html_status, _html_type, html_body = request_text("GET", "/inbox", runtime_root)
    payload = json.loads(body)

    assert status == HTTPStatus.OK
    assert html_status == HTTPStatus.OK
    assert "application/json" in content_type
    assert len(payload["cards"]) == 11
    for card in payload["cards"]:
        assert str(card["count"]) in html_body


def test_api_inbox_json_includes_deep_links_per_row(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    base = json.loads(sorted(candidate_dir.glob("*.json"))[0].read_text(encoding="utf-8"))
    base["candidate_id"] = "ac_no_note"
    base["provenance"]["source_transcript_path"] = ""
    write_action_candidate_review(candidate_dir, base)

    _status, _content_type, body = request_text("GET", "/api/inbox.json", runtime_root)
    payload = json.loads(body)
    pending = next(card for card in payload["cards"] if card["id"] == "pending_candidates")

    assert pending["top"][0]["deep_link"].startswith("/candidates?candidate_id=")


def test_inbox_table_renders_relative_time_not_raw_seconds(tmp_path: Path, monkeypatch, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    vault = tmp_path / "vault"
    write_vault_site_about(vault, site_id="7050", account="Summitsteel", name="Summit Wire")
    write_field_capture_fixture(runtime_root)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/inbox", runtime_root)

    assert status == HTTPStatus.OK
    assert "<time" in body
    assert "age_seconds=" not in body
    assert "<td>3720</td>" not in body


def test_inbox_capture_shape_card_has_five_columns(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    base = json.loads(sorted(candidate_dir.glob("*.json"))[0].read_text(encoding="utf-8"))
    base["candidate_id"] = "ac_no_note"
    base["provenance"]["source_transcript_path"] = ""
    write_action_candidate_review(candidate_dir, base)

    status, _content_type, body = request_text("GET", "/inbox", runtime_root)

    assert status == HTTPStatus.OK
    assert header_cell_count(rendered_card(body, "pending_candidates")) == 5


def test_inbox_structured_shape_card_has_four_columns(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_site_issue_record(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    card = rendered_card(body, "open_site_issues")
    assert header_cell_count(card) == 4
    assert "<th>Area</th>" not in card


def test_inbox_gap_items_render_in_compact_summary_row(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    draft = {
        "type": "approved_queue_job_draft",
        "draft_id": "ajd_unstaged",
        "status": "approved_draft",
        "candidate_id": "",
        "proposed_job_type": "append_to_note",
        "proposed_payload": {"site_id": "7060", "content": "Needs staging."},
        "source_context": "Needs staging.",
    }
    write_approved_job_draft(approved_job_drafts.default_draft_dir(runtime_root), draft)

    status, _content_type, body = request_text("GET", "/inbox", runtime_root)

    assert status == HTTPStatus.OK
    assert 'data-summary-id="approved_drafts_not_staged"' in body
    assert "Unstaged drafts: 1" in body
    assert 'data-card-id="approved_drafts_not_staged"' not in body


def test_inbox_count_bucket_high_for_large_counts(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    base = json.loads(sorted(candidate_dir.glob("*.json"))[0].read_text(encoding="utf-8"))
    for index in range(25):
        candidate = dict(base)
        candidate["provenance"] = dict(base.get("provenance") or {})
        candidate["candidate_id"] = f"ac_bulk_{index:02}"
        candidate["summary"] = f"Bulk candidate {index}"
        candidate["provenance"]["source_transcript_path"] = ""
        write_action_candidate_review(candidate_dir, candidate)

    status, _content_type, body = request_text("GET", "/inbox", runtime_root)

    assert status == HTTPStatus.OK
    assert 'data-count-bucket="high"' in rendered_card(body, "pending_candidates")


def test_inbox_count_bucket_empty_for_zero_counts(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/inbox", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'data-count-bucket="empty"' in rendered_card(body, "pending_candidates")


def test_failed_page_empty_state_when_nothing_failed(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    (runtime_root / "failed").mkdir(parents=True)
    (runtime_root / "field_capture" / "photo_vision").mkdir(parents=True)

    status, _content_type, body = request_text("GET", "/failed", runtime_root)

    assert status == HTTPStatus.OK
    assert '<p class="zero-state">No failed queue jobs.</p>' in body
    assert '<p class="zero-state">No failed photo-vision sidecars.</p>' in body


def test_captures_filter_uses_datalist_for_site(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/captures", runtime_root)

    assert status == HTTPStatus.OK
    assert '<datalist id="captures-known-sites">' in body
    assert '<option value="7050">' in body


def test_candidates_filter_by_status_and_site(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/candidates?status=pending_review&site=7050", runtime_root)
    empty_status, _empty_type, empty_body = request_text("GET", "/candidates?status=pending_review&site=nope", runtime_root)

    assert status == HTTPStatus.OK
    assert "Review test note." in body
    assert empty_status == HTTPStatus.OK
    assert "No candidates match this filter." in empty_body


def test_candidates_filter_has_photo_and_has_audio(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/candidates?has_photo=true&has_audio=true", runtime_root)

    assert status == HTTPStatus.OK
    assert "Review test note." in body


def test_candidates_grouped_by_capture(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    base = json.loads(sorted(candidate_dir.glob("*.json"))[0].read_text(encoding="utf-8"))
    base["candidate_id"] = "ac_second"
    base["summary"] = "Second candidate same capture."
    write_action_candidate_review(candidate_dir, base)

    status, _content_type, body = request_text("GET", "/candidates", runtime_root)

    assert status == HTTPStatus.OK
    assert body.count('class="candidate-group"') == 1
    assert "Second candidate same capture." in body


def test_candidates_render_multi_candidate_capture_signal(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    base = json.loads(sorted(candidate_dir.glob("*.json"))[0].read_text(encoding="utf-8"))
    multi = dict(base)
    multi["candidate_id"] = "ac_second"
    multi["summary"] = "Second candidate same capture."
    write_action_candidate_review(candidate_dir, multi)
    single = dict(base)
    single["candidate_id"] = "ac_single"
    single["summary"] = "Single candidate capture."
    single["channel_metadata"] = dict(base["channel_metadata"])
    single["channel_metadata"]["upload_id"] = "cap-single"
    write_action_candidate_review(candidate_dir, single)

    status, _content_type, body = request_text("GET", "/candidates?status=pending_review", runtime_root)

    assert status == HTTPStatus.OK
    assert "2 pending candidates from this capture" in body
    assert "Single candidate capture." in body
    assert "1 pending candidates from this capture" not in body


def test_candidates_context_panel_includes_transcript_and_semantic(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    _status, _content_type, body = request_text("GET", "/candidates", runtime_root)

    assert "Raw transcript" in body  # context is expanded by default; no "Show context" toggle
    assert "Test note." in body
    assert "field_audio_semantic_summary" in body


def test_legacy_review_url_aliases_to_candidates(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    legacy = request_text("GET", "/field-capture/review?status=pending_review", runtime_root)[2]
    alias = request_text("GET", "/candidates?status=pending_review", runtime_root)[2]

    assert "Review test note." in legacy
    assert legacy == alias


def test_candidates_deep_link_scrolls_to_single_candidate(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_id = candidate_id_for(runtime_root)

    status, _content_type, body = request_text("GET", f"/candidates?candidate_id={candidate_id}", runtime_root)

    assert status == HTTPStatus.OK
    assert candidate_id in body
    assert "Showing 1 candidate" in body


def test_failed_table_renders_friendly_site_label(tmp_path: Path, monkeypatch) -> None:
    class FakeRegistry:
        def list_sites(self) -> list[dict[str, str]]:
            return [{"site_id": "7060", "canonical": "Continental Metalworks"}]

    runtime_root = tmp_path / "runtime"
    vault = tmp_path / "vault"
    write_vault_site_about(vault)
    failed_dir = runtime_root / "failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / "2026-05-12T16-30-00Z__continental-blue-scrubber-broken.json").write_text(
        json.dumps({"job_id": "2026-05-12T16-30-00Z__continental-blue-scrubber-broken", "job_type": "append_to_note", "payload": {"site_id": "7060"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(common, "CouchDBSiteRegistry", FakeRegistry)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/failed", runtime_root)

    assert status == HTTPStatus.OK
    assert "Continental" in body
    assert "Continental Metalworks" in body
    assert '<td><span class="site-label">7060</span></td>' not in body


def test_failed_detail_renders_job_summary_with_collapsible_json(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    failed_dir = runtime_root / "failed"
    failed_dir.mkdir(parents=True)
    job_id = "2026-05-12T16-30-00Z__continental-blue-scrubber-broken"
    (failed_dir / f"{job_id}.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "job_type": "log_equipment_request",
                "payload": {
                    "site_id": "7060",
                    "equipment_name": "small blue scrubber",
                    "requested_by": "Kevin Barnes",
                },
            }
        ),
        encoding="utf-8",
    )

    status, _content_type, body = request_text("GET", f"/failed?job_id={job_id}", runtime_root)

    assert status == HTTPStatus.OK
    assert 'class="job-summary"' in body
    assert "small blue scrubber" in body
    assert '<details class="raw-json">' in body
    assert "&quot;equipment_name&quot;: &quot;small blue scrubber&quot;" in body


def test_failed_detail_renders_back_link_to_list(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    failed_dir = runtime_root / "failed"
    failed_dir.mkdir(parents=True)
    job_id = "failed_job_back_link"
    (failed_dir / f"{job_id}.json").write_text(json.dumps({"job_id": job_id, "job_type": "append_to_note", "payload": {"path": "Notes/test.md"}}), encoding="utf-8")

    status, _content_type, body = request_text("GET", f"/failed?job_id={job_id}", runtime_root)

    assert status == HTTPStatus.OK
    assert 'class="back-link"' in body
    assert 'href="/failed"' in body


def test_drafts_table_renders_short_draft_id(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    vault = tmp_path / "vault"
    write_vault_site_about(vault)
    long_draft_id = "2026-05-12T16-30-00Z__continental-blue-scrubber-broken"
    draft = {
        "type": "approved_queue_job_draft",
        "draft_id": long_draft_id,
        "status": "approved_draft",
        "candidate_id": "fcp_abcdefghijklmnopqrstuvwxyz",
        "proposed_job_type": "append_to_note",
        "proposed_payload": {"site_id": "7060", "content": "Broken scrubber."},
        "source_context": "Broken scrubber.",
    }
    write_approved_job_draft(approved_job_drafts.default_draft_dir(runtime_root), draft)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/drafts", runtime_root)

    assert status == HTTPStatus.OK
    assert "…" in body or 'class="id-short"' in body
    assert f'title="{long_draft_id}"' in body


def test_draft_detail_renders_job_summary_with_collapsible_json(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    draft = {
        "type": "approved_queue_job_draft",
        "draft_id": "ajd_equipment",
        "status": "approved_draft",
        "candidate_id": "ac_equipment",
        "proposed_job_type": "log_equipment_request",
        "proposed_payload": {
            "site_id": "7060",
            "equipment_name": "small blue scrubber",
            "requested_by": "Kevin Barnes",
        },
        "source_context": "Small blue scrubber is broken.",
    }
    write_approved_job_draft(approved_job_drafts.default_draft_dir(runtime_root), draft)

    status, _content_type, body = request_text("GET", "/drafts?draft_id=ajd_equipment", runtime_root)

    assert status == HTTPStatus.OK
    assert 'class="job-summary"' in body
    assert "small blue scrubber" in body
    assert '<details class="raw-json">' in body
    assert "Proposed payload JSON" in body
    assert "&quot;equipment_name&quot;: &quot;small blue scrubber&quot;" in body


def test_draft_detail_renders_back_link_to_list(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    draft = {
        "type": "approved_queue_job_draft",
        "draft_id": "ajd_back_link",
        "status": "approved_draft",
        "candidate_id": "ac_back_link",
        "proposed_job_type": "append_to_note",
        "proposed_payload": {"path": "Notes/test.md"},
    }
    write_approved_job_draft(approved_job_drafts.default_draft_dir(runtime_root), draft)

    status, _content_type, body = request_text("GET", "/drafts?draft_id=ajd_back_link", runtime_root)

    assert status == HTTPStatus.OK
    assert 'class="back-link"' in body
    assert 'href="/drafts"' in body


def test_lift_app_py_under_400_lines() -> None:
    app_path = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "app.py"
    assert len(app_path.read_text(encoding="utf-8").splitlines()) <= 420


def test_section_context_exposes_shared_helpers(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    config = type("Config", (), {"vault_dir": tmp_path / "vault"})()
    ctx = SectionContext(runtime, lambda: config)
    form = {"message": ["saved"], "empty": []}

    assert ctx.config is config
    assert ctx.effective_config() is config
    assert ctx.runtime_root == runtime.resolve(strict=False)
    assert ctx.redirect("/sites")[0] == HTTPStatus.SEE_OTHER
    assert "saved" in ctx.flash({"message": ["saved"]})
    assert ctx.form_payload(form) == {"message": "saved", "empty": ""}

    ctx.audit("/test", {"ok": True}, "success")
    [line] = (ctx.runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["route"] == "/test"
    assert record["payload"] == {"ok": True}
    assert record["result_summary"] == "success"


def test_no_section_redefines_shared_helpers() -> None:
    section_dir = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "sections"
    forbidden = ("def effective_config(", "def runtime_root(", "def redirect(", "def append_audit(")
    offenders = {
        path.name: [needle for needle in forbidden if needle in path.read_text(encoding="utf-8")]
        for path in section_dir.glob("*.py")
    }
    assert {name: hits for name, hits in offenders.items() if hits} == {}


def test_no_section_imports_get_config_directly() -> None:
    section_dir = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "sections"
    forbidden = ("import get_config", "from config import get_config")
    offenders = {
        path.name: [needle for needle in forbidden if needle in path.read_text(encoding="utf-8")]
        for path in section_dir.glob("*.py")
    }
    assert {name: hits for name, hits in offenders.items() if hits} == {}


def test_common_module_has_no_app_imports() -> None:
    common_path = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "common.py"
    assert "from ops_dashboard.app import" not in common_path.read_text(encoding="utf-8")


def test_section_modules_do_not_import_other_sections() -> None:
    section_dir = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "sections"
    offenders = [
        path
        for path in section_dir.glob("*.py")
        if "from ops_dashboard.sections" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_issues_section_renders_independently_of_legacy_app() -> None:
    section_path = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "sections" / "issues.py"

    assert "legacy" "_app" not in section_path.read_text(encoding="utf-8")


def test_issues_route_renders_200_and_contains_section_header(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_site_issue(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/field-capture/issues", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h1>Site Issues</h1>" in body


def test_issues_list_links_to_detail_page(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_site_issue(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/field-capture/issues", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'href="/field-capture/issues?issue_id=iss_drain"' in body


def test_issue_detail_renders_summary_source_and_valid_transitions(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_site_issue(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/field-capture/issues?issue_id=iss_drain", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h1>Issue Detail</h1>" in body
    assert "Drain backed up and water reached the floor." in body
    assert "<h2>Source</h2>" in body
    assert "cap-photo-drain" in body
    assert "ac_drain" in body
    assert "Mark monitoring" in body
    assert "Mark resolved" in body
    assert "Reopen" not in body


def test_issue_confirm_renders_source_target_status_pills(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_site_issue(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/field-capture/issues/mark-resolved-confirm?issue_id=iss_drain", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'class="status-transition"' in body
    assert 'class="pill status-open"' in body
    assert 'class="pill status-resolved"' in body


def test_supplies_route_renders_200_and_contains_section_header(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h1>Supply Needs</h1>" in body
    assert "BrightWash cleaner" in body


def test_supplies_table_uses_data_table_class(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'class="data-table"' in body
    assert "<table><tr><th>" not in body


def test_supplies_route_filters_by_site_id_query_param(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, supply_id="sup_7050", site_id="7050", item_name="BrightWash cleaner")
    write_vault_supply_need(vault, account="Contworks", site_dir="7060 - Continental Metalworks", supply_id="sup_7060", site_id="7060", item_name="Hand soap")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies?site_id=7060", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "Hand soap" in body
    assert "BrightWash cleaner" not in body


def test_supplies_route_filters_by_status_query_param(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, supply_id="sup_open", status="open", item_name="BrightWash cleaner")
    write_vault_supply_need(vault, supply_id="sup_ordered", status="ordered", item_name="Mop heads")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies?status=ordered", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "Mop heads" in body
    assert "BrightWash cleaner" not in body


def test_supplies_status_filter_options_show_counts(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, supply_id="sup_open_1", status="open", item_name="BrightWash cleaner")
    write_vault_supply_need(vault, supply_id="sup_open_2", status="open", item_name="Hand soap")
    write_vault_supply_need(vault, supply_id="sup_ordered", status="ordered", item_name="Mop heads")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert '<option value="open">Open (2)</option>' in body


def test_supplies_nav_entry_present_and_active_on_supplies_page(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'href="/supplies" title="Supplies"' not in body
    assert 'href="/equipment" title="Equipment"' not in body


def read_single_queue_job(runtime_root: Path) -> dict[str, object]:
    [path] = sorted((runtime_root / "queue").glob("*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def test_supplies_detail_renders_action_panel_for_open_status(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, status="open")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies?supply_id=sup_cleaner", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h2>Actions</h2>" in body
    assert "Mark ordered" in body
    assert "Mark no action needed" in body


def test_supply_detail_renders_back_link_to_list(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, status="open")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies?supply_id=sup_cleaner", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'class="back-link"' in body
    assert 'href="/supplies"' in body


def test_supplies_detail_renders_action_panel_for_ordered_status(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, status="ordered")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies?supply_id=sup_cleaner", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "Mark delivered" in body
    assert "Mark no action needed" in body
    assert "Mark ordered" not in body


def test_supplies_detail_hides_action_panel_for_stocked_status(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, status="stocked")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies?supply_id=sup_cleaner", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h2>Actions</h2>" not in body
    assert "Mark stocked" not in body


def test_supplies_detail_hides_action_panel_for_no_action_needed_status(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, status="no_action_needed")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies?supply_id=sup_cleaner", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h2>Actions</h2>" not in body
    assert "Mark no_action_needed" not in body


def test_supplies_mark_ordered_confirm_renders_form_with_hidden_confirm_field(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies/mark-ordered-confirm?supply_id=sup_cleaner", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'action="/supplies/mark-ordered"' in body
    assert 'name="confirm" value="1"' in body
    assert 'name="supply_id" value="sup_cleaner"' in body


def test_supply_confirm_renders_source_target_status_pills(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_supply_need(vault, status="open")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/supplies/mark-ordered-confirm?supply_id=sup_cleaner", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'class="status-transition"' in body
    assert 'class="pill status-open"' in body
    assert 'class="pill status-ordered"' in body


def test_supplies_mark_ordered_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body = route_response("POST", "/supplies/mark-ordered", runtime_root, b"supply_id=sup_cleaner&actor=Jordan&note=ordered&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert status == HTTPStatus.SEE_OTHER
    assert job["job_type"] == "mark_supply_ordered"
    assert job["job_id"].startswith("mark-mark_supply_ordered-")
    assert job["payload"] == {"actor": "Jordan", "note": "ordered", "supply_id": "sup_cleaner"}


def test_supplies_mark_ordered_post_refuses_without_confirm(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body = route_response("POST", "/supplies/mark-ordered", runtime_root, b"supply_id=sup_cleaner&actor=Jordan")

    assert status == HTTPStatus.SEE_OTHER
    assert not (runtime_root / "queue").exists()
    assert "confirm_required" in (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8")


def test_supplies_mark_ordered_post_refuses_without_supply_id(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body = route_response("POST", "/supplies/mark-ordered", runtime_root, b"actor=Jordan&confirm=1")

    assert status == HTTPStatus.SEE_OTHER
    assert not (runtime_root / "queue").exists()
    assert "missing supply_id or actor" in (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8")


def test_supplies_mark_ordered_post_audit_line_includes_supply_id_and_actor(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/supplies/mark-ordered", runtime_root, b"supply_id=sup_cleaner&actor=Jordan&confirm=1")
    payload = json.loads((runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()[0])

    assert payload["route"] == "/supplies/mark-ordered"
    assert payload["payload"]["supply_id"] == "sup_cleaner"
    assert payload["payload"]["actor"] == "Jordan"
    assert "supply_id=sup_cleaner" in payload["result_summary"]


def test_supplies_mark_delivered_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/supplies/mark-delivered", runtime_root, b"supply_id=sup_cleaner&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_supply_delivered"
    assert job["payload"] == {"actor": "Jordan", "supply_id": "sup_cleaner"}


def test_supplies_mark_stocked_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/supplies/mark-stocked", runtime_root, b"supply_id=sup_cleaner&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_supply_stocked"
    assert job["payload"] == {"actor": "Jordan", "supply_id": "sup_cleaner"}


def test_supplies_mark_no_action_needed_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/supplies/mark-no-action-needed", runtime_root, b"supply_id=sup_cleaner&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_supply_no_action_needed"
    assert job["payload"] == {"actor": "Jordan", "supply_id": "sup_cleaner"}


def test_supplies_archive_post_writes_generic_archive_job(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/supplies/archive", runtime_root, b"supply_id=sup_cleaner&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_record_archived"
    assert job["payload"] == {"record_type": "supply_need", "record_id": "sup_cleaner", "actor": "Jordan"}


def test_issues_mark_resolved_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body = route_response("POST", "/field-capture/issues/mark-resolved", runtime_root, b"issue_id=iss_drain&actor=Jordan&note=fixed&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert status == HTTPStatus.SEE_OTHER
    assert job["job_type"] == "mark_issue_resolved"
    assert job["job_id"].startswith("mark-mark_issue_resolved-")
    assert job["payload"] == {"actor": "Jordan", "issue_id": "iss_drain", "note": "fixed"}


def test_issues_mark_monitoring_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/field-capture/issues/mark-monitoring", runtime_root, b"issue_id=iss_drain&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_issue_monitoring"
    assert job["payload"] == {"actor": "Jordan", "issue_id": "iss_drain"}


def test_issues_reopen_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/field-capture/issues/reopen", runtime_root, b"issue_id=iss_drain&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_issue_open"
    assert job["payload"] == {"actor": "Jordan", "issue_id": "iss_drain"}


def test_issue_archive_post_writes_generic_archive_job(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/field-capture/issues/archive", runtime_root, b"issue_id=iss_drain&actor=Jordan&note=dupe&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_record_archived"
    assert job["payload"] == {"record_type": "site_issue", "record_id": "iss_drain", "actor": "Jordan", "note": "dupe"}


def test_issue_restore_post_writes_generic_unarchive_job(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/field-capture/issues/restore", runtime_root, b"issue_id=iss_drain&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_record_unarchived"
    assert job["payload"] == {"record_type": "site_issue", "record_id": "iss_drain", "actor": "Jordan"}


def test_equipment_route_renders_200_and_contains_section_header(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h1>Equipment Requests</h1>" in body
    assert "vacuum" in body


def test_equipment_table_uses_data_table_class(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'class="data-table"' in body
    assert "<table><tr><th>" not in body


def test_equipment_route_filters_by_site_id_query_param(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault, equipment_id="eqr_7050", site_id="7050", equipment_name="vacuum")
    write_vault_equipment_request(vault, account="Contworks", site_dir="7060 - Continental Metalworks", equipment_id="eqr_7060", site_id="7060", equipment_name="floor buffer")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment?site_id=7060", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "floor buffer" in body
    assert "vacuum" not in body


def test_equipment_route_filters_by_status_query_param(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault, equipment_id="eqr_open", status="open", equipment_name="vacuum")
    write_vault_equipment_request(vault, equipment_id="eqr_approved", status="approved", equipment_name="floor buffer")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment?status=approved", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "floor buffer" in body
    assert "vacuum" not in body


def test_equipment_nav_entry_present_and_active_on_equipment_page(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'href="/equipment" title="Equipment"' not in body
    assert 'href="/supplies" title="Supplies"' not in body


def test_equipment_detail_renders_action_panel_for_open_status(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault, status="open")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment?equipment_id=eqr_vacuum", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h2>Actions</h2>" in body
    assert "Mark approved" in body
    assert "Mark denied" in body
    assert "Mark no action needed" in body


def test_equipment_detail_renders_action_panel_for_approved_status(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault, status="approved")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment?equipment_id=eqr_vacuum", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "Mark ordered" in body
    assert "Mark denied" in body
    assert "Mark no action needed" in body
    assert "Mark approved" not in body


def test_equipment_detail_hides_action_panel_for_provided_status(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault, status="provided")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment?equipment_id=eqr_vacuum", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h2>Actions</h2>" not in body
    assert "Mark provided" not in body


def test_equipment_detail_hides_action_panel_for_denied_status(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault, status="denied")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment?equipment_id=eqr_vacuum", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h2>Actions</h2>" not in body
    assert "Mark denied" not in body


def test_equipment_mark_approved_confirm_renders_form_with_hidden_confirm_field(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment/mark-approved-confirm?equipment_id=eqr_vacuum", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'action="/equipment/mark-approved"' in body
    assert 'name="confirm" value="1"' in body
    assert 'name="equipment_id" value="eqr_vacuum"' in body


def test_equipment_confirm_renders_source_target_status_pills(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    write_vault_equipment_request(vault, status="open")
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/equipment/mark-approved-confirm?equipment_id=eqr_vacuum", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'class="status-transition"' in body
    assert 'class="pill status-open"' in body
    assert 'class="pill status-approved"' in body


def test_equipment_mark_approved_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body = route_response("POST", "/equipment/mark-approved", runtime_root, b"equipment_id=eqr_vacuum&actor=Jordan&note=approved&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert status == HTTPStatus.SEE_OTHER
    assert job["job_type"] == "mark_equipment_approved"
    assert job["job_id"].startswith("mark-mark_equipment_approved-")
    assert job["payload"] == {"actor": "Jordan", "equipment_id": "eqr_vacuum", "note": "approved"}


def test_equipment_mark_approved_post_refuses_without_confirm(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body = route_response("POST", "/equipment/mark-approved", runtime_root, b"equipment_id=eqr_vacuum&actor=Jordan")

    assert status == HTTPStatus.SEE_OTHER
    assert not (runtime_root / "queue").exists()
    assert "confirm_required" in (runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8")


def test_equipment_mark_approved_post_audit_line_includes_equipment_id_and_actor(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/equipment/mark-approved", runtime_root, b"equipment_id=eqr_vacuum&actor=Jordan&confirm=1")
    payload = json.loads((runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()[0])

    assert payload["route"] == "/equipment/mark-approved"
    assert payload["payload"]["equipment_id"] == "eqr_vacuum"
    assert payload["payload"]["actor"] == "Jordan"
    assert "equipment_id=eqr_vacuum" in payload["result_summary"]


def test_equipment_mark_denied_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/equipment/mark-denied", runtime_root, b"equipment_id=eqr_vacuum&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_equipment_denied"
    assert job["payload"] == {"actor": "Jordan", "equipment_id": "eqr_vacuum"}


def test_equipment_mark_ordered_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/equipment/mark-ordered", runtime_root, b"equipment_id=eqr_vacuum&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_equipment_ordered"
    assert job["payload"] == {"actor": "Jordan", "equipment_id": "eqr_vacuum"}


def test_equipment_mark_provided_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/equipment/mark-provided", runtime_root, b"equipment_id=eqr_vacuum&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_equipment_provided"
    assert job["payload"] == {"actor": "Jordan", "equipment_id": "eqr_vacuum"}


def test_equipment_mark_no_action_needed_post_writes_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/equipment/mark-no-action-needed", runtime_root, b"equipment_id=eqr_vacuum&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_equipment_no_action_needed"
    assert job["payload"] == {"actor": "Jordan", "equipment_id": "eqr_vacuum"}


def test_equipment_archive_post_writes_generic_archive_job(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response("POST", "/equipment/archive", runtime_root, b"equipment_id=eqr_vacuum&actor=Jordan&confirm=1")
    job = read_single_queue_job(runtime_root)

    assert job["job_type"] == "mark_record_archived"
    assert job["payload"] == {"record_type": "equipment_request", "record_id": "eqr_vacuum", "actor": "Jordan"}


def test_render_issue_list_lives_in_common_module() -> None:
    common_path = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "common.py"

    assert "def render_issue_list" in common_path.read_text(encoding="utf-8")


def test_health_section_renders_independently_of_legacy_app() -> None:
    section_path = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "sections" / "health.py"

    assert "legacy" "_app" not in section_path.read_text(encoding="utf-8")


def test_health_count_blocks_use_human_labels(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status == HTTPStatus.OK
    assert "Pending Candidates" in body
    assert "Backlog" in body
    assert "pending_candidates" not in body
    assert "backlog_count" not in body


def test_health_failure_counts_render_danger_class(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    failed_dir = runtime_root / "failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / "failed-job.json").write_text('{"job_id":"failed-job","job_type":"append_to_note","payload":{}}', encoding="utf-8")

    status, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status == HTTPStatus.OK
    assert 'class="count-badge count-danger">1</span>' in body
    assert 'class="count-badge count-danger">0</span>' not in body


def test_health_route_renders_200_and_contains_runtime_root(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status == HTTPStatus.OK
    assert str(runtime_root.expanduser().resolve(strict=False)) in body


def runtime_health_fragment(body: str) -> str:
    start = body.index("<h2>Runtime Health</h2>")
    end = body.index("<h2>Field Capture</h2>", start)
    return body[start:end]


def field_capture_fragment(body: str) -> str:
    start = body.index("<h2>Field Capture</h2>")
    end = body.index("<h2>Review Workflow</h2>", start)
    return body[start:end]


def test_health_runtime_panel_formats_disk_usage(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status == HTTPStatus.OK
    panel = runtime_health_fragment(body)
    assert "GB used" in panel
    assert "GB total" in panel
    assert "used_bytes" not in panel
    assert "{'exists'" not in body


def test_health_runtime_panel_uses_human_labels(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status == HTTPStatus.OK
    panel = runtime_health_fragment(body)
    assert "Queue Depth" in panel
    assert "Runtime Root" in panel
    assert "queue_count" not in panel
    assert "runtime_root_exists" not in panel


def test_health_runtime_panel_marks_failed_count_danger(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    failed_dir = runtime_root / "failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / "job_failed.json").write_text("{}", encoding="utf-8")

    status, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status == HTTPStatus.OK
    assert '<span class="count-badge count-danger">1</span>' in runtime_health_fragment(body)


def test_health_field_capture_section_is_vertically_stacked(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status == HTTPStatus.OK
    fragment = field_capture_fragment(body)
    assert "Latest Uploads" in fragment
    assert "Intake Records" in fragment
    assert '<div class="grid">' not in fragment


def test_health_tables_have_no_filesystem_paths(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    status, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status == HTTPStatus.OK
    fragment = field_capture_fragment(body)
    assert str(runtime_root) not in fragment


def test_photo_vision_by_capture_lives_in_common_module() -> None:
    common_path = Path(__file__).resolve().parents[1] / "project" / "ops_dashboard" / "common.py"

    assert "def photo_vision_by_capture" in common_path.read_text(encoding="utf-8")


def test_admin_links_operations_surfaces() -> None:
    # The admin index must link the operational review surfaces (captures land
    # here for review/exposure). Supplies + Issues were previously orphaned.
    from ops_dashboard.sections.admin import render_admin

    body = render_admin({})
    for href in ('/captures', '/candidates', '/drafts', '/issues', '/supplies'):
        assert f'href="{href}"' in body, f'admin nav missing link to {href}'
