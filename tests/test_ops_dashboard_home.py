from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.app import route_response_with_headers
from ops_dashboard.sections import home
from tests.test_ops_dashboard import request_text


def home_group(body: str, group_class: str) -> str:
    start = body.index(f'<div class="home-group {group_class}">')
    next_group = body.find('<div class="home-group ', start + 1)
    end_grid = body.find("</div></div>", start)
    end = next_group if next_group != -1 else end_grid
    return body[start:end]


# Redesign (372): the home landing no longer embeds the inline console or a
# photos filter form. The "Needs you" queues are metric cards linking out, and
# the recent-photos strip is a named thumbnail strip. install_full_home now
# stubs only what the redesigned render() actually calls: the action-card
# counts (console_counts), the projector views (query_view), the employee
# picker, and the latest-photo cards.
def install_full_home(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict]]:
    view_rows: dict[str, list[dict]] = {
        "by_type": [
            {
                "key": ["opportunity", "opp_x"],
                "value": None,
                "doc": {
                    "type": "opportunity",
                    "_id": "opp_x",
                    "site_id": "42",
                    "location": "Test Site",
                    "account": "TestCo",
                },
            }
        ],
        "locations_all": [],
        "employees_by_site": [],
        "opportunities_by_site_status": [],
        "visits_by_site_date": [],
    }

    def fake_query_view(
        _base_url: str,
        _auth_headers: dict,
        _database: str,
        _ddoc: str,
        view: str,
        **_kwargs: object,
    ) -> list[dict]:
        return view_rows[view]

    monkeypatch.setattr(home._inbox_mod, "console_counts", lambda _ctx: {"review": 0, "issues": 0, "supplies": 0, "equipment": 0})
    monkeypatch.setattr(home, "query_view", fake_query_view)
    monkeypatch.setattr(home, "_voice_memo_employees_lookup", lambda: [])
    monkeypatch.setattr(home._field_photos_mod, "latest_photo_cards", lambda _runtime_root, limit: ("", False))
    return view_rows


def home_card(card_id: str, *, count: int = 0, title: str | None = None, see_all: str | None = None) -> dict:
    return {
        "id": card_id,
        "title": title if title is not None else card_id.replace("_", " ").title(),
        "count": count,
        "top": [],
        "see_all": see_all if see_all is not None else f"/inbox?card={card_id}",
        "shape": "capture",
    }


def home_card_titles(cards: list[dict]) -> list[str]:
    return [str(card["title"]) for card in cards]


def capture_observation_card(body: str) -> str:
    # QA fix (375, #4): single capture affordance — the landing shows the
    # "Capture observation" label ONCE (the header button). The full voice
    # card lives behind the "#quick-capture" <details> ("Text or voice note"),
    # rendered as the <section> wrapping the captureForm. Slice that section.
    start = body.index('<section><form id="captureForm"')
    end = body.index("</section>", start)
    return body[start:end]


def account_directory_fixture() -> dict[str, list[home._SiteRecord]]:
    return {
        "AcctA": [
            home._SiteRecord("a-2", "Alpha Two", "AcctA", "Alice", "555-0102", "a2@example.com"),
            home._SiteRecord("a-1", "Alpha One", "AcctA", "Ari", "555-0101", "a1@example.com"),
        ],
        "AcctB": [
            home._SiteRecord("b-2", "Beta Two", "AcctB", "Blair", "555-0202", "b2@example.com"),
            home._SiteRecord("b-1", "Beta One", "AcctB", "Bea", "555-0201", "b1@example.com"),
        ],
    }


