from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from event_pipeline import couchdb_config
from field_capture.photo_vision_couchdb import query_photo_vision_by_capture_ids
from media_store import media_key_from_stored_path
from token_store import UNIVERSAL_SITE_SCOPE, TokenRecord


PAGE_SIZE = 60
DATE_UNAVAILABLE = "Date unavailable"
INVALID_REFERENCE = "invalid_reference"
MEDIA_AVAILABLE = "available"
MEDIA_UNAVAILABLE = "unavailable"
MEDIA_CHECK_FAILED = "check_failed"
MEDIA_UNCHECKED = "unchecked"
VISION_AVAILABLE = "available"
VISION_UNAVAILABLE = "unavailable"
VIEWER_ROLES = frozenset({"read_only", "site_admin"})


class TokenSiteScopeError(ValueError):
    """The authenticated token cannot form an explicit viewer site scope."""


@dataclass(frozen=True)
class TokenSiteScope:
    role: str
    allowed_site_ids: tuple[str, ...]
    selected_site_id: str | None
    site_labels: Mapping[str, str]

    @classmethod
    def from_token(
        cls,
        record: TokenRecord,
        *,
        selected_site_id: str | None = None,
        label_resolver: Callable[[str], str | None] | None = None,
    ) -> TokenSiteScope:
        if not record.can_view_site or record.role not in VIEWER_ROLES:
            raise TokenSiteScopeError("token is not permitted to use the site photo viewer")

        site_ids = tuple(sorted({str(value).strip() for value in record.site_ids if str(value).strip()}))
        if not site_ids:
            raise TokenSiteScopeError("viewer tokens require an explicit site scope")
        if UNIVERSAL_SITE_SCOPE in site_ids:
            raise TokenSiteScopeError("wildcard site scope is not supported by the viewer")

        selected = str(selected_site_id or "").strip() or None
        if selected is not None and selected not in site_ids:
            raise TokenSiteScopeError("selected site is outside the token scope")
        if selected is None and len(site_ids) == 1:
            selected = site_ids[0]

        labels = {
            site_id: (str(label_resolver(site_id) or "").strip() if label_resolver else "") or site_id
            for site_id in site_ids
        }
        return cls(
            role=record.role,
            allowed_site_ids=site_ids,
            selected_site_id=selected,
            site_labels=labels,
        )

    @property
    def selected_site(self) -> str | None:
        return self.selected_site_id


@dataclass(frozen=True)
class CapturePhotoProjection:
    capture_id: str
    site_id: str
    site_label: str
    captured_at: str
    capture_date: str
    capture_category: str
    filename: str
    mime_type: str
    media_key: str | None
    upload_id: str
    photo_id: str
    availability_state: str

    @classmethod
    def from_capture_rows(
        cls,
        captures: Iterable[Mapping[str, object]],
        *,
        site_id: str,
        site_label: str,
        upload_root: Path,
    ) -> tuple[CapturePhotoProjection, ...]:
        normalized_site_id = str(site_id).strip()
        rows: list[CapturePhotoProjection] = []
        for capture in captures:
            row_site_id = str(capture.get("site_id") or "").strip()
            if row_site_id and row_site_id != normalized_site_id:
                continue
            capture_id = str(capture.get("capture_id") or capture.get("_id") or "").strip()
            captured_at = _capture_timestamp(capture)
            capture_date = _capture_local_date(captured_at)
            capture_category = str(capture.get("qc_category") or capture.get("category") or "").strip()
            photos = capture.get("photos")
            if not isinstance(photos, list):
                continue
            for index, photo in enumerate(photos):
                if not isinstance(photo, Mapping):
                    continue
                filename = str(photo.get("filename") or "").strip()
                upload_id = str(photo.get("upload_id") or "").strip()
                media_key = _media_key_for_photo(photo, upload_root)
                photo_id = (
                    upload_id
                    or media_key
                    or str(photo.get("photo_id") or "").strip()
                    or f"{capture_id}:{index}:{filename}"
                )
                rows.append(
                    cls(
                        capture_id=capture_id,
                        site_id=normalized_site_id,
                        site_label=str(site_label).strip() or normalized_site_id,
                        captured_at=captured_at,
                        capture_date=capture_date,
                        capture_category=capture_category,
                        filename=filename,
                        mime_type=str(photo.get("mime_type") or photo.get("mimeType") or "").strip(),
                        media_key=media_key,
                        upload_id=upload_id,
                        photo_id=photo_id,
                        availability_state=MEDIA_UNCHECKED if media_key else INVALID_REFERENCE,
                    )
                )
        return tuple(rows)

    @property
    def local_capture_date(self) -> str:
        return self.capture_date

    @property
    def canonical_site_label(self) -> str:
        return self.site_label

    @property
    def stable_photo_identity(self) -> str:
        return self.photo_id


