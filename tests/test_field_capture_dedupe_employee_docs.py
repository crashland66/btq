from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import btq
import btq_cli.backfill
from field_capture.dedupe_employee_docs import dedupe_employee_docs
from token_store import TokenStore


class FakeCanonicalStore:
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = {doc_id: dict(doc) for doc_id, doc in docs.items()}
        self.puts: list[tuple[dict[str, Any], str | None]] = []

    def find_employee_docs(self) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self.docs.values() if doc.get("type") == "employee" and not doc.get("_deleted")]

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        doc = self.docs.get(doc_id)
        return dict(doc) if doc is not None and not doc.get("_deleted") else None

    def put_with_rev(self, doc: dict[str, Any], *, expected_rev: str | None) -> dict[str, Any]:
        doc_id = str(doc["_id"])
        current = self.docs.get(doc_id)
        current_rev = None if current is None else current.get("_rev")
        assert expected_rev == current_rev
        self.puts.append((dict(doc), expected_rev))
        if doc.get("_deleted"):
            assert current is not None
            self.docs.pop(doc_id)
            return {"_id": doc_id, "_rev": f"{expected_rev}-deleted", "_deleted": True}
        stored = dict(doc)
        stored["_rev"] = f"{expected_rev or 'new'}-next"
        self.docs[doc_id] = stored
        return dict(stored)


class FailingEmployeeDiscoveryStore(FakeCanonicalStore):
    def find_employee_docs(self) -> list[dict[str, Any]]:
        raise RuntimeError("employee discovery unavailable")


class FailingEmployeeHydrationStore(FakeCanonicalStore):
    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        if doc_id == "employee_keller_bruce":
            raise RuntimeError("employee hydration unavailable")
        return super().get_optional(doc_id)


def employee_doc(
    doc_id: str,
    *,
    rev: str,
    first: str = "Bruce",
    last: str = "Keller",
    person_id: str | None = None,
    btq_job_ids: list[str] | None = None,
    content: str = "Bruce note.",
    **fields: Any,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": doc_id,
        "_rev": rev,
        "type": "employee",
        "name": f"{first} {last}",
        "first": first,
        "last": last,
        "content": content,
        "btq_job_ids": btq_job_ids or [],
    }
    if person_id is not None:
        doc["person_id"] = person_id
    doc.update(fields)
    return doc


def token_store_with(tmp_path: Path, *person_ids: str) -> TokenStore:
    token_store = TokenStore(tmp_path / "field_capture_tokens.sqlite3")
    for person_id in person_ids:
        token_store.create_token(person_id, label=f"{person_id} phone")
    return token_store


def test_dedupe_merges_underscore_doc_into_hyphen_token_survivor_identical_content(tmp_path: Path) -> None:
    store = FakeCanonicalStore(
        {
            "employee_keller-bruce": employee_doc(
                "employee_keller-bruce",
                rev="1-survivor",
                person_id="keller-bruce",
                btq_job_ids=["job-a", "job-b"],
                role="lead",
            ),
            "employee_keller_bruce": employee_doc(
                "employee_keller_bruce",
                rev="1-dup",
                btq_job_ids=["job-b", "job-c"],
                phone="555-0100",
            ),
        }
    )

    report = dedupe_employee_docs(store, token_store_with(tmp_path, "keller-bruce"), dry_run=False)

    assert report.errors == []
    assert report.applied_merges == 1
    assert report.deleted_docs == 1
    assert "employee_keller_bruce" not in store.docs
    survivor = store.docs["employee_keller-bruce"]
    assert survivor["_id"] == "employee_keller-bruce"
    assert survivor["person_id"] == "keller-bruce"
    assert "vault_path" not in survivor
    assert survivor["btq_job_ids"] == ["job-a", "job-b", "job-c"]
    assert survivor["content"] == "Bruce note."
    assert survivor["role"] == "lead"
    assert survivor["phone"] == "555-0100"
    assert report.items[0].content_actions == {"employee_keller_bruce": "unchanged_identical"}


