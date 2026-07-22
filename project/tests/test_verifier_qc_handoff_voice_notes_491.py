"""Independent verifier gate for 491 — QC voice-note transcripts on the handoff board.

Authored by the verifier, not the implementer.  Every fixture is synthetic: no real
employee, client, or site name appears here, because real transcripts name real
people.  The tests drive the real render and join paths with CouchDB, the site
registry, and runtime discovery replaced by local stubs, so nothing operational is
read or mutated.

Coverage maps one-to-one onto the acceptance criteria in prompt 491, plus the
cross-cutting invariants it inherits: the 490 QC lane, the 489 local-date
semantics, the 487 Safari drag contract, and the route's read-only guarantee.
"""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path

import pytest

from field_capture.display_categories import BUILTIN_FALLBACK_CATEGORIES, QC_CAPTURE_CATEGORY
from ops_dashboard.sections import field_photos
from tests.test_qc_handoff_488 import _ctx, _sidecar


PANEL_MARKER = "qc-handoff-voice-notes"
CARD_MARKER = "qc-handoff-voice-note"


@pytest.fixture(autouse=True)
def _synthetic_site_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        field_photos,
        "resolve_site_label",
        lambda site_id, _vault_root=None: str(site_id),
    )


def _transcript_dir(tmp_path: Path) -> Path:
    return tmp_path / "runtime" / "field_capture" / "audio_transcripts"


def _write_transcript(
    tmp_path: Path,
    asset_id: str,
    *,
    upload_id: str,
    raw_text: str = "Synthetic dictation for the verifier gate.",
    status: str = "complete",
    error: object = None,
    doc_type: str = "field_audio_transcript",
    person_id: str = "person-synthetic-01",
    area: str = "qc",
) -> Path:
    directory = _transcript_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "type": doc_type,
        "upload_id": upload_id,
        "raw_text": raw_text,
        "status": status,
        "area": area,
        "site_id": "site-one",
        "person_id": person_id,
        "audio_asset_id": asset_id,
        "created_at": "2026-07-15T12:00:00Z",
    }
    if error is not None:
        payload["error"] = error
    path = directory / f"{asset_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _board(
    tmp_path: Path,
    sidecars: list[dict[str, object]],
    *,
    submitters: dict[str, dict[str, str]] | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: submitters or {})
    return field_photos._render_handoff_board(
        _ctx(tmp_path),
        sidecars,
        "site-one",
        "2026-07-15",
        False,
        False,
        BUILTIN_FALLBACK_CATEGORIES,
    )


def _form_subtree(rendered: str) -> str:
    start = rendered.index('<form method="post" action="/field-photos/export"')
    end = rendered.index("</form>", start) + len("</form>")
    return rendered[start:end]


# --------------------------------------------------------------------------
# Acceptance: the happy path
# --------------------------------------------------------------------------


def test_transcribed_capture_renders_one_card_with_text_time_name_and_photo_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_transcript(
        tmp_path,
        "audio-alpha",
        upload_id="walk-alpha",
        raw_text="Mop closet needs restocking on the second floor.",
    )
    sidecars = [
        _sidecar("alpha-one", "Restrooms", capture_id="walk-alpha", minute=7),
        _sidecar("alpha-two", "Hallways", capture_id="walk-alpha", minute=7),
    ]
    rendered = _board(
        tmp_path,
        sidecars,
        submitters={"walk-alpha": {"submitter_name": "Robin Synthetic", "submitter_id": "person-synthetic-01"}},
        monkeypatch=monkeypatch,
    )

    assert rendered.count(f'<section class="{PANEL_MARKER}"') == 1
    assert rendered.count(f'<article class="{CARD_MARKER}">') == 1
    assert "Mop closet needs restocking on the second floor." in rendered
    # Local time, first name only, and the count of THIS capture's photos on the board.
    assert "12:07 · Robin · 2 photos" in rendered
    assert "Synthetic" not in rendered.split(PANEL_MARKER, 1)[1].split("</section>", 1)[0]
    assert "1 capture with a voice note" in rendered


