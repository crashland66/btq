from __future__ import annotations

import html
import json
from pathlib import Path

from field_capture import audio_transcription
from field_capture import audio_semantics
from ops_dashboard.common import (
    audio_player,
    is_audio_file,
    read_json_artifact,
    render_kv,
    render_relative_time,
    render_short_id,
    render_table,
    voice_memo_intake_state,
    voice_memo_status,
)
from ops_dashboard.layout import html_page
from processing_core.status import is_terminal_status
from voice_memo.couchdb import VoiceMemoCouchDBConfig, VoiceMemoCouchDBError, query_couchdb_find


RECENT_LIMIT = 12


def _field_audio_assets(runtime_root: Path) -> list[audio_transcription.FieldAudioAsset]:
    return audio_transcription.discover_audio_assets(
        audio_transcription.default_intake_dir(runtime_root),
        audio_transcription.default_upload_dir(runtime_root),
    )


def _field_audio_rows(runtime_root: Path) -> list[dict[str, object]]:
    transcript_dir = audio_transcription.default_transcript_dir(runtime_root)
    semantic_dir = audio_semantics.default_semantic_dir(runtime_root)
    rows: list[dict[str, object]] = []
    for asset in _field_audio_assets(runtime_root):
        transcript_path = audio_transcription.transcript_path_for(transcript_dir, asset.audio_asset_id)
        transcript, _transcript_error = read_json_artifact(transcript_path)
        semantic_path = audio_semantics.semantic_path_for(semantic_dir, asset.audio_asset_id)
        semantic, _semantic_error = read_json_artifact(semantic_path)

        transcript_status = str(transcript.get("status") or "") if transcript else "pending"
        semantic_status = str(semantic.get("status") or "") if semantic else ("pending" if is_terminal_status(transcript_status) else "")
        row_state = "awaiting transcript"
        if transcript and not is_terminal_status(transcript_status):
            row_state = "transcribing"
        elif transcript and semantic:
            row_state = "semantic ready" if is_terminal_status(semantic_status) else "semantic pending"
        elif transcript:
            row_state = "awaiting semantic"

        rows.append(
            {
                "capture_id": asset.upload_id,
                "audio_asset_id": asset.audio_asset_id,
                "site_id": asset.site_id,
                "area": asset.area,
                "filename": asset.audio_filename,
                "media": asset.audio_media_url,
                "transcript_status": transcript_status,
                "semantic_status": semantic_status,
                "state": row_state,
            }
        )
    return rows


def _field_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    pending_transcript = sum(1 for row in rows if row["transcript_status"] == "pending")
    pending_semantic = sum(1 for row in rows if row["transcript_status"] != "pending" and not row["semantic_status"])
    return {
        "raw_audio": len(rows),
        "pending_transcripts": pending_transcript,
        "pending_semantics": pending_semantic,
        "complete": sum(1 for row in rows if row["semantic_status"] in {"complete", "completed"}),
    }


def _render_field_audio(rows: list[dict[str, object]]) -> str:
    recent = rows[-RECENT_LIMIT:]
    recent.reverse()
    columns = [
        {"key": "media", "label": "Audio", "format": lambda value, _row: audio_player(value)},
        {
            "key": "capture_id",
            "label": "Capture",
            "format": lambda value, _row: f'<a href="/captures?capture_id={html.escape(str(value), quote=True)}">{render_short_id(value)}</a>',
            "nowrap": True,
        },
        {"key": "site_id", "label": "Site", "nowrap": True},
        {"key": "area", "label": "Area", "priority": 2},
        {"key": "state", "label": "State", "format": lambda value, _row: f'<span class="pill status-pending">{html.escape(str(value))}</span>', "nowrap": True},
        {"key": "transcript_status", "label": "Transcript", "nowrap": True, "priority": 2},
        {"key": "semantic_status", "label": "Semantic", "nowrap": True, "priority": 2},
    ]
    return render_table(recent, columns, empty_text="No field-capture audio found.")


def _voice_inbox_rows(runtime_root: Path) -> list[dict[str, object]]:
    inbox = runtime_root / "voice_inbox"
    if not inbox.exists():
        return []
    rows: list[dict[str, object]] = []
    for audio_path in sorted((p for p in inbox.glob("*") if p.is_file() and is_audio_file(p)), key=lambda p: p.stat().st_mtime, reverse=True):
        sidecar_path = audio_path.with_suffix(".metadata.json")
        sidecar, _error = read_json_artifact(sidecar_path)
        rows.append(
            {
                "filename": audio_path.name,
                "capture_id": str(sidecar.get("capture_id") or sidecar.get("voice_memo_capture_id") or "") if sidecar else "",
                "source": str(sidecar.get("source") or "") if sidecar else "",
                "site_id": str(sidecar.get("site_id") or "") if sidecar else "",
                "created_at": str(sidecar.get("created_at") or sidecar.get("uploaded_at") or "") if sidecar else "",
            }
        )
    return rows[:RECENT_LIMIT]


def _voice_memo_config_from_env() -> VoiceMemoCouchDBConfig | None:
    import os

    url = (os.environ.get("VOICE_MEMO_COUCHDB_URL") or "").strip()
    if not url:
        return None
    return VoiceMemoCouchDBConfig(
        base_url=url.rstrip("/"),
        username=os.environ.get("VOICE_MEMO_COUCHDB_USER") or "",
        password=os.environ.get("VOICE_MEMO_COUCHDB_PASSWORD") or "",
        timeout=4.0,
        database=os.environ.get("VOICE_MEMO_COUCHDB_DB") or "btq_voice_memos",
    )


