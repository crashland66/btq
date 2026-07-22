"""INDEPENDENT VERIFIER gate for prompt 492 (`create_supply_request`).

Authored by the verifier, not the executor. Every test here maps to an
acceptance criterion or hard constraint in prompt 492. Fixtures are
PUBLIC-SAFE SYNTHETIC ONLY: no real vendors, sites, people, or prices.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import queue_processor.main as qp
from queue_processor.canonical_rmw import SiteContext
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import supplies_equipment, supply_requests
from queue_processor.registry import JOB_HANDLERS
from queue_spec import (
    ALLOWED_JOB_TYPES,
    JOB_CREATE_SUPPLY_REQUEST,
    JOB_LOG_SUPPLY_NEED,
    JOB_SCHEMAS,
    validate_job,
)
from tests.test_queue_processor_couchdb_write import (
    RecordingRmwVaultStore,
    context_for,
    job,
    make_queue_file,
)

SYNTHETIC_SITE = "synthetic-site-alpha"
SYNTHETIC_REQUESTER = "Synthetic Requester"
FINANCIAL_KEY_MARKERS = ("price", "cost", "budget", "subtotal", "tax", "total", "amount")


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


def vpayload(**overrides: Any) -> dict[str, Any]:
    """Deliberately NON-alphabetical item order so any sort/dedup mutation shows."""
    payload: dict[str, Any] = {
        "site_id": SYNTHETIC_SITE,
        "requested_by": SYNTHETIC_REQUESTER,
        "observed_at": "2026-07-22T08:00:00-04:00",
        "items": [
            {"item_name": "Zeta wipes", "quantity": "3 boxes"},
            {"item_name": "Alpha liners", "quantity": 12, "unit": "rolls"},
            {"item_name": "Mid-shelf spray", "note": "Second floor closet"},
        ],
        "notes": "Synthetic restock note",
        "related_capture_ids": ["synthetic-capture-1", "synthetic-capture-2"],
        "source": "field_capture_audio",
    }
    payload.update(overrides)
    return payload


def _as_job(payload: dict[str, Any]) -> dict[str, Any]:
    return {"job_type": JOB_CREATE_SUPPLY_REQUEST, "payload": payload}


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: RecordingRmwVaultStore):
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    monkeypatch.setattr(
        supply_requests,
        "resolve_site_context",
        lambda _store, value: SiteContext(str(value), "Synthetic Site Alpha", "Synthetic Account"),
    )
    return context_for(tmp_path)


def run_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: RecordingRmwVaultStore,
    payload: dict[str, Any],
    *,
    job_id: str = "verifier-job-1",
    filename: str | None = None,
    dry_run: bool = False,
):
    context = _prepare(tmp_path, monkeypatch, store)
    if dry_run:
        context = replace(context, dry_run=True)
    queue_file = make_queue_file(context, filename or job_id)
    processed_dir = context.runtime_root / "processed"
    supply_requests.process_create_supply_request_job(
        queue_file,
        job(JOB_CREATE_SUPPLY_REQUEST, payload, job_id=job_id),
        context,
        processed_dir,
    )
    return context, queue_file


def requests_in(store: RecordingRmwVaultStore) -> list[dict[str, Any]]:
    return [doc for doc in store.docs if doc.get("type") == "supply_request"]


def collect_keys(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            out.add(str(key).lower())
            collect_keys(value, out)
    elif isinstance(node, list):
        for value in node:
            collect_keys(value, out)


# --------------------------------------------------------------------------
# AC: the job is registered and passes queue_spec validation
# --------------------------------------------------------------------------


def test_job_is_registered_and_spec_validated() -> None:
    """Gates the AMENDED 2026-07-22 contract: observed_at is REQUIRED."""
    assert JOB_CREATE_SUPPLY_REQUEST == "create_supply_request"
    assert JOB_CREATE_SUPPLY_REQUEST in ALLOWED_JOB_TYPES
    assert JOB_HANDLERS[JOB_CREATE_SUPPLY_REQUEST] is supply_requests.process_create_supply_request_job
    assert JOB_SCHEMAS[JOB_CREATE_SUPPLY_REQUEST] == [
        "site_id",
        "requested_by",
        "items",
        "observed_at",
    ]
    assert validate_job(_as_job(vpayload())) is True
    assert (
        validate_job(
            _as_job(
                {
                    "site_id": SYNTHETIC_SITE,
                    "requested_by": SYNTHETIC_REQUESTER,
                    "observed_at": "2026-07-22T08:00:00-04:00",
                    "items": [{"item_name": "Alpha liners"}],
                }
            )
        )
        is True
    )
    # request_id is optional but accepted.
    assert validate_job(_as_job(vpayload(request_id="synthetic-request-override"))) is True


def test_observed_at_is_required_and_must_be_a_real_timestamp() -> None:
    """AMENDED contract: observed_at absent is what allowed the Defect 2 collision."""
    minimal = {
        "site_id": SYNTHETIC_SITE,
        "requested_by": SYNTHETIC_REQUESTER,
        "items": [{"item_name": "Alpha liners"}],
    }
    assert validate_job(_as_job(minimal)) is False, "observed_at must now be required"
    assert validate_job(_as_job({**minimal, "observed_at": None})) is False
    assert validate_job(_as_job({**minimal, "observed_at": "   "})) is False
    assert validate_job(_as_job({**minimal, "observed_at": "not-a-timestamp"})) is False
    assert validate_job(_as_job({**minimal, "observed_at": 20260722})) is False
    assert validate_job(_as_job({**minimal, "observed_at": "2026-07-22T08:00:00-04:00"})) is True
    assert validate_job(_as_job({**minimal, "observed_at": "2026-07-22T12:00:00Z"})) is True


# --------------------------------------------------------------------------
# AC: valid multi-item payload -> ONE record, all items, submitted order
# --------------------------------------------------------------------------


def test_multi_item_payload_writes_one_record_in_submitted_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    context, queue_file = run_request(tmp_path, monkeypatch, store, vpayload())

    docs = requests_in(store)
    assert len(docs) == 1, "a multi-item request must produce exactly ONE supply_request record"
    doc = docs[0]

    # ORDER as submitted, every line kept, nothing dropped.
    assert [item["item_name"] for item in doc["items"]] == [
        "Zeta wipes",
        "Alpha liners",
        "Mid-shelf spray",
    ]
    assert len(doc["items"]) == 3
    assert doc["items"][0]["quantity"] == "3 boxes"
    assert doc["items"][1]["quantity"] == 12
    assert doc["items"][1]["unit"] == "rolls"
    assert doc["items"][2]["note"] == "Second floor closet"

    assert doc["type"] == "supply_request"
    assert doc["_id"] == f"supply_request_{doc['supply_request_id']}"
    assert doc["site_id"] == SYNTHETIC_SITE
    assert doc["requested_by"] == SYNTHETIC_REQUESTER
    assert doc["observed_at"] == "2026-07-22T08:00:00-04:00"
    assert doc["related_capture_ids"] == ["synthetic-capture-1", "synthetic-capture-2"]
    assert doc["notes"] == "Synthetic restock note"
    assert doc["source"] == "field_capture_audio"
    assert doc["status"] == "open"
    assert len(store.update_doc_calls) == 1
    assert (context.runtime_root / "processed" / queue_file.name).exists()


def test_item_order_is_load_bearing_not_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reordering the submitted lines must be observable end-to-end."""
    forward = [{"item_name": "Zeta wipes"}, {"item_name": "Alpha liners"}]
    reverse = [{"item_name": "Alpha liners"}, {"item_name": "Zeta wipes"}]

    store_a = RecordingRmwVaultStore()
    run_request(tmp_path / "fwd", monkeypatch, store_a, vpayload(items=forward), job_id="verifier-fwd")
    store_b = RecordingRmwVaultStore()
    run_request(tmp_path / "rev", monkeypatch, store_b, vpayload(items=reverse), job_id="verifier-rev")

    assert [i["item_name"] for i in requests_in(store_a)[0]["items"]] == ["Zeta wipes", "Alpha liners"]
    assert [i["item_name"] for i in requests_in(store_b)[0]["items"]] == ["Alpha liners", "Zeta wipes"]
    # Distinct submissions -> distinct deterministic ids (no order-insensitive hashing).
    assert requests_in(store_a)[0]["_id"] != requests_in(store_b)[0]["_id"]


