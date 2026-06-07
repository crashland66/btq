from __future__ import annotations

"""Gating tests for prompt 299: candidate-review detail blocks migrate from
kv-tables to field-group panels (``<dl class="fields">``) while the review
workflow (pills, resolve/notify forms, hidden fields, POST actions, section
structure, <h4> headers) stays intact.
"""

from ops_dashboard.sections import candidates


def test_candidate_vision_details_use_field_panel_not_kv_table() -> None:
    vision_items = [
        {
            "status": "ok",
            "photo_asset_id": "photo-1",
            "area_guess": "lobby",
            "description": "wet floor near entrance",
            "model_name": "mlx-vision",
            "confidence": "0.9",
        }
    ]

    rendered = candidates.render_candidate_vision(vision_items)

    # migrated detail block.
    assert '<dl class="fields">' in rendered
    assert 'class="kv-table"' not in rendered
    assert "<dt>Description</dt>" in rendered
    assert "wet floor near entrance" in rendered
    # preserved structure: <h4> header + <details><summary> card.
    assert "<h4>Vision</h4>" in rendered
    assert "<details><summary>lobby</summary>" in rendered


def test_proposed_job_item_details_use_field_panel_not_kv_table() -> None:
    rendered = candidates.render_proposed_job_item(
        "set_entity_status",
        {"entity_type": "site", "entity_id": "loc-7", "status": "active"},
        "",
    )

    # migrated payload detail block.
    assert '<dl class="fields">' in rendered
    assert 'class="kv-table"' not in rendered
    assert "<dt>Entity Id</dt><dd>loc-7</dd>" in rendered
    # preserved section structure / wrapper.
    assert 'class="proposed-job-item"' in rendered
    assert "<strong>" in rendered


def test_resolution_progression_panel_keeps_pill_and_resolve_form() -> None:
    candidate = {
        "candidate_id": "cand-42",
        "status": "approved",
        "resolution": {"status": "client_notified"},
        "client_notification": {
            "client_informed": True,
            "client_informed_method": "email",
            "client_informed_by": "Jordan",
            "client_informed_at": "2026-06-06",
            "client_informed_note": "Emailed client with photo.",
        },
    }

    rendered = candidates.render_resolution_progression(candidate)

    # migrated client/resolution detail block.
    assert '<dl class="fields">' in rendered
    assert 'class="kv-table"' not in rendered
    assert "<dt>Client Informed Method</dt><dd>email</dd>" in rendered
    # preserved workflow: <h4> header, resolution pill, resolve form + hidden field + POST action.
    assert "<h4>Resolution</h4>" in rendered
    assert 'class="pill resolution-notified"' in rendered
    assert "<form" in rendered
    assert 'action="/field-capture/review/resolve"' in rendered
    assert 'name="candidate_id" value="cand-42"' in rendered


def test_resolution_progression_open_state_keeps_notify_and_resolve_forms() -> None:
    candidate = {
        "candidate_id": "cand-99",
        "status": "approved",
        "resolution": {"status": "open"},
        "client_notification": {},
    }

    rendered = candidates.render_resolution_progression(candidate)

    # both workflow forms still present in the open state.
    assert 'action="/field-capture/review/client-informed"' in rendered
    assert 'action="/field-capture/review/resolve"' in rendered
    assert 'name="candidate_id" value="cand-99"' in rendered
