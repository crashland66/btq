from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import pytest

import capture_ingest


def sample_limits() -> capture_ingest.IngestLimits:
    return capture_ingest.IngestLimits(
        max_images=2,
        max_upload_bytes=8,
        request_max_bytes=1024,
        photo_mime_extensions={
            "image/jpeg": ".jpg",
            "image/png": ".png",
        },
        audio_mime_extensions={
            "audio/webm": ".webm",
            "audio/mp4": ".m4a",
        },
        audio_allowed_extensions={
            "audio/webm": {".webm"},
            "audio/mp4": {".m4a"},
        },
    )


def multipart_body(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----btq-capture-ingest-test"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    for name, filename, content_type, content in files:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8"))
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def assert_ingest_error(exc_info: pytest.ExceptionInfo[capture_ingest.SubmissionError], status: HTTPStatus, code: str) -> None:
    assert exc_info.value.status == status
    assert exc_info.value.code == code


def test_parse_multipart_accumulates_repeated_photo_uploads() -> None:
    body, content_type = multipart_body(
        {"site": "Summit Wire"},
        [
            ("photos", "sink.jpg", "image/jpeg", b"one"),
            ("photos", "floor.png", "image/png", b"two"),
        ],
    )

    fields, uploads = capture_ingest.parse_multipart(body, content_type)

    assert fields == {"site": "Summit Wire"}
    assert uploads == [
        capture_ingest.UploadedFile("photos", "sink.jpg", "image/jpeg", b"one"),
        capture_ingest.UploadedFile("photos", "floor.png", "image/png", b"two"),
    ]


def test_read_multipart_submission_partitions_photos_and_audio() -> None:
    body, content_type = multipart_body(
        {"capture_id": "cap-1"},
        [
            ("photos", "sink.jpg", "image/jpeg", b"photo"),
            ("audio", "voice.webm", "audio/webm", b"audio"),
        ],
    )

    fields, photos, audio_files = capture_ingest.read_multipart_submission(
        str(len(body)),
        content_type,
        lambda length: body[:length],
        sample_limits(),
    )

    assert fields == {"capture_id": "cap-1"}
    assert [photo.filename for photo in photos] == ["sink.jpg"]
    assert [audio.filename for audio in audio_files] == ["voice.webm"]