def test_single_photo_capture_uses_singular_photo_wording(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_transcript(tmp_path, "audio-solo", upload_id="walk-solo")
    rendered = _board(
        tmp_path,
        [_sidecar("solo-one", "Restrooms", capture_id="walk-solo", minute=3)],
        submitters={"walk-solo": {"submitter_name": "Robin Synthetic"}},
        monkeypatch=monkeypatch,
    )

    assert "12:03 · Robin · 1 photo" in rendered
    assert "1 photos" not in rendered


def test_two_transcribed_captures_render_two_cards_ordered_by_capture_clock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_transcript(tmp_path, "audio-late", upload_id="walk-late", raw_text="Later note.")
    _write_transcript(tmp_path, "audio-early", upload_id="walk-early", raw_text="Earlier note.")
    rendered = _board(
        tmp_path,
        [
            # Deliberately out of clock order in board order.
            _sidecar("late-one", "Restrooms", capture_id="walk-late", minute=40),
            _sidecar("early-one", "Restrooms", capture_id="walk-early", minute=5),
        ],
        monkeypatch=monkeypatch,
    )

    assert rendered.count(f'<article class="{CARD_MARKER}">') == 2
    assert rendered.index("Earlier note.") < rendered.index("Later note.")
    assert "2 captures with a voice note" in rendered
    # Each card counts only ITS OWN capture's photos, not the board total.
    assert "12:05 · 1 photo" in rendered
    assert "12:40 · 1 photo" in rendered
    assert "2 photos" not in rendered.split('<form method="post"', 1)[0]


def test_photo_count_reflects_deduplicated_board_cards_not_raw_loader_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The join must ride the rendered (post-dedup) set, so the count stays honest.

    ``group_handoff_sidecars`` collapses repeated rows by ``photo_asset_id``.  A
    count taken from the raw loader list would over-report ("3 photos" for one
    visible card) and would also mean the join reads a set the board never
    rendered — the 490 lane invariant is structural only if it reads the board.
    """
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Deduped note.")
    duplicated = _sidecar("repeat-one", "Restrooms", capture_id="walk-alpha", minute=6)
    unique = _sidecar("repeat-two", "Restrooms", capture_id="walk-alpha", minute=6)
    rendered = _board(
        tmp_path,
        [duplicated, dict(duplicated), dict(duplicated), unique],
        monkeypatch=monkeypatch,
    )

    assert rendered.count(f'<article class="{CARD_MARKER}">') == 1
    assert "2 photos on this QC day" in rendered
    assert "12:06 · 2 photos" in rendered
    assert "4 photos" not in rendered
    assert "3 photos" not in rendered


# --------------------------------------------------------------------------
# Acceptance: the quiet common case and omission rules
# --------------------------------------------------------------------------


def test_day_without_any_transcript_renders_no_panel_markup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _transcript_dir(tmp_path).mkdir(parents=True)
    rendered = _board(
        tmp_path,
        [_sidecar("only-one", "Restrooms", capture_id="walk-quiet")],
        monkeypatch=monkeypatch,
    )

    # Assert the absence of the markup itself, not merely of transcript text.
    assert PANEL_MARKER not in rendered
    assert CARD_MARKER not in rendered
    assert "Voice notes" not in rendered
    assert "with a voice note" not in rendered
    # The board itself still renders.
    assert "1 photo on this QC day" in rendered


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("pending status", {"status": "pending"}),
        ("failed status", {"status": "failed"}),
        ("blank status", {"status": ""}),
        ("empty raw_text", {"raw_text": ""}),
        ("whitespace raw_text", {"raw_text": "   \n  "}),
        ("populated error", {"error": "whisper subprocess exited 1"}),
        ("wrong artifact type", {"doc_type": "personal_voice_memo"}),
    ],
)
def test_unusable_transcript_artifacts_are_omitted_entirely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    label: str,
    kwargs: dict[str, object],
) -> None:
    sentinel = "SENTINEL-SHOULD-NOT-RENDER"
    payload_kwargs: dict[str, object] = {"raw_text": sentinel}
    payload_kwargs.update(kwargs)
    _write_transcript(tmp_path, "audio-bad", upload_id="walk-alpha", **payload_kwargs)

    rendered = _board(
        tmp_path,
        [_sidecar("alpha-one", "Restrooms", capture_id="walk-alpha")],
        monkeypatch=monkeypatch,
    )

    assert PANEL_MARKER not in rendered, label
    assert sentinel not in rendered, label
    # Pipeline internals must never reach the operator.
    assert "whisper" not in rendered.lower(), label
    assert "subprocess" not in rendered.lower(), label


def test_transcript_matching_no_board_capture_produces_no_orphan_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_transcript(
        tmp_path,
        "audio-orphan",
        upload_id="walk-not-on-this-board",
        raw_text="Orphan transcript text.",
    )
    rendered = _board(
        tmp_path,
        [_sidecar("alpha-one", "Restrooms", capture_id="walk-alpha")],
        monkeypatch=monkeypatch,
    )

    assert PANEL_MARKER not in rendered
    assert "Orphan transcript text." not in rendered
    assert "walk-not-on-this-board" not in rendered


def test_transcript_for_another_day_capture_does_not_reach_the_selected_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The board is already day-filtered; the join must not reintroduce other days."""
    _write_transcript(
        tmp_path,
        "audio-yesterday",
        upload_id="walk-yesterday",
        raw_text="Yesterday note that must not appear.",
    )
    _write_transcript(tmp_path, "audio-today", upload_id="walk-today", raw_text="Today note.")
    other_day = _sidecar("yesterday-one", "Restrooms", capture_id="walk-yesterday")
    other_day["generated_at"] = "2026-07-14T12:00:00Z"
    other_day["provenance"] = {
        "captured_at": "2026-07-14T12:00:00Z",
        "image_media_url": "/media/yesterday-one.jpg",
    }

    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    monkeypatch.setattr(
        field_photos,
        "load_filtered_photo_sidecars",
        lambda _ctx, **_filters: (
            [_sidecar("today-one", "Restrooms", capture_id="walk-today"), other_day],
            False,
            False,
        ),
    )
    rendered = field_photos.render(
        _ctx(tmp_path, {"site_id": ["site-one"], "qc_date": ["2026-07-15"]})
    )

    assert "Today note." in rendered
    assert "Yesterday note that must not appear." not in rendered
    assert rendered.count(f'<article class="{CARD_MARKER}">') == 1


# --------------------------------------------------------------------------
# Acceptance: the 490 QC-lane invariant
# --------------------------------------------------------------------------


def test_non_qc_capture_transcript_never_appears_on_the_qc_board(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Proves the join rides the QC-filtered set and cannot widen the lane."""
    _write_transcript(tmp_path, "audio-qc", upload_id="qc-walk", raw_text="QC lane note.")
    _write_transcript(
        tmp_path,
        "audio-baseline",
        upload_id="baseline-walk-sentinel",
        raw_text="Baseline lane note that must never surface.",
        area="baseline",
    )
    docs = [
        _sidecar("qc-one", "Restrooms", capture_id="qc-walk", minute=1),
        _sidecar(
            "baseline-one",
            "Restrooms",
            qc_category="baseline",
            capture_id="baseline-walk-sentinel",
            minute=2,
        ),
    ]
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})

    def load(_ctx: object, **filters: object) -> tuple[list[dict[str, object]], bool, bool]:
        calls.append(filters)
        selected = docs
        if filters.get("qc_category"):
            selected = [row for row in docs if row["qc_category"] == filters["qc_category"]]
        return selected, False, False

    monkeypatch.setattr(field_photos, "load_filtered_photo_sidecars", load)

    board = field_photos.render(
        _ctx(tmp_path, {"site_id": ["site-one"], "qc_date": ["2026-07-15"]})
    )
    discovery = field_photos.render(_ctx(tmp_path, {"site_id": ["site-one"]}))

    assert calls == [
        {"site_id": "site-one", "qc_category": QC_CAPTURE_CATEGORY},
        {"site_id": "site-one", "qc_category": QC_CAPTURE_CATEGORY},
    ]
    assert "QC lane note." in board
    for leaked in ("Baseline lane note that must never surface.", "baseline-walk-sentinel", "baseline-one"):
        assert leaked not in board, leaked
        assert leaked not in discovery, leaked
    # The transcript did not add a capture, a photo, or a section.
    assert "1 photo on this QC day" in board
    assert "1 photo · 1 contributing capture" in discovery
    assert board.count(f'<article class="{CARD_MARKER}">') == 1


def test_counts_sections_discovery_and_export_are_unchanged_by_transcripts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Baseline the whole board without transcripts, then diff against with-transcripts."""
    sidecars = [
        _sidecar("one", "Restrooms", capture_id="walk-alpha", minute=1),
        _sidecar("two", "Hallways", capture_id="walk-alpha", minute=2),
        _sidecar("three", "Imaginary Wing", capture_id="walk-beta", minute=3),
    ]
    baseline = _board(tmp_path, sidecars, monkeypatch=monkeypatch)

    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Note A.")
    _write_transcript(tmp_path, "audio-b", upload_id="walk-beta", raw_text="Note B.")
    with_notes = _board(tmp_path, sidecars, monkeypatch=monkeypatch)

    assert PANEL_MARKER not in baseline
    assert with_notes.count(f'<article class="{CARD_MARKER}">') == 2
    # Everything downstream of the panel — toolbar count, sections, cards, export
    # controls, drag markup, filenames — is untouched.
    assert _form_subtree(baseline) == _form_subtree(with_notes)
    assert "3 photos on this QC day" in baseline and "3 photos on this QC day" in with_notes
    assert baseline.count("data-photo-file-drag") == with_notes.count("data-photo-file-drag")
    assert baseline.count("qc-handoff-category") == with_notes.count("qc-handoff-category")

    # Date discovery is likewise transcript-blind.
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    monkeypatch.setattr(
        field_photos,
        "load_filtered_photo_sidecars",
        lambda _ctx, **_filters: (sidecars, False, False),
    )
    discovery = field_photos.render(_ctx(tmp_path, {"site_id": ["site-one"]}))
    assert PANEL_MARKER not in discovery
    assert "Note A." not in discovery and "Note B." not in discovery
    assert "3 photos · 2 contributing captures" in discovery


# --------------------------------------------------------------------------
# Acceptance: 487 drag contract + export payload isolation
# --------------------------------------------------------------------------


def test_rendered_drag_markup_is_byte_identical_with_and_without_transcripts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sidecars = [_sidecar("drag-one", "Restrooms", capture_id="walk-alpha")]
    baseline = _board(tmp_path, sidecars, monkeypatch=monkeypatch)
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Drag note.")
    with_notes = _board(tmp_path, sidecars, monkeypatch=monkeypatch)

    def drag_fragments(rendered: str) -> list[str]:
        return [
            chunk[: chunk.index(">") + 1]
            for chunk in rendered.split("data-photo-file-drag")[1:]
        ]

    assert drag_fragments(baseline) == drag_fragments(with_notes)
    assert drag_fragments(baseline)
    # The drag initializer script itself is untouched and still emitted once.
    script = field_photos.render_photo_file_drag_script()
    assert baseline.count(script) == with_notes.count(script) == 1


def test_voice_note_panel_sits_outside_the_export_form_and_cannot_enter_its_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sentinel = "Transcript text that must never be posted."
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text=sentinel)
    rendered = _board(
        tmp_path,
        [_sidecar("one", "Restrooms", capture_id="walk-alpha")],
        monkeypatch=monkeypatch,
    )

    panel_start = rendered.index(f'<section class="{PANEL_MARKER}"')
    form_start = rendered.index('<form method="post" action="/field-photos/export"')
    assert panel_start < form_start, "panel must precede the export form"

    form = _form_subtree(rendered)
    assert sentinel not in form
    assert PANEL_MARKER not in form
    assert CARD_MARKER not in form
    # No form control of any kind lives inside the panel, so nothing it renders can
    # be serialized into the POST body or a drag payload.
    panel = rendered[panel_start : rendered.index("</section>", panel_start)]
    for control in ("<input", "<button", "<select", "<textarea", "name=", "data-photo-file-drag"):
        assert control not in panel, control


# --------------------------------------------------------------------------
# Acceptance: escaping
# --------------------------------------------------------------------------


def test_transcript_html_metacharacters_are_escaped_not_interpreted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hostile = '<script>alert("x")</script> & <img src=q onerror=go> \'quote\''
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text=hostile)
    rendered = _board(
        tmp_path,
        [_sidecar("one", "Restrooms", capture_id="walk-alpha")],
        submitters={"walk-alpha": {"submitter_name": "<b>Robin</b> Synthetic"}},
        monkeypatch=monkeypatch,
    )

    assert "<script>alert" not in rendered
    assert "<img src=q onerror=go>" not in rendered
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in rendered
    assert "&amp;" in rendered
    # The author name is operator-adjacent data and is escaped on the same line.
    assert "<b>Robin</b>" not in rendered
    assert "&lt;b&gt;Robin&lt;/b&gt;" in rendered