@dataclass(frozen=True)
class VisionByCaptureProjection:
    by_capture_id: Mapping[str, tuple[dict[str, Any], ...]]

    @classmethod
    def fetch(
        cls,
        config: couchdb_config.CouchDBConfig,
        capture_ids: Iterable[str],
        *,
        database: str | None = None,
    ) -> VisionByCaptureProjection:
        cleaned = tuple(sorted({str(value).strip() for value in capture_ids if str(value).strip()}))
        fetched = query_photo_vision_by_capture_ids(config, list(cleaned), database=database)
        return cls(
            by_capture_id={
                capture_id: tuple(doc for doc in fetched.get(capture_id, []) if isinstance(doc, dict))
                for capture_id in cleaned
            }
        )

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Sequence[Mapping[str, object]]],
        *,
        capture_ids: Iterable[str] | None = None,
    ) -> VisionByCaptureProjection:
        ids = (
            {str(value).strip() for value in capture_ids if str(value).strip()}
            if capture_ids is not None
            else {str(value).strip() for value in mapping if str(value).strip()}
        )
        return cls(
            by_capture_id={
                capture_id: tuple(dict(doc) for doc in mapping.get(capture_id, ()) if isinstance(doc, Mapping))
                for capture_id in sorted(ids)
            }
        )

    @property
    def docs_by_capture_id(self) -> Mapping[str, tuple[dict[str, Any], ...]]:
        return self.by_capture_id

    def match(self, photo: CapturePhotoProjection) -> dict[str, Any] | None:
        docs = self.by_capture_id.get(photo.capture_id, ())
        identities = {value for value in (photo.upload_id, photo.media_key) if value}
        for doc in docs:
            if str(doc.get("photo_id") or "").strip() in identities:
                return doc
        if not photo.filename:
            return None
        for doc in docs:
            if photo.filename in _vision_filenames(doc):
                return doc
        return None


@dataclass(frozen=True)
class _SitePhotoRecord:
    capture_id: str
    site_id: str
    site_label: str
    captured_at: str
    capture_date: str
    capture_category: str
    filename: str
    mime_type: str
    media_key: str | None
    upload_id: str
    photo_id: str
    availability_state: str
    vision_state: str
    vision_document_id: str
    description: str
    summary: str
    area_guess: str
    qc_category: str
    searchable_text: str


@dataclass(frozen=True)
class SitePhotoCorpus:
    photos: tuple[_SitePhotoRecord, ...]

    @classmethod
    def join(
        cls,
        capture_photos: Iterable[CapturePhotoProjection],
        vision: VisionByCaptureProjection,
    ) -> SitePhotoCorpus:
        capture_photo_rows = tuple(capture_photos)
        matched_vision = _match_corpus_vision(capture_photo_rows, vision.by_capture_id)
        joined: list[_SitePhotoRecord] = []
        for index, photo in enumerate(capture_photo_rows):
            doc = matched_vision.get(index)
            description = _vision_string(doc, "description")
            summary = _vision_string(doc, "summary")
            area_guess = _vision_string(doc, "area_guess")
            qc_category = _vision_string(doc, "qc_category")
            searchable_text = " ".join(
                value
                for value in (
                    description,
                    summary,
                    area_guess,
                    qc_category,
                    photo.capture_date,
                    photo.capture_category,
                    photo.site_label,
                )
                if value
            ).casefold()
            joined.append(
                _SitePhotoRecord(
                    **photo.__dict__,
                    vision_state=VISION_AVAILABLE if doc is not None else VISION_UNAVAILABLE,
                    vision_document_id=str(
                        (doc or {}).get("_id")
                        or (doc or {}).get("photo_asset_id")
                        or (
                            f"{(doc or {}).get('capture_id') or 'unknown'}:"
                            f"{(doc or {}).get('photo_id') or photo.photo_id}"
                            if doc is not None
                            else ""
                        )
                    ).strip(),
                    description=description,
                    summary=summary,
                    area_guess=area_guess,
                    qc_category=qc_category,
                    searchable_text=searchable_text,
                )
            )
        return cls(photos=tuple(joined))

    @property
    def records(self) -> tuple[_SitePhotoRecord, ...]:
        return self.photos


@dataclass(frozen=True)
class _PhotoDateGroup:
    label: str
    photos: tuple[_SitePhotoRecord, ...]