def _voice_memo_docs() -> list[dict[str, object]]:
    config = _voice_memo_config_from_env()
    if config is None:
        return []
    try:
        response = query_couchdb_find(
            config,
            config.database,
            {
                "selector": {"_id": {"$gt": None}},
                "limit": 100,
            },
        )
    except VoiceMemoCouchDBError:
        return []
    docs = response.get("docs") if isinstance(response, dict) else []
    if not isinstance(docs, list):
        return []
    return [doc for doc in docs if isinstance(doc, dict)]


def _voice_audio_basename(doc: dict[str, object]) -> str:
    capture_id = str(doc.get("capture_id") or doc.get("_id") or "").strip()
    audio_path = str(doc.get("audio_path") or "").strip()
    suffix = Path(audio_path).suffix or ".webm"
    if capture_id.startswith("vm-"):
        return f"{capture_id}{suffix}"
    if capture_id:
        return f"vm-{capture_id}{suffix}"
    return Path(audio_path).name


def _voice_local_state(runtime_root: Path, doc: dict[str, object]) -> str:
    basename = _voice_audio_basename(doc)
    if not basename:
        return ""
    local_root = Path(__file__).resolve().parents[3] / "local"
    checks = [
        (runtime_root / "completed" / "audio" / basename, "completed audio"),
        (runtime_root / "failed" / "audio" / basename, "failed transcription"),
        (runtime_root / "claimed" / "audio" / basename, "claimed"),
        (runtime_root / "voice_inbox" / basename, "in inbox"),
        (local_root / "audio_processing" / f"{basename}.whisper.txt", "transcribed"),
        (local_root / "audio_processing" / basename, "processing"),
    ]
    for path, label in checks:
        if path.exists():
            return label
    return ""


def _voice_memo_doc_rows(runtime_root: Path) -> list[dict[str, object]]:
    docs = _voice_memo_docs()
    docs.sort(key=lambda doc: str(doc.get("created_at") or doc.get("uploaded_at") or doc.get("_id") or ""), reverse=True)
    rows: list[dict[str, object]] = []
    for doc in docs[:RECENT_LIMIT]:
        capture_id = str(doc.get("capture_id") or doc.get("_id") or "")
        local_state = _voice_local_state(runtime_root, doc)
        rows.append(
            {
                "capture_id": capture_id,
                "created_at": str(doc.get("created_at") or doc.get("uploaded_at") or ""),
                "mode": str(doc.get("mode") or ""),
                "site": " - ".join(part for part in [str(doc.get("site_account") or ""), str(doc.get("site_location") or "")] if part)
                or str(doc.get("site_id") or ""),
                "couch_state": str(doc.get("processing_state") or ""),
                "local_state": local_state,
                "duration": str(doc.get("duration_seconds") or ""),
            }
        )
    return rows


def _render_voice_memo(runtime_root: Path) -> str:
    status = voice_memo_status(runtime_root, runtime_root / "voice_inbox")
    intake = voice_memo_intake_state()
    counts = status | {"couchdb_intake": json.dumps(intake.get("counts") or {}, sort_keys=True)}
    rows = _voice_inbox_rows(runtime_root)
    table = render_table(
        rows,
        [
            {"key": "filename", "label": "File"},
            {"key": "capture_id", "label": "Capture", "format": lambda value, _row: render_short_id(value), "nowrap": True},
            {"key": "source", "label": "Source", "priority": 2},
            {"key": "site_id", "label": "Site", "priority": 2},
            {"key": "created_at", "label": "Created", "format": lambda value, _row: render_relative_time(value), "nowrap": True},
        ],
        empty_text="No voice memo inbox audio found.",
    )
    docs_table = render_table(
        _voice_memo_doc_rows(runtime_root),
        [
            {"key": "capture_id", "label": "Capture", "format": lambda value, _row: render_short_id(value), "nowrap": True},
            {"key": "created_at", "label": "Created", "format": lambda value, _row: render_relative_time(value), "nowrap": True},
            {"key": "mode", "label": "Mode", "priority": 2},
            {"key": "site", "label": "Site", "priority": 2},
            {"key": "couch_state", "label": "CouchDB", "nowrap": True},
            {"key": "local_state", "label": "Local", "nowrap": True},
            {"key": "duration", "label": "Seconds", "nowrap": True, "priority": 2},
        ],
        empty_text="No voice memo CouchDB records found.",
    )
    return f"<h2>Voice Memo Audio</h2>{render_kv(counts)}<h3>Recent Records</h3>{docs_table}<h3>Inbox</h3>{table}"




def render(ctx: object) -> str:
    runtime_root = getattr(ctx, "runtime_root", Path(".")).expanduser().resolve(strict=False)
    rows = _field_audio_rows(runtime_root)
    body = f"""
    <header>
      <h1>Audio Processing</h1>
      <p class="muted">Uploaded audio, transcription, semantic cleanup, and voice memo intake.</p>
    </header>
    <section>
      <h2>Field Capture Audio</h2>
      {render_kv(_field_summary(rows))}
      {_render_field_audio(rows)}
    </section>
    <section>
      {_render_voice_memo(runtime_root)}
    </section>
    """
    return html_page("Audio Processing — BTQ Ops", body, active_section="audio")
