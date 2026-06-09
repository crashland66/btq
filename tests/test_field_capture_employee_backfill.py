from __future__ import annotations

from pathlib import Path
from typing import Any

from field_capture.backfill_employees import backfill_all_people_employees, backfill_field_capture_employees, people_matches
from field_capture.person_slugs import employee_slug_candidates
from token_store import TokenStore


class FakeCanonicalStore:
    def __init__(self, docs: dict[str, dict[str, Any]] | None = None) -> None:
        self.docs = {doc_id: dict(doc) for doc_id, doc in (docs or {}).items()}
        self.writes: list[dict[str, Any]] = []

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        doc = self.docs.get(doc_id)
        return dict(doc) if doc is not None else None

    def put_with_rev(self, doc: dict[str, Any], *, expected_rev: str | None) -> dict[str, Any]:
        assert expected_rev is None
        doc_id = str(doc["_id"])
        if doc_id in self.docs:
            raise AssertionError(f"unexpected overwrite: {doc_id}")
        stored = dict(doc)
        self.docs[doc_id] = stored
        self.writes.append(stored)
        return stored

    def find_employee_docs(self) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self.docs.values() if doc.get("type") == "employee"]


class FailingEmployeeScanStore(FakeCanonicalStore):
    def find_employee_docs(self) -> list[dict[str, Any]]:
        raise RuntimeError("employee scan unavailable")


def write_person(vault_root: Path, filename: str, frontmatter: str, body: str = "Crew note.\n") -> Path:
    path = vault_root / "People" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return path


