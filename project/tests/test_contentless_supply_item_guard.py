"""Gating contract for prompt 403: drop contentless supply/equipment items and
strip trailing verb (copula) clauses.

Authored by the INDEPENDENT VERIFIER (not the executor). Composes on top of 401
(multi-item split) + 402 (operator-classification override).

The bugs (live, post-401 deploy):
  * a ``log_supply_need`` candidate whose item was literally ``and``
    ("Log supply need: and.") leaked into the review queue;
  * a split fragment ``heavy duty lint brushes are the required supplies`` (a
    trailing copula/verb clause) was not stripped.

The fix (``field_capture/action_candidates.py``): a new helper
``supply_equipment_item_is_contentless(text)`` drops items that are empty, a
bare conjunction (``and`` / ``or`` / ``&`` / ``and/or``), article-only
(``the`` / ``a`` / ``an``), pure punctuation/whitespace, or have no alphabetic
*content* word -- wired into BOTH the split-fragment loop and the final
structured candidate emission. The trailing-clause strip is extended to
copula/verb clauses (``is/are/was/were/be/been/being ...``) behind 401's
>=2-word-prefix guard.

THE CRITICAL FALSE-POSITIVE GATE: the guard must key on CONTENT, never length.
Real short items (``TP``, ``mop``, ``wax``, ``c-fold``, ...) MUST survive.

These tests drive the REAL composed path end to end (402 override -> artifact
-> 401/403 split) exactly like ``test_supply_multi_item_splitting.py``, and also
unit-test the helper in isolation.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from field_capture.action_candidates import (
    coordinated_item_field_items,
    structured_payloads_from_semantic,
    supply_equipment_item_is_contentless,
)
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    extracted_actions_from_model_payload,
)
from queue_spec import JOB_LOG_EQUIPMENT_REQUEST, JOB_LOG_SUPPLY_NEED


# --------------------------------------------------------------------------- #
# Harness -- sandbox identity, non-supply area so the supply-area post-step does
# not re-derive the action. Mirrors test_supply_multi_item_splitting.py so a
# build-to-the-test impl cannot slip past: asserted candidates are whatever
# production actually emits through the composed path.
# --------------------------------------------------------------------------- #
def _input(note: str) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id="cap-403",
        source_kind="ops_dashboard_text",
        source_text=note,
        site_id="SANDBOX",
        site_label="Sandbox Site",
        area="",
    )


def _model_payload(job_type: str, payload_fields: dict, *, excerpt: str) -> dict:
    item = payload_fields.get("item_name") or payload_fields.get("equipment_name") or ""
    return {
        "extracted_actions": [
            {
                "action_key": "k",
                "candidate_type": "field_capture_follow_up",
                "target_type": "site",
                "target_label": "Sandbox Site",
                "summary": f"Supply request: {item}.",
                "source_excerpt": excerpt,
                "job_type": job_type,
                "payload_fields": dict(payload_fields),
            }
        ]
    }


def _candidates(note: str, model_job_type: str, payload_fields: dict, *, capture_id: str = "cap-403") -> list[dict]:
    actions = extracted_actions_from_model_payload(
        _model_payload(model_job_type, payload_fields, excerpt=note),
        _input(note),
    )
    artifact = {
        "capture_id": capture_id,
        "upload_id": capture_id,
        "extracted_actions": [asdict(a) for a in actions],
    }
    artifact = json.loads(json.dumps(artifact))  # tuple -> list, exactly like on-disk
    return structured_payloads_from_semantic(Path(f"{capture_id}.json"), artifact)


def _items(candidate: dict) -> str:
    pf = (candidate.get("channel_metadata") or {}).get("payload_fields") or {}
    return (pf.get("item_name") or pf.get("equipment_name") or "").strip()


def _job_type(candidate: dict) -> str:
    return (candidate.get("channel_metadata") or {}).get("job_type") or ""


def _item_set(candidates: list[dict]) -> set[str]:
    return {_items(c).lower() for c in candidates}


# =========================================================================== #
# Criterion 1 -- contentless items emit ZERO candidates (composed path).
# This is the gating test the mutation check flips RED.
# =========================================================================== #
@pytest.mark.parametrize("item", ["and", "or", "the", "a", "an", "and/or", "&", "..."])
def test_contentless_single_item_emits_zero_candidates(item: str) -> None:
    cands = _candidates("supply need: " + item, JOB_LOG_SUPPLY_NEED, {"item_name": item})
    assert cands == [], f"contentless item {item!r} leaked candidates: {[_items(c) for c in cands]}"
    # Specifically: no candidate whose item is the bare conjunction "and".
    assert "and" not in _item_set(cands)


def test_live_supply_need_and_emits_no_and_candidate() -> None:
    # The exact live regression: "Log supply need: and." must yield no candidate.
    cands = _candidates("supply need: and", JOB_LOG_SUPPLY_NEED, {"item_name": "and"})
    assert cands == []
    assert all(_items(c).lower() != "and" for c in cands)


# =========================================================================== #
# Criterion 2 -- THE CRITICAL FALSE-POSITIVE GATE.
# Real short items survive, including "TP". Guard is CONTENT-based, not length.
# =========================================================================== #
def test_paper_towels_and_TP_keeps_TP() -> None:
    note = "supply need: paper towels and TP"
    cands = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "paper towels and TP"})
    assert len(cands) == 2, [_items(c) for c in cands]
    assert _item_set(cands) == {"paper towels", "tp"}
    # TP (toilet paper) MUST be present -- the headline false-positive risk.
    assert any(_items(c) == "TP" for c in cands), f"TP was dropped: {[_items(c) for c in cands]}"


@pytest.mark.parametrize("item", ["TP", "mop", "wax", "rags", "gloves", "lye", "c-fold"])
def test_real_short_single_item_survives_composed(item: str) -> None:
    cands = _candidates("supply need: " + item, JOB_LOG_SUPPLY_NEED, {"item_name": item})
    assert len(cands) == 1, f"{item!r} wrongly dropped/split: {[_items(c) for c in cands]}"
    assert _items(cands[0]).lower() == item.lower()


# =========================================================================== #
# Criterion 3 -- mixed list: contentless fragment dropped, real one kept.
# =========================================================================== #
@pytest.mark.parametrize("text", ["mop and", "mop, and"])
def test_mixed_list_drops_contentless_fragment_keeps_real(text: str) -> None:
    cands = _candidates("supply need: " + text, JOB_LOG_SUPPLY_NEED, {"item_name": text})
    assert _item_set(cands) == {"mop"}, [_items(c) for c in cands]
    assert "and" not in _item_set(cands)


# =========================================================================== #
# Criterion 4 -- verb/copula clause strip on the last item.
# =========================================================================== #
def test_copula_clause_stripped_from_last_item() -> None:
    # The live regression: the last fragment "heavy duty lint brushes are the
    # required supplies" must have its copula clause stripped. (The upstream 402/
    # extraction layer normalizes the leading article "a new" off "a new vacuum"
    # before 403 sees it, so the first item renders as "vacuum" -- a pre-existing
    # behavior, not a 403 contentless drop.)
    note = "supply need: a new vacuum and heavy duty lint brushes are the required supplies"
    cands = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "a new vacuum and heavy duty lint brushes are the required supplies"})
    items = _item_set(cands)
    assert "heavy duty lint brushes" in items, items
    # The copula clause is never carried on an item nor emitted as a phantom.
    assert not any("required supplies" in i or i.startswith("are ") for i in items), items
    assert len(cands) == 2, items


@pytest.mark.parametrize(
    "text, expected",
    [
        # Prefix >= 2 words -> copula clause stripped (401's >=2-word guard met).
        ("a new vacuum and heavy duty lint brushes are the required supplies",
         ["a new vacuum", "heavy duty lint brushes"]),
        ("dust mops and floor pads were the missing items",
         ["dust mops", "floor pads"]),
    ],
)
def test_copula_clause_strip_unit(text: str, expected: list[str]) -> None:
    assert coordinated_item_field_items(text) == expected


def test_copula_strip_respects_401_two_word_prefix_guard() -> None:
    # Single-word last-item prefix ("bucket") is BELOW the >=2-word guard, so the
    # copula clause is NOT stripped (matches 401's conservative no-overstrip rule).
    # This documents the guard boundary -- not a contentless drop.
    out = coordinated_item_field_items("mop and bucket is broken")
    assert out[0] == "mop"
    assert "bucket" in out[1].lower()


# =========================================================================== #
# Criterion 5 -- NO 401/402 regression. Real splits + single items unchanged.
# =========================================================================== #
def test_four_item_comma_list_unchanged() -> None:
    note = "supply need: disinfectant, dust mops, frames, poles"
    cands = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "disinfectant, dust mops, frames, poles"})
    assert len(cands) == 4, [_items(c) for c in cands]
    assert _item_set(cands) == {"disinfectant", "dust mops", "frames", "poles"}


def test_two_item_split_with_trailing_to_clause_unchanged() -> None:
    note = "supply need: squeegee and extension pole to clean the tall windows"
    cands = _candidates(note, JOB_LOG_EQUIPMENT_REQUEST, {"equipment_name": "extension pole"})
    assert _item_set(cands) == {"squeegee", "extension pole"}
    assert all(_job_type(c) == JOB_LOG_SUPPLY_NEED for c in cands)


def test_multiword_single_item_not_split_unchanged() -> None:
    cands = _candidates("supply need: backpack vacuum", JOB_LOG_SUPPLY_NEED, {"item_name": "backpack vacuum"})
    assert len(cands) == 1
    assert _items(cands[0]).lower() == "backpack vacuum"


def test_composes_with_402_supply_need_A_and_B_two_supply_candidates() -> None:
    # 402 forces supply (model misrouted to equipment); 403/401 split into two.
    note = "supply need: mop and wax"
    cands = _candidates(note, JOB_LOG_EQUIPMENT_REQUEST, {"equipment_name": "wax"})
    assert len(cands) == 2
    assert all(_job_type(c) == JOB_LOG_SUPPLY_NEED for c in cands)
    assert _item_set(cands) == {"mop", "wax"}


# =========================================================================== #
# Helper unit battery -- contentless vs real tokens, in isolation.
# =========================================================================== #
@pytest.mark.parametrize(
    "token",
    ["", "  ", "and", "or", "&", "and/or", "the", "a", "an", "And", "OR",
     "...", "!!", "( )", ",", "the the", "a an"],
)
def test_helper_flags_contentless(token: str) -> None:
    assert supply_equipment_item_is_contentless(token) is True


@pytest.mark.parametrize(
    "token",
    ["TP", "mop", "wax", "rags", "gloves", "lye", "c-fold",
     "paper towels", "toilet paper", "backpack vacuum",
     "a new vacuum", "heavy duty lint brushes", "the mop"],
)
def test_helper_keeps_real_items(token: str) -> None:
    assert supply_equipment_item_is_contentless(token) is False, f"real item {token!r} wrongly flagged contentless"