# --------------------------------------------------------------------------
# Acceptance: fail-soft
# --------------------------------------------------------------------------


def test_missing_transcript_directory_renders_the_board_normally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert not _transcript_dir(tmp_path).exists()
    rendered = _board(
        tmp_path,
        [_sidecar("one", "Restrooms", capture_id="walk-alpha")],
        monkeypatch=monkeypatch,
    )

    assert PANEL_MARKER not in rendered
    assert "1 photo on this QC day" in rendered
    assert 'method="post" action="/field-photos/export"' in rendered
    assert field_photos.load_handoff_voice_transcripts(tmp_path / "runtime") == {}


def test_transcript_path_occupied_by_a_file_degrades_to_no_voice_notes(tmp_path: Path) -> None:
    parent = tmp_path / "runtime" / "field_capture"
    parent.mkdir(parents=True)
    (parent / "audio_transcripts").write_text("not a directory", encoding="utf-8")

    assert field_photos.load_handoff_voice_transcripts(tmp_path / "runtime") == {}


def test_malformed_and_unreadable_transcript_files_never_break_the_board(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = _transcript_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "truncated.json").write_text('{"type": "field_audio_transcript", "raw', encoding="utf-8")
    (directory / "not-an-object.json").write_text('["a list, not an object"]', encoding="utf-8")
    (directory / "empty.json").write_text("", encoding="utf-8")
    (directory / "stray.txt").write_text("ignored, not JSON", encoding="utf-8")
    (directory / "a-directory.json").mkdir()
    _write_transcript(tmp_path, "audio-good", upload_id="walk-alpha", raw_text="Good note survives.")

    rendered = _board(
        tmp_path,
        [_sidecar("one", "Restrooms", capture_id="walk-alpha")],
        monkeypatch=monkeypatch,
    )

    assert "Good note survives." in rendered
    assert "1 photo on this QC day" in rendered
    assert "truncated" not in rendered and "not-an-object" not in rendered


