"""Gating contract for prompt 401: split a coordinated multi-item supply/equipment
note into N independently-approvable candidates.

Authored by the INDEPENDENT VERIFIER (not the executor).

The bug (live 2026-06-19): ``supply need: squeegee and extension pole to clean
the tall windows`` produced ONE candidate (extension pole), silently dropping
the squeegee. A note naming N items must produce N independently-approvable
candidates.

The fix (in ``field_capture/action_candidates.py``): in
``structured_payloads_from_semantic``, when a supply/equipment action's item
field (``item_name`` for ``log_supply_need``, ``equipment_name`` for
``log_equipment_request``) is a COORDINATED LIST, emit one cloned candidate per
item, each with its own item text and a distinct deterministic ``candidate_id``,
all sharing the same provenance. A trailing descriptive clause on the last
fragment ("... to clean the tall windows") is stripped; clause-like fragments
are rejected so descriptive text is never emitted as a phantom item.

These tests drive the REAL composed path end to end:

    hand-built model payload
      -> extracted_actions_from_model_payload   (prompt 402 override)
      -> serialize to a semantic artifact dict
      -> structured_payloads_from_semantic       (prompt 401 split)

so a build-to-the-test impl can not slip past: the asserted candidates are
whatever production actually emits.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from field_capture.action_candidates import (
    coordinated_item_field_items,
    structured_payloads_from_semantic,
)
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    extracted_actions_from_model_payload,
)
from queue_spec import JOB_LOG_EQUIPMENT_REQUEST, JOB_LOG_SUPPLY_NEED


# --------------------------------------------------------------------------- #
# Harness. Sandbox identity. Non-supply area ("") so the supply-area post-step
# does not re-derive the action -- we assert exactly the 402-override +
# 401-split product of the hand-built model action.
# --------------------------------------------------------------------------- #
def _input(note: str) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id="cap-401",
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


def _candidates(note: str, model_job_type: str, payload_fields: dict, *, capture_id: str = "cap-401") -> list[dict]:
    """Full composed path: 402 override -> artifact -> 401 split candidates."""
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


# --------------------------------------------------------------------------- #
# Criterion 1 -- LIVE 2-item case, end to end (402 forces supply).
# --------------------------------------------------------------------------- #
def test_live_two_item_supply_splits_into_two_supply_candidates() -> None:
    note = "supply need: squeegee and extension pole to clean the tall windows in the entrance"
    # The model misclassified this as equipment (saw "pole"); 402 forces supply.
    cands = _candidates(note, JOB_LOG_EQUIPMENT_REQUEST, {"equipment_name": "extension pole"})

    assert len(cands) == 2, [(_job_type(c), _items(c)) for c in cands]
    # Both are SUPPLY (composes with 402), never equipment.
    assert all(_job_type(c) == JOB_LOG_SUPPLY_NEED for c in cands)
    # Exactly the two items -- NOT "extension pole to clean the tall windows".
    assert _item_set(cands) == {"squeegee", "extension pole"}
    # The trailing descriptive clause is NOT carried on either item.
    for c in cands:
        assert "to clean" not in _items(c).lower()
        assert "entrance" not in _items(c).lower()


def test_two_item_independently_approvable_same_provenance() -> None:
    note = "supply need: paper towels and hand soap"
    cands = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "paper towels and hand soap"})

    assert len(cands) == 2
    assert _item_set(cands) == {"paper towels", "hand soap"}
    # Each candidate independently approvable: carries its own approval_metadata
    # with a proposed_queue_job whose payload names only its own item.
    for c in cands:
        am = (c.get("approval_metadata") or {}).get("proposed_queue_job")
        # approval metadata is best-effort (site registry may be unavailable in
        # the test env); when present it must name only this candidate's item.
        if am is not None:
            payload = am.get("payload") or {}
            assert payload.get("item_name", "").lower() in {"paper towels", "hand soap"}
    # Same provenance / source excerpt across the split.
    provs = {json.dumps(c.get("provenance"), sort_keys=True) for c in cands}
    assert len(provs) == 1, "split candidates must share one provenance"
    excerpts = {c.get("source_text") for c in cands}
    assert len(excerpts) == 1, "split candidates must share one source excerpt"


# --------------------------------------------------------------------------- #
# Criterion 2 -- 3-item list -> exactly THREE candidates.
# --------------------------------------------------------------------------- #
def test_three_item_supply_splits_into_three_candidates() -> None:
    note = "supply need: trash bags, gloves, and glass cleaner"
    cands = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "trash bags, gloves, and glass cleaner"})

    assert len(cands) == 3, [(_job_type(c), _items(c)) for c in cands]
    assert _item_set(cands) == {"trash bags", "gloves", "glass cleaner"}
    assert all(_job_type(c) == JOB_LOG_SUPPLY_NEED for c in cands)


# --------------------------------------------------------------------------- #
# Criterion 3 -- single item -> exactly ONE candidate (no spurious split).
# --------------------------------------------------------------------------- #
def test_single_item_supply_stays_one_candidate() -> None:
    note = "supply need: paper towels"
    cands = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "paper towels"})
    assert len(cands) == 1
    assert _items(cands[0]).lower() == "paper towels"


def test_single_item_equipment_stays_one_candidate() -> None:
    note = "equipment: auto scrubber"
    cands = _candidates(note, JOB_LOG_EQUIPMENT_REQUEST, {"equipment_name": "auto scrubber"})
    assert len(cands) == 1
    assert _job_type(cands[0]) == JOB_LOG_EQUIPMENT_REQUEST
    assert _items(cands[0]).lower() == "auto scrubber"


# --------------------------------------------------------------------------- #
# Criterion 4 -- conservative: a multi-word SINGLE item is not split on its
# internal space, and a descriptive clause is never emitted as a phantom item.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("item", ["backpack vacuum", "auto scrubber", "wet/dry shop vac"])
def test_multiword_single_item_not_split(item: str) -> None:
    note = f"supply need: {item}"
    cands = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": item})
    assert len(cands) == 1, [_items(c) for c in cands]
    assert _items(cands[0]).lower() == item.lower()


def test_no_phantom_clause_candidate() -> None:
    note = "supply need: squeegee and extension pole to clean the tall windows in the entrance"
    cands = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "squeegee and extension pole"})
    # Exactly two real items; the descriptive clause must never be its own item.
    items = _item_set(cands)
    assert items == {"squeegee", "extension pole"}
    assert not any("clean" in i or "window" in i or "entrance" in i for i in items)


# --------------------------------------------------------------------------- #
# Criterion 5 -- distinct deterministic ids, idempotent / replay-safe.
# --------------------------------------------------------------------------- #
def test_split_candidates_have_distinct_ids() -> None:
    note = "supply need: mop and bucket"
    cands = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "mop and bucket"})
    ids = [c["candidate_id"] for c in cands]
    assert len(ids) == 2
    assert len(set(ids)) == 2, f"split candidate ids must be distinct, got {ids}"


def test_split_is_idempotent_across_reruns() -> None:
    note = "supply need: mop and bucket"
    first = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "mop and bucket"}, capture_id="cap-replay")
    second = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "mop and bucket"}, capture_id="cap-replay")
    assert [c["candidate_id"] for c in first] == [c["candidate_id"] for c in second]


# --------------------------------------------------------------------------- #
# Criterion 6 -- composes with 402: supply token -> SUPPLY (not equipment), and
# an equipment token -> EQUIPMENT, each then split.
# --------------------------------------------------------------------------- #
def test_compose_supply_token_forces_supply_then_splits() -> None:
    note = "supply need: squeegee and extension pole"
    # Model routed to equipment; 402 forces supply, 401 splits.
    cands = _candidates(note, JOB_LOG_EQUIPMENT_REQUEST, {"equipment_name": "extension pole"})
    assert len(cands) == 2
    assert all(_job_type(c) == JOB_LOG_SUPPLY_NEED for c in cands)
    assert all(_job_type(c) != JOB_LOG_EQUIPMENT_REQUEST for c in cands)
    assert _item_set(cands) == {"squeegee", "extension pole"}


def test_compose_equipment_token_forces_equipment_then_splits() -> None:
    note = "equipment: backpack vacuum and lint brushes"
    # Model routed to supply; 402 forces equipment, 401 splits the multi-word list.
    cands = _candidates(note, JOB_LOG_SUPPLY_NEED, {"item_name": "backpack vacuum and lint brushes"})
    assert len(cands) == 2, [_items(c) for c in cands]
    assert all(_job_type(c) == JOB_LOG_EQUIPMENT_REQUEST for c in cands)
    # The multi-word item ("backpack vacuum") stays intact.
    assert _item_set(cands) == {"backpack vacuum", "lint brushes"}


# --------------------------------------------------------------------------- #
# CLAUSE-STRIPPING STRESS -- the fragile heuristic. Must not DROP a real item
# nor LEAK a clause. These exercise coordinated_item_field_items directly (the
# unit under the split) for tight, deterministic coverage.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, expected",
    [
        # Real two-item lists: both items survive, nothing dropped.
        ("squeegee, extension pole", ["squeegee", "extension pole"]),
        ("mop, bucket", ["mop", "bucket"]),
        ("paper towels, hand soap", ["paper towels", "hand soap"]),
        # Multi-word item must stay intact, not split on internal space.
        ("backpack vacuum, lint brushes", ["backpack vacuum", "lint brushes"]),
        # Trailing descriptive clause on the LAST fragment is stripped, last item kept.
        (
            "squeegee, extension pole to clean the tall windows in the entrance",
            ["squeegee", "extension pole"],
        ),
        # "bucket" must survive (over-stripping guard: do not drop a short last item).
        ("mop, bucket", ["mop", "bucket"]),
        # Single item, multi-word: no split.
        ("auto scrubber", ["auto scrubber"]),
        # Three-item comma list (no Oxford "and").
        ("trash bags, gloves, glass cleaner", ["trash bags", "gloves", "glass cleaner"]),
    ],
)
def test_coordinated_split_unit(text: str, expected: list[str]) -> None:
    assert coordinated_item_field_items(text) == expected


def test_clause_stripping_does_not_drop_a_real_short_last_item() -> None:
    # "mop and bucket"-style cleaned form: "bucket" is short but a real item.
    assert coordinated_item_field_items("mop, bucket") == ["mop", "bucket"]


def test_to_clause_is_not_emitted_as_standalone_item() -> None:
    # A comma-introduced "to ..." clause is correctly rejected (DESCRIPTIVE_
    # FRAGMENT_START_RE covers "to"): the heuristic falls back to keeping the
    # phrase whole (one fragment) rather than emitting the clause as item #2.
    out = coordinated_item_field_items("gloves, to be used on the dock floor")
    assert len(out) == 1, f"a 'to' clause must not become a second item, got {out}"


def test_for_clause_does_not_leak_as_phantom_item() -> None:
    # KNOWN DEFECT PROBE: "for" is NOT in the descriptive-clause vocabulary, so a
    # comma-introduced "for the dock" clause leaks as a standalone phantom item.
    # A correct heuristic must NOT emit a bare prepositional clause as an item.
    out = coordinated_item_field_items("gloves, for the dock")
    assert not any(frag.lower().startswith("for ") for frag in out), (
        f"a 'for ...' clause leaked as a phantom item: {out}"
    )


def test_for_clause_does_not_contaminate_the_last_item() -> None:
    # KNOWN DEFECT PROBE: the prompt's stress case. The last real item must be
    # "trash bags", NOT "trash bags for the dock" -- the trailing "for ..."
    # descriptive clause should be stripped like "to ..." is.
    out = coordinated_item_field_items("gloves and trash bags for the dock")
    assert out == ["gloves", "trash bags"], (
        f"trailing 'for the dock' clause contaminated the last item: {out}"
    )
