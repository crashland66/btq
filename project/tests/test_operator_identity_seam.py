"""Independent gating tests for the operator-identity seam.

Verifies the behavior-preserving refactor that replaced the hardcoded
``OPERATOR_ID_GREG`` write-stamps (and the ``RECORD_OPERATOR`` read literal in
records.py) with a single resolver ``current_operator_id()``.

Contract:
  * default (no env) -> "op_greg"  (zero behavior change today)
  * BTQ_OPERATOR_ID set -> that value (the multi-operator switch)
  * every write-stamp + the records selectors/gate flow through the ONE resolver
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from btq_vault.entity_types import OPERATOR_ID_GREG, current_operator_id


# --------------------------------------------------------------------------
# 1. Resolver behavior
# --------------------------------------------------------------------------

def test_resolver_default_is_op_greg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_OPERATOR_ID", raising=False)
    assert current_operator_id() == "op_greg"
    assert current_operator_id() == OPERATOR_ID_GREG


def test_resolver_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_custom")
    assert current_operator_id() == "op_custom"


def test_resolver_empty_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_OPERATOR_ID", "")
    assert current_operator_id() == "op_greg"


# --------------------------------------------------------------------------
# 2. Real write-stampers reflect the resolver
# --------------------------------------------------------------------------

def test_prospect_stamp_defaults_to_op_greg(monkeypatch: pytest.MonkeyPatch) -> None:
    from field_capture.prospects import build_prospect_document

    monkeypatch.delenv("BTQ_OPERATOR_ID", raising=False)
    doc = build_prospect_document(prospect_id="p1", name="Acme")
    assert doc["operator"] == "op_greg"
    assert doc["operator"] == current_operator_id()


def test_prospect_stamp_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from field_capture.prospects import build_prospect_document

    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_other")
    doc = build_prospect_document(prospect_id="p1", name="Acme")
    assert doc["operator"] == "op_other"


def _unknown_capture_payload() -> dict[str, Any]:
    return {
        "path": "unknowns/2026-06-16/clip.md",
        "timestamp": "2026-06-16T12:00:00Z",
        "audio_file": "clip.m4a",
    }


def test_unknown_capture_stamp_defaults_to_op_greg(monkeypatch: pytest.MonkeyPatch) -> None:
    from queue_processor.handlers.unknowns import build_unknown_capture_doc_fields

    monkeypatch.delenv("BTQ_OPERATOR_ID", raising=False)
    doc = build_unknown_capture_doc_fields(_unknown_capture_payload())
    assert doc["operator"] == "op_greg"
    assert doc["operator"] == current_operator_id()


def test_unknown_capture_stamp_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from queue_processor.handlers.unknowns import build_unknown_capture_doc_fields

    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_other")
    doc = build_unknown_capture_doc_fields(_unknown_capture_payload())
    assert doc["operator"] == "op_other"


# --------------------------------------------------------------------------
# 3. records.py selectors + gate flow through the resolver
# --------------------------------------------------------------------------

def _capture_find(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace records._find with a recorder that returns the captured payloads."""
    import ops_dashboard.sections.records as records

    captured: list[dict[str, Any]] = []

    def fake_find(payload: dict[str, Any]) -> list[dict[str, Any]]:
        captured.append(payload)
        return []

    monkeypatch.setattr(records, "_find", fake_find)
    return captured


def test_shift_report_selector_default_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    import ops_dashboard.sections.records as records

    monkeypatch.delenv("BTQ_OPERATOR_ID", raising=False)
    captured = _capture_find(monkeypatch)
    records._shift_report_docs()
    assert captured[-1]["selector"]["operator"] == "op_greg"


def test_day_record_selector_default_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    import ops_dashboard.sections.records as records

    monkeypatch.delenv("BTQ_OPERATOR_ID", raising=False)
    captured = _capture_find(monkeypatch)
    records._day_record_docs()
    assert captured[-1]["selector"]["operator"] == "op_greg"


def test_selectors_follow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import ops_dashboard.sections.records as records

    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_other")
    captured = _capture_find(monkeypatch)
    records._shift_report_docs()
    records._day_record_docs()
    assert captured[-2]["selector"]["operator"] == "op_other"
    assert captured[-1]["selector"]["operator"] == "op_other"


def test_load_record_gate_matches_default_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    import ops_dashboard.sections.records as records

    monkeypatch.delenv("BTQ_OPERATOR_ID", raising=False)

    monkeypatch.setattr(
        records,
        "_find",
        lambda payload: [{"_id": "x", "type": "shift_report", "operator": "op_greg"}],
    )
    assert records._load_record("x") is not None


def test_load_record_gate_rejects_other_operator_under_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ops_dashboard.sections.records as records

    monkeypatch.delenv("BTQ_OPERATOR_ID", raising=False)

    # A doc stamped for a different operator must be gated out for op_greg.
    monkeypatch.setattr(
        records,
        "_find",
        lambda payload: [{"_id": "x", "type": "shift_report", "operator": "op_other"}],
    )
    assert records._load_record("x") is None


def test_load_record_gate_shifts_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import ops_dashboard.sections.records as records

    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_other")
    # Now an op_other doc passes, an op_greg doc is gated out.
    monkeypatch.setattr(
        records,
        "_find",
        lambda payload: [{"_id": "x", "type": "shift_report", "operator": "op_other"}],
    )
    assert records._load_record("x") is not None

    monkeypatch.setattr(
        records,
        "_find",
        lambda payload: [{"_id": "x", "type": "shift_report", "operator": "op_greg"}],
    )
    assert records._load_record("x") is None


# --------------------------------------------------------------------------
# 4. Mutation check: ONE sentinel env flows to BOTH a stamp AND the records
#    selector, proving a single shared seam (not parallel copies).
# --------------------------------------------------------------------------

def test_single_seam_env_flows_to_stamp_and_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from field_capture.prospects import build_prospect_document
    import ops_dashboard.sections.records as records

    sentinel = "op_SENTINEL_42"
    monkeypatch.setenv("BTQ_OPERATOR_ID", sentinel)

    captured = _capture_find(monkeypatch)
    records._shift_report_docs()
    stamp = build_prospect_document(prospect_id="p1", name="Acme")

    assert stamp["operator"] == sentinel
    assert captured[-1]["selector"]["operator"] == sentinel

    # Unset -> both snap back to the default with zero behavior change.
    monkeypatch.delenv("BTQ_OPERATOR_ID", raising=False)
    captured2 = _capture_find(monkeypatch)
    records._shift_report_docs()
    stamp2 = build_prospect_document(prospect_id="p1", name="Acme")
    assert stamp2["operator"] == "op_greg"
    assert captured2[-1]["selector"]["operator"] == "op_greg"