def test_non_utf8_transcript_artifact_must_not_break_the_board(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fail-soft on an *unreadable* artifact, not just a malformed-JSON one.

    ``read_json_artifact`` (ops_dashboard/common.py:248) catches only
    ``(OSError, json.JSONDecodeError)``.  A non-UTF-8 byte in any ``*.json`` file
    under ``audio_transcripts`` raises ``UnicodeDecodeError`` — a ``ValueError``
    that is neither — straight out of ``load_handoff_voice_transcripts`` and out
    of ``_render_handoff_board``, 500-ing the whole QC Handoff board.  The
    transcription pipeline writes into this directory while the dashboard reads
    it, so a half-flushed multibyte sequence is reachable in production.
    """
    directory = _transcript_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "non-utf8.json").write_bytes(b'\xff\xfe{"type": "field_audio_transcript"}')
    _write_transcript(tmp_path, "audio-good", upload_id="walk-alpha", raw_text="Good note survives.")

    assert field_photos.load_handoff_voice_transcripts(tmp_path / "runtime") == {
        "walk-alpha": ["Good note survives."]
    }
    rendered = _board(
        tmp_path,
        [_sidecar("one", "Restrooms", capture_id="walk-alpha")],
        monkeypatch=monkeypatch,
    )
    assert "Good note survives." in rendered
    assert "1 photo on this QC day" in rendered


def test_unparseable_capture_timestamp_still_renders_the_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Bad clock note.")
    sidecar = _sidecar("one", "Restrooms", capture_id="walk-alpha")
    sidecar["provenance"] = {"captured_at": "not-a-timestamp", "image_media_url": "/media/one.jpg"}
    sidecar["generated_at"] = "not-a-timestamp"

    rendered = _board(tmp_path, [sidecar], monkeypatch=monkeypatch)

    assert "Bad clock note." in rendered
    assert "1 photo" in rendered
    assert "not-a-timestamp" not in rendered
    assert field_photos._handoff_local_capture_time(sidecar) == ""


# --------------------------------------------------------------------------
# Cross-cutting: 489 local-date semantics, read-only, efficiency
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("captured_at", "expected_date", "expected_time"),
    [
        ("2026-07-15T12:07:00Z", "2026-07-15", "12:07"),
        # The client's own offset already describes the operator's clock; converting
        # zones would move both the day and the time (the 489 invariant).
        ("2026-07-15T23:30:00-04:00", "2026-07-15", "23:30"),
        ("2026-07-16T01:15:00+09:00", "2026-07-16", "01:15"),
    ],
)
def test_capture_time_is_zone_preserving_and_agrees_with_the_local_date(
    captured_at: str,
    expected_date: str,
    expected_time: str,
) -> None:
    sidecar = _sidecar("one", "Restrooms")
    sidecar["provenance"] = {"captured_at": captured_at, "image_media_url": "/media/one.jpg"}

    assert field_photos._handoff_local_capture_date(sidecar) == expected_date
    assert field_photos._handoff_local_capture_time(sidecar) == expected_time


def test_rendering_with_transcripts_writes_nothing_to_the_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Read-only note.")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    stats_before = {
        path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()
    }

    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    monkeypatch.setattr(
        field_photos,
        "load_filtered_photo_sidecars",
        lambda _ctx, **_filters: ([_sidecar("one", "Restrooms", capture_id="walk-alpha")], False, False),
    )
    rendered = field_photos.render(
        _ctx(tmp_path, {"site_id": ["site-one"], "qc_date": ["2026-07-15"]})
    )

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    stats_after = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}

    assert "Read-only note." in rendered
    assert before == after
    assert stats_before == stats_after


def test_transcript_directory_is_read_once_per_render_not_once_per_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for index in range(3):
        _write_transcript(
            tmp_path,
            f"audio-{index}",
            upload_id=f"walk-{index}",
            raw_text=f"Note {index}.",
        )
    sidecars = [
        _sidecar(f"photo-{index}-{shot}", "Restrooms", capture_id=f"walk-{index}", minute=index)
        for index in range(3)
        for shot in range(4)
    ]

    reads: list[Path] = []
    real_reader = field_photos.read_json_artifact

    def counting_reader(path: Path) -> tuple[dict[str, object] | None, str]:
        reads.append(path)
        return real_reader(path)

    monkeypatch.setattr(field_photos, "read_json_artifact", counting_reader)

    loads: list[object] = []
    real_loader = field_photos.load_handoff_voice_transcripts

    def counting_loader(runtime_root: Path) -> dict[str, list[str]]:
        loads.append(runtime_root)
        return real_loader(runtime_root)

    monkeypatch.setattr(field_photos, "load_handoff_voice_transcripts", counting_loader)

    rendered = _board(tmp_path, sidecars, monkeypatch=monkeypatch)

    assert rendered.count(f'<article class="{CARD_MARKER}">') == 3
    assert len(loads) == 1, "the transcript directory must be scanned once per render"
    # One read per artifact on disk — not one per sidecar (12) or per card (3 x 3).
    assert len(reads) == 3
    assert len(set(reads)) == 3


def test_multiple_transcripts_for_one_capture_stay_in_a_single_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_transcript(tmp_path, "audio-1", upload_id="walk-alpha", raw_text="First dictation.")
    _write_transcript(tmp_path, "audio-2", upload_id="walk-alpha", raw_text="Second dictation.")
    rendered = _board(
        tmp_path,
        [_sidecar("one", "Restrooms", capture_id="walk-alpha")],
        monkeypatch=monkeypatch,
    )

    assert rendered.count(f'<article class="{CARD_MARKER}">') == 1
    assert "First dictation." in rendered and "Second dictation." in rendered
    assert "1 capture with a voice note" in rendered


def test_capture_without_a_submitter_entry_still_renders_a_usable_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No submitter metadata must not swallow the note or emit a blank separator."""
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Nameless note.")
    rendered = _board(
        tmp_path,
        [_sidecar("one", "Restrooms", capture_id="walk-alpha", minute=9)],
        submitters={},
        monkeypatch=monkeypatch,
    )

    assert "Nameless note." in rendered
    assert "12:09 · 1 photo" in rendered
    assert " ·  · " not in rendered
    assert "· ·" not in rendered


# --------------------------------------------------------------------------
# Change 1 — the exception clause in the read loop must stay narrow.
#
# ``read_json_artifact`` already absorbs OSError and JSONDecodeError internally,
# so ``UnicodeDecodeError`` (a ValueError subclass) is the only failure that can
# still escape it.  Catching bare ``ValueError`` there would silently skip an
# artifact whenever a genuine future regression in this loop raised one.  The
# clause must therefore swallow UnicodeDecodeError and nothing broader.
# --------------------------------------------------------------------------


def _raising_reader(exc: BaseException, only: str | None = None):
    """Reader stub that raises for one artifact (or all) and reads the rest for real."""
    real = field_photos.read_json_artifact

    def reader(path: Path) -> tuple[dict[str, object] | None, str]:
        if only is None or path.name == only:
            raise exc
        return real(path)

    return reader


@pytest.mark.parametrize(
    ("label", "exc"),
    [
        ("plain ValueError", ValueError("synthetic regression in the transcript loop")),
        ("JSONDecodeError", json.JSONDecodeError("synthetic", "{}", 0)),
        ("TypeError", TypeError("synthetic regression in the transcript loop")),
        ("KeyError", KeyError("synthetic")),
    ],
)
def test_non_unicode_read_failures_propagate_and_are_not_masked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    label: str,
    exc: BaseException,
) -> None:
    """Anti-masking: the loop must not swallow anything but UnicodeDecodeError.

    ``JSONDecodeError`` is included deliberately — it is a ``ValueError`` subclass,
    so a widened ``except ValueError`` would catch it here and hide the fact that
    the shared reader stopped absorbing it.
    """
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Note.")
    monkeypatch.setattr(field_photos, "read_json_artifact", _raising_reader(exc))

    with pytest.raises(type(exc)):
        field_photos.load_handoff_voice_transcripts(tmp_path / "runtime")


