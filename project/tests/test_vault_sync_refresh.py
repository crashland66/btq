from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from vault_sync import refresh
from vault_sync.parsing import VaultParsingError, person_id_for, read_frontmatter, site_id_for


class FakeCouchDBClient:
    def __init__(self) -> None:
        self.databases: dict[str, dict[str, dict]] = {"btq_sites": {}, "btq_people": {}}
        self.puts: list[tuple[str, dict]] = []

    def get_doc(self, database: str, doc_id: str) -> dict | None:
        doc = self.databases.setdefault(database, {}).get(doc_id)
        return dict(doc) if doc is not None else None

    def put_doc(self, database: str, doc: dict) -> dict:
        self.puts.append((database, dict(doc)))
        db = self.databases.setdefault(database, {})
        doc_id = doc["_id"]
        if doc.get("_deleted"):
            current = db.get(doc_id, {})
            db[doc_id] = {"_id": doc_id, "_rev": next_rev(current), "_deleted": True}
        else:
            stored = dict(doc)
            stored["_rev"] = next_rev(db.get(doc_id, {}))
            db[doc_id] = stored
        return {"ok": True, "id": doc_id, "rev": db[doc_id]["_rev"]}

    def list_vault_docs(self, database: str) -> dict[str, dict]:
        return {
            doc_id: dict(doc)
            for doc_id, doc in self.databases.setdefault(database, {}).items()
            if doc.get("synced_from_vault") is True and not doc.get("_deleted")
        }


def next_rev(current: dict) -> str:
    rev = str(current.get("_rev", "0-empty")).split("-", 1)[0]
    return f"{int(rev) + 1}-fake"


def args_for(vault_root: Path, client: FakeCouchDBClient, **overrides):
    values = {
        "vault_root": vault_root,
        "sites_only": False,
        "people_only": False,
        "no_prune": False,
        "dry_run": False,
        "json": False,
        "log_path": None,
        "couchdb_client": client,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def write_site(root: Path, account: str = "Contworks", job: str = "7060") -> Path:
    path = root / "Accounts" / account / "Locations" / f"{job} - Continental Metalworks" / "about.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
account: {account}
location: Continental Metalworks
job: {job}
customer_email: customer@example.com
type: location
status: active
---
# Site
""",
        encoding="utf-8",
    )
    return path


def write_person(root: Path, name: str = "Hutton, Maria") -> Path:
    path = root / "People" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
first: Maria
last: Hutton
phone: 8145550101
ehub_id: "5272"
email: sissy@example.com
job: 7050
type: employee
status: active
---
# Person
""",
        encoding="utf-8",
    )
    return path


def test_parse_frontmatter_site(tmp_path: Path) -> None:
    path = write_site(tmp_path)
    fm = read_frontmatter(path)
    assert fm is not None
    assert fm["type"] == "location"
    assert fm["job"] == 7060


def test_parse_frontmatter_person(tmp_path: Path) -> None:
    path = write_person(tmp_path)
    fm = read_frontmatter(path)
    assert fm is not None
    assert fm["type"] == "employee"
    assert fm["first"] == "Maria"


def test_parse_frontmatter_no_block_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("# No frontmatter\n", encoding="utf-8")
    assert read_frontmatter(path) is None


def test_parse_frontmatter_malformed_yaml_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("---\nkey: [unterminated\n---\n", encoding="utf-8")
    assert read_frontmatter(path) is None


def test_site_id_for_uses_job() -> None:
    assert site_id_for({"job": 7060}) == "7060"


def test_site_id_for_rejects_missing_job() -> None:
    with pytest.raises(VaultParsingError):
        site_id_for({})


def test_person_id_for_uses_slug_even_when_ehub_id_present() -> None:
    assert person_id_for({"ehub_id": "5272", "first": "Maria", "last": "Hutton"}) == "hutton-maria"


def test_person_id_for_raises_when_name_missing() -> None:
    with pytest.raises(VaultParsingError):
        person_id_for({"first": "", "last": "Hutton"})
    with pytest.raises(VaultParsingError):
        person_id_for({"first": "Maria", "last": ""})


def test_person_id_for_handles_diacritics() -> None:
    assert person_id_for({"first": "José", "last": "Caraballo Ortiz"}) == "caraballo-ortiz-jose"


def test_refresh_run_doc_body_keeps_ehub_id(tmp_path: Path) -> None:
    write_person(tmp_path)
    client = FakeCouchDBClient()
    report = refresh.refresh(args_for(tmp_path, client, people_only=True))
    doc = client.databases["btq_people"]["hutton-maria"]
    assert report.people.created == 1
    assert doc["_id"] == "hutton-maria"
    assert doc["ehub_id"] == "5272"


def test_refresh_run_idempotent(tmp_path: Path) -> None:
    write_site(tmp_path)
    write_person(tmp_path)
    client = FakeCouchDBClient()
    first = refresh.refresh(args_for(tmp_path, client))
    second = refresh.refresh(args_for(tmp_path, client))
    assert first.sites.created == 1
    assert first.people.created == 1
    assert second.sites.created == 0
    assert second.sites.updated == 0
    assert second.sites.deleted == 0
    assert second.people.created == 0
    assert second.people.updated == 0
    assert second.people.deleted == 0
    assert second.sites.skipped == 1
    assert second.people.skipped == 1


def test_refresh_run_detects_collision(tmp_path: Path) -> None:
    write_site(tmp_path, account="A", job="7060")
    write_site(tmp_path, account="B", job="7060")
    with pytest.raises(refresh.RefreshError):
        refresh.refresh(args_for(tmp_path, FakeCouchDBClient()))


def test_refresh_run_prune_marks_orphaned_docs_deleted(tmp_path: Path) -> None:
    path = write_site(tmp_path)
    client = FakeCouchDBClient()
    refresh.refresh(args_for(tmp_path, client))
    path.unlink()
    report = refresh.refresh(args_for(tmp_path, client))
    assert report.sites.deleted == 1
    assert client.databases["btq_sites"]["7060"]["_deleted"] is True


def test_refresh_run_no_prune_skips_deletion(tmp_path: Path) -> None:
    path = write_site(tmp_path)
    client = FakeCouchDBClient()
    refresh.refresh(args_for(tmp_path, client))
    path.unlink()
    report = refresh.refresh(args_for(tmp_path, client, no_prune=True))
    assert report.sites.deleted == 0
    assert client.databases["btq_sites"]["7060"].get("_deleted") is not True
