import json
import re
from pathlib import Path

import pytest

from docs_export import DocsExportError, export_docs, git_commit_for, load_manifest, parse_metadata_header


def write_manifest(path: Path, exports: list[dict[str, str]]) -> None:
    path.write_text(json.dumps({"exports": exports}, indent=2) + "\n", encoding="utf-8")


def test_docs_export_creates_directories_and_writes_changed_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("docs_export.git_commit_for", lambda _root: "abc1234")
    repo = tmp_path / "repo"
    docs_root = tmp_path / "BTDocs"
    source = repo / "project" / "docs" / "ai" / "README_FIRST.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Start\n", encoding="utf-8")
    manifest = repo / "manifest.json"
    write_manifest(manifest, [{"source": "project/docs/ai/README_FIRST.md", "target": "AI/README_FIRST.md"}])

    summary = export_docs(manifest, docs_root, source_root=repo)

    target = docs_root / "AI" / "README_FIRST.md"
    metadata, body = parse_metadata_header(target.read_text(encoding="utf-8"))
    assert body == "# Start\n"
    assert metadata["BTQ_DOC_VERSION"] == "v1"
    assert metadata["BTQ_GIT_COMMIT"] == "abc1234"
    assert metadata["BTQ_SOURCE_PATH"] == "project/docs/ai/README_FIRST.md"
    assert metadata["BTQ_EXPORTER_VERSION"] == "1"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", metadata["BTQ_EXPORT_TIME"])
    assert summary.written == 1
    assert summary.unchanged == 0


def test_docs_export_skips_unchanged_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("docs_export.git_commit_for", lambda _root: "abc1234")
    repo = tmp_path / "repo"
    docs_root = tmp_path / "BTDocs"
    source = repo / "README.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Same\n", encoding="utf-8")
    target = docs_root / "README.md"
    target.parent.mkdir(parents=True)
    manifest = repo / "manifest.json"
    write_manifest(manifest, [{"source": "README.md", "target": "README.md"}])
    export_docs(manifest, docs_root, source_root=repo)

    before_mtime = target.stat().st_mtime_ns
    summary = export_docs(manifest, docs_root, source_root=repo)

    assert target.stat().st_mtime_ns == before_mtime
    assert summary.written == 0
    assert summary.unchanged == 1


def test_docs_export_overwrites_changed_content_deterministically(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("docs_export.git_commit_for", lambda _root: "abc1234")
    repo = tmp_path / "repo"
    docs_root = tmp_path / "BTDocs"
    source = repo / "project" / "docs" / "queue_authoring_guide.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Canonical\n", encoding="utf-8")
    target = docs_root / "Queue" / "queue_authoring_guide.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Stale\n", encoding="utf-8")
    manifest = repo / "manifest.json"
    write_manifest(manifest, [{"source": "project/docs/queue_authoring_guide.md", "target": "Queue/queue_authoring_guide.md"}])

    summary = export_docs(manifest, docs_root, source_root=repo)

    _metadata, body = parse_metadata_header(target.read_text(encoding="utf-8"))
    assert body == "# Canonical\n"
    assert summary.written == 1
    assert summary.unchanged == 0


def test_docs_export_does_not_mutate_source_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("docs_export.git_commit_for", lambda _root: "abc1234")
    repo = tmp_path / "repo"
    docs_root = tmp_path / "BTDocs"
    source = repo / "README.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Source\n", encoding="utf-8")
    manifest = repo / "manifest.json"
    write_manifest(manifest, [{"source": "README.md", "target": "README.md"}])

    export_docs(manifest, docs_root, source_root=repo)

    assert source.read_text(encoding="utf-8") == "# Source\n"


def test_docs_export_git_fallback_unknown(tmp_path: Path, monkeypatch) -> None:
    def fail_run(*_args, **_kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr("docs_export.subprocess.run", fail_run)
    assert git_commit_for(tmp_path) == "unknown"


def test_docs_export_manifest_is_written_and_stable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("docs_export.git_commit_for", lambda _root: "abc1234")
    repo = tmp_path / "repo"
    docs_root = tmp_path / "BTDocs"
    source = repo / "README.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Manifest\n", encoding="utf-8")
    manifest = repo / "manifest.json"
    write_manifest(manifest, [{"source": "README.md", "target": "README.md"}])

    first = export_docs(manifest, docs_root, source_root=repo)
    manifest_path = docs_root / "export_manifest.json"
    before_mtime = manifest_path.stat().st_mtime_ns
    second = export_docs(manifest, docs_root, source_root=repo)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert first.manifest_status == "written"
    assert second.manifest_status == "unchanged"
    assert manifest_path.stat().st_mtime_ns == before_mtime
    assert payload["docs_schema_version"] == "v1"
    assert payload["git_commit"] == "abc1234"
    assert payload["exports"][0]["source"] == "README.md"
    assert payload["exports"][0]["target"] == "README.md"
    assert payload["exports"][0]["sha256"]


def test_docs_export_rejects_missing_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    docs_root = tmp_path / "BTDocs"
    manifest = repo / "manifest.json"
    write_manifest(manifest, [{"source": "missing.md", "target": "README_FIRST.md"}])

    with pytest.raises(DocsExportError, match="Docs source does not exist"):
        load_manifest(manifest, repo, docs_root)


def test_docs_export_rejects_unsafe_target_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "README.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Safe\n", encoding="utf-8")
    manifest = repo / "manifest.json"
    write_manifest(manifest, [{"source": "README.md", "target": "../vault/README.md"}])

    with pytest.raises(DocsExportError, match="safe relative path"):
        load_manifest(manifest, repo, tmp_path / "BTDocs")


def test_docs_export_rejects_duplicate_targets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    first = repo / "a.md"
    second = repo / "b.md"
    repo.mkdir()
    first.write_text("a\n", encoding="utf-8")
    second.write_text("b\n", encoding="utf-8")
    manifest = repo / "manifest.json"
    write_manifest(
        manifest,
        [
            {"source": "a.md", "target": "README_FIRST.md"},
            {"source": "b.md", "target": "README_FIRST.md"},
        ],
    )

    with pytest.raises(DocsExportError, match="Duplicate docs export target"):
        load_manifest(manifest, repo, tmp_path / "BTDocs")