def test_unicode_decode_error_drops_only_the_offending_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The narrow clause is per-artifact: one bad file must not cost the others."""
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Note A.")
    _write_transcript(tmp_path, "audio-b", upload_id="walk-beta", raw_text="Note B.")
    _write_transcript(tmp_path, "audio-c", upload_id="walk-gamma", raw_text="Note C.")
    monkeypatch.setattr(
        field_photos,
        "read_json_artifact",
        _raising_reader(
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
            only="audio-b.json",
        ),
    )

    loaded = field_photos.load_handoff_voice_transcripts(tmp_path / "runtime")

    assert loaded == {"walk-alpha": ["Note A."], "walk-gamma": ["Note C."]}
    assert "walk-beta" not in loaded


def test_read_loop_exception_clause_is_narrow_by_construction() -> None:
    """A widened clause is a source-level regression; gate it at the source."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(field_photos.load_handoff_voice_transcripts))
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    caught: set[str] = set()
    for handler in handlers:
        node = handler.type
        names = node.elts if isinstance(node, ast.Tuple) else [node]
        caught.update(name.id for name in names if isinstance(name, ast.Name))

    assert "ValueError" not in caught, "bare ValueError would mask genuine regressions"
    assert "Exception" not in caught and "BaseException" not in caught
    assert "UnicodeDecodeError" in caught


