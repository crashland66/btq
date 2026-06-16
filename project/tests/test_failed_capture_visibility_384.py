"""Independent gating tests for prompt 384: failed-capture visibility.

384 surfaces canonical CouchDB `btq_field_captures` docs with
processing_state=="failed" as a THIRD source on /failed, a `failed_captures`
count via inbox.console_counts(), and a "Needs you" action card on home.

These tests drive the REAL functions in-process and mock the canonical CouchDB
read at its module seam (`query_couchdb_find` + `couchdb_config`) so no live
CouchDB is required. Fixtures use the sandbox identity only.

Covered:
  * /failed "Failed captures" section: error_reason present renders the reason;
    absent / empty-string error_reason renders the graceful placeholder, and the
    literal "None" never leaks as the reason.
  * inbox.console_counts() exposes an integer `failed_captures` count matching
    the number of failed docs.
  * Graceful degradation: when the canonical count query raises, console_counts
    still returns (omitting failed_captures) and home._home_action_cards renders
    without raising and without the failed-captures card.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ops_dashboard.common import render_table
from ops_dashboard.sections import failed as failed_section
from ops_dashboard.sections import home as home_section
from ops_dashboard.sections import inbox as inbox_section

# Sandbox identity only.
SANDBOX_SITE = "SANDBOX"
SANDBOX_SITE_LABEL = "Sandbox Site"
SANDBOX_USER = "sandbox-user"


def _capture_doc(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "_id": "field_capture_cap_sandbox_0001",
        "capture_id": "cap_sandbox_0001",
        "type": "field_capture",
        "processing_state": "failed",
        "site_id": SANDBOX_SITE,
        "captured_at": "2026-06-15T12:00:00Z",
        "error_reason": "vision model timeout",
        "photos": ["fcp_sandbox_a"],
    }
    base.update(overrides)
    return base


class _FakeCfg:
    base_url = "http://couch.invalid:5984"


def _install_couch(monkeypatch: pytest.MonkeyPatch, module: object, docs: list[dict[str, object]]) -> None:
    """Mock the canonical field-captures read seam on the given section module."""
    monkeypatch.setattr(module.couchdb_config, "from_env", lambda *a, **k: _FakeCfg())
    monkeypatch.setattr(module.couchdb_config, "field_captures_database", lambda *a, **k: "btq_field_captures")
    monkeypatch.setattr(module, "query_couchdb_find", lambda cfg, db, selector: {"docs": list(docs), "bookmark": None})


def _clear_count_cache() -> None:
    with inbox_section._failed_capture_count_lock:
        inbox_section._failed_capture_count_cache.clear()


# --------------------------------------------------------------------------
# /failed "Failed captures" section: reason rendering + placeholder.
# --------------------------------------------------------------------------

def _reason_cell_html(rows: list[dict[str, object]]) -> str:
    """Render rows through the SAME reason column the /failed section uses."""
    return render_table(rows, [{"key": "reason", "label": "Failure reason"}], empty_text="No failed captures.")


def test_failed_captures_renders_error_reason_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_couch(monkeypatch, failed_section, [_capture_doc(error_reason="vision model timeout")])
    rows, err = failed_section.failed_captures()
    assert err == ""
    assert len(rows) == 1
    assert rows[0]["reason"] == "vision model timeout"
    cell = _reason_cell_html(rows)
    assert "vision model timeout" in cell


def test_failed_captures_missing_error_reason_uses_placeholder_not_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _capture_doc()
    doc.pop("error_reason")
    _install_couch(monkeypatch, failed_section, [doc])
    rows, err = failed_section.failed_captures()
    assert err == ""
    assert len(rows) == 1
    assert rows[0]["reason"] == failed_section.FAILED_CAPTURE_REASON_PLACEHOLDER
    cell = _reason_cell_html(rows)
    assert failed_section.FAILED_CAPTURE_REASON_PLACEHOLDER in cell
    # The literal "None" must never leak as the reason, nor a blank cell.
    assert ">None<" not in cell
    assert "<td></td>" not in cell


def test_failed_captures_empty_string_error_reason_uses_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Probe: error_reason present but empty string -> placeholder, not blank.
    _install_couch(monkeypatch, failed_section, [_capture_doc(error_reason="")])
    rows, _err = failed_section.failed_captures()
    assert len(rows) == 1
    assert rows[0]["reason"] == failed_section.FAILED_CAPTURE_REASON_PLACEHOLDER
    cell = _reason_cell_html(rows)
    assert ">None<" not in cell
    assert "<td></td>" not in cell


def test_failed_captures_only_includes_failed_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mango is the authoritative filter, but the row builder also guards state.
    failed_doc = _capture_doc(capture_id="cap_sandbox_failed")
    non_failed = _capture_doc(
        _id="field_capture_cap_sandbox_done",
        capture_id="cap_sandbox_done",
        processing_state="completed",
    )
    _install_couch(monkeypatch, failed_section, [failed_doc, non_failed])
    rows, _err = failed_section.failed_captures()
    ids = {row["capture_id"] for row in rows}
    assert "cap_sandbox_failed" in ids
    assert "cap_sandbox_done" not in ids


# --------------------------------------------------------------------------
# inbox.console_counts() exposes an integer failed_captures count.
# --------------------------------------------------------------------------

def _counts_ctx() -> object:
    # console_counts caches review/issues/supplies/equipment via console_cards;
    # we pre-seed the cards/review caches so only failed_capture_count is live.
    return SimpleNamespace()


def _stub_console_subcounts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        inbox_section,
        "console_review_queue_counts",
        lambda _ctx: {"pending_approval": 0},
    )
    monkeypatch.setattr(
        inbox_section,
        "console_cards",
        lambda _ctx: {
            "issues": {"count": 0},
            "supplies": {"count": 0},
            "equipment": {"count": 0},
        },
    )


def test_console_counts_exposes_failed_captures_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_count_cache()
    _stub_console_subcounts(monkeypatch)
    docs = [
        _capture_doc(capture_id="cap_sandbox_1", _id="field_capture_1"),
        _capture_doc(capture_id="cap_sandbox_2", _id="field_capture_2"),
        _capture_doc(capture_id="cap_sandbox_3", _id="field_capture_3"),
    ]
    _install_couch(monkeypatch, inbox_section, docs)
    counts = inbox_section.console_counts(_counts_ctx())
    assert counts["failed_captures"] == 3
    assert isinstance(counts["failed_captures"], int)
    _clear_count_cache()


def test_console_counts_failed_captures_matches_doc_count(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_count_cache()
    _stub_console_subcounts(monkeypatch)
    docs = [_capture_doc(capture_id=f"cap_sandbox_{n}", _id=f"field_capture_{n}") for n in range(7)]
    _install_couch(monkeypatch, inbox_section, docs)
    counts = inbox_section.console_counts(_counts_ctx())
    assert counts["failed_captures"] == 7
    _clear_count_cache()


# --------------------------------------------------------------------------
# Graceful degradation: count query raises -> no 500, card omitted.
# --------------------------------------------------------------------------

def _install_couch_raising(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    monkeypatch.setattr(module.couchdb_config, "from_env", lambda *a, **k: _FakeCfg())
    monkeypatch.setattr(module.couchdb_config, "field_captures_database", lambda *a, **k: "btq_field_captures")

    def _boom(*_a: object, **_k: object) -> dict[str, object]:
        raise RuntimeError("CouchDB unreachable")

    monkeypatch.setattr(module, "query_couchdb_find", _boom)


def test_failed_capture_count_returns_none_when_query_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_count_cache()
    _install_couch_raising(monkeypatch, inbox_section)
    assert inbox_section.failed_capture_count() is None
    _clear_count_cache()


def test_console_counts_omits_failed_captures_when_query_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_count_cache()
    _stub_console_subcounts(monkeypatch)
    _install_couch_raising(monkeypatch, inbox_section)
    counts = inbox_section.console_counts(_counts_ctx())
    # Degrades: the key is omitted entirely (not a crash, not a bogus 0).
    assert "failed_captures" not in counts
    # The rest of the counts still render.
    assert counts["review"] == 0
    _clear_count_cache()


def test_home_action_cards_render_without_raising_when_query_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_count_cache()
    _stub_console_subcounts(monkeypatch)
    _install_couch_raising(monkeypatch, inbox_section)
    # Drive the REAL home action-card builder; it calls inbox.console_counts.
    cards, notice = home_section._home_action_cards(_counts_ctx())
    # Home must still render fully.
    assert isinstance(cards, list) and cards
    # The failed-captures card is OMITTED (count unavailable), not 500ing.
    assert all(card.get("id") != "failed_captures" for card in cards)
    _clear_count_cache()


def test_home_action_cards_show_accented_failed_card_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_count_cache()
    _stub_console_subcounts(monkeypatch)
    docs = [_capture_doc(capture_id="cap_sandbox_x", _id="field_capture_x")]
    _install_couch(monkeypatch, inbox_section, docs)
    cards, _notice = home_section._home_action_cards(_counts_ctx())
    failed_cards = [c for c in cards if c.get("id") == "failed_captures"]
    assert len(failed_cards) == 1
    assert failed_cards[0]["count"] == 1
    assert failed_cards[0]["see_all"] == "/failed"
    _clear_count_cache()
