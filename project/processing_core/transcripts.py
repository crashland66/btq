from __future__ import annotations

from datetime import datetime, timezone

from processing_core.status import STATUS_COMPLETE, STATUS_FAILED


def transcript_metadata_payload(
    *,
    capture_id: str,
    audio_file: str,
    source_fingerprint: dict[str, object],
    transcript_file: str,
    created_at: str | None = None,
) -> dict[str, object]:
    return {
        "capture_id": capture_id,
        "audio_file": audio_file,
        "source_fingerprint": source_fingerprint,
        "transcript_file": transcript_file,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }


def transcript_base_payload(
    *,
    artifact_type: str,
    artifact_id_field: str,
    artifact_id: str,
    engine_field: str,
    engine_name: str,
    provenance: dict[str, object],
    created_at: str | None = None,
) -> dict[str, object]:
    return {
        "type": artifact_type,
        **provenance,
        artifact_id_field: artifact_id,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        engine_field: engine_name,
    }


def transcript_success_payload(
    base_payload: dict[str, object],
    *,
    raw_text: str,
    transcription_payload: dict[str, object] | None,
) -> dict[str, object]:
    payload = dict(base_payload)
    payload.update(
        {
            "status": STATUS_COMPLETE,
            "raw_text": raw_text.strip(),
            "error": None,
            "transcription": transcription_payload,
        }
    )
    return payload


def transcript_failed_payload(base_payload: dict[str, object], error: Exception) -> dict[str, object]:
    payload = dict(base_payload)
    payload.update(
        {
            "status": STATUS_FAILED,
            "raw_text": "",
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
            "transcription": None,
        }
    )
    return payload