# --------------------------------------------------------------------------
# Change 2 — fail-soft boundary around the voice-note chain.
#
# Prompt 491 requires transcript trouble to degrade to "no voice notes" and never
# break the board.  Each of the four functions in the chain is forced to raise in
# turn; the board must survive intact, the panel must vanish completely, and the
# failure must be logged with enough context to debug.
# --------------------------------------------------------------------------


VOICE_NOTE_CHAIN = [
    "_handoff_transcript_dir",
    "load_handoff_voice_transcripts",
    "_handoff_voice_notes",
    "_render_handoff_voice_notes",
]


@pytest.mark.parametrize("failing_function", VOICE_NOTE_CHAIN)
def test_any_failure_in_the_voice_note_chain_still_renders_the_whole_board(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    failing_function: str,
) -> None:
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Unreachable note.")
    marker = f"synthetic {failing_function} failure"

    def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(marker)

    monkeypatch.setattr(field_photos, failing_function, explode)

    with caplog.at_level(logging.WARNING, logger=field_photos.__name__):
        rendered = _board(
            tmp_path,
            [
                _sidecar("one", "Restrooms", capture_id="walk-alpha", minute=1),
                _sidecar("two", "Hallways", capture_id="walk-alpha", minute=2),
            ],
            monkeypatch=monkeypatch,
        )

    # The load-bearing surface survives: photos, sections, toolbar, export, drag.
    assert "2 photos on this QC day" in rendered
    assert 'method="post" action="/field-photos/export"' in rendered
    assert "one.jpg" in rendered and "two.jpg" in rendered
    assert 'data-download-photo-category="Restrooms"' in rendered
    assert "data-photo-file-drag" in rendered
    assert "data-download-all-photos" in rendered

    # The panel is absent in full — no heading, no card, no orphaned fragment.
    assert PANEL_MARKER not in rendered
    assert CARD_MARKER not in rendered
    assert "Voice notes" not in rendered
    assert "with a voice note" not in rendered
    assert "Unreachable note." not in rendered
    assert rendered.count("<section") == rendered.count("</section>")
    assert rendered.count("<article") == rendered.count("</article>")

    # The operator sees nothing; the operator's engineer sees everything.
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"
    record = warnings[0]
    message = record.getMessage()
    assert "voice notes" in message.lower()
    assert "site-one" in message, "site scope missing from the log line"
    assert "2026-07-15" in message, "qc_date scope missing from the log line"
    assert marker in message, "the underlying exception text is missing"
    assert record.exc_info is not None, "no traceback captured; the cause would be invisible"
    assert record.exc_info[0] is RuntimeError
    # The traceback must name the function that actually failed, otherwise a real
    # bug is only nominally visible.
    traceback_text = "".join(traceback.format_exception(*record.exc_info))
    assert failing_function in traceback_text or marker in traceback_text


