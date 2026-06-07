from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib import parse

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb.migrate_vault import is_excluded_markdown_path, parse_vault_file, request_json
from vault_markdown import markdown_with_frontmatter


MIGRATION_ADDED_KEYS = {"_id", "_rev", "operator", "vault_path", "content"}


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


def expected_migrated_paths(vault_root: Path) -> set[str]:
    paths: set[str] = set()
    for path in sorted(vault_root.rglob("*.md")):
        if not path.is_file() or is_excluded_markdown_path(path, vault_root):
            continue
        try:
            doc = parse_vault_file(path, vault_root)
        except Exception:
            continue
        if doc is not None:
            paths.add(str(doc["vault_path"]))
    return paths


def main() -> int:
    vault_root = get_config().vault_dir.expanduser().resolve(strict=False)
    database = couchdb_config.vault_database()
    docs = all_vault_docs(database)
    docs_with_paths = [doc for doc in docs if isinstance(doc.get("vault_path"), str)]
    seen_paths: set[str] = set()
    exact_matches = 0
    diffs: list[tuple[str, str]] = []

    for doc in docs_with_paths:
        vault_path = str(doc["vault_path"])
        seen_paths.add(vault_path)
        original_path = vault_root / vault_path
        if not original_path.exists():
            diffs.append((vault_path, "original file is missing"))
            continue
        generated = regenerate_markdown(doc)
        original = original_path.read_text(encoding="utf-8")
        if generated == original:
            exact_matches += 1
        else:
            diffs.append((vault_path, first_differing_line(generated, original)))

    missing_in_couchdb = sorted(expected_migrated_paths(vault_root) - seen_paths)
    print(f"total docs: {len(docs_with_paths)}")
    print(f"exact matches: {exact_matches}")
    print(f"diffs: {len(diffs)}")
    for vault_path, detail in diffs:
        print(f"diff: {vault_path}: {detail}")
    print(f"files in vault not found in CouchDB: {len(missing_in_couchdb)}")
    for vault_path in missing_in_couchdb:
        print(f"missing: {vault_path}")
    return 1 if diffs or missing_in_couchdb else 0


if __name__ == "__main__":
    raise SystemExit(main())