# --------------------------------------------------------------------------
# AC: N=1 is not a special case
# --------------------------------------------------------------------------


def test_single_item_payload_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = RecordingRmwVaultStore()
    run_request(
        tmp_path,
        monkeypatch,
        store,
        vpayload(items=[{"item_name": "Alpha liners"}]),
        job_id="verifier-single",
    )
    docs = requests_in(store)
    assert len(docs) == 1
    assert docs[0]["items"] == [{"item_name": "Alpha liners"}]


# --------------------------------------------------------------------------
# AC: duplicate item names are BOTH preserved (no silent dedup)
# --------------------------------------------------------------------------


def test_duplicate_item_names_are_both_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    items = [
        {"item_name": "Bowl cleaner", "note": "North restroom"},
        {"item_name": "Bowl cleaner", "note": "South restroom"},
    ]
    run_request(tmp_path, monkeypatch, store, vpayload(items=items), job_id="verifier-dup")
    written = requests_in(store)[0]["items"]
    assert len(written) == 2, "duplicate-looking lines must NOT be deduplicated"
    assert written == items


def test_exactly_identical_duplicate_lines_are_both_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Byte-identical lines are the hardest dedup case; both must survive."""
    store = RecordingRmwVaultStore()
    items = [{"item_name": "Bowl cleaner"}, {"item_name": "Bowl cleaner"}]
    run_request(tmp_path, monkeypatch, store, vpayload(items=items), job_id="verifier-dup2")
    assert requests_in(store)[0]["items"] == items


# --------------------------------------------------------------------------
# AC: rejections fail loud, with NO partial record written
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,label",
    [
        ({"site_id": SYNTHETIC_SITE, "requested_by": SYNTHETIC_REQUESTER}, "missing-items"),
        (vpayload(items=[]), "empty-items"),
        (vpayload(items=None), "null-items"),
        (vpayload(items={"item_name": "Alpha liners"}), "items-not-a-list"),
        (vpayload(items=[{"item_name": "   "}]), "whitespace-only-item-name"),
        (vpayload(items=[{"item_name": ""}]), "empty-item-name"),
        (vpayload(items=[{"quantity": "2 cases"}]), "item-missing-item-name"),
        (
            vpayload(items=[{"item_name": "Alpha liners"}, {"item_name": "  "}]),
            "second-line-blank-name",
        ),
        (vpayload(items=[{"item_name": None}]), "null-item-name"),
        (vpayload(items=["Alpha liners"]), "item-not-a-dict"),
        (vpayload(site_id="   "), "blank-site-id"),
        (vpayload(requested_by=""), "blank-requested-by"),
        (
            {
                "site_id": SYNTHETIC_SITE,
                "requested_by": SYNTHETIC_REQUESTER,
                "items": [{"item_name": "Alpha liners"}],
            },
            "missing-observed-at",
        ),
        (vpayload(observed_at="   "), "blank-observed-at"),
        (vpayload(observed_at="not-a-timestamp"), "malformed-observed-at"),
        (vpayload(request_id="   "), "blank-request-id"),
        (vpayload(request_id=42), "non-string-request-id"),
    ],
)
def test_invalid_payloads_are_rejected_with_no_partial_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], label: str
) -> None:
    assert validate_job(_as_job(payload)) is False, f"queue_spec must reject: {label}"

    store = RecordingRmwVaultStore()
    context = _prepare(tmp_path, monkeypatch, store)
    queue_file = make_queue_file(context, f"verifier-invalid-{label}")
    with pytest.raises(shared.QueueProcessorError):
        supply_requests.process_create_supply_request_job(
            queue_file,
            job(JOB_CREATE_SUPPLY_REQUEST, payload, job_id=f"verifier-invalid-{label}"),
            context,
            context.runtime_root / "processed",
        )
    assert store.update_doc_calls == [], f"partial write on rejected payload: {label}"
    assert store.docs == [], f"partial record persisted on rejected payload: {label}"
    assert not (context.runtime_root / "processed" / queue_file.name).exists()


def test_a_blank_line_rejects_the_whole_request_rather_than_dropping_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped line item is a supply that never arrives — fail loud instead."""
    store = RecordingRmwVaultStore()
    context = _prepare(tmp_path, monkeypatch, store)
    payload = vpayload(
        items=[
            {"item_name": "Alpha liners"},
            {"item_name": "   "},
            {"item_name": "Zeta wipes"},
        ]
    )
    queue_file = make_queue_file(context, "verifier-blank-mid")
    with pytest.raises(shared.QueueProcessorError):
        supply_requests.process_create_supply_request_job(
            queue_file,
            job(JOB_CREATE_SUPPLY_REQUEST, payload, job_id="verifier-blank-mid"),
            context,
            context.runtime_root / "processed",
        )
    assert requests_in(store) == []


