from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from btq_vault.entity_types import OPERATOR_ID_GREG
from queue_processor.handlers import _shared, misc
from queue_processor.main import QueueJob, RunContext, process_job, process_voice_memo_note_job
from test_helpers.queue_processor_stores import RecordingVaultStore


class RecordingRmwVaultStore(RecordingVaultStore):
    def __init__(self) -> None:
        super().__init__()
        self.update_doc_calls: list[str] = []

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                return dict(doc)
        return None

    def update_doc(
        self,
        doc_id: str,
        transform: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        *,
        create: Callable[[], dict[str, Any]] | None = None,
        require_existing: bool = True,
        max_conflict_retries: int = 1,
    ) -> dict[str, Any]:
        self.update_doc_calls.append(doc_id)
        current_index = next((index for index, doc in enumerate(self.docs) if doc.get("_id") == doc_id), None)
        current = dict(self.docs[current_index]) if current_index is not None else None
        if current is None:
            if require_existing:
                raise RuntimeError(f"missing required doc: {doc_id}")
            seed = create() if create is not None else None
        else:
            seed = current
        outgoing = transform(seed)
        if outgoing is None:
            if current is None:
                raise RuntimeError(f"no-op has no document: {doc_id}")
            return current
        stored = dict(outgoing)
        if current_index is None:
            self.docs.append(stored)
        else:
            self.docs[current_index] = stored
        return dict(stored)


def context_for(root: Path) -> RunContext:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return RunContext(
        project_root=root,
        vault_root=root / "vault",
        personal_vault_root=root / "personal",
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=False,
        valid_site_ids={"7060"},
        site_id_to_opportunities_dir={},
    )


def write_site(vault: Path, *, job_ids: list[str] | None = None) -> Path:
    job_id_block = ""
    if job_ids:
        job_id_block = "btq_job_ids:\n" + "".join(f"  - {job_id}\n" for job_id in job_ids)
    path = vault / "Accounts" / "Contworks" / "Locations" / "7060 - Continental Metalworks" / "about.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: location\n"
        "site_id: 7060\n"
        "account: Contworks\n"
        "location: Continental Metalworks\n"
        f"{job_id_block}"
        "---\n"
        "# Continental Metalworks\n",
        encoding="utf-8",
    )
    return path


def write_person(vault: Path, filename: str, person_id: str, name: str) -> Path:
    path = vault / "People" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    first, last = name.split(" ", 1)
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"first: {first}\n"
        f"last: {last}\n"
        f"person_id: {person_id}\n"
        "---\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    return path


def canonical_employee_doc_id_for_voice_person_path(path: Path) -> str:
    stem = path.stem.strip()
    if "," in stem:
        last, first = [part.strip() for part in stem.split(",", 1)]
        name = f"{last} {first}".strip()
    else:
        name = stem
    slug = _shared.slugify_issue_component(name).replace("-", "_")
    return f"employee_{slug}"


def voice_job(payload_overrides: dict | None = None, job_id: str = "job-one") -> tuple[Path, QueueJob]:
    payload = {
        "capture_id": "vm-test-1",
        "timestamp": "2026-05-10T17:20:23+00:00",
        "audio_file": "vm-test-1.webm",
        "raw_transcript_path": "/tmp/vm-test-1.webm.whisper.txt",
        "transcript_text": "The hallway looked good.",
        "routing_flag": "site_tagged",
        "site_id": "",
        "site": "",
        "note": "",
        "employees": [],
    }
    if payload_overrides:
        payload.update(payload_overrides)
    job = QueueJob(job_id=job_id, job_type="voice_memo_note", payload=payload, metadata={"capture_id": payload["capture_id"]}, intent={})
    return Path(f"{job_id}.json"), job