def test_dedupe_preserves_differing_content_with_marker_and_is_idempotent(tmp_path: Path) -> None:
    store = FakeCanonicalStore(
        {
            "employee_keller-bruce": employee_doc(
                "employee_keller-bruce",
                rev="1-survivor",
                person_id="keller-bruce",
                btq_job_ids=["job-a"],
                content="Short survivor note.",
            ),
            "employee_keller_bruce": employee_doc(
                "employee_keller_bruce",
                rev="1-dup",
                btq_job_ids=["job-b"],
                content="Longer duplicate note with schedule context.",
            ),
        }
    )
    token_store = token_store_with(tmp_path, "keller-bruce")

    report = dedupe_employee_docs(store, token_store, dry_run=False)
    second_report = dedupe_employee_docs(store, token_store, dry_run=False)

    survivor = store.docs["employee_keller-bruce"]
    assert report.errors == []
    assert "Longer duplicate note with schedule context." in survivor["content"]
    assert "Short survivor note." in survivor["content"]
    assert "## Merged from duplicate employee_keller_bruce" in survivor["content"]
    assert report.items[0].content_actions == {
        "employee_keller_bruce": "kept_longer_duplicate_and_appended_prior_survivor"
    }
    assert second_report.candidate_groups == 0
    assert second_report.applied_merges == 0
    assert second_report.deleted_docs == 0


def test_dedupe_dry_run_writes_nothing(tmp_path: Path) -> None:
    store = FakeCanonicalStore(
        {
            "employee_keller-bruce": employee_doc(
                "employee_keller-bruce",
                rev="1-survivor",
                person_id="keller-bruce",
                btq_job_ids=["job-a"],
            ),
            "employee_keller_bruce": employee_doc("employee_keller_bruce", rev="1-dup", btq_job_ids=["job-b"]),
        }
    )

    report = dedupe_employee_docs(store, token_store_with(tmp_path, "keller-bruce"), dry_run=True)

    assert report.planned_merges == 1
    assert report.applied_merges == 0
    assert report.deleted_docs == 0
    assert store.puts == []
    assert "employee_keller_bruce" in store.docs


def test_dedupe_logs_and_counts_skips_on_bad_doc(tmp_path: Path, caplog) -> None:
    store = FailingEmployeeHydrationStore(
        {
            "employee_keller-bruce": employee_doc("employee_keller-bruce", rev="1-survivor", person_id="keller-bruce"),
            "employee_keller_bruce": employee_doc("employee_keller_bruce", rev="1-dup"),
        }
    )

    report = dedupe_employee_docs(store, token_store_with(tmp_path, "keller-bruce"), dry_run=True)

    assert report.planned_merges == 1
    assert report.errors == [
        {"group_key": "employee_keller_bruce", "message": "employee hydration unavailable"}
    ]
    assert "using employee dedupe summary doc after full-doc lookup failed for employee_keller_bruce" in caplog.text


def test_durable_path_does_not_silently_drop(tmp_path: Path) -> None:
    store = FailingEmployeeDiscoveryStore({})

    report = dedupe_employee_docs(store, token_store_with(tmp_path, "keller-bruce"), dry_run=True)

    assert report.employee_docs == 0
    assert report.candidate_groups == 0
    assert report.errors == [{"group_key": "find_employee_docs", "message": "employee discovery unavailable"}]


def test_dedupe_refuses_groups_with_no_token_matched_doc(tmp_path: Path) -> None:
    store = FakeCanonicalStore(
        {
            "employee_keller_bruce": employee_doc("employee_keller_bruce", rev="1-a", btq_job_ids=["job-a"]),
            "employee_bruce_keller": employee_doc("employee_bruce_keller", rev="1-b", btq_job_ids=["job-b"]),
        }
    )

    report = dedupe_employee_docs(store, token_store_with(tmp_path, "another-person"), dry_run=False)

    assert report.applied_merges == 0
    assert report.deleted_docs == 0
    assert report.skipped_groups == 1
    assert report.errors == [
        {"group_key": "name:keller-bruce", "message": "expected exactly one active-token survivor, found 0"}
    ]
    assert store.puts == []
    assert set(store.docs) == {"employee_keller_bruce", "employee_bruce_keller"}