# --------------------------------------------------------------------------
# AC: replay is idempotent
# --------------------------------------------------------------------------


def test_replay_of_same_job_id_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    payload = vpayload()
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-replay", filename="first")
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-replay", filename="second")

    docs = requests_in(store)
    assert len(docs) == 1, "replay must not create a second supply_request record"
    assert len(docs[0]["items"]) == 3, "replay must not duplicate line items"
    assert [i["item_name"] for i in docs[0]["items"]] == [
        "Zeta wipes",
        "Alpha liners",
        "Mid-shelf spray",
    ]


def test_resubmission_under_a_new_job_id_does_not_duplicate_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same request content, different job id (re-enqueue after a failure)."""
    store = RecordingRmwVaultStore()
    payload = vpayload()
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-a", filename="a")
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-b", filename="b")

    docs = requests_in(store)
    assert len(docs) == 1, "deterministic id must collapse identical resubmissions"
    assert len(docs[0]["items"]) == 3


def test_replay_preserves_created_at_and_operator_set_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    payload = vpayload()
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-s1", filename="s1")
    doc = requests_in(store)[0]
    original_created_at = doc["created_at"]
    # Operator moves it along; a resubmission must not silently reopen it.
    doc["status"] = "ordered"

    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-s2", filename="s2")
    after = requests_in(store)[0]
    assert after["created_at"] == original_created_at
    assert after["status"] == "ordered"


def test_resubmission_preserves_prior_job_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect 1 regression gate. btq_job_ids is the canonical audit trail."""
    store = RecordingRmwVaultStore()
    payload = vpayload()
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-p1", filename="p1")
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-p2", filename="p2")

    job_ids = requests_in(store)[0].get("btq_job_ids")
    assert job_ids == ["verifier-p1", "verifier-p2"], (
        "prior job ids were wiped; the record no longer proves which jobs produced it, "
        "and a later replay of the earlier job would rewrite instead of no-op"
    )