def test_backfill_creates_missing_employee_docs_from_active_tokens_and_people_files(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_person(
        vault_root,
        "Keller, Bruce.md",
        "first: Bruce\nlast: Keller\npreferred_name: Bruce\njob: 7050\nadditional_jobs: [7060]\nstatus: active\n",
    )
    write_person(vault_root, "Worker, TokenScoped.md", "first: TokenScoped\nlast: Worker\nstatus: active\n")
    token_store = TokenStore(tmp_path / "field_capture_tokens.sqlite3")
    token_store.create_token("keller-bruce", label="Bruce phone", site_ids=["9999"])
    token_store.create_token("worker-tokenscoped", label="Token scoped", site_ids=["7040"])
    revoked = token_store.create_token("revoked-person", label="Revoked")
    token_store.revoke_token(revoked.record.token_id)
    store = FakeCanonicalStore()

    report = backfill_field_capture_employees(store, token_store, vault_root, dry_run=False)

    assert report.active_tokens == 2
    assert report.distinct_people == 2
    assert report.created == 2
    assert store.docs["employee_keller-bruce"]["type"] == "employee"
    assert store.docs["employee_keller-bruce"]["person_id"] == "keller-bruce"
    assert store.docs["employee_keller-bruce"]["name"] == "Bruce Keller"
    assert store.docs["employee_keller-bruce"]["site_ids"] == ["7050", "7060"]
    assert "vault_path" not in store.docs["employee_keller-bruce"]
    assert store.docs["employee_worker-tokenscoped"]["site_ids"] == ["7040"]
    assert "employee_revoked-person" not in store.docs


def test_backfill_matches_first_last_token_person_ids_to_people_files(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    people = [
        ("Russo, Frank.md", "Frank", "Russo", "7050", ["7060"], "frank-russo"),
        ("Barnes, Kevin.md", "Kevin", "Barnes", "7040", [], "kevin-barnes"),
        ("Malcolm, Diana.md", "Diana", "Malcolm", "7060", ["7050"], "diana-malcolm"),
        ("Hill, Tasha.md", "Tasha", "Hill", "7092", [], "tasha-hill"),
        ("Mason, Dylan.md", "Dylan", "Mason", "7093", ["7040"], "dylan-mason"),
    ]
    token_store = TokenStore(tmp_path / "field_capture_tokens.sqlite3")
    for filename, first, last, job, additional_jobs, person_id in people:
        additional_jobs_line = f"additional_jobs: {additional_jobs}\n" if additional_jobs else ""
        write_person(
            vault_root,
            filename,
            f"first: {first}\nlast: {last}\njob: {job}\n{additional_jobs_line}status: active\n",
        )
        token_store.create_token(person_id, label=f"{first} phone", site_ids=["token-site"])
    store = FakeCanonicalStore()

    report = backfill_field_capture_employees(store, token_store, vault_root, dry_run=False)

    assert report.created == 5
    assert report.missing_people == []
    assert report.ambiguous == []
    for filename, first, last, job, additional_jobs, person_id in people:
        doc_id = f"employee_{person_id}"
        assert store.docs[doc_id]["person_id"] == person_id
        assert store.docs[doc_id]["name"] == f"{first} {last}"
        assert "vault_path" not in store.docs[doc_id]
        assert store.docs[doc_id]["site_ids"] == [job, *additional_jobs]


def test_shared_slug_helper_matches_backfill_both_order_behavior() -> None:
    slugs = employee_slug_candidates(first="Frank", last="Russo", filename_stem="Russo, Frank J.")

    assert {"russo-frank", "frank-russo", "russo-frank-j", "frank-j-russo"} <= slugs


def test_backfill_skips_existing_docs_without_overwrite(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_person(vault_root, "Carver, Damon.md", "first: Damon\nlast: Carver\njob: 7050\nstatus: active\n")
    token_store = TokenStore(tmp_path / "field_capture_tokens.sqlite3")
    token_store.create_token("carver-damon", label="Damon phone", site_ids=["7060"])
    existing = {
        "_id": "employee_carver-damon",
        "type": "employee",
        "person_id": "carver-damon",
        "name": "Existing Employee",
        "site_ids": ["existing"],
    }
    store = FakeCanonicalStore({"employee_carver-damon": existing})

    report = backfill_field_capture_employees(store, token_store, vault_root, dry_run=False)

    assert report.created == 0
    assert report.skipped_existing == 1
    assert store.writes == []
    assert store.docs["employee_carver-damon"]["name"] == "Existing Employee"


def test_backfill_reports_ambiguous_people_match_without_creating_doc(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_person(vault_root, "Worker, Sam.md", "first: Sam\nlast: Worker\njob: 7050\nstatus: active\n")
    write_person(vault_root, "Sam, Worker.md", "first: Worker\nlast: Sam\njob: 7060\nstatus: active\n")
    token_store = TokenStore(tmp_path / "field_capture_tokens.sqlite3")
    token_store.create_token("sam-worker", label="Sam phone", site_ids=["7040"])
    store = FakeCanonicalStore()

    report = backfill_field_capture_employees(store, token_store, vault_root, dry_run=False)

    assert report.created == 0
    assert report.would_create == 0
    assert report.missing_people == []
    assert report.ambiguous == [
        {
            "person_id": "sam-worker",
            "labels": ["Sam phone"],
            "site_ids": ["7040"],
            "matches": ["People/Sam, Worker.md", "People/Worker, Sam.md"],
        }
    ]
    assert report.errors == [
        {
            "id": "employee_sam-worker",
            "message": "ambiguous People match for sam-worker: People/Sam, Worker.md, People/Worker, Sam.md",
        }
    ]
    assert store.docs == {}
    assert store.writes == []


def test_backfill_dry_run_reports_would_create_without_writing(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_person(vault_root, "Jones, Alice.md", "first: Alice\nlast: Jones\njob: 7050\nstatus: active\n")
    token_store = TokenStore(tmp_path / "field_capture_tokens.sqlite3")
    token_store.create_token("jones-alice", label="Alice phone", site_ids=["7060"])
    store = FakeCanonicalStore()

    report = backfill_field_capture_employees(store, token_store, vault_root, dry_run=True)

    assert report.created == 0
    assert report.would_create == 1
    assert report.would_create_docs[0]["_id"] == "employee_jones-alice"
    assert store.docs == {}
    assert store.writes == []


def test_backfill_reports_missing_people_without_fabricating_docs(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    token_store = TokenStore(tmp_path / "field_capture_tokens.sqlite3")
    token_store.create_token("absent-worker", label="Absent phone", site_ids=["7050"])
    store = FakeCanonicalStore()

    report = backfill_field_capture_employees(store, token_store, vault_root, dry_run=False)

    assert report.created == 0
    assert report.missing_people == [{"person_id": "absent-worker", "labels": ["Absent phone"], "site_ids": ["7050"]}]
    assert store.docs == {}


def test_all_people_backfill_creates_non_token_employee_docs_add_only(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_person(
        vault_root,
        "Mills, Carol.md",
        "first: Carol\nlast: Mills\npreferred_name: \"\"\njob: 7040\nstatus: active\nrole: lead\n",
    )
    write_person(vault_root, "Keller, Bruce.md", "first: Bruce\nlast: Keller\njob: 7050\nstatus: active\n")
    store = FakeCanonicalStore(
        {
            "employee_keller-bruce": {
                "_id": "employee_keller-bruce",
                "type": "employee",
                "person_id": "keller-bruce",
                "name": "Existing Bruce",
            }
        }
    )

    report = backfill_all_people_employees(store, vault_root, dry_run=False)

    assert report.created == 1
    assert report.skipped_existing == 1
    deb = store.docs["employee_mills-carol"]
    assert deb["type"] == "employee"
    assert deb["person_id"] == "mills-carol"
    assert deb["name"] == "Carol Mills"
    assert deb["site_ids"] == ["7040"]
    assert deb["preferred_name"] == ""
    assert deb["first"] == "Carol"
    assert deb["last"] == "Mills"
    assert deb["status"] == "active"
    assert deb["role"] == "lead"
    assert "vault_path" not in deb


def test_backfill_logs_and_counts_skips_on_bad_doc(tmp_path: Path, caplog) -> None:
    vault_root = tmp_path / "vault"
    write_person(vault_root, "Jones, Alice.md", "first: Alice\nlast: Jones\njob: 7050\nstatus: active\n")
    store = FailingEmployeeScanStore()

    report = backfill_all_people_employees(store, vault_root, dry_run=False)

    assert report.created == 0
    assert report.errors == [{"id": "find_employee_docs", "message": "employee scan unavailable"}]
    assert "skipped existing employee docs scan: employee scan unavailable" in caplog.text
    assert store.writes == []


def test_durable_path_does_not_silently_drop(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_person(vault_root, "Jones, Alice.md", "first: Alice\nlast: Jones\njob: 7050\nstatus: active\n")
    store = FailingEmployeeScanStore()

    report = backfill_all_people_employees(store, vault_root, dry_run=True)

    assert report.errors
    assert report.would_create == 0
    assert report.as_dict()["errors"] == [{"id": "find_employee_docs", "message": "employee scan unavailable"}]


def test_people_matches_and_all_people_backfill_ignore_nested_event_notes(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_person(vault_root, "Worker, Sam.md", "first: Sam\nlast: Worker\njob: 7050\nstatus: active\n")
    write_person(
        vault_root,
        "Worker, Sam/Events/evt_abc123__status-update.md",
        "status: active\n",
        body="Personnel event note.\n",
    )
    store = FakeCanonicalStore()

    match_index = people_matches(vault_root)
    matched_paths = {match.relative_path for match in match_index.matches.values()}
    report = backfill_all_people_employees(store, vault_root, dry_run=True)
    would_create_ids = [doc["_id"] for doc in report.would_create_docs]

    assert matched_paths == {"People/Worker, Sam.md"}
    assert match_index.ambiguous == {}
    assert report.distinct_people == 1
    assert would_create_ids == ["employee_worker-sam"]
    assert not any(doc_id.startswith("employee_evt-") for doc_id in would_create_ids)
    assert store.docs == {}
    assert store.writes == []


def test_people_matches_and_all_people_backfill_skip_dashboard_people_files(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_person(vault_root, "Worker, Sam.md", "first: Sam\nlast: Worker\njob: 7050\nstatus: active\n")
    write_person(vault_root, "about.md", "type: dashboard\n")
    write_person(vault_root, "dashboard.md", "type: dashboard\n")
    store = FakeCanonicalStore()

    match_index = people_matches(vault_root)
    matched_paths = {match.relative_path for match in match_index.matches.values()}
    report = backfill_all_people_employees(store, vault_root, dry_run=True)
    would_create_ids = [doc["_id"] for doc in report.would_create_docs]

    assert matched_paths == {"People/Worker, Sam.md"}
    assert match_index.ambiguous == {}
    assert report.distinct_people == 1
    assert would_create_ids == ["employee_worker-sam"]
    assert "employee_about" not in would_create_ids
    assert "employee_dashboard" not in would_create_ids
    assert report.unkeyable_people == []
    assert store.docs == {}
    assert store.writes == []


def test_all_people_backfill_dry_run_writes_nothing_and_reports_unkeyable(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_person(vault_root, "Jones, Alice.md", "first: Alice\nlast: Jones\njob: 7050\nstatus: active\n")
    path = vault_root / "People" / "!!!.md"
    path.write_text("---\nfirst: Mystery\nstatus: active\n---\n\nNo usable name.\n", encoding="utf-8")
    store = FakeCanonicalStore()

    report = backfill_all_people_employees(store, vault_root, dry_run=True)

    assert report.created == 0
    assert report.would_create == 1
    assert report.would_create_docs[0]["_id"] == "employee_jones-alice"
    assert report.unkeyable_people == [{"path": "People/!!!.md", "message": "could not derive person_id"}]
    assert store.docs == {}
    assert store.writes == []
