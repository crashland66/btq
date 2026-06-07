from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from field_capture.server import (
    SubmissionError,
    build_capture_document,
    build_submission_job,
    parse_multipart,
    validate_uploaded_audio,
    validate_uploaded_photos,
    write_capture_media,
)


def multipart_body(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----btq-field-capture-test"
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


def valid_fields() -> dict[str, str]:
    return {
        "job_id": "2026-05-02T00-00-00-04-00__photo-capture-summit-wire",
        "capture_id": "cap-photo-test",
        "site": "Summit Wire",
        "site_id": "7050",
        "qc_category": "Restrooms",
        "note": "Sink needs attention.",
        "captured_at": "2026-05-02T00:00:00-04:00",
        "exported_at": "2026-05-02T00:01:00-04:00",
    }


def fake_session() -> SimpleNamespace:
    return SimpleNamespace(
        person=SimpleNamespace(person_id="jordan-avery", canonical_name="Jordan Avery"),
        record=SimpleNamespace(token_id="fct_test", label="Jordan iPhone"),
    )


def test_submit_with_photo_builds_capture_document_and_stores_file(tmp_path: Path) -> None:
    body, content_type = multipart_body(valid_fields(), [("photos", "C:\\fakepath\\sink.jpg", "image/jpeg", b"\xff\xd8photo")])
    fields, uploads = parse_multipart(body, content_type)
    photos = [upload for upload in uploads if upload.field_name == "photos"]
    validate_uploaded_photos(photos, max_images=6, max_upload_bytes=1024 * 1024)

    job = build_submission_job(fields, photos, [], fake_session(), tmp_path / "runtime" / "uploads")
    doc = build_capture_document(fields, photos, [], fake_session(), tmp_path / "runtime" / "uploads")
    write_capture_media(job["payload"]["photos"] + job["payload"]["audio"], photos, tmp_path / "runtime" / "uploads")

    stored_photo = tmp_path / "runtime" / "uploads" / "2026-05-02" / "cap-photo-test" / "sink.jpg"
    assert stored_photo.read_bytes() == b"\xff\xd8photo"
    photo = doc["photos"][0]
    assert doc["type"] == "field_capture"
    assert doc["processing_state"] == "pending"
    assert doc["person_id"] == "jordan-avery"
    assert doc["person_name"] == "Jordan Avery"
    assert doc["field_capture_token_id"] == "fct_test"
    assert doc["field_capture_token_label"] == "Jordan iPhone"
    assert "fc_" not in json.dumps(doc)
    assert photo["filename"] == "sink.jpg"
    assert photo["mime_type"] == "image/jpeg"
    assert photo["stored_path"] == str(stored_photo)
    assert photo["upload_id"] == "2026-05-02/cap-photo-test/sink.jpg"
    assert "fakepath" not in json.dumps(doc)
    assert "data_url" not in photo


def test_submit_without_photo_is_rejected() -> None:
    body, content_type = multipart_body(valid_fields(), [])
    _fields, uploads = parse_multipart(body, content_type)
    photos = [upload for upload in uploads if upload.field_name == "photos"]

    with pytest.raises(SubmissionError) as error:
        validate_uploaded_photos(photos, max_images=6, max_upload_bytes=1024 * 1024)

    assert error.value.status.value == 400
    assert error.value.code == "missing_photo"


def test_submit_with_audio_stores_file_and_builds_audio_metadata(tmp_path: Path) -> None:
    body, content_type = multipart_body(
        valid_fields() | {"audio_duration_seconds": "12"},
        [
            ("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo"),
            ("audio", "voice.webm", "audio/webm", b"audio-bytes"),
        ],
    )
    fields, uploads = parse_multipart(body, content_type)
    photos = [upload for upload in uploads if upload.field_name == "photos"]
    audio_files = [upload for upload in uploads if upload.field_name == "audio"]
    validate_uploaded_photos(photos, max_images=6, max_upload_bytes=1024 * 1024)
    validate_uploaded_audio(audio_files, max_upload_bytes=1024 * 1024)

    job = build_submission_job(fields, photos, audio_files, fake_session(), tmp_path / "runtime" / "uploads")
    doc = build_capture_document(fields, photos, audio_files, fake_session(), tmp_path / "runtime" / "uploads")
    write_capture_media(job["payload"]["photos"] + job["payload"]["audio"], photos + audio_files, tmp_path / "runtime" / "uploads")

    stored_audio = tmp_path / "runtime" / "uploads" / "2026-05-02" / "cap-photo-test" / "voice.webm"
    assert stored_audio.read_bytes() == b"audio-bytes"
    audio = doc["audio"][0]
    assert audio["media_type"] == "audio"
    assert audio["filename"] == "voice.webm"
    assert audio["mime_type"] == "audio/webm"
    assert audio["stored_path"] == str(stored_audio)
    assert audio["upload_id"] == "2026-05-02/cap-photo-test/voice.webm"
    assert audio["size_bytes"] == len(b"audio-bytes")
    assert audio["duration_seconds"] == "12"


def test_submit_rejects_unsupported_audio_type() -> None:
    body, content_type = multipart_body(
        valid_fields(),
        [
            ("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo"),
            ("audio", "voice.exe", "application/octet-stream", b"nope"),
        ],
    )
    _fields, uploads = parse_multipart(body, content_type)
    photos = [upload for upload in uploads if upload.field_name == "photos"]
    audio_files = [upload for upload in uploads if upload.field_name == "audio"]
    validate_uploaded_photos(photos, max_images=6, max_upload_bytes=1024 * 1024)

    with pytest.raises(SubmissionError) as error:
        validate_uploaded_audio(audio_files, max_upload_bytes=1024 * 1024)

    assert error.value.status.value == 400
    assert error.value.code == "unsupported_audio_type"


def test_submit_rejects_unsupported_audio_extension_without_writing_artifacts(tmp_path: Path) -> None:
    body, content_type = multipart_body(
        valid_fields(),
        [
            ("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo"),
            ("audio", "voice.exe", "audio/webm", b"audio-bytes"),
        ],
    )
    _fields, uploads = parse_multipart(body, content_type)
    photos = [upload for upload in uploads if upload.field_name == "photos"]
    audio_files = [upload for upload in uploads if upload.field_name == "audio"]
    validate_uploaded_photos(photos, max_images=6, max_upload_bytes=1024 * 1024)

    with pytest.raises(SubmissionError) as error:
        validate_uploaded_audio(audio_files, max_upload_bytes=1024 * 1024)

    assert error.value.status.value == 400
    assert error.value.code == "unsupported_audio_extension"
    assert not (tmp_path / "runtime" / "uploads").exists()
    assert not (tmp_path / "runtime" / "queue").exists()
