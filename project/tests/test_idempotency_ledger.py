"""Independent unit tests for ``queue_processor.idempotency_ledger``.

Authored from scratch by an independent test author (did not write the module
under test). Behavior was verified against the source before asserting:

* ``payload_hash_for`` delegates to ``compute_job_id`` which serializes
  ``{"job_type", "payload"}`` with ``json.dumps(sort_keys=True)`` then SHA-256.
  => deterministic, key-order independent, sensitive to job_type + any payload.
* ``append_record`` writes ``sort_keys=True`` JSON, OMITS ``None`` fields, and
  appends a newline-terminated line via O_APPEND.
* ``iter_records`` skips blank/whitespace-only lines, but RAISES ``ValueError``
  on malformed JSON or a record missing a string ``idempotency_key``.
  (Confirmed in source -- it is NOT silently tolerant of those two cases.)
* ``latest_record_for`` keeps the LAST matching record (most recent wins);
  returns ``None`` for an absent key or a non-existent ledger file.
* ``build_record`` computes ``payload_hash`` itself, stringifies paths, omits
  None for the optional path field, and stamps a UTC ISO timestamp.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from queue_processor.idempotency import compute_job_id
from queue_processor.idempotency_ledger import (
    IdempotencyRecord,
    append_record,
    build_record,
    iter_records,
    latest_record_for,
    ledger_path_for,
    payload_hash_for,
)


# --------------------------------------------------------------------------- #
# payload_hash_for
# --------------------------------------------------------------------------- #
def test_payload_hash_is_deterministic() -> None:
    payload = {"alpha": 1, "beta": "two", "gamma": [3, 4]}
    first = payload_hash_for("person_upsert", payload)
    second = payload_hash_for("person_upsert", payload)
    assert first == second


def test_payload_hash_is_a_sha256_hexdigest() -> None:
    digest = payload_hash_for("person_upsert", {"k": "v"})
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_payload_hash_is_key_order_independent() -> None:
    # Two dicts with the SAME contents but different insertion order must hash
    # identically -- the canonical serializer sorts keys.
    payload_a = {"first": 1, "second": 2, "third": 3}
    payload_b = {"third": 3, "first": 1, "second": 2}
    assert list(payload_a.keys()) != list(payload_b.keys())  # genuinely different order
    assert payload_hash_for("job", payload_a) == payload_hash_for("job", payload_b)


def test_payload_hash_key_order_independence_holds_for_nested_dicts() -> None:
    nested_a = {"outer": {"x": 1, "y": 2}, "z": 3}
    nested_b = {"z": 3, "outer": {"y": 2, "x": 1}}
    assert payload_hash_for("job", nested_a) == payload_hash_for("job", nested_b)


def test_payload_hash_differs_when_job_type_differs() -> None:
    payload = {"id": "abc", "value": 7}
    assert payload_hash_for("type_one", payload) != payload_hash_for("type_two", payload)


def test_payload_hash_differs_when_a_payload_value_differs() -> None:
    base = {"id": "abc", "value": 7}
    changed = {"id": "abc", "value": 8}
    assert payload_hash_for("job", base) != payload_hash_for("job", changed)


def test_payload_hash_differs_when_a_payload_key_is_added() -> None:
    base = {"id": "abc"}
    extra = {"id": "abc", "note": "added"}
    assert payload_hash_for("job", base) != payload_hash_for("job", extra)


def test_payload_hash_matches_documented_canonical_form() -> None:
    # Pin the contract: payload_hash_for is exactly compute_job_id over
    # {"job_type", "payload"}. This locks the canonicalization so a future
    # refactor that drops job_type or payload from the digest is caught.
    job_type = "person_upsert"
    payload = {"b": 2, "a": 1}
    assert payload_hash_for(job_type, payload) == compute_job_id(
        {"job_type": job_type, "payload": payload}
    )


def test_payload_hash_empty_payload_distinct_from_populated() -> None:
    assert payload_hash_for("job", {}) != payload_hash_for("job", {"k": "v"})


# --------------------------------------------------------------------------- #
# ledger_path_for
# --------------------------------------------------------------------------- #
def test_ledger_path_resolves_under_runtime_root(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    assert path == tmp_path / "idempotency_keys.jsonl"
    assert path.parent == tmp_path
    assert path.name == "idempotency_keys.jsonl"


def test_ledger_path_for_nested_runtime_root(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime" / "state"
    path = ledger_path_for(runtime_root)
    assert path.parent == runtime_root
    assert str(path).startswith(str(tmp_path))


# --------------------------------------------------------------------------- #
# append_record -> iter_records round trip
# --------------------------------------------------------------------------- #
def _make_record(key: str, **overrides: object) -> IdempotencyRecord:
    fields: dict[str, object] = {
        "idempotency_key": key,
        "job_type": "person_upsert",
        "payload_hash": "deadbeef",
        "computed_job_id": "job-" + key,
        "target_path": "/srv/sandbox/people/" + key + ".md",
        "timestamp": "2026-06-24T00:00:00+00:00",
    }
    fields.update(overrides)
    return IdempotencyRecord(**fields)  # type: ignore[arg-type]


def test_append_then_iter_round_trips_a_record(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    record = _make_record("key-1", person_id="p-1", run_id="run-1")
    append_record(path, record)

    records = iter_records(path)
    assert len(records) == 1
    got = records[0]
    assert got["idempotency_key"] == "key-1"
    assert got["job_type"] == "person_upsert"
    assert got["payload_hash"] == "deadbeef"
    assert got["computed_job_id"] == "job-key-1"
    assert got["target_path"] == "/srv/sandbox/people/key-1.md"
    assert got["timestamp"] == "2026-06-24T00:00:00+00:00"
    assert got["person_id"] == "p-1"
    assert got["run_id"] == "run-1"


def test_append_creates_parent_directory(tmp_path: Path) -> None:
    # append_record must mkdir the parent; the runtime_root dir need not pre-exist.
    runtime_root = tmp_path / "does" / "not" / "exist" / "yet"
    path = ledger_path_for(runtime_root)
    assert not runtime_root.exists()
    append_record(path, _make_record("k"))
    assert path.exists()
    assert len(iter_records(path)) == 1


def test_append_omits_none_optional_fields(tmp_path: Path) -> None:
    # Optional fields left at their None default must NOT be serialized.
    path = ledger_path_for(tmp_path)
    append_record(path, _make_record("key-1"))  # person_id/source_queue_file/run_id all None
    raw = path.read_text(encoding="utf-8").strip()
    on_disk = json.loads(raw)
    assert "person_id" not in on_disk
    assert "source_queue_file" not in on_disk
    assert "run_id" not in on_disk
    # And iter_records reflects that absence.
    assert "person_id" not in iter_records(path)[0]


def test_multiple_appends_preserve_insertion_order(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    for i in range(5):
        append_record(path, _make_record(f"key-{i}"))
    records = iter_records(path)
    assert [r["idempotency_key"] for r in records] == [f"key-{i}" for i in range(5)]


def test_each_record_is_its_own_jsonl_line(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    append_record(path, _make_record("a"))
    append_record(path, _make_record("b"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["idempotency_key"] == "a"
    assert json.loads(lines[1])["idempotency_key"] == "b"


def test_on_disk_json_keys_are_sorted(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    append_record(path, _make_record("a", person_id="p", run_id="r"))
    line = path.read_text(encoding="utf-8").splitlines()[0]
    obj = json.loads(line)
    # json.dumps(sort_keys=True) -> the serialized key order is sorted.
    serialized_keys = [seg.split(":", 1)[0].strip('"') for seg in line.strip("{}").split(",")]
    assert serialized_keys == sorted(obj.keys())


# --------------------------------------------------------------------------- #
# iter_records: blank-line tolerance + malformed-line behavior (raises)
# --------------------------------------------------------------------------- #
def test_iter_records_missing_file_returns_empty_list(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    assert not path.exists()
    assert iter_records(path) == []


def test_iter_records_skips_blank_and_whitespace_lines(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    append_record(path, _make_record("a"))
    append_record(path, _make_record("b"))
    # Inject blank + whitespace-only lines into the middle/around the records.
    raw = path.read_text(encoding="utf-8")
    path.write_text("\n" + raw.replace("\n", "\n   \n", 0) + "\n   \n\n", encoding="utf-8")
    records = iter_records(path)
    assert [r["idempotency_key"] for r in records] == ["a", "b"]


def test_iter_records_empty_file_returns_empty_list(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    path.write_text("", encoding="utf-8")
    assert iter_records(path) == []


def test_iter_records_raises_on_malformed_json_line(tmp_path: Path) -> None:
    # Confirmed against source: a non-JSON content line is NOT tolerated -- it
    # raises ValueError citing the 1-based line number.
    path = ledger_path_for(tmp_path)
    append_record(path, _make_record("good"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("this is not json\n")
    with pytest.raises(ValueError) as excinfo:
        iter_records(path)
    assert "line 2" in str(excinfo.value)


def test_iter_records_raises_on_record_missing_idempotency_key(tmp_path: Path) -> None:
    # A syntactically valid JSON object that lacks a string idempotency_key is
    # rejected (partial / structurally-wrong record).
    path = ledger_path_for(tmp_path)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"job_type": "x", "payload_hash": "h"}) + "\n")
    with pytest.raises(ValueError) as excinfo:
        iter_records(path)
    assert "missing idempotency_key" in str(excinfo.value)


def test_iter_records_raises_when_idempotency_key_not_a_string(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"idempotency_key": 123}) + "\n")
    with pytest.raises(ValueError):
        iter_records(path)


def test_iter_records_raises_when_top_level_is_not_a_dict(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(["just", "a", "list"]) + "\n")
    with pytest.raises(ValueError):
        iter_records(path)


# --------------------------------------------------------------------------- #
# latest_record_for
# --------------------------------------------------------------------------- #
def test_latest_record_for_returns_most_recent_of_several(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    # Three records share the key "dup"; the LAST appended must win.
    append_record(path, _make_record("dup", computed_job_id="v1", timestamp="2026-01-01T00:00:00+00:00"))
    append_record(path, _make_record("other"))
    append_record(path, _make_record("dup", computed_job_id="v2", timestamp="2026-02-01T00:00:00+00:00"))
    append_record(path, _make_record("dup", computed_job_id="v3", timestamp="2026-03-01T00:00:00+00:00"))

    latest = latest_record_for(path, "dup")
    assert latest is not None
    assert latest["computed_job_id"] == "v3"
    assert latest["timestamp"] == "2026-03-01T00:00:00+00:00"


def test_latest_record_for_single_match(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    append_record(path, _make_record("a"))
    append_record(path, _make_record("only", computed_job_id="solo"))
    append_record(path, _make_record("b"))
    latest = latest_record_for(path, "only")
    assert latest is not None
    assert latest["computed_job_id"] == "solo"


def test_latest_record_for_absent_key_returns_none(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    append_record(path, _make_record("present"))
    assert latest_record_for(path, "absent") is None


def test_latest_record_for_missing_file_returns_none(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path)
    assert not path.exists()
    assert latest_record_for(path, "anything") is None


# --------------------------------------------------------------------------- #
# build_record
# --------------------------------------------------------------------------- #
def test_build_record_populates_record_shape(tmp_path: Path) -> None:
    target = tmp_path / "people" / "p-1.md"
    source = tmp_path / "queue" / "job.md"
    payload = {"name": "Sandbox Person", "site": "Sandbox Site"}

    record = build_record(
        idempotency_key="idem-1",
        job_type="person_upsert",
        payload=payload,
        computed_job_id="job-123",
        target_path=target,
        person_id="p-1",
        source_queue_file=source,
        run_id="run-9",
    )

    assert isinstance(record, IdempotencyRecord)
    assert record.idempotency_key == "idem-1"
    assert record.job_type == "person_upsert"
    assert record.computed_job_id == "job-123"
    assert record.person_id == "p-1"
    assert record.run_id == "run-9"
    # Paths are stringified.
    assert record.target_path == str(target)
    assert record.source_queue_file == str(source)
    # payload_hash is computed from (job_type, payload), not passed in.
    assert record.payload_hash == payload_hash_for("person_upsert", payload)


def test_build_record_payload_hash_is_self_consistent() -> None:
    payload = {"value": 42}
    record = build_record(
        idempotency_key="k",
        job_type="t",
        payload=payload,
        computed_job_id="j",
        target_path=Path("/srv/sandbox/x.md"),
    )
    assert record.payload_hash == compute_job_id({"job_type": "t", "payload": payload})


def test_build_record_optional_fields_default_to_none() -> None:
    record = build_record(
        idempotency_key="k",
        job_type="t",
        payload={"a": 1},
        computed_job_id="j",
        target_path=Path("/srv/sandbox/x.md"),
    )
    assert record.person_id is None
    assert record.run_id is None
    # None source path must STAY None (not the string "None").
    assert record.source_queue_file is None


def test_build_record_timestamp_is_utc_iso() -> None:
    record = build_record(
        idempotency_key="k",
        job_type="t",
        payload={"a": 1},
        computed_job_id="j",
        target_path=Path("/srv/sandbox/x.md"),
    )
    assert isinstance(record.timestamp, str)
    # Round-trips as an aware UTC datetime.
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(record.timestamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_build_record_round_trips_through_ledger(tmp_path: Path) -> None:
    # End-to-end: build -> append -> latest_record_for recovers the same data,
    # and the None source_queue_file is omitted on disk.
    path = ledger_path_for(tmp_path)
    payload = {"name": "Sandbox Person"}
    record = build_record(
        idempotency_key="end-to-end",
        job_type="person_upsert",
        payload=payload,
        computed_job_id="job-xyz",
        target_path=tmp_path / "out.md",
        person_id="p-7",
    )
    append_record(path, record)
    got = latest_record_for(path, "end-to-end")
    assert got is not None
    assert got["computed_job_id"] == "job-xyz"
    assert got["payload_hash"] == payload_hash_for("person_upsert", payload)
    assert got["person_id"] == "p-7"
    assert "source_queue_file" not in got  # None optional dropped
    assert "run_id" not in got
