from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class PhotoArtifact:
    filename: str
    source: str
    media_present: bool
    sidecar_present: bool = False
    sidecar_capture_id: str = ""
    expects_sidecar: bool = False


@dataclass(frozen=True)
class CaptureArtifactSnapshot:
    capture_id: str
    intake_present: bool
    intake_valid: bool
    photos: tuple[PhotoArtifact, ...]


@dataclass(frozen=True)
class CaptureReconcileResult:
    doc: dict[str, Any]
    capture_id: str
    site: str
    error_reason: str
    complete_on_disk: bool
    reason: str
    missing_media: tuple[str, ...] = ()
    missing_sidecars: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaptureReconcilePlan:
    reconcilable: tuple[CaptureReconcileResult, ...]
    needs_attention: tuple[CaptureReconcileResult, ...]


def capture_id_for_doc(doc: dict[str, Any]) -> str:
    return str(doc.get("capture_id") or doc.get("_id") or "").strip()


def site_for_doc(doc: dict[str, Any]) -> str:
    site = str(doc.get("site_id") or "").strip()
    if not site and str(doc.get("target_type") or "").strip() == "location":
        site = str(doc.get("target_id") or "").strip()
    return site or str(doc.get("site") or "").strip()


def error_reason_for_doc(doc: dict[str, Any]) -> str:
    return str(doc.get("error_reason") or "").strip()


def photo_filenames_from_record_list(records: object) -> tuple[str, ...]:
    if not isinstance(records, list):
        return ()
    filenames: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        filename = str(record.get("filename") or "").strip()
        if not filename:
            upload_id = str(record.get("upload_id") or "").strip()
            filename = upload_id.rsplit("/", 1)[-1] if upload_id else ""
        if filename and filename not in seen:
            seen.add(filename)
            filenames.append(filename)
    return tuple(filenames)


def intake_photo_filenames(intake_payload: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(intake_payload, dict):
        return ()
    payload = intake_payload.get("payload")
    if not isinstance(payload, dict):
        return ()
    return photo_filenames_from_record_list(payload.get("photos"))


def doc_photo_filenames(doc: dict[str, Any]) -> tuple[str, ...]:
    return photo_filenames_from_record_list(doc.get("photos"))


def classify_failed_capture(
    doc: dict[str, Any],
    snapshot: CaptureArtifactSnapshot,
) -> CaptureReconcileResult:
    capture_id = snapshot.capture_id or capture_id_for_doc(doc)
    error_reason = error_reason_for_doc(doc)

    if not capture_id:
        return _result(doc, capture_id, False, "missing capture_id")
    if not snapshot.intake_present:
        return _result(doc, capture_id, False, "missing intake", error_reason=error_reason)
    if not snapshot.intake_valid:
        return _result(doc, capture_id, False, "invalid intake", error_reason=error_reason)
    if not snapshot.photos:
        return _result(doc, capture_id, False, "no photos referenced", error_reason=error_reason)

    missing_media = tuple(photo.filename for photo in snapshot.photos if not photo.media_present)
    if missing_media:
        return _result(
            doc,
            capture_id,
            False,
            "missing media",
            error_reason=error_reason,
            missing_media=missing_media,
        )

    missing_sidecars = tuple(
        photo.filename
        for photo in snapshot.photos
        if photo.expects_sidecar
        and (
            not photo.sidecar_present
            or (photo.sidecar_capture_id and photo.sidecar_capture_id != capture_id)
        )
    )
    if missing_sidecars:
        return _result(
            doc,
            capture_id,
            False,
            "missing sidecar",
            error_reason=error_reason,
            missing_sidecars=missing_sidecars,
        )

    if not any(photo.expects_sidecar for photo in snapshot.photos):
        return _result(doc, capture_id, False, "no expected sidecars", error_reason=error_reason)

    return _result(doc, capture_id, True, "complete on disk", error_reason=error_reason)


def categorize_failed_captures(
    docs_and_snapshots: Iterable[tuple[dict[str, Any], CaptureArtifactSnapshot]],
) -> CaptureReconcilePlan:
    reconcilable: list[CaptureReconcileResult] = []
    needs_attention: list[CaptureReconcileResult] = []
    for doc, snapshot in docs_and_snapshots:
        result = classify_failed_capture(doc, snapshot)
        if result.complete_on_disk:
            reconcilable.append(result)
        else:
            needs_attention.append(result)
    return CaptureReconcilePlan(tuple(reconcilable), tuple(needs_attention))


def _result(
    doc: dict[str, Any],
    capture_id: str,
    complete_on_disk: bool,
    reason: str,
    *,
    error_reason: str | None = None,
    missing_media: tuple[str, ...] = (),
    missing_sidecars: tuple[str, ...] = (),
) -> CaptureReconcileResult:
    return CaptureReconcileResult(
        doc=doc,
        capture_id=capture_id,
        site=site_for_doc(doc),
        error_reason=error_reason_for_doc(doc) if error_reason is None else error_reason,
        complete_on_disk=complete_on_disk,
        reason=reason,
        missing_media=missing_media,
        missing_sidecars=missing_sidecars,
    )
