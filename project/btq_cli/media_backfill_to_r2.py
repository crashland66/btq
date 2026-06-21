from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol

from instance_config import get_instance_config
from media_store import _guard_s3_key, build_s3_store


class WritableMediaStore(Protocol):
    def write(self, key: str, data: bytes) -> None:
        ...

    def read(self, key: str) -> bytes:
        ...

    def exists(self, key: str) -> bool:
        ...


@dataclass(frozen=True)
class BackfillError:
    key: str
    reason: str


@dataclass
class BackfillMediaReport:
    total_scanned: int = 0
    copied: int = 0
    skipped_present: int = 0
    verified: int = 0
    verify_failed: int = 0
    would_copy: int = 0
    would_skip_present: int = 0
    errors: list[BackfillError] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_scanned": self.total_scanned,
            "copied": self.copied,
            "skipped_present": self.skipped_present,
            "verified": self.verified,
            "verify_failed": self.verify_failed,
            "would_copy": self.would_copy,
            "would_skip_present": self.would_skip_present,
            "errors": [{"key": item.key, "reason": item.reason} for item in self.errors],
        }


@dataclass(frozen=True)
class IntegrityCheck:
    matches: bool
    failed: bool = False
    reason: str = ""


def backfill_media_to_store(
    *,
    runtime_root: Path,
    store: WritableMediaStore,
    dry_run: bool = False,
    limit: int | None = None,
    verbose: bool = False,
) -> BackfillMediaReport:
    uploads_root = runtime_root.expanduser().resolve(strict=False) / "uploads"
    report = BackfillMediaReport()
    if limit is not None and limit < 0:
        raise ValueError("--limit must be zero or greater")
    if not uploads_root.is_dir():
        raise FileNotFoundError(f"uploads directory not found under runtime root: {uploads_root}")

    for path in _iter_upload_files(uploads_root, limit=limit):
        key = _key_for_upload_path(path, uploads_root)
        report.total_scanned += 1
        try:
            local_bytes = path.read_bytes()
        except OSError:
            _record_failure(report, key, "read-local-failed", verbose=verbose)
            continue

        present_check = _remote_matches_local(store, key, local_bytes)
        if present_check.failed:
            _record_failure(report, key, present_check.reason or "verify-present-failed", verbose=verbose)
            continue
        if present_check.matches:
            report.skipped_present += 1
            if dry_run:
                report.would_skip_present += 1
            if verbose:
                action = "would-skip-present" if dry_run else "skip-present"
                print(f"{action}: {key}")
            continue

        if dry_run:
            report.would_copy += 1
            if verbose:
                print(f"would-copy: {key}")
            continue

        try:
            store.write(key, local_bytes)
        except Exception:
            _record_failure(report, key, "write-failed", verbose=verbose)
            continue

        verify_check = _remote_matches_local(store, key, local_bytes)
        if verify_check.failed or not verify_check.matches:
            _record_failure(report, key, verify_check.reason or "verify-after-write-failed", verbose=verbose)
            continue
        report.copied += 1
        report.verified += 1
        if verbose:
            print(f"copied: {key}")

    return report


def handle_backfill_media_to_r2(args: argparse.Namespace) -> int:
    try:
        store = build_s3_store(get_instance_config())
    except Exception as exc:
        raise SystemExit(f"backfill-media-to-r2: unable to build R2 media store from instance config: {exc}") from exc
    try:
        report = backfill_media_to_store(
            runtime_root=args.runtime_root,
            store=store,
            dry_run=bool(args.dry_run),
            limit=args.limit,
            verbose=bool(args.verbose),
        )
    except Exception as exc:
        raise SystemExit(f"backfill-media-to-r2: {exc}") from exc

    mode = "dry-run" if args.dry_run else "write"
    print(f"BTQ media FS-to-R2 backfill ({mode})")
    print(
        "total_scanned={total_scanned} copied={copied} skipped_present={skipped_present} "
        "verified={verified} verify_failed={verify_failed} would_copy={would_copy} "
        "would_skip_present={would_skip_present} errors={errors}".format(
            **{**report.as_dict(), "errors": len(report.errors)}
        )
    )
    for item in report.errors:
        print(f"error: {item.key}: {item.reason}")
    return 1 if report.verify_failed or report.errors else 0


def _iter_upload_files(uploads_root: Path, *, limit: int | None) -> Iterator[Path]:
    if limit == 0:
        return
    count = 0
    for path in sorted(uploads_root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        yield path
        count += 1
        if limit is not None and count >= limit:
            break


def _key_for_upload_path(path: Path, uploads_root: Path) -> str:
    key = path.relative_to(uploads_root).as_posix()
    return _guard_s3_key(key)


def _remote_matches_local(store: WritableMediaStore, key: str, local_bytes: bytes) -> IntegrityCheck:
    try:
        if not store.exists(key):
            return IntegrityCheck(matches=False)
    except Exception:
        return IntegrityCheck(matches=False, failed=True, reason="exists-check-failed")

    etag = _single_part_etag(store, key)
    if etag is not None:
        return IntegrityCheck(matches=etag == hashlib.md5(local_bytes).hexdigest())

    try:
        remote_bytes = store.read(key)
    except Exception:
        return IntegrityCheck(matches=False, failed=True, reason="read-remote-failed")
    return IntegrityCheck(matches=remote_bytes == local_bytes)


def _single_part_etag(store: WritableMediaStore, key: str) -> str | None:
    client = getattr(store, "client", None)
    bucket = getattr(store, "bucket", "")
    if client is None or not bucket:
        return None
    try:
        response = client.head_object(Bucket=bucket, Key=_guard_s3_key(key))
    except Exception:
        return None
    etag = str(response.get("ETag", "")).strip().strip('"')
    if "-" in etag or len(etag) != 32:
        return None
    try:
        int(etag, 16)
    except ValueError:
        return None
    return etag.lower()


def _record_failure(report: BackfillMediaReport, key: str, reason: str, *, verbose: bool) -> None:
    report.verify_failed += 1
    report.errors.append(BackfillError(key=key, reason=reason))
    if verbose:
        print(f"failed: {key}: {reason}")