@pytest.mark.parametrize(
    ("raw_length", "status", "code"),
    [
        ("abc", HTTPStatus.BAD_REQUEST, "invalid_content_length"),
        ("0", HTTPStatus.BAD_REQUEST, "empty_request"),
        ("1025", HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large"),
    ],
)
def test_validate_content_length_errors(raw_length: str, status: HTTPStatus, code: str) -> None:
    with pytest.raises(capture_ingest.SubmissionError) as exc_info:
        capture_ingest.validate_content_length(raw_length, 1024)

    assert_ingest_error(exc_info, status, code)


def test_read_multipart_submission_rejects_non_multipart_content_type() -> None:
    with pytest.raises(capture_ingest.SubmissionError) as exc_info:
        capture_ingest.read_multipart_submission("1", "application/json", lambda _length: b"{}", sample_limits())

    assert_ingest_error(exc_info, HTTPStatus.BAD_REQUEST, "expected_multipart")


@pytest.mark.parametrize(
    ("photos", "status", "code"),
    [
        ([], HTTPStatus.BAD_REQUEST, "missing_photo"),
        (
            [
                capture_ingest.UploadedFile("photos", "one.jpg", "image/jpeg", b"1"),
                capture_ingest.UploadedFile("photos", "two.jpg", "image/jpeg", b"2"),
                capture_ingest.UploadedFile("photos", "three.jpg", "image/jpeg", b"3"),
            ],
            HTTPStatus.BAD_REQUEST,
            "too_many_photos",
        ),
        ([capture_ingest.UploadedFile("photos", "one.gif", "image/gif", b"1")], HTTPStatus.BAD_REQUEST, "unsupported_photo_type"),
        ([capture_ingest.UploadedFile("photos", "one.jpg", "image/jpeg", b"123456789")], HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "photo_too_large"),
    ],
)
def test_validate_uploaded_photos_error_paths(
    photos: list[capture_ingest.UploadedFile],
    status: HTTPStatus,
    code: str,
) -> None:
    with pytest.raises(capture_ingest.SubmissionError) as exc_info:
        capture_ingest.validate_uploaded_photos(photos, sample_limits())

    assert_ingest_error(exc_info, status, code)


@pytest.mark.parametrize(
    ("audio_files", "status", "code"),
    [
        (
            [
                capture_ingest.UploadedFile("audio", "one.webm", "audio/webm", b"1"),
                capture_ingest.UploadedFile("audio", "two.webm", "audio/webm", b"2"),
            ],
            HTTPStatus.BAD_REQUEST,
            "too_many_audio_files",
        ),
        ([capture_ingest.UploadedFile("audio", "one.ogg", "audio/ogg", b"1")], HTTPStatus.BAD_REQUEST, "unsupported_audio_type"),
        ([capture_ingest.UploadedFile("audio", "one.mp4", "audio/mp4", b"1")], HTTPStatus.BAD_REQUEST, "unsupported_audio_extension"),
        ([capture_ingest.UploadedFile("audio", "one.webm", "audio/webm", b"123456789")], HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "audio_too_large"),
    ],
)
def test_validate_uploaded_audio_error_paths(
    audio_files: list[capture_ingest.UploadedFile],
    status: HTTPStatus,
    code: str,
) -> None:
    with pytest.raises(capture_ingest.SubmissionError) as exc_info:
        capture_ingest.validate_uploaded_audio(audio_files, sample_limits())

    assert_ingest_error(exc_info, status, code)


def test_normalize_capture_id_uses_client_value_or_prefix_fallback() -> None:
    assert capture_ingest.normalize_capture_id(" cap-client ", fallback_prefix="cap-photo-") == "cap-client"

    generated = capture_ingest.normalize_capture_id("", fallback_prefix="cap-photo-")

    assert generated.startswith("cap-photo-")
    assert len(generated) == len("cap-photo-") + 32


def test_lookup_existing_capture_uses_injected_getter() -> None:
    calls: list[str] = []

    def get_document(capture_id: str) -> dict[str, str]:
        calls.append(capture_id)
        return {"_id": capture_id}

    assert capture_ingest.lookup_existing_capture("cap-1", get_document) == {"_id": "cap-1"}
    assert capture_ingest.lookup_existing_capture("", get_document) is None
    assert calls == ["cap-1"]


def test_build_capture_media_records_and_write_capture_media(tmp_path: Path) -> None:
    uploads = [
        capture_ingest.UploadedFile("photos", r"C:\fakepath\sink.jpg", "image/jpeg", b"photo"),
        capture_ingest.UploadedFile("audio", "voice.webm", "audio/webm", b"audio"),
    ]
    paths = capture_ingest.build_capture_media_records(
        captured_at="2026-05-02T00:00:00-04:00",
        capture_id="cap-1",
        photos=[uploads[0]],
        audio_files=[uploads[1]],
        upload_dir=tmp_path,
        limits=sample_limits(),
        audio_duration_seconds="3.5",
    )

    assert paths.photo_records[0]["upload_id"] == "2026-05-02/cap-1/sink.jpg"
    assert paths.audio_records[0]["upload_id"] == "2026-05-02/cap-1/voice.webm"
    assert paths.audio_records[0]["duration_seconds"] == "3.5"

    capture_ingest.write_capture_media(paths.photo_records + paths.audio_records, uploads, tmp_path)

    assert (tmp_path / "2026-05-02" / "cap-1" / "sink.jpg").read_bytes() == b"photo"
    assert (tmp_path / "2026-05-02" / "cap-1" / "voice.webm").read_bytes() == b"audio"


def test_write_capture_media_rejects_path_traversal(tmp_path: Path) -> None:
    records = [{"stored_path": str(tmp_path.parent / "outside.jpg")}]
    uploads = [capture_ingest.UploadedFile("photos", "outside.jpg", "image/jpeg", b"photo")]

    with pytest.raises(capture_ingest.SubmissionError) as exc_info:
        capture_ingest.write_capture_media(records, uploads, tmp_path)

    assert_ingest_error(exc_info, HTTPStatus.BAD_REQUEST, "invalid_upload_path")


def test_build_capture_document_envelope_merges_domain_fields() -> None:
    doc = capture_ingest.build_capture_document_envelope(
        capture_id="cap-1",
        doc_type="field_capture",
        domain_fields={"site": "Summit Wire", "captured_at": "2026-05-02T00:00:00-04:00"},
        photos=[{"upload_id": "2026-05-02/cap-1/sink.jpg"}],
        audio=[{"upload_id": "2026-05-02/cap-1/voice.webm"}],
        created_at="2026-05-02T04:00:00Z",
    )

    assert doc == {
        "_id": "cap-1",
        "type": "field_capture",
        "capture_id": "cap-1",
        "site": "Summit Wire",
        "captured_at": "2026-05-02T00:00:00-04:00",
        "photos": [{"upload_id": "2026-05-02/cap-1/sink.jpg"}],
        "processing_state": "pending",
        "created_at": "2026-05-02T04:00:00Z",
        "audio": [{"upload_id": "2026-05-02/cap-1/voice.webm"}],
    }
