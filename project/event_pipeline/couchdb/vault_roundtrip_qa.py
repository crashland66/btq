from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib import parse

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb.migrate_vault import is_excluded_markdown_path, parse_vault_file, request_json
from vault_markdown import markdown_with_frontmatter


MIGRATION_ADDED_KEYS = {"_id", "_rev", "operator", "content"}


def first_differing_line(left: str, right: str) -> str:
    left_lines = left.splitlines()
    right_lines = right.splitlines()
    max_lines = max(len(left_lines), len(right_lines))
    for index in range(max_lines):
        left_line = left_lines[index] if index < len(left_lines) else "<missing>"
        right_line = right_lines[index] if index < len(right_lines) else "<missing>"
        if left_line != right_line:
            return f"line {index + 1}: generated={left_line!r} original={right_line!r}"
    return "texts differ without a line-level difference"


def all_vault_docs(database: str) -> list[dict[str, Any]]:
    quoted_db = parse.quote(database, safe="")
    _status, payload = request_json("GET", f"{quoted_db}/_all_docs?include_docs=true")
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    docs: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("doc"), dict):
            docs.append(row["doc"])
    return docs


def regenerate_markdown(doc: dict[str, Any]) -> str:
    frontmatter = {key: value for key, value in doc.items() if key not in MIGRATION_ADDED_KEYS}
    return markdown_with_frontmatter(frontmatter, str(doc.get("content") or ""))


def expected_migrated_docs(vault_root: Path) -> dict[str, Path]:
    docs: dict[str, Path] = {}
    for path in sorted(vault_root.rglob("*.md")):
        if not path.is_file() or is_excluded_markdown_path(path, vault_root):
            continue
        try:
            doc = parse_vault_file(path, vault_root)
        except Exception:
            continue
        doc_id = str(doc.get("_id") or "").strip() if doc is not None else ""
        if doc_id:
            docs[doc_id] = path
    return docs


def main() -> int:
    vault_root = get_config().vault_dir.expanduser().resolve(strict=False)
    database = couchdb_config.vault_database()
    docs = all_vault_docs(database)
    docs_by_id = {str(doc.get("_id")): doc for doc in docs if str(doc.get("_id") or "").strip()}
    expected_docs = expected_migrated_docs(vault_root)
    exact_matches = 0
    diffs: list[tuple[str, str]] = []

    for doc_id, original_path in sorted(expected_docs.items()):
        doc = docs_by_id.get(doc_id)
        relative_path = original_path.relative_to(vault_root).as_posix()
        if doc is None:
            diffs.append((relative_path, f"CouchDB doc is missing: {doc_id}"))
            continue
        if not original_path.exists():
            diffs.append((relative_path, "original file is missing"))
            continue
        generated = regenerate_markdown(doc)
        original = original_path.read_text(encoding="utf-8")
        if generated == original:
            exact_matches += 1
        else:
            diffs.append((relative_path, first_differing_line(generated, original)))

    print(f"expected docs: {len(expected_docs)}")
    print(f"exact matches: {exact_matches}")
    print(f"diffs: {len(diffs)}")
    for path, detail in diffs:
        print(f"diff: {path}: {detail}")
    return 1 if diffs else 0


if __name__ == "__main__":
    raise SystemExit(main())