def test_capture_card_renders_tape_deck_markup(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    card = capture_observation_card(body)

    assert status == HTTPStatus.OK
    assert 'id="captureRecordButton"' in card
    assert 'id="captureStopButton"' in card
    assert 'id="captureClearButton"' in card
    assert 'id="captureVoicePreview"' in card
    assert 'id="captureForm"' in card
    assert 'type="file" name="audio"' not in card


def test_capture_form_action_unchanged(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    card = capture_observation_card(body)

    assert status == HTTPStatus.OK
    assert 'action="/vault-home/voice-memo"' in card
    assert 'enctype="multipart/form-data"' in card


def test_tape_deck_banner_present_in_admin_shell(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'id="opsRecorderRetryBanner"' in body
    assert 'class="notice"' in body
    assert "border-left-color:var(--pending)" in body
    assert body.index('/static/db.js') < body.index('/static/recorder.js')


def test_capture_card_inline_init_uses_dom_content_loaded(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    # QA fix (375, #4): the capture surface is reached via the single
    # "Capture observation" header button + the quick-capture <details>; the
    # voice card is the captureForm <section> behind it. The recorder still
    # initializes on DOMContentLoaded.
    assert '<section><form id="captureForm"' in body
    assert '>Capture observation</a>' in body
    assert 'document.addEventListener("DOMContentLoaded"' in body


def test_capture_card_inline_init_calls_attach_recorder(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "window.attachRecorder(" in body
    for element_id in (
        "captureRecordButton",
        "captureStopButton",
        "captureClearButton",
        "captureVoicePreview",
        "captureVoiceStatus",
        "captureVoiceSupport",
    ):
        assert element_id in body


def test_capture_card_no_immediately_invoked_init(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    attach_call = body.index("window.attachRecorder(")
    init_tail = body[attach_call : body.index("</script>", attach_call)]

    assert status == HTTPStatus.OK
    assert "})();" not in init_tail


def test_handle_voice_memo_post_unchanged_for_multipart_audio(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_record_upload(**kwargs):
        calls.append(kwargs)
        from voice_memo.core import UploadResult

        return UploadResult(record={})

    cfg = SimpleNamespace(
        base_url="http://couchdb.invalid",
        auth_header=lambda: {},
    )
    monkeypatch.setattr(home.couchdb_config, "from_env", lambda: cfg)
    monkeypatch.setattr(home, "record_upload", fake_record_upload)

    boundary = "testboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="note"\r\n\r\n'
        "soap is low\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="site_id"\r\n\r\n'
        "42\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="voice-note.webm"\r\n'
        "Content-Type: audio/webm\r\n\r\n"
    ).encode("utf-8") + (b"audio" * 30) + f"\r\n--{boundary}--\r\n".encode("utf-8")
    content_type = f"multipart/form-data; boundary={boundary}"

    status, _response_type, _response_body, headers = route_response_with_headers(
        "POST",
        "/vault-home/voice-memo",
        tmp_path / "runtime",
        body,
        content_type,
    )

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/?message=memo+recorded"
    assert calls
    assert calls[0]["memo"].filename == "voice-note.webm"
    assert calls[0]["memo"].content_type == "audio/webm"
    assert calls[0]["site_id"] == "42"


def test_handle_voice_memo_post_passes_capture_id_to_writer(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_record_upload(**kwargs):
        calls.append(kwargs)
        from voice_memo.core import UploadResult

        return UploadResult(record={})

    cfg = SimpleNamespace(
        base_url="http://couchdb.invalid",
        auth_header=lambda: {},
    )
    monkeypatch.setattr(home.couchdb_config, "from_env", lambda: cfg)
    monkeypatch.setattr(home, "record_upload", fake_record_upload)
    monkeypatch.setattr(home, "_couchdb_doc_exists", lambda *_args: False)

    boundary = "testboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="capture_id"\r\n\r\n'
        "cap-tapedeck-1234567890-abc123xy\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="voice-note.webm"\r\n'
        "Content-Type: audio/webm\r\n\r\n"
    ).encode("utf-8") + (b"audio" * 30) + f"\r\n--{boundary}--\r\n".encode("utf-8")

    status, _response_type, _response_body, headers = route_response_with_headers(
        "POST",
        "/vault-home/voice-memo",
        tmp_path / "runtime",
        body,
        f"multipart/form-data; boundary={boundary}",
    )

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/?message=memo+recorded"
    assert calls[0]["capture_id"] == "cap-tapedeck-1234567890-abc123xy"


def test_handle_voice_memo_post_hands_home_audio_to_transcription_inbox(monkeypatch, tmp_path: Path) -> None:
    cfg = SimpleNamespace(
        base_url="http://couchdb.invalid",
        auth_header=lambda: {},
    )
    monkeypatch.setattr(home.couchdb_config, "from_env", lambda: cfg)
    monkeypatch.setattr(home, "_couchdb_doc_exists", lambda *_args: False)
    monkeypatch.setattr(home, "_make_couchdb_put", lambda _cfg: lambda _record: {})

    boundary = "testboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="capture_id"\r\n\r\n'
        "cap-tapedeck-1234567890-abc123xy\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="voice-note.webm"\r\n'
        "Content-Type: audio/webm\r\n\r\n"
    ).encode("utf-8") + (b"audio" * 30) + f"\r\n--{boundary}--\r\n".encode("utf-8")

    status, _response_type, _response_body, headers = route_response_with_headers(
        "POST",
        "/vault-home/voice-memo",
        tmp_path / "runtime",
        body,
        f"multipart/form-data; boundary={boundary}",
    )

    inbox_audio = tmp_path / "runtime" / "voice_inbox" / "vm-cap-tapedeck-1234567890-abc123xy.webm"
    sidecar = tmp_path / "runtime" / "voice_inbox" / "vm-cap-tapedeck-1234567890-abc123xy.metadata.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/?message=memo+recorded"
    assert inbox_audio.read_bytes() == b"audio" * 30
    assert payload["source"] == "voice_memo"
    assert payload["capture_id"] == "cap-tapedeck-1234567890-abc123xy"
    assert payload["routing_flag"] == "general"
    assert payload["mode"] == "operations"


def test_handle_voice_memo_post_uses_site_lookup_for_tagged_audio(monkeypatch, tmp_path: Path) -> None:
    cfg = SimpleNamespace(
        base_url="http://couchdb.invalid",
        auth_header=lambda: {},
    )
    monkeypatch.setattr(home.couchdb_config, "from_env", lambda: cfg)
    monkeypatch.setattr(home, "_couchdb_doc_exists", lambda *_args: False)
    monkeypatch.setattr(home, "_make_couchdb_put", lambda _cfg: lambda _record: {})
    monkeypatch.setattr(
        home,
        "_voice_memo_sites_lookup",
        lambda: [{"id": "7030", "account": "Wgtco", "location": "Western Gas Transmission"}],
    )

    boundary = "testboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="capture_id"\r\n\r\n'
        "cap-tapedeck-1234567890-site7030\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="site_id"\r\n\r\n'
        "7030\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="voice-note.webm"\r\n'
        "Content-Type: audio/webm\r\n\r\n"
    ).encode("utf-8") + (b"audio" * 30) + f"\r\n--{boundary}--\r\n".encode("utf-8")

    status, _response_type, _response_body, headers = route_response_with_headers(
        "POST",
        "/vault-home/voice-memo",
        tmp_path / "runtime",
        body,
        f"multipart/form-data; boundary={boundary}",
    )

    sidecar = tmp_path / "runtime" / "voice_inbox" / "vm-cap-tapedeck-1234567890-site7030.metadata.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/?message=memo+recorded"
    assert payload["routing_flag"] == "site_tagged"
    assert payload["site_id"] == "7030"
    assert payload["site_location"] == "Western Gas Transmission"


def test_capture_card_renders_employee_selector() -> None:
    html = home._render_voice_card(
        {},
        [{"id": "hutton-maria", "label": "Maria Hutton — Cleaner", "first": "Maria", "last": "Hutton", "preferred_name": "", "job": "Cleaner"}],
    )

    assert 'name="employee_slugs"' in html
    assert 'value="hutton-maria"' in html
    assert "Maria Hutton" in html


def test_handle_voice_memo_post_passes_employee_slugs_to_writer(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_record_upload(**kwargs):
        calls.append(kwargs)
        from voice_memo.core import UploadResult

        return UploadResult(record={})

    cfg = SimpleNamespace(base_url="http://couchdb.invalid", auth_header=lambda: {})
    monkeypatch.setattr(home.couchdb_config, "from_env", lambda: cfg)
    monkeypatch.setattr(home, "record_upload", fake_record_upload)
    monkeypatch.setattr(home, "_couchdb_doc_exists", lambda *_args: False)

    boundary = "testboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="employee_slugs"\r\n\r\n'
        "hutton-maria\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="voice-note.webm"\r\n'
        "Content-Type: audio/webm\r\n\r\n"
    ).encode("utf-8") + (b"audio" * 30) + f"\r\n--{boundary}--\r\n".encode("utf-8")

    status, _response_type, _response_body, headers = route_response_with_headers(
        "POST",
        "/vault-home/voice-memo",
        tmp_path / "runtime",
        body,
        f"multipart/form-data; boundary={boundary}",
    )

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/?message=memo+recorded"
    assert calls[0]["employee_slugs"] == "hutton-maria"
    assert callable(calls[0]["employees_lookup"])


def test_handle_voice_memo_post_employee_tag_sidecar(monkeypatch, tmp_path: Path) -> None:
    cfg = SimpleNamespace(base_url="http://couchdb.invalid", auth_header=lambda: {})
    monkeypatch.setattr(home.couchdb_config, "from_env", lambda: cfg)
    monkeypatch.setattr(home, "_couchdb_doc_exists", lambda *_args: False)
    monkeypatch.setattr(home, "_make_couchdb_put", lambda _cfg: lambda _record: {})
    monkeypatch.setattr(
        home,
        "_voice_memo_employees_lookup",
        lambda: [{"id": "hutton-maria", "label": "Maria Hutton — Cleaner", "first": "Maria", "last": "Hutton", "preferred_name": "", "job": "Cleaner"}],
    )

    boundary = "testboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="capture_id"\r\n\r\n'
        "cap-tapedeck-1234567890-emp001\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="employee_slugs"\r\n\r\n'
        "hutton-maria\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="voice-note.webm"\r\n'
        "Content-Type: audio/webm\r\n\r\n"
    ).encode("utf-8") + (b"audio" * 30) + f"\r\n--{boundary}--\r\n".encode("utf-8")

    route_response_with_headers("POST", "/vault-home/voice-memo", tmp_path / "runtime", body, f"multipart/form-data; boundary={boundary}")

    payload = json.loads((tmp_path / "runtime" / "voice_inbox" / "vm-cap-tapedeck-1234567890-emp001.metadata.json").read_text(encoding="utf-8"))
    assert payload["employee_slugs"] == ["hutton-maria"]
    assert payload["employee_names"] == ["Maria Hutton"]
    assert payload["routing_flag"] == "employee_tagged"


def test_handle_voice_memo_post_site_and_employee_tags_sidecar(monkeypatch, tmp_path: Path) -> None:
    cfg = SimpleNamespace(base_url="http://couchdb.invalid", auth_header=lambda: {})
    monkeypatch.setattr(home.couchdb_config, "from_env", lambda: cfg)
    monkeypatch.setattr(home, "_couchdb_doc_exists", lambda *_args: False)
    monkeypatch.setattr(home, "_make_couchdb_put", lambda _cfg: lambda _record: {})
    monkeypatch.setattr(home, "_voice_memo_sites_lookup", lambda: [{"id": "7030", "account": "Wgtco", "location": "Western Gas Transmission"}])
    monkeypatch.setattr(
        home,
        "_voice_memo_employees_lookup",
        lambda: [{"id": "hutton-maria", "label": "Maria Hutton — Cleaner", "first": "Maria", "last": "Hutton", "preferred_name": "", "job": "Cleaner"}],
    )

    boundary = "testboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="capture_id"\r\n\r\n'
        "cap-tapedeck-1234567890-both01\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="site_id"\r\n\r\n'
        "7030\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="employee_slugs"\r\n\r\n'
        "hutton-maria\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="voice-note.webm"\r\n'
        "Content-Type: audio/webm\r\n\r\n"
    ).encode("utf-8") + (b"audio" * 30) + f"\r\n--{boundary}--\r\n".encode("utf-8")

    route_response_with_headers("POST", "/vault-home/voice-memo", tmp_path / "runtime", body, f"multipart/form-data; boundary={boundary}")

    payload = json.loads((tmp_path / "runtime" / "voice_inbox" / "vm-cap-tapedeck-1234567890-both01.metadata.json").read_text(encoding="utf-8"))
    assert payload["routing_flag"] == "site_tagged"
    assert payload["site_id"] == "7030"
    assert payload["employee_slugs"] == ["hutton-maria"]
    assert payload["employee_names"] == ["Maria Hutton"]


def test_handle_voice_memo_post_idempotent_replay_redirects_without_rewrite(monkeypatch, tmp_path: Path) -> None:
    calls = []
    exists_checks = []

    cfg = SimpleNamespace(
        base_url="http://couchdb.invalid",
        auth_header=lambda: {},
    )
    monkeypatch.setattr(home.couchdb_config, "from_env", lambda: cfg)
    monkeypatch.setattr(home, "record_upload", lambda **kwargs: calls.append(kwargs))

    def fake_exists(*args):
        exists_checks.append(args)
        return True

    monkeypatch.setattr(home, "_couchdb_doc_exists", fake_exists)

    boundary = "testboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="capture_id"\r\n\r\n'
        "cap-tapedeck-1234567890-abc123xy\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="voice-note.webm"\r\n'
        "Content-Type: audio/webm\r\n\r\n"
    ).encode("utf-8") + (b"audio" * 30) + f"\r\n--{boundary}--\r\n".encode("utf-8")

    status, _response_type, _response_body, headers = route_response_with_headers(
        "POST",
        "/vault-home/voice-memo",
        tmp_path / "runtime",
        body,
        f"multipart/form-data; boundary={boundary}",
    )

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/?message=memo+recorded"
    assert not calls
    assert exists_checks == [(cfg, "btq_voice_memos", "cap-tapedeck-1234567890-abc123xy")]


def test_home_renders_needs_you_metric_row_linking_to_queues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Redesign (372): the old inline console group is replaced by the "Needs you"
    # metric-card row — four cards each LINKING to its queue (don't-lose-info).
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h2>Needs you</h2>" in body
    metric_grid_start = body.index('<div class="metric-grid"')
    metric_grid = body[metric_grid_start:body.index("</section>", metric_grid_start)]
    for stat_id, href in (
        ("pending_candidates", "/candidates?status=pending_approval"),
        ("open_site_issues", "/field-capture/issues"),
        ("open_supply_needs", "/supplies?status=open"),
        ("open_equipment_requests", "/equipment?status=open"),
    ):
        assert f'data-stat-id="{stat_id}"' in metric_grid
        assert f'href="{href}"' in metric_grid
    # Each metric card is an anchor (the whole card links to its queue).
    assert metric_grid.count('<a class="metric-card stat-badge"') == 4


def test_home_cards_all_zero_render_one_empty_strip() -> None:
    cards = [home_card(card_id) for card_id in sorted(home.HOME_CARD_IDS)]

    html = home._render_home_cards(cards)

    assert html.count('<section class="empty-strip">') == 1
    assert html.count("<section><h2>") == 0
    for title in home_card_titles(cards):
        assert f'<span class="empty-check" aria-hidden="true">✓</span> {title}' in html


def test_home_cards_mixed_renders_full_panels_plus_strip() -> None:
    cards = [
        home_card("captures_with_note", count=2),
        home_card("pending_candidates", count=5),
        home_card("open_site_issues", count=1),
        home_card("open_supply_needs"),
        home_card("open_equipment_requests"),
        home_card("unknown_captures"),
        home_card("failed_queue_jobs"),
    ]

    html = home._render_home_cards(cards)

    assert html.count("<section><h2>") == 3
    assert html.count('<section class="empty-strip">') == 1
    for title in home_card_titles(cards[:3]):
        assert f"<section><h2>{title}" in html
    for title in home_card_titles(cards[3:]):
        assert f'<span class="empty-check" aria-hidden="true">✓</span> {title}' in html


def test_home_cards_no_empty_no_strip() -> None:
    cards = [home_card(card_id, count=1) for card_id in sorted(home.HOME_CARD_IDS)]

    html = home._render_home_cards(cards)

    assert '<section class="empty-strip">' not in html
    assert html.count("<section><h2>") == len(cards)


def test_empty_strip_pills_link_to_see_all() -> None:
    cards = [
        home_card("pending_candidates", see_all="/candidates?status=pending_review"),
        home_card("open_site_issues", see_all="/field-capture/issues"),
        home_card("pipeline_health", see_all="/health/pipeline"),
    ]

    html = home._render_home_cards(cards)

    for card in cards:
        assert f'<li><a href="{card["see_all"]}">' in html


def test_empty_strip_pill_titles_escape_html() -> None:
    html = home._render_home_cards([home_card("pending_candidates", title="<script>alert(1)</script>")])

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_home_metric_row_replaces_old_stacked_triage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Redesign (372): neither the old stacked-triage group nor the inline console
    # group survives — the queues are promoted to the "Needs you" metric row.
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert 'class="metric-grid"' in body
    assert "home-group--triage" not in body
    assert "home-group--console" not in body


def test_home_renders_capture_group_around_voice_card(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    capture = home_group(body, "home-group--capture")

    assert status == HTTPStatus.OK
    assert '<div class="home-group home-group--capture">' in body
    # QA fix (375, #4): the voice card is promoted behind the quick-capture
    # <details> ("Text or voice note"); the single "Capture observation" label
    # is the header button (asserted once below — not duplicated inside the
    # capture group). The voice card is the captureForm <section>.
    assert '<summary>Text or voice note</summary>' in capture
    assert '<section><form id="captureForm"' in capture
    assert capture.count("Capture observation") == 0
    assert body.count("Capture observation") == 1


def test_home_renders_directory_group_full_width(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    directory = home_group(body, "home-group--directory")

    assert status == HTTPStatus.OK
    assert '<div class="home-group home-group--directory">' in body
    assert '<table class="account-directory">' in directory
    assert '<tr class="acct-divider"><th colspan="2" scope="rowgroup">TestCo</th></tr>' in directory


def test_home_includes_field_photos_preview_strip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Redesign (372): recent photos are now a NAMED thumbnail strip linking out
    # ("View all" -> /field-photos), inside the Recent activity operational group.
    # The old inline filter form on the landing is GONE (an explicit invariant —
    # no filter form on the landing; filtering lives on /field-photos).
    install_full_home(monkeypatch)
    monkeypatch.setattr(
        home._field_photos_mod,
        "latest_photo_cards",
        lambda _runtime_root, limit: ("<article>Photo</article>", False),
    )

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h2>Recent field photos</h2>" in body
    photos_start = body.index("<h2>Recent field photos</h2>")
    photos = body[photos_start:body.index("</section>", photos_start)]
    assert '<a href="/field-photos">View all</a>' in photos
    assert '<div class="site-gallery">' in photos
    assert "<article>Photo</article>" in photos
    # No inline photos filter form on the landing.
    assert '<form method="get" action="/field-photos"' not in body


def test_home_photos_strip_renders_unavailable_notice_when_couchdb_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Degrade-gracefully: a CouchDB-down photos read shows a muted notice, no 500.
    install_full_home(monkeypatch)
    monkeypatch.setattr(home._field_photos_mod, "latest_photo_cards", lambda _runtime_root, limit: ("", True))

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "<h2>Recent field photos</h2>" in body
    assert '<p class="muted">Photos unavailable.</p>' in body


def test_home_directory_rows_carry_data_label_attrs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    directory = home_group(body, "home-group--directory")
    row_start = directory.index("<tr>", directory.index('<tr class="acct-divider"'))
    row_end = directory.index("</tr>", row_start)
    row = directory[row_start:row_end]

    assert status == HTTPStatus.OK
    expected_labels = ["Site", "Contact"]
    for label in expected_labels:
        assert f'<td data-label="{label}"' in row
    assert [row.index(f'data-label="{label}"') for label in expected_labels] == sorted(
        row.index(f'data-label="{label}"') for label in expected_labels
    )


def test_home_directory_has_single_column_header_row() -> None:
    html = home._render_account_directory(account_directory_fixture())

    assert html.count("<thead><tr>") == 1
    assert html.count("</tr></thead>") == 1
    assert "<thead><tr><th>Site</th><th>Contact</th></tr></thead>" in html


def test_home_directory_renders_one_divider_row_per_account() -> None:
    html = home._render_account_directory(account_directory_fixture())

    assert html.count('<tr class="acct-divider">') == 2
    assert '<tr class="acct-divider"><th colspan="2" scope="rowgroup">AcctA</th></tr>' in html
    assert '<tr class="acct-divider"><th colspan="2" scope="rowgroup">AcctB</th></tr>' in html


def test_home_directory_drops_phone_and_email_columns() -> None:
    directory = _directory_group_from_table(home._render_account_directory(account_directory_fixture()))

    assert "<th>Phone</th>" not in directory
    assert "<th>Email</th>" not in directory


def test_home_directory_site_links_point_to_dynamic_detail_page() -> None:
    html = home._render_account_directory(account_directory_fixture())

    assert '<a href="/sites/a-1">Alpha One</a>' in html
    assert '<a href="/sites/b-2">Beta Two</a>' in html


def test_home_directory_group_includes_employee_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    view_rows = install_full_home(monkeypatch)
    view_rows["employees_by_site"] = [
        {
            "key": ["site-17", "employee-alice"],
            "doc": {
                "_id": "employee-alice",
                "first": "Alice",
                "last": "Zephyr",
                "phone": "8145550100",
                "email": "alice@example.com",
                "job": "site-17",
                "status": "active",
            },
        },
        {
            "key": ["site-22", "employee-ben"],
            "doc": {
                "_id": "employee-ben",
                "first": "Ben",
                "last": "Young",
                "phone": "814-555-0101",
                "email": "ben@example.com",
                "job": "site-22",
            },
        },
        {
            "key": ["site-33", "employee-ivy"],
            "doc": {
                "_id": "employee-ivy",
                "first": "Ivy",
                "last": "Xavier",
                "phone": "814-555-0102",
                "email": "ivy@example.com",
                "job": "site-33",
                "status": "inactive",
            },
        },
    ]

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    directory = home_group(body, "home-group--directory")

    assert status == HTTPStatus.OK
    # Redesign (372): the directory <summary> is sentence-case with a count badge.
    assert "<summary>Employee directory · 2</summary>" in directory
    assert "Zephyr, Alice" in directory
    assert "(814) 555-0100" in directory
    assert "alice@example.com" in directory
    assert '<a href="/sites/site-17">site-17</a>' in directory
    assert "Young, Ben" in directory
    assert "(814) 555-0101" in directory
    assert "ben@example.com" in directory
    assert '<a href="/sites/site-22">site-22</a>' in directory
    assert "Xavier, Ivy" not in directory
    assert "(814) 555-0102" not in directory
    assert "ivy@example.com" not in directory
    # NOTE: site-33 still appears in the SITE directory (unrelated to the employee
    # active-filter); only the inactive employee Ivy is excluded, asserted above.


def test_employee_directory_is_collapsible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    view_rows = install_full_home(monkeypatch)
    view_rows["employees_by_site"] = [
        {
            "key": ["site-17", "employee-alice"],
            "doc": {
                "_id": "employee-alice",
                "first": "Alice",
                "last": "Zephyr",
                "phone": "8145550100",
                "email": "alice@example.com",
                "job": "site-17",
            },
        }
    ]

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    directory = home_group(body, "home-group--directory")
    # Redesign (372): collapsibles persist their open/closed state in localStorage
    # and render closed by default (no hardcoded `open`); state is restored client
    # side from the storage key.
    details_start = directory.index('<details class="home-collapsible" id="employee-directory">')
    details_end = directory.index("</details>", details_start)
    employee_details = directory[details_start:details_end]

    assert status == HTTPStatus.OK
    assert "<summary>Employee directory · 1</summary>" in employee_details
    assert '<table class="data-table">' in employee_details
    assert "Zephyr, Alice" in employee_details
    assert "(814) 555-0100" in employee_details
    assert "btq-home-employees-collapsed" in directory


def test_site_directory_is_collapsible_like_employee_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    directory = home_group(body, "home-group--directory")
    start = directory.index('<details class="home-collapsible" id="site-directory">')
    site_details = directory[start:directory.index("</details>", start)]

    assert status == HTTPStatus.OK
    # Redesign (372): sentence-case summary with a count badge.
    assert "<summary>Site directory · 1</summary>" in site_details
    # the account/site table is now nested inside the collapsible block
    assert '<table class="account-directory">' in site_details
    assert "btq-home-sites-collapsed" in directory
    # both directories use the same collapsible mechanism
    assert directory.count('class="home-collapsible"') == 2


def test_site_directory_precedes_employee_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Both directories are now collapsible (site directory made collapsible per
    # Jordan's request); the site directory still renders first.
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    directory = home_group(body, "home-group--directory")
    site_details_start = directory.index('<details class="home-collapsible" id="site-directory">')
    employee_details_start = directory.index('<details class="home-collapsible" id="employee-directory">')
    account_table_start = directory.index('<table class="account-directory">')

    assert status == HTTPStatus.OK
    assert site_details_start < employee_details_start
    # the account/site table is nested inside the site-directory collapsible
    assert site_details_start < account_table_start < employee_details_start


def test_home_omits_inactive_location_from_site_directory_and_open_opportunities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    view_rows = install_full_home(monkeypatch)
    view_rows["by_type"] = [
        {
            "key": ["location", "location_7030"],
            "value": None,
            "doc": {
                "type": "location",
                "_id": "location_7030",
                "active": False,
                "site_id": "7030",
                "location": "Western Gas Transmission",
                "account": "Wgtco",
            },
        }
    ]
    view_rows["opportunities_by_site_status"] = [
        {"key": ["7030", "open", "opp_x"], "value": {"title": "Contract follow-up"}}
    ]

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "/sites/7030" not in body
    assert "Western Gas Transmission" not in body
    assert "Contract follow-up" not in body


def test_home_employee_directory_deduplicates_employees_by_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    view_rows = install_full_home(monkeypatch)
    employee_doc = {
        "_id": "employee-alice",
        "first": "Alice",
        "last": "Zephyr",
        "phone": "8145550100",
        "email": "alice@example.com",
        "job": "site-17",
    }
    view_rows["employees_by_site"] = [
        {"key": ["site-17", "employee-alice"], "doc": employee_doc},
        {"key": ["site-99", "employee-alice"], "doc": employee_doc},
    ]

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    directory = home_group(body, "home-group--directory")

    assert status == HTTPStatus.OK
    assert directory.count("Zephyr, Alice") == 1
    assert directory.count("(814) 555-0100") == 1


def test_employee_name_links_to_employee_detail() -> None:
    employee_id = "employee/anne & bob"
    html = home._render_employee_directory(
        [
            {
                "doc": {
                    "_id": employee_id,
                    "first": "Anne",
                    "last": "O'Neil & Sons",
                    "phone": "8145550100",
                    "email": "anne@example.com",
                    "job": "site-17",
                }
            }
        ],
        {},
    )

    assert (
        f'<a href="/employees/{home._url_path(employee_id)}">'
        "O&#x27;Neil &amp; Sons, Anne</a>"
    ) in html


def test_employee_name_without_id_renders_plain() -> None:
    html = home._render_employee_directory(
        [
            {
                "doc": {
                    "first": "Anne",
                    "last": "O'Neil & Sons",
                    "phone": "8145550100",
                    "email": "anne@example.com",
                    "job": "site-17",
                }
            }
        ],
        {},
    )

    assert "O&#x27;Neil &amp; Sons, Anne" in html
    assert "/vault/entity/employee/" not in html


def test_home_employee_directory_empty_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    directory = home_group(body, "home-group--directory")

    assert status == HTTPStatus.OK
    assert "<summary>Employee directory · 0</summary>" in directory
    assert '<p class="zero-state">No employee records.</p>' in directory


def _directory_group_from_table(table_html: str) -> str:
    return f'<div class="home-group home-group--directory">{table_html}</div>'


def test_home_renders_operational_group_for_grouped_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert '<div class="home-group home-group--operational">' in body
    # Redesign (372): the two-up "needs attention" panel and the recent-activity
    # panel are both operational groups with sentence-case, named, counted lists.
    # First operational group: the "needs attention" two-up.
    first_op = home_group(body, "home-group--operational")
    assert "No visit in 30 days &middot;" in first_op
    assert "Open opportunities &middot;" in first_op
    # The recent-activity operational group carries recent field photos + visits.
    assert "<h2>Recent field photos</h2>" in body
    assert "Recent visits &middot;" in body


def test_home_grid_shell_wraps_all_groups(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    grid_start = body.index('<div class="home-grid">')
    grid = body[grid_start:body.index("</main>", grid_start)]

    assert status == HTTPStatus.OK
    assert body.count('<div class="home-grid">') == 1
    # Redesign (372): the grid shell wraps the redesigned groups — the capture
    # surface, the operational "needs attention" + "recent activity" groups, and
    # the directory group. (Console/photos groups were folded into the metric row
    # and the operational recent-activity group respectively.)
    for group_class in (
        "home-group--capture",
        "home-group--directory",
        "home-group--operational",
    ):
        assert group_class in grid
    # The promoted "Needs you" metric row also lives inside the grid.
    assert 'class="metric-grid"' in grid


def test_home_grid_orders_metric_row_before_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Redesign (372): the old home-main / home-rail two-column split is gone — the
    # page is a single home-grid. The "Needs you" metric row leads; the directory
    # (full-width collapsibles) comes after. No leftover rail wrapper.
    install_full_home(monkeypatch)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")
    grid_start = body.index('<div class="home-grid">')
    grid = body[grid_start:body.index("</main>", grid_start)]

    metric_start = grid.index('class="metric-grid"')
    directory_start = grid.index("home-group--directory")

    assert status == HTTPStatus.OK
    assert metric_start < directory_start
    assert "home-main" not in body
    assert "home-rail" not in body


def test_admin_css_defines_home_grid(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/static/admin.css", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert ".home-grid" in body
    assert "@media (max-width: 900px)" in body


def test_admin_css_defines_600px_breakpoint(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/static/admin.css", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "@media (max-width: 600px)" in body


def test_admin_css_600px_block_includes_directory_transform(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/static/admin.css", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert ".home-group--directory tbody td::before" in body
    assert 'content: attr(data-label) ": ";' in body


def test_admin_css_600px_block_includes_capture_full_width(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/static/admin.css", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert ".home-group--capture select" in body
    assert ".home-group--capture textarea" in body
    assert 'width: 100%' in body