def test_dedupe_refuses_to_delete_token_matched_or_auth_resolving_docs(tmp_path: Path) -> None:
    store = FakeCanonicalStore(
        {
            "employee_keller-bruce": employee_doc(
                "employee_keller-bruce",
                rev="1-survivor",
                person_id="keller-bruce",
                btq_job_ids=["job-a"],
            ),
            "employee_keller_bruce": employee_doc(
                "employee_keller_bruce",
                rev="1-dup",
                person_id="keller-bruce",
                btq_job_ids=["job-b"],
            ),
        }
    )

    report = dedupe_employee_docs(store, token_store_with(tmp_path, "keller-bruce"), dry_run=False)

    assert report.applied_merges == 0
    assert report.deleted_docs == 0
    assert report.skipped_groups == 1
    assert report.errors == [
        {
            "group_key": "name:keller-bruce",
            "message": "refusing to delete protected active-token/auth-resolving docs: employee_keller_bruce",
        }
    ]
    assert store.puts == []
    assert set(store.docs) == {"employee_keller-bruce", "employee_keller_bruce"}


def test_dedupe_refuses_group_with_two_token_matched_docs(tmp_path: Path) -> None:
    store = FakeCanonicalStore(
        {
            "employee_keller-bruce": employee_doc("employee_keller-bruce", rev="1-a", person_id="keller-bruce"),
            "employee_bruce-keller": employee_doc("employee_bruce-keller", rev="1-b", person_id="bruce-keller"),
        }
    )

    report = dedupe_employee_docs(store, token_store_with(tmp_path, "keller-bruce", "bruce-keller"), dry_run=False)

    assert report.applied_merges == 0
    assert report.deleted_docs == 0
    assert report.skipped_groups == 1
    assert report.errors == [
        {"group_key": "name:keller-bruce", "message": "expected exactly one active-token survivor, found 2"}
    ]
    assert store.puts == []


def test_dedupe_single_doc_person_is_untouched(tmp_path: Path) -> None:
    store = FakeCanonicalStore(
        {
            "employee_keller-bruce": employee_doc(
                "employee_keller-bruce",
                rev="1-survivor",
                person_id="keller-bruce",
                btq_job_ids=["job-a"],
            )
        }
    )

    report = dedupe_employee_docs(store, token_store_with(tmp_path, "keller-bruce"), dry_run=False)

    assert report.candidate_groups == 0
    assert report.applied_merges == 0
    assert report.deleted_docs == 0
    assert store.puts == []


def test_btq_dedupe_employee_docs_cli_defaults_to_dry_run_json(tmp_path: Path, monkeypatch) -> None:
    store = FakeCanonicalStore(
        {
            "employee_keller-bruce": employee_doc(
                "employee_keller-bruce",
                rev="1-survivor",
                person_id="keller-bruce",
                btq_job_ids=["job-a"],
            ),
            "employee_keller_bruce": employee_doc("employee_keller_bruce", rev="1-dup", btq_job_ids=["job-b"]),
        }
    )
    token_store = token_store_with(tmp_path, "keller-bruce")
    monkeypatch.setattr(btq_cli.backfill, "get_config", lambda: SimpleNamespace(runtime_root=tmp_path))
    monkeypatch.setattr(btq_cli.backfill.CouchDBEntityStore, "from_env", classmethod(lambda cls: store))

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = btq.run(["dedupe-employee-docs", "--token-db", str(token_store.db_path), "--json"])

    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert payload["dry_run"] is True
    assert payload["planned_merges"] == 1
    assert payload["applied_merges"] == 0
    assert store.puts == []