@dataclass(frozen=True)
class SitePhotoPage:
    total_results: int
    page_size: int
    page_number: int
    first_position: int
    last_position: int
    previous_url: str | None
    next_url: str | None
    groups: tuple[_PhotoDateGroup, ...]
    query_terms: tuple[str, ...]

    @classmethod
    def from_corpus(
        cls,
        corpus: SitePhotoCorpus,
        query: str = "",
        page_number: int = 1,
        *,
        url_for_page: Callable[[int], str] | None = None,
    ) -> SitePhotoPage:
        if page_number < 1:
            raise ValueError("page number must be at least 1")
        terms = tuple(part for part in str(query).casefold().split() if part)
        matched = [
            photo
            for photo in corpus.photos
            if all(term in photo.searchable_text for term in terms)
        ]
        matched.sort(key=_photo_sort_key, reverse=True)
        total = len(matched)
        start = (page_number - 1) * PAGE_SIZE
        page_photos = matched[start : start + PAGE_SIZE]
        groups_by_date: dict[str, list[_SitePhotoRecord]] = {}
        for photo in page_photos:
            label = photo.capture_date or DATE_UNAVAILABLE
            groups_by_date.setdefault(label, []).append(photo)
        groups = tuple(
            _PhotoDateGroup(label=label, photos=tuple(photos))
            for label, photos in groups_by_date.items()
        )
        first = start + 1 if page_photos else 0
        last = start + len(page_photos) if page_photos else 0
        previous_url = url_for_page(page_number - 1) if url_for_page and page_number > 1 else None
        next_url = url_for_page(page_number + 1) if url_for_page and start + len(page_photos) < total else None
        return cls(
            total_results=total,
            page_size=PAGE_SIZE,
            page_number=page_number,
            first_position=first,
            last_position=last,
            previous_url=previous_url,
            next_url=next_url,
            groups=groups,
            query_terms=terms,
        )

    @property
    def photos(self) -> tuple[_SitePhotoRecord, ...]:
        return tuple(photo for group in self.groups for photo in group.photos)

    def resolve_media_availability(
        self,
        exists: Callable[[str], bool],
    ) -> SitePhotoPage:
        """Return this page with every displayed media key checked once.

        Invalid references are preserved from the capture projection. A store
        error is a per-photo honest state rather than a dead image or an empty
        gallery.
        """
        states: dict[str, str] = {}
        groups: list[_PhotoDateGroup] = []
        for group in self.groups:
            photos: list[_SitePhotoRecord] = []
            for photo in group.photos:
                state = photo.availability_state
                if photo.media_key:
                    if photo.media_key not in states:
                        try:
                            states[photo.media_key] = (
                                MEDIA_AVAILABLE if exists(photo.media_key) else MEDIA_UNAVAILABLE
                            )
                        except Exception:
                            states[photo.media_key] = MEDIA_CHECK_FAILED
                    state = states[photo.media_key]
                photos.append(replace(photo, availability_state=state))
            groups.append(replace(group, photos=tuple(photos)))
        return replace(self, groups=tuple(groups))


def _capture_timestamp(capture: Mapping[str, object]) -> str:
    return str(
        capture.get("captured_at")
        or capture.get("exported_at")
        or capture.get("created_at")
        or ""
    ).strip()


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _capture_local_date(value: str) -> str:
    parsed = _parse_timestamp(value)
    return parsed.date().isoformat() if parsed is not None else ""


def _media_key_for_photo(photo: Mapping[str, object], upload_root: Path) -> str | None:
    candidates = (photo.get("upload_id"), photo.get("stored_path"))
    for candidate in candidates:
        if not str(candidate or "").strip():
            continue
        try:
            return media_key_from_stored_path(candidate, upload_root)
        except (OSError, ValueError):
            continue
    return None


def _vision_filenames(doc: Mapping[str, object]) -> set[str]:
    provenance = doc.get("provenance") if isinstance(doc.get("provenance"), Mapping) else {}
    values = (
        doc.get("filename"),
        doc.get("source_image_path"),
        doc.get("photo_id"),
        provenance.get("image_filename"),
        provenance.get("source_image_path"),
        provenance.get("photo_id"),
    )
    return {
        PurePosixPath(str(value or "").replace("\\", "/")).name
        for value in values
        if str(value or "").strip()
    }


def _vision_identity(doc: Mapping[str, object], index: int) -> str:
    return str(doc.get("_id") or doc.get("photo_asset_id") or f"row:{index}").strip()


def _match_corpus_vision(
    photos: Sequence[CapturePhotoProjection],
    by_capture_id: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[int, Mapping[str, object]]:
    """Assign exact identities corpus-wide before considering legacy names."""
    matched: dict[int, Mapping[str, object]] = {}
    used: dict[str, set[str]] = {}
    for photo_index, photo in enumerate(photos):
        identities = {value for value in (photo.upload_id, photo.media_key) if value}
        for doc_index, doc in enumerate(by_capture_id.get(photo.capture_id, ())):
            doc_identity = _vision_identity(doc, doc_index)
            capture_used = used.setdefault(photo.capture_id, set())
            if (
                doc_identity not in capture_used
                and str(doc.get("photo_id") or "").strip() in identities
            ):
                capture_used.add(doc_identity)
                matched[photo_index] = doc
                break
    for photo_index, photo in enumerate(photos):
        if photo_index in matched or not photo.filename:
            continue
        for doc_index, doc in enumerate(by_capture_id.get(photo.capture_id, ())):
            doc_identity = _vision_identity(doc, doc_index)
            capture_used = used.setdefault(photo.capture_id, set())
            if doc_identity not in capture_used and photo.filename in _vision_filenames(doc):
                capture_used.add(doc_identity)
                matched[photo_index] = doc
                break
    return matched


def _vision_string(doc: Mapping[str, object] | None, key: str) -> str:
    return str((doc or {}).get(key) or "").strip()


def _photo_sort_key(photo: _SitePhotoRecord) -> tuple[int, datetime, str, str]:
    parsed = _parse_timestamp(photo.captured_at)
    if parsed is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc), photo.capture_id, photo.photo_id)
    return (1, parsed.astimezone(timezone.utc), photo.capture_id, photo.photo_id)
