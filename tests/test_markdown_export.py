from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import btq
import btq_vault.markdown_export as markdown_export
from btq_vault.couch_store import CouchDBEntityStore
from btq_vault.markdown_export import export_all, render_entity_markdown

from test_queue_processor_dual_write import (
    employee_doc,
    equipment_doc,
    personnel_event_doc,
    site_issue_doc,
    supply_doc,
    visit_doc,
)


class RecordingVaultStore:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.get_optional_calls: list[str] = []

    def iter_by_type(self, type_name: str):
        for doc in self.docs:
            if doc.get("type") == type_name:
                yield doc

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        self.get_optional_calls.append(doc_id)
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                return dict(doc)
        return None


def test_render_entity_markdown_returns_path_and_text_per_entity_type() -> None:
    visit = dict(visit_doc())
    visit["account"] = "Summitsteel"
    docs = [employee_doc(), visit, site_issue_doc(), supply_doc(), equipment_doc(), personnel_event_doc()]

    rendered = [render_entity_markdown(doc) for doc in docs]

    assert [path for path, _text in rendered if path is not None] == [
        Path("People/Dalton, Eric Daniel.md"),
        Path("Accounts/Summitsteel/Locations/7050 - Summit Wire/Visits/2026-05-31.md"),
        Path("Accounts/Summitsteel/Locations/7050 - Summit Wire/Issues/iss_summit_drain__restroom-drain-backup.md"),
        Path("Accounts/Summitsteel/Locations/7050 - Summit Wire/Supplies/sup_summit_brightwash__brightwash-cleaner.md"),
        Path("Accounts/Summitsteel/Locations/7050 - Summit Wire/Equipment/eqr_summit_vacuum__vacuum.md"),
        Path("People/Dalton, Eric Daniel/Events/evt_training__completed-floor-care-training.md"),
    ]
    assert all(text.startswith("---\n") for _path, text in rendered if text is not None)


def test_export_all_is_idempotent_on_unchanged_projection(tmp_path: Path) -> None:
    store = RecordingVaultStore([site_issue_doc(), supply_doc()])

    first = export_all(store, tmp_path)
    second = export_all(store, tmp_path)

    assert first.seen == 2
    assert first.written == 2
    assert first.unchanged == 0
    assert second.seen == 2
    assert second.written == 0
    assert second.unchanged == 2
    assert not second.errors


def test_export_all_dry_run_writes_nothing(tmp_path: Path) -> None:
    store = RecordingVaultStore([site_issue_doc()])

    report = export_all(store, tmp_path, dry_run=True)

    assert report.seen == 1
    assert report.written == 0
    assert report.would_write == 1
    assert not list(tmp_path.rglob("*.md"))


def test_markdown_export_enriches_visit_path_from_canonical_location_doc(tmp_path: Path) -> None:
    visit = dict(visit_doc())
    visit.pop("vault_path", None)
    visit.pop("account", None)
    visit["site"] = "Stale Site Name"
    store = RecordingVaultStore(
        [
            {
                "_id": "location_7050",
                "type": "location",
                "site_id": "7050",
                "account": "Summitsteel",
                "location": "Summit Wire",
            },
            visit,
        ]
    )

    report = export_all(store, tmp_path)

    assert report.errors == []
    assert report.written == 1
    assert store.get_optional_calls == ["location_7050"]
    target = tmp_path / "Accounts/Summitsteel/Locations/7050 - Summit Wire/Visits/2026-05-31.md"
    assert target.exists()


def test_markdown_export_normalizes_quoted_site_id_in_site_child_folder(tmp_path: Path) -> None:
    issue = dict(site_issue_doc())
    issue.pop("vault_path")
    issue["site_id"] = ' "789" '
    issue["account"] = "Summitsteel"
    issue["site_name"] = "Summit Wire"
    store = RecordingVaultStore([issue])

    report = export_all(store, tmp_path)

    assert report.errors == []
    assert report.written == 1
    assert store.get_optional_calls == ["location_789"]
    target = tmp_path / "Accounts/Summitsteel/Locations/789 - Summit Wire/Issues/iss_summit_drain__restroom-drain-backup.md"
    assert target.exists()


def test_markdown_export_updates_existing_personnel_event_in_place_by_event_id(tmp_path: Path) -> None:
    # The event filename embeds a slug of the (mutable) summary. A re-export after the summary
    # changes must UPDATE the existing file in place — located by the stable event_id, not
    # vault_path — rather than orphan it under a new slug. (Original pre-C3 behavior.)
    event = dict(personnel_event_doc())
    event.pop("vault_path", None)
    old_target = tmp_path / "People/Dalton, Eric Daniel/Events/evt_training__older-existing-title.md"
    old_target.parent.mkdir(parents=True)
    old_target.write_text("---\ntype: personnel_event\n---\n", encoding="utf-8")
    store = RecordingVaultStore([event])

    report = export_all(store, tmp_path)

    assert report.errors == []
    assert report.written == 1
    # The existing file is reused (updated), and NO new-slug file is created.
    new_slug_target = tmp_path / "People/Dalton, Eric Daniel/Events/evt_training__completed-floor-care-training.md"
    assert not new_slug_target.exists()
    assert old_target.exists()
    event_files = sorted((tmp_path / "People/Dalton, Eric Daniel/Events").glob("*.md"))
    assert len(event_files) == 1


def test_markdown_export_no_longer_imports_resolver_or_reads_vault_path() -> None:
    source = inspect.getsource(markdown_export)

    assert "resolve_site_vault_path" not in source
    assert 'doc.get("vault_path")' not in source
    # NOTE: a single filesystem glob remains by design — `_existing_personnel_event_path` finds an
    # already-projected personnel-event file by its stable event_id (slug-agnostic) for in-place
    # update. It does NOT read vault_path or resolve a site path, so it is not asserted against.


def test_markdown_export_cli_dry_run_writes_nothing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    store = RecordingVaultStore([site_issue_doc()])
    monkeypatch.setattr(CouchDBEntityStore, "from_env", classmethod(lambda cls: store))

    exit_code = btq.run(["markdown-export", "--vault-root", str(tmp_path), "--dry-run", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["seen"] == 1
    assert output["written"] == 0
    assert output["would_write"] == 1
    assert not list(tmp_path.rglob("*.md"))


def test_markdown_export_cli_real_run_writes_files(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    store = RecordingVaultStore([site_issue_doc()])
    monkeypatch.setattr(CouchDBEntityStore, "from_env", classmethod(lambda cls: store))

    exit_code = btq.run(["markdown-export", "--vault-root", str(tmp_path), "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["written"] == 1
    target = tmp_path / "Accounts/Summitsteel/Locations/7050 - Summit Wire/Issues/iss_summit_drain__restroom-drain-backup.md"
    assert target.read_text(encoding="utf-8").startswith("---\ntype: site_issue\n")
