from __future__ import annotations

import json
import os
import types
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "btq-migrate-malformed-scalars"


def load_script_module() -> types.ModuleType:
    text = SCRIPT.read_text(encoding="utf-8")
    source = text.split("<<'PYEOF'\n", 1)[1].rsplit("\nPYEOF", 1)[0]
    os.environ.setdefault("BTQ_USER", "admin")
    os.environ.setdefault("BTQ_PASS", "secret")
    os.environ.setdefault("BTQ_URL", "http://couchdb.test")
    module = types.ModuleType("btq_migrate_malformed_scalars")
    module.__dict__["__name__"] = module.__name__
    exec(compile(source, str(SCRIPT), "exec"), module.__dict__)
    return module


def discover(tmp_path: Path, doc: dict[str, Any]) -> dict[str, Any]:
    module = load_script_module()
    output = tmp_path / "proposed_fixes.json"

    def fake_request(method: str, path: str, data: Any | None = None) -> dict[str, Any]:
        assert method == "GET"
        assert path == "/btq_vault/_all_docs?include_docs=true"
        assert data is None
        return {"rows": [{"id": "doc-1", "doc": {"_id": "doc-1", **doc}}]}

    module.couch_request = fake_request
    assert module.discover(["btq_vault"], str(output)) == 0
    return json.loads(output.read_text(encoding="utf-8"))


def only_fix(proposal: dict[str, Any]) -> dict[str, Any]:
    fixes = proposal["fixes"]
    assert len(fixes) == 1
    return fixes[0]


def test_discover_finds_list_of_one_string(tmp_path: Path) -> None:
    fix = only_fix(discover(tmp_path, {"job": ["7060"], "_rev": "1-x"}))

    assert fix["proposed_value"] == "7060"
    assert fix["auto_fix"] is True
    assert fix["rationale"] == "list_of_one_lift"


def test_discover_finds_json_quoted_string(tmp_path: Path) -> None:
    fix = only_fix(discover(tmp_path, {"site_id": '"7050"', "_rev": "1-y"}))

    assert fix["proposed_value"] == "7050"
    assert fix["rationale"] == "json_quote_strip"


def test_discover_flags_multi_element_list_for_manual_review(tmp_path: Path) -> None:
    fix = only_fix(discover(tmp_path, {"site_id": ["7060", "7050"], "_rev": "1-z"}))

    assert fix["auto_fix"] is False
    assert fix["rationale"] == "multi_element_list_needs_manual_review"
    assert fix["proposed_value"] is None


def test_discover_skips_clean_scalars(tmp_path: Path) -> None:
    proposal = discover(tmp_path, {"site_id": "7050", "_rev": "1-clean"})

    assert proposal["fixes"] == []


def test_discover_handles_empty_list(tmp_path: Path) -> None:
    fix = only_fix(discover(tmp_path, {"job": [], "_rev": "1-empty"}))

    assert fix["proposed_value"] is None
    assert fix["rationale"] == "empty_list_drop"
    assert fix["auto_fix"] is True


def test_discover_handles_list_of_one_none(tmp_path: Path) -> None:
    fix = only_fix(discover(tmp_path, {"job": [None], "_rev": "1-none"}))

    assert fix["proposed_value"] is None
    assert fix["rationale"] == "list_of_one_none"
    assert fix["auto_fix"] is True


def write_proposal(tmp_path: Path, fixes: list[dict[str, Any]]) -> Path:
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps({"fixes": fixes}), encoding="utf-8")
    return path


def fix_entry(
    doc_id: str,
    *,
    field: str = "job",
    current_rev: str = "1-x",
    proposed_value: Any = "7060",
    auto_fix: bool = True,
) -> dict[str, Any]:
    return {
        "database": "btq_vault",
        "doc_id": doc_id,
        "current_rev": current_rev,
        "field": field,
        "current_value": ["7060"],
        "proposed_value": proposed_value,
        "auto_fix": auto_fix,
        "rationale": "list_of_one_lift" if auto_fix else "multi_element_list_needs_manual_review",
    }


class FakeCouch:
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = docs
        self.puts: list[dict[str, Any]] = []

    def request(self, method: str, path: str, data: Any | None = None) -> dict[str, Any]:
        database, doc_id = path.strip("/").split("/", 1)
        assert database == "btq_vault"
        if method == "GET":
            assert data is None
            return self.docs[doc_id]
        if method == "PUT":
            assert data is not None
            self.puts.append(data)
            generation = int(str(data["_rev"]).split("-", 1)[0]) + 1
            self.docs[doc_id] = {**data, "_rev": f"{generation}-fake"}
            return {"ok": True, "id": doc_id, "rev": self.docs[doc_id]["_rev"]}
        raise AssertionError(f"unexpected request: {method} {path}")


def test_apply_writes_corrected_doc(tmp_path: Path, capsys) -> None:
    module = load_script_module()
    fake = FakeCouch({"doc-1": {"_id": "doc-1", "_rev": "1-x", "job": ["7060"], "keep": True}})
    module.couch_request = fake.request
    proposal = write_proposal(tmp_path, [fix_entry("doc-1")])

    assert module.apply_proposal(str(proposal), dry_run=False) == 0

    assert "btq_vault/doc-1: rotated" in capsys.readouterr().out
    assert fake.puts[0]["job"] == "7060"
    assert fake.puts[0]["keep"] is True


def test_apply_skips_on_rev_mismatch(tmp_path: Path, capsys) -> None:
    module = load_script_module()
    fake = FakeCouch({"doc-1": {"_id": "doc-1", "_rev": "2-new", "job": ["7060"]}})
    module.couch_request = fake.request
    proposal = write_proposal(tmp_path, [fix_entry("doc-1", current_rev="1-old")])

    assert module.apply_proposal(str(proposal), dry_run=False) == 0

    assert "rev mismatch" in capsys.readouterr().out
    assert fake.puts == []


def test_apply_idempotent_on_already_correct(tmp_path: Path, capsys) -> None:
    module = load_script_module()
    fake = FakeCouch({"doc-1": {"_id": "doc-1", "_rev": "2-new", "job": "7060"}})
    module.couch_request = fake.request
    proposal = write_proposal(tmp_path, [fix_entry("doc-1", current_rev="1-old")])

    assert module.apply_proposal(str(proposal), dry_run=False) == 0

    assert "already current" in capsys.readouterr().out
    assert fake.puts == []


def test_apply_summary_counts_correct(tmp_path: Path, capsys) -> None:
    module = load_script_module()
    fake = FakeCouch(
        {
            "apply-1": {"_id": "apply-1", "_rev": "1-x", "job": ["7060"]},
            "apply-2": {"_id": "apply-2", "_rev": "1-x", "site_id": ["7050"]},
            "skip-1": {"_id": "skip-1", "_rev": "2-new", "job": ["7060"]},
            "manual-1": {"_id": "manual-1", "_rev": "1-x", "job": ["7060", "7050"]},
        }
    )
    module.couch_request = fake.request
    proposal = write_proposal(
        tmp_path,
        [
            fix_entry("apply-1"),
            fix_entry("apply-2", field="site_id", proposed_value="7050"),
            fix_entry("skip-1", current_rev="1-old"),
            fix_entry("manual-1", auto_fix=False),
        ],
    )

    assert module.apply_proposal(str(proposal), dry_run=False) == 0

    assert "applied=2 skipped=1 manual_review=1 errors=0" in capsys.readouterr().out