def write_process_queue_file(context: RunContext, payload: dict, job_id: str = "job-one") -> Path:
    queue_dir = context.runtime_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_file = queue_dir / f"{job_id}.json"
    queue_file.write_text(
        json.dumps({"job_id": job_id, "job_type": "voice_memo_note", "payload": payload}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return queue_file


class QueueProcessorVoiceMemoNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RecordingRmwVaultStore()
        _shared._VAULT_STORE = self.store

    def tearDown(self) -> None:
        _shared._VAULT_STORE = None

    def seed_site_doc(
        self,
        path: Path,
        *,
        content: str | None = None,
        job_ids: list[str] | None = None,
        capture_ids: list[str] | None = None,
    ) -> None:
        text = path.read_text(encoding="utf-8")
        doc = {
            "_id": _shared.canonical_location_doc_id_for_projection(path, text),
            "type": "location",
            "site_id": "7060",
            "content": _shared.canonical_content_body(text) if content is None else content,
            "btq_job_ids": list(job_ids or []),
            "vault_path": str(path),
        }
        if capture_ids is not None:
            doc["voice_memo_capture_ids"] = list(capture_ids)
        self.store.docs.append(doc)

    def seed_employee_doc(
        self,
        path: Path,
        *,
        content: str | None = None,
        job_ids: list[str] | None = None,
        capture_ids: list[str] | None = None,
    ) -> None:
        text = path.read_text(encoding="utf-8")
        frontmatter, _body, _has_frontmatter = _shared.parse_frontmatter_text(text)
        doc = {
            "_id": canonical_employee_doc_id_for_voice_person_path(path),
            "type": "employee",
            "person_id": str(frontmatter.get("person_id") or "").strip(),
            "name": str(frontmatter.get("name") or "").strip(),
            "content": _shared.canonical_content_body(text) if content is None else content,
            "btq_job_ids": list(job_ids or []),
            "vault_path": str(path),
        }
        if capture_ids is not None:
            doc["voice_memo_capture_ids"] = list(capture_ids)
        self.store.docs.append(doc)

    def test_voice_memo_person_link_uses_canonical_vault_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            person_path = write_person(context.vault_root, "Hutton, Maria.md", "hutton-maria", "Maria Hutton")
            self.seed_employee_doc(person_path)

            self.assertEqual(
                misc.voice_memo_person_link(context, {"slug": "hutton-maria", "name": "Maria Hutton"}),
                "[[People/Hutton, Maria]]",
            )

    def test_voice_memo_person_link_missing_canonical_path_returns_bare_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)

            self.assertEqual(
                misc.voice_memo_person_link(context, {"slug": "hutton-maria", "name": "Maria Hutton"}),
                "Maria Hutton",
            )

    def test_voice_memo_employee_projection_path_uses_canonical_vault_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            person_path = write_person(context.vault_root, "Hutton, Maria.md", "hutton-maria", "Maria Hutton")
            self.seed_employee_doc(person_path)

            self.assertEqual(misc._voice_memo_employee_projection_path(context, "hutton-maria"), person_path.resolve())

    def test_voice_memo_employee_projection_path_missing_canonical_path_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)

            self.assertIsNone(misc._voice_memo_employee_projection_path(context, "hutton-maria"))

    def test_voice_memo_note_writes_to_site_about_when_site_id_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            target = write_site(context.vault_root)
            self.seed_site_doc(target)
            job_path, job = voice_job({"site_id": "7060", "site": "Contworks - Continental Metalworks"})
            queue_file = context.runtime_root / job_path
            queue_file.write_text("{}\n", encoding="utf-8")

            process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            text = target.read_text(encoding="utf-8")
            self.assertIn("### 2026-05-10T17:20:23+00:00 — voice memo", text)
            self.assertIn("source_audio: vm-test-1.webm", text)
            self.assertIn("> The hallway looked good.", text)

    def test_voice_memo_note_without_location_type_still_patches_canonical_site_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            target = write_site(context.vault_root)
            target.write_text(target.read_text(encoding="utf-8").replace("type: location\n", ""), encoding="utf-8")
            self.seed_site_doc(target)
            job_path, job = voice_job({"site_id": "7060", "site": "Contworks - Continental Metalworks"})
            queue_file = context.runtime_root / job_path
            queue_file.write_text("{}\n", encoding="utf-8")

            process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            canonical_doc = next(doc for doc in self.store.docs if doc["_id"] == "location_7060")
            self.assertIn("### 2026-05-10T17:20:23+00:00 — voice memo", canonical_doc["content"])
            self.assertEqual(canonical_doc["content"], _shared.canonical_content_body(target.read_text(encoding="utf-8")))

    def test_voice_memo_note_writes_to_each_employee_when_no_site(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            first = write_person(context.vault_root, "Hutton, Maria.md", "hutton-maria", "Maria Hutton")
            second = write_person(context.vault_root, "Smith, Alex.md", "smith-alex", "Alex Smith")
            self.seed_employee_doc(first)
            self.seed_employee_doc(second)
            job_path, job = voice_job(
                {
                    "routing_flag": "employee_tagged",
                    "employees": [
                        {"slug": "hutton-maria", "name": "Maria Hutton"},
                        {"slug": "smith-alex", "name": "Alex Smith"},
                    ],
                }
            )
            queue_file = context.runtime_root / job_path
            queue_file.write_text("{}\n", encoding="utf-8")

            process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            self.assertIn("voice memo", first.read_text(encoding="utf-8"))
            self.assertIn("voice memo", second.read_text(encoding="utf-8"))

    def test_voice_memo_note_writes_to_inbox_when_general(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            job_path, job = voice_job({"routing_flag": "general"})
            queue_file = context.runtime_root / job_path
            queue_file.write_text("{}\n", encoding="utf-8")

            process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            target = context.vault_root / "Inbox" / "voice-memo-general.md"
            self.assertTrue(target.exists())
            self.assertIn("The hallway looked good.", target.read_text(encoding="utf-8"))

    def test_frontmatter_merges_voice_memo_capture_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            target = write_site(context.vault_root)
            self.seed_site_doc(target)
            for capture_id, job_id in (("vm-test-1", "job-one"), ("vm-test-2", "job-two"), ("vm-test-2", "job-three")):
                job_path, job = voice_job({"site_id": "7060", "capture_id": capture_id}, job_id=job_id)
                queue_file = context.runtime_root / job_path
                queue_file.write_text("{}\n", encoding="utf-8")
                process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            text = target.read_text(encoding="utf-8")
            self.assertIn("voice_memo_capture_ids:", text)
            self.assertIn("  - vm-test-1", text)
            self.assertEqual(text.count("  - vm-test-2"), 1)

    def test_voice_memo_note_site_appends_to_canonical_location_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            target = write_site(context.vault_root)
            self.seed_site_doc(target)
            job_path, job = voice_job({"site_id": "7060", "site": "Contworks - Continental Metalworks"})
            queue_file = context.runtime_root / job_path
            queue_file.write_text("{}\n", encoding="utf-8")

            process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            canonical_doc = self.store.get_optional("location_7060")
            self.assertIsNotNone(canonical_doc)
            assert canonical_doc is not None
            self.assertIn("### 2026-05-10T17:20:23+00:00 — voice memo", canonical_doc["content"])
            self.assertIn("> The hallway looked good.", canonical_doc["content"])
            self.assertEqual(canonical_doc["voice_memo_capture_ids"], ["vm-test-1"])

    def test_voice_memo_note_employees_append_to_each_canonical_employee(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            first = write_person(context.vault_root, "Hutton, Maria.md", "hutton-maria", "Maria Hutton")
            second = write_person(context.vault_root, "Smith, Alex.md", "smith-alex", "Alex Smith")
            self.seed_employee_doc(first)
            self.seed_employee_doc(second)
            job_path, job = voice_job(
                {
                    "routing_flag": "employee_tagged",
                    "employees": [
                        {"slug": "hutton-maria", "name": "Maria Hutton"},
                        {"slug": "smith-alex", "name": "Alex Smith"},
                    ],
                }
            )
            queue_file = context.runtime_root / job_path
            queue_file.write_text("{}\n", encoding="utf-8")

            process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            first_doc = self.store.get_optional("employee_hutton_maria")
            second_doc = self.store.get_optional("employee_smith_alex")
            self.assertIsNotNone(first_doc)
            self.assertIsNotNone(second_doc)
            assert first_doc is not None
            assert second_doc is not None
            self.assertIn("voice memo", first_doc["content"])
            self.assertIn("voice memo", second_doc["content"])
            self.assertEqual(first_doc["voice_memo_capture_ids"], ["vm-test-1"])
            self.assertEqual(second_doc["voice_memo_capture_ids"], ["vm-test-1"])

    def test_voice_memo_note_general_inbox_creates_canonical_note_doc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            job_path, job = voice_job({"routing_flag": "general"})
            queue_file = context.runtime_root / job_path
            queue_file.write_text("{}\n", encoding="utf-8")

            process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            canonical_doc = self.store.get_optional("note_voice_memo_general")
            self.assertIsNotNone(canonical_doc)
            assert canonical_doc is not None
            self.assertEqual(canonical_doc["type"], "note")
            self.assertEqual(canonical_doc["scope"], "operational_inbox")
            self.assertEqual(canonical_doc["operator"], OPERATOR_ID_GREG)
            self.assertIn("The hallway looked good.", canonical_doc["content"])
            self.assertEqual(canonical_doc["voice_memo_capture_ids"], ["vm-test-1"])

    def test_voice_memo_note_capture_id_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            target = write_site(context.vault_root)
            self.seed_site_doc(target)
            for job_id in ("job-one", "job-two"):
                job_path, job = voice_job({"site_id": "7060", "capture_id": "vm-test-1"}, job_id=job_id)
                queue_file = context.runtime_root / job_path
                queue_file.write_text("{}\n", encoding="utf-8")
                process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            canonical_doc = self.store.get_optional("location_7060")
            self.assertIsNotNone(canonical_doc)
            assert canonical_doc is not None
            self.assertEqual(canonical_doc["voice_memo_capture_ids"], ["vm-test-1"])

    def test_voice_memo_note_stale_markdown_does_not_cause_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            target = write_site(context.vault_root, job_ids=["job-one"])
            self.seed_site_doc(target, job_ids=[])
            job_path, job = voice_job({"site_id": "7060", "site": "Contworks - Continental Metalworks"})
            queue_file = context.runtime_root / job_path
            queue_file.write_text("{}\n", encoding="utf-8")

            process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            canonical_doc = self.store.get_optional("location_7060")
            self.assertIsNotNone(canonical_doc)
            assert canonical_doc is not None
            self.assertIn("The hallway looked good.", canonical_doc["content"])
            self.assertEqual(canonical_doc["btq_job_ids"], ["job-one"])
            self.assertEqual(self.store.update_doc_calls, ["location_7060"])

    def test_voice_memo_note_missing_site_doc_fails_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            write_site(context.vault_root)
            _job_path, job = voice_job({"site_id": "7060", "site": "Contworks - Continental Metalworks"})
            queue_file = write_process_queue_file(context, job.payload, "job-one")
            processed_dir = context.runtime_root / "processed"
            failed_dir = context.runtime_root / "failed"

            process_job(queue_file, context, processed_dir, failed_dir)

            self.assertTrue((failed_dir / queue_file.name).exists())
            self.assertFalse((processed_dir / queue_file.name).exists())
            self.assertIn("canonical couchdb write failed job_type=voice_memo_note", context.log_path.read_text(encoding="utf-8"))

    def test_voice_memo_note_unknown_employee_fails_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            _job_path, job = voice_job(
                {
                    "routing_flag": "employee_tagged",
                    "employees": [{"slug": "unknown-employee", "name": "Unknown Employee"}],
                }
            )
            queue_file = write_process_queue_file(context, job.payload, "job-one")
            processed_dir = context.runtime_root / "processed"
            failed_dir = context.runtime_root / "failed"

            process_job(queue_file, context, processed_dir, failed_dir)

            self.assertEqual(self.store.update_doc_calls, [])
            self.assertTrue((failed_dir / queue_file.name).exists())
            self.assertFalse((processed_dir / queue_file.name).exists())
            self.assertIn("Could not resolve canonical employee target: unknown-employee", context.log_path.read_text(encoding="utf-8"))

    def test_voice_memo_note_replay_skips_without_double_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            target = write_site(context.vault_root)
            existing_content = "# Continental Metalworks\n\n### 2026-05-10T17:20:23+00:00 — voice memo\n\nAlready applied.\n"
            self.seed_site_doc(target, content=existing_content, job_ids=["job-one"], capture_ids=["vm-test-1"])
            job_path, job = voice_job({"site_id": "7060", "site": "Contworks - Continental Metalworks"})
            queue_file = context.runtime_root / job_path
            queue_file.write_text("{}\n", encoding="utf-8")

            process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            canonical_doc = self.store.get_optional("location_7060")
            self.assertIsNotNone(canonical_doc)
            assert canonical_doc is not None
            self.assertEqual(canonical_doc["content"], existing_content)
            self.assertEqual(self.store.update_doc_calls, [])
            self.assertTrue((context.runtime_root / "processed" / queue_file.name).exists())

    def test_voice_memo_note_succeeds_without_markdown_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = context_for(root)
            self.store.docs.append({
                "_id": "location_7060",
                "type": "location",
                "site_id": "7060",
                "content": "# Continental Metalworks\n",
                "btq_job_ids": [],
            })
            job_path, job = voice_job({"site_id": "7060", "site": "Contworks - Continental Metalworks"})
            queue_file = context.runtime_root / job_path
            queue_file.write_text("{}\n", encoding="utf-8")

            process_voice_memo_note_job(queue_file, job, context, context.runtime_root / "processed")

            canonical_doc = self.store.get_optional("location_7060")
            self.assertIsNotNone(canonical_doc)
            assert canonical_doc is not None
            self.assertIn("The hallway looked good.", canonical_doc["content"])
            self.assertFalse((context.vault_root / "Accounts").exists())
            self.assertTrue((context.runtime_root / "processed" / queue_file.name).exists())


if __name__ == "__main__":
    unittest.main()