def test_earlier_jobs_replay_marker_survives_a_later_resubmission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect 1 regression gate, second half: A -> B -> A must not ping-pong.

    Once job A has been applied its marker must survive job B, so a later replay
    of A is a genuine no-op instead of a rewrite that wipes B.
    """
    store = RecordingRmwVaultStore()
    payload = vpayload()
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-a", filename="a1")
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-b", filename="b1")
    writes_before_replay = len(store.update_doc_calls)

    # Replay A. Its marker is still on the doc, so apply_canonical_rmw must skip.
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-a", filename="a2")

    assert len(store.update_doc_calls) == writes_before_replay, (
        "replaying an already-applied job rewrote the record instead of no-opping"
    )
    doc = requests_in(store)[0]
    assert doc["btq_job_ids"] == ["verifier-a", "verifier-b"]
    assert len(requests_in(store)) == 1


def test_distinct_observed_at_yields_distinct_records_and_keeps_both_evidence_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect 2 regression gate.

    Two genuinely separate requests, same site, same requester, IDENTICAL item
    list, months apart. Previously these collided on one doc and the later write
    silently destroyed the earlier submission's capture evidence.
    """
    store = RecordingRmwVaultStore()
    items = [{"item_name": "Alpha liners"}, {"item_name": "Zeta wipes"}]
    july = vpayload(
        items=items,
        observed_at="2026-07-22T08:00:00-04:00",
        related_capture_ids=["synthetic-capture-july"],
        notes="July run",
    )
    september = vpayload(
        items=items,
        observed_at="2026-09-14T08:00:00-04:00",
        related_capture_ids=["synthetic-capture-september"],
        notes="September run",
    )

    assert supply_requests.supply_request_id(july) != supply_requests.supply_request_id(september)

    run_request(tmp_path, monkeypatch, store, july, job_id="verifier-july", filename="july")
    run_request(tmp_path, monkeypatch, store, september, job_id="verifier-sept", filename="sept")

    docs = requests_in(store)
    assert len(docs) == 2, "distinct requests must not collapse onto one record"
    by_observed = {doc["observed_at"]: doc for doc in docs}
    assert by_observed["2026-07-22T08:00:00-04:00"]["related_capture_ids"] == [
        "synthetic-capture-july"
    ], "the earlier submission's capture evidence was overwritten"
    assert by_observed["2026-09-14T08:00:00-04:00"]["related_capture_ids"] == [
        "synthetic-capture-september"
    ]
    assert by_observed["2026-07-22T08:00:00-04:00"]["notes"] == "July run"
    assert by_observed["2026-09-14T08:00:00-04:00"]["notes"] == "September run"


