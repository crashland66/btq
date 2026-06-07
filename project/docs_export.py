from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_config, repo_root
from io_atomic import atomic_write_text


DEFAULT_MANIFEST = repo_root() / "project" / "docs" / "docs_export_manifest.json"
DOCS_SCHEMA_VERSION = "v1"
EXPORTER_VERSION = "1"
EXPORT_MANIFEST_NAME = "export_manifest.json"


class DocsExportError(RuntimeError):
    """Raised when the docs export manifest or target is unsafe."""


@dataclass(frozen=True)
class ExportItem:
    source: Path
    target: Path
    source_rel: Path
    target_rel: Path


@dataclass(frozen=True)
class ExportSummary:
    written: int
    unchanged: int
    manifest_status: str
    items: list[tuple[str, str, str]]


def ensure_relative_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DocsExportError(f"{label} must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DocsExportError(f"{label} must be a safe relative path: {value}")
    return path


def ensure_within_root(path: Path, root: Path, label: str) -> Path:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        raise DocsExportError(f"{label} is outside allowed root: {resolved_path}")
    return resolved_path


def load_manifest(manifest_path: Path, source_root: Path, docs_root: Path) -> list[ExportItem]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    exports = payload.get("exports") if isinstance(payload, dict) else None
    if not isinstance(exports, list):
        raise DocsExportError("Docs export manifest must contain an exports list")

    items: list[ExportItem] = []
    seen_targets: set[Path] = set()
    for index, raw_item in enumerate(exports, start=1):
        if not isinstance(raw_item, dict):
            raise DocsExportError(f"Export item {index} must be an object")
        source_rel = ensure_relative_path(raw_item.get("source"), f"exports[{index}].source")
        target_rel = ensure_relative_path(raw_item.get("target"), f"exports[{index}].target")
        source_path = ensure_within_root(source_root / source_rel, source_root, "Docs source")
        target_path = ensure_within_root(docs_root / target_rel, docs_root, "Docs target")
        if target_path in seen_targets:
            raise DocsExportError(f"Duplicate docs export target: {target_rel}")
        if not source_path.exists() or not source_path.is_file():
            raise DocsExportError(f"Docs source does not exist: {source_path}")
        if source_path.suffix.lower() != ".md" or target_path.suffix.lower() != ".md":
            raise DocsExportError(f"Docs exports must be markdown files: {source_rel} -> {target_rel}")
        seen_targets.add(target_path)
        items.append(ExportItem(source=source_path, target=target_path, source_rel=source_rel, target_rel=target_rel))
    return items


def utc_export_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def git_commit_for(source_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return "unknown"
    value = result.stdout.strip()
    return value or "unknown"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def metadata_block(*, export_time: str, git_commit: str, source_path: Path) -> str:
    return (
        "<!--\n"
        f"BTQ_DOC_VERSION: {DOCS_SCHEMA_VERSION}\n"
        f"BTQ_EXPORT_TIME: {export_time}\n"
        f"BTQ_GIT_COMMIT: {git_commit}\n"
        f"BTQ_SOURCE_PATH: {source_path.as_posix()}\n"
        f"BTQ_EXPORTER_VERSION: {EXPORTER_VERSION}\n"
        "-->\n\n"
    )


def parse_metadata_header(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("<!--\n"):
        return {}, text
    closing = text.find("-->\n")
    if closing == -1:
        return {}, text
    raw_header = text[len("<!--\n"):closing]
    body = text[closing + len("-->\n"):]
    if body.startswith("\n"):
        body = body[1:]
    metadata: dict[str, str] = {}
    for line in raw_header.splitlines():
        if ":" not in line:
            return {}, text
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata, body


def render_exported_markdown(source_content: str, item: ExportItem, git_commit: str, export_time: str) -> str:
    return f"{metadata_block(export_time=export_time, git_commit=git_commit, source_path=item.source_rel)}{source_content}"


def existing_export_is_current(existing_text: str, source_content: str, item: ExportItem, git_commit: str) -> bool:
    metadata, body = parse_metadata_header(existing_text)
    if body != source_content:
        return False
    return (
        metadata.get("BTQ_DOC_VERSION") == DOCS_SCHEMA_VERSION
        and metadata.get("BTQ_GIT_COMMIT") == git_commit
        and metadata.get("BTQ_SOURCE_PATH") == item.source_rel.as_posix()
        and metadata.get("BTQ_EXPORTER_VERSION") == EXPORTER_VERSION
        and bool(metadata.get("BTQ_EXPORT_TIME"))
    )


def build_manifest_payload(items: list[ExportItem], docs_root: Path, git_commit: str, export_time: str) -> dict[str, Any]:
    exported_docs: list[dict[str, str]] = []
    for item in items:
        target_text = item.target.read_text(encoding="utf-8")
        metadata, body = parse_metadata_header(target_text)
        exported_docs.append(
            {
                "source": item.source_rel.as_posix(),
                "target": item.target_rel.as_posix(),
                "sha256": sha256_text(target_text),
                "source_sha256": sha256_text(body),
                "export_time": metadata.get("BTQ_EXPORT_TIME", export_time),
            }
        )
    return {
        "docs_schema_version": DOCS_SCHEMA_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "git_commit": git_commit,
        "export_time": export_time,
        "docs_root": str(docs_root),
        "exports": exported_docs,
    }


def stable_manifest_payload(payload: dict[str, Any]) -> dict[str, Any]:
    stable = dict(payload)
    stable.pop("export_time", None)
    return stable


def write_export_manifest(docs_root: Path, payload: dict[str, Any]) -> str:
    manifest_path = docs_root / EXPORT_MANIFEST_NAME
    desired_text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if manifest_path.exists():
        try:
            existing_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_payload = None
        if isinstance(existing_payload, dict) and stable_manifest_payload(existing_payload) == stable_manifest_payload(payload):
            return "unchanged"
        if manifest_path.read_text(encoding="utf-8") == desired_text:
            return "unchanged"
    atomic_write_text(manifest_path, desired_text)
    return "written"


def export_docs(manifest_path: Path, docs_root: Path, source_root: Path | None = None) -> ExportSummary:
    resolved_source_root = repo_root() if source_root is None else source_root
    resolved_docs_root = docs_root.expanduser()
    items = load_manifest(manifest_path.expanduser(), resolved_source_root, resolved_docs_root)
    git_commit = git_commit_for(resolved_source_root)
    export_time = utc_export_timestamp()
    summary_items: list[tuple[str, str, str]] = []
    written = 0
    unchanged = 0
    for item in items:
        source_content = item.source.read_text(encoding="utf-8")
        if item.target.exists() and existing_export_is_current(item.target.read_text(encoding="utf-8"), source_content, item, git_commit):
            unchanged += 1
            summary_items.append(("unchanged", str(item.source), str(item.target)))
            continue
        rendered = render_exported_markdown(source_content, item, git_commit, export_time)
        if item.target.exists() and item.target.read_text(encoding="utf-8") == rendered:
            unchanged += 1
            summary_items.append(("unchanged", str(item.source), str(item.target)))
            continue
        item.target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(item.target, rendered)
        written += 1
        summary_items.append(("written", str(item.source), str(item.target)))
    resolved_docs_root.mkdir(parents=True, exist_ok=True)
    manifest_status = write_export_manifest(resolved_docs_root, build_manifest_payload(items, resolved_docs_root, git_commit, export_time))
    return ExportSummary(written=written, unchanged=unchanged, manifest_status=manifest_status, items=summary_items)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export BTQ bootstrap/config docs outside the operational vault.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--docs-dir", type=Path, default=get_config().docs_dir)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def format_summary(summary: ExportSummary, json_output: bool) -> str:
    if json_output:
        return json.dumps(
            {
                "written": summary.written,
                "unchanged": summary.unchanged,
                "manifest_status": summary.manifest_status,
                "items": [
                    {"status": status, "source": source, "target": target}
                    for status, source, target in summary.items
                ],
            },
            indent=2,
            sort_keys=True,
        )
    lines = [f"BTQ docs export: written={summary.written} unchanged={summary.unchanged} manifest={summary.manifest_status}"]
    for status, source, target in summary.items:
        lines.append(f"- {status}: {source} -> {target}")
    return "\n".join(lines)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = export_docs(args.manifest, args.docs_dir)
    print(format_summary(summary, args.json))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