def test_failing_chain_board_is_identical_to_a_board_with_no_transcripts_at_all(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Degrading must land exactly on the quiet common case, not somewhere near it."""
    sidecars = [
        _sidecar("one", "Restrooms", capture_id="walk-alpha", minute=1),
        _sidecar("two", "Imaginary Wing", capture_id="walk-beta", minute=2),
    ]
    quiet = _board(tmp_path, sidecars, monkeypatch=monkeypatch)

    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Unreachable.")

    def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic chain failure")

    monkeypatch.setattr(field_photos, "load_handoff_voice_transcripts", explode)
    with caplog.at_level(logging.WARNING, logger=field_photos.__name__):
        degraded = _board(tmp_path, sidecars, monkeypatch=monkeypatch)

    assert degraded == quiet
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_a_healthy_render_logs_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """The boundary must not be noisy, or its warnings stop meaning anything."""
    _write_transcript(tmp_path, "audio-a", upload_id="walk-alpha", raw_text="Healthy note.")

    with caplog.at_level(logging.WARNING, logger=field_photos.__name__):
        rendered = _board(
            tmp_path,
            [_sidecar("one", "Restrooms", capture_id="walk-alpha")],
            monkeypatch=monkeypatch,
        )

    assert "Healthy note." in rendered
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


def test_failsoft_boundary_wraps_only_the_voice_note_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A broad ``except Exception`` is only acceptable if it is narrowly *scoped*.

    Structural gate: the ``try`` in ``_render_handoff_board`` may contain exactly
    one statement — the ``voice_notes_html`` assignment.  If anyone ever widens it
    to cover grouping, card rendering, or the export form, this fails.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(field_photos._render_handoff_board))
    tries = [node for node in ast.walk(tree) if isinstance(node, ast.Try)]
    assert len(tries) == 1, "the board should have exactly one fail-soft boundary"
    body = tries[0].body
    assert len(body) == 1, "the boundary must wrap a single statement"
    assigned = body[0]
    assert isinstance(assigned, ast.Assign)
    assert [target.id for target in assigned.targets] == ["voice_notes_html"]
    handler = tries[0].handlers[0]
    assert isinstance(handler.type, ast.Name) and handler.type.id == "Exception"

    # Behavioural half: a failure in the surrounding board is NOT absorbed.
    def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic card-render failure")

    monkeypatch.setattr(field_photos, "_render_handoff_card_shell", explode)
    with pytest.raises(RuntimeError, match="synthetic card-render failure"):
        _board(
            tmp_path,
            [_sidecar("one", "Restrooms", capture_id="walk-alpha")],
            monkeypatch=monkeypatch,
        )