def test_observed_at_is_part_of_the_identity_seed() -> None:
    """Narrow unit gate: only observed_at differs, so only observed_at can explain the id."""
    base = vpayload(items=[{"item_name": "Alpha liners"}])
    a = {**base, "observed_at": "2026-07-22T08:00:00-04:00"}
    b = {**base, "observed_at": "2026-07-22T08:00:01-04:00"}
    assert supply_requests.supply_request_id(a) != supply_requests.supply_request_id(b)


def test_explicit_request_id_is_honored_verbatim_as_the_identity_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AMENDED contract: the escape hatch log_supply_need's supply_id already offered."""
    override = "synthetic-request-override-1"
    payload = vpayload(request_id=override)
    assert supply_requests.supply_request_id(payload) == override

    store = RecordingRmwVaultStore()
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-override")
    doc = requests_in(store)[0]
    assert doc["supply_request_id"] == override
    assert doc["_id"] == f"supply_request_{override}"
    # The override wins over the derived seed, and is not itself stored as payload noise.
    assert not doc["supply_request_id"].startswith("srq_")


def test_explicit_request_id_disambiguates_otherwise_identical_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests identical in every derived field stay separate under distinct ids."""
    store = RecordingRmwVaultStore()
    shared_fields = dict(items=[{"item_name": "Alpha liners"}])
    first = vpayload(request_id="synthetic-request-a", **shared_fields)
    second = vpayload(request_id="synthetic-request-b", **shared_fields)

    run_request(tmp_path, monkeypatch, store, first, job_id="verifier-ov-a", filename="ova")
    run_request(tmp_path, monkeypatch, store, second, job_id="verifier-ov-b", filename="ovb")

    ids = sorted(doc["_id"] for doc in requests_in(store))
    assert ids == ["supply_request_synthetic-request-a", "supply_request_synthetic-request-b"]


def test_explicit_request_id_replay_is_still_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    payload = vpayload(request_id="synthetic-request-replay")
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-ovr", filename="r1")
    run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-ovr", filename="r2")
    docs = requests_in(store)
    assert len(docs) == 1
    assert len(docs[0]["items"]) == 3


def test_identity_is_deterministic_not_wall_clock_salted() -> None:
    """AMENDED contract explicitly forbids wall-clock salting (breaks replay idempotency)."""
    payload = vpayload()
    ids = {supply_requests.supply_request_id(payload) for _ in range(5)}
    assert len(ids) == 1, "the id must be a pure function of the payload"
    # And a fresh but equal payload must hash the same.
    assert supply_requests.supply_request_id(vpayload()) == ids.pop()


# --------------------------------------------------------------------------
# AC: --dry-run writes nothing but reports the intended target
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_reports_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = RecordingRmwVaultStore()
    payload = vpayload()
    expected_id = supply_requests.supply_request_id(payload)
    context, queue_file = run_request(
        tmp_path, monkeypatch, store, payload, job_id="verifier-dry", dry_run=True
    )
    output = capsys.readouterr().out

    assert f"supply_request_{expected_id}" in output, "dry-run must report the intended target"
    assert store.update_doc_calls == [], "dry-run must not write"
    assert store.docs == []
    assert queue_file.exists(), "dry-run must leave the queue file in place"
    assert not (context.runtime_root / "processed" / queue_file.name).exists()


def test_dry_run_still_rejects_an_invalid_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    context = replace(_prepare(tmp_path, monkeypatch, store), dry_run=True)
    queue_file = make_queue_file(context, "verifier-dry-invalid")
    with pytest.raises(shared.QueueProcessorError):
        supply_requests.process_create_supply_request_job(
            queue_file,
            job(JOB_CREATE_SUPPLY_REQUEST, vpayload(items=[]), job_id="verifier-dry-invalid"),
            context,
            context.runtime_root / "processed",
        )


def test_target_path_hint_matches_the_written_doc_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    payload = vpayload()
    context, _ = run_request(tmp_path, monkeypatch, store, payload, job_id="verifier-hint")
    hint = qp.target_path_hint(job(JOB_CREATE_SUPPLY_REQUEST, payload, job_id="verifier-hint"), context)
    assert hint == requests_in(store)[0]["_id"]


# --------------------------------------------------------------------------
# CONSTRAINT: no financial data anywhere in this phase
# --------------------------------------------------------------------------


def test_written_record_contains_no_financial_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    run_request(tmp_path, monkeypatch, store, vpayload(), job_id="verifier-nofin")
    keys: set[str] = set()
    collect_keys(requests_in(store)[0], keys)
    offenders = sorted(k for k in keys if any(m in k for m in FINANCIAL_KEY_MARKERS))
    assert offenders == [], f"financial data leaked into a worker-submittable record: {offenders}"


@pytest.mark.parametrize(
    "payload,label",
    [
        (vpayload(estimated_cost=10), "top-level-estimated_cost"),
        (vpayload(budget_remaining=100), "top-level-budget_remaining"),
        (vpayload(total=5), "top-level-total"),
        (vpayload(items=[{"item_name": "Alpha liners", "unit_price": 1}]), "item-unit_price"),
        (vpayload(items=[{"item_name": "Alpha liners", "extended_price": 1}]), "item-extended_price"),
        (vpayload(items=[{"item_name": "Alpha liners", "sku": "X"}]), "item-sku"),
        (vpayload(vendor="Synthetic Vendor"), "top-level-vendor"),
    ],
)
def test_financial_and_receipt_fields_are_rejected(payload: dict[str, Any], label: str) -> None:
    assert validate_job(_as_job(payload)) is False, f"must reject financial/receipt field: {label}"


# --------------------------------------------------------------------------
# CONSTRAINT: canonical write goes through apply_canonical_rmw ONLY
# --------------------------------------------------------------------------


def test_canonical_write_goes_only_through_apply_canonical_rmw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    context = _prepare(tmp_path, monkeypatch, store)
    calls: list[str] = []

    def fake_rmw(_store, target, job_id, transform):
        calls.append(target.doc_id)
        return None  # emulate "already applied" so the handler takes its skip path

    monkeypatch.setattr(supply_requests, "apply_canonical_rmw", fake_rmw)
    queue_file = make_queue_file(context, "verifier-rmw")
    supply_requests.process_create_supply_request_job(
        queue_file,
        job(JOB_CREATE_SUPPLY_REQUEST, vpayload(), job_id="verifier-rmw"),
        context,
        context.runtime_root / "processed",
    )
    assert len(calls) == 1, "the canonical write must route through apply_canonical_rmw"
    assert calls[0].startswith("supply_request_")
    assert store.update_doc_calls == [], "no direct-write path may bypass apply_canonical_rmw"
    assert store.docs == []


def test_canonical_target_is_a_supply_request_not_a_supply_order_or_need(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    run_request(tmp_path, monkeypatch, store, vpayload(), job_id="verifier-naming")
    doc = requests_in(store)[0]
    assert doc["type"] == "supply_request"
    assert doc["_id"].startswith("supply_request_")
    assert not doc["_id"].startswith("supply_order")
    assert not doc["_id"].startswith("supply_need")
    assert [d for d in store.docs if d.get("type") in {"supply_order", "supply_need"}] == []


# --------------------------------------------------------------------------
# CONSTRAINT: log_supply_need is provably unchanged
# --------------------------------------------------------------------------


def test_log_supply_need_contract_is_untouched() -> None:
    assert JOB_SCHEMAS[JOB_LOG_SUPPLY_NEED] == ["site_id", "item_name", "requested_by"]
    assert JOB_HANDLERS[JOB_LOG_SUPPLY_NEED] is supplies_equipment.process_log_supply_need_job
    assert (
        validate_job(
            {
                "job_type": JOB_LOG_SUPPLY_NEED,
                "payload": {
                    "site_id": SYNTHETIC_SITE,
                    "item_name": "Alpha liners",
                    "requested_by": SYNTHETIC_REQUESTER,
                },
            }
        )
        is True
    )
    # log_supply_need must NOT have acquired an items field.
    assert (
        validate_job(
            {
                "job_type": JOB_LOG_SUPPLY_NEED,
                "payload": {
                    "site_id": SYNTHETIC_SITE,
                    "item_name": "Alpha liners",
                    "requested_by": SYNTHETIC_REQUESTER,
                    "items": [{"item_name": "Zeta wipes"}],
                },
            }
        )
        is False
    )


def test_log_supply_need_still_writes_its_own_single_item_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    monkeypatch.setattr(
        supplies_equipment,
        "resolve_site_context",
        lambda _store, value: SiteContext(str(value), "Synthetic Site Alpha", "Synthetic Account"),
    )
    context = context_for(tmp_path)
    queue_file = make_queue_file(context, "verifier-need")
    supplies_equipment.process_log_supply_need_job(
        queue_file,
        job(
            JOB_LOG_SUPPLY_NEED,
            {
                "site_id": SYNTHETIC_SITE,
                "item_name": "Alpha liners",
                "requested_by": SYNTHETIC_REQUESTER,
                "supply_id": "synthetic-need-1",
            },
            job_id="verifier-need",
        ),
        context,
        context.runtime_root / "processed",
    )
    needs = [d for d in store.docs if d.get("type") == "supply_need"]
    assert len(needs) == 1
    assert needs[0]["_id"] == "supply_need_synthetic-need-1"
    assert needs[0]["item_name"] == "Alpha liners"
    assert "items" not in needs[0]


# --------------------------------------------------------------------------
# CONSTRAINT: no Phase 2-5 scope creep
# --------------------------------------------------------------------------


def test_no_phase_2_to_5_scope_creep_in_the_handler() -> None:
    source = Path(supply_requests.__file__).read_text(encoding="utf-8").lower()
    for marker in ("staples", "price", "cost", "budget", "vendor", "invoice", "supplyorder"):
        assert marker not in source, f"phase 2-5 concept leaked into the phase-1 handler: {marker}"


def test_handler_does_not_import_the_staples_receipt_struct() -> None:
    source = Path(supply_requests.__file__).read_text(encoding="utf-8")
    assert "supply_orders" not in source
    assert "site_supply_budgets" not in source
