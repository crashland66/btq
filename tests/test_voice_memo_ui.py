from __future__ import annotations

from pathlib import Path

from shared_pwa.assets import DB_JS_PATH, render_service_worker


PUBLIC_ROOT = Path(__file__).resolve().parents[1] / "project" / "voice_memo" / "public"


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}()")
    body_start = source.index("{", start)
    depth = 0
    for index in range(body_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[body_start + 1 : index]
    raise AssertionError(f"Could not find body for {name}")


def test_voice_memo_local_first_uses_idb_queue() -> None:
    db_js = DB_JS_PATH.read_text(encoding="utf-8")
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "window.fieldCaptureDb" in db_js
    assert "putCapture" in db_js
    assert "listByStatus" in db_js
    assert 'DB_NAME = "field_capture_v1"' in db_js
    assert "onversionchange" in db_js
    assert "isConnectionClosingError" in db_js
    assert "withConnection" in db_js
    assert "StorageRestartedError" in db_js or "local_storage_restarted" in db_js
    assert "window.fieldCaptureDb" in app_js
    assert "putCapture(record)" in app_js
    assert "listByStatus('pending')" in app_js
    assert "blob: state.audio.blob" in app_js
    assert "photos: []" in app_js


def test_voice_memo_save_replaces_submit_flow() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")
    html = (PUBLIC_ROOT / "index.html").read_text(encoding="utf-8")

    assert "async function saveMemo" in app_js
    assert "function buildMemoRecord" in app_js
    assert "function submitMemo" not in app_js
    assert "async function submitMemo" not in app_js
    assert ">Save voice memo<" in html
    assert "Voice memo saved locally" in app_js


def test_voice_memo_capture_id_includes_random_suffix() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")
    html = (PUBLIC_ROOT / "index.html").read_text(encoding="utf-8")

    assert "function randomSuffix()" in app_js
    assert "crypto.randomUUID" in app_js
    assert "Math.random().toString(36).slice(2, 10)" in app_js
    assert "const captureId = `cap-voice-${capturedAt.replace(/[:.]/g, '-')}-${randomSuffix()}`;" in app_js
    assert '<script src="/app.js?v=20260529-01"></script>' in html


def test_voice_memo_drain_loop_present() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "async function drainQueue" in app_js
    assert "uploadOneCapture" in app_js
    assert "DRAIN_BACKOFF_SCHEDULE_MS" in app_js
    assert "DRAIN_PERMANENT_FAILURE_HOURS" in app_js
    assert "nextBackoffMs(attempts - 1)" in app_js
    assert "addEventListener('online'" in app_js
    assert "addEventListener('visibilitychange'" in app_js
    assert "exceeded_retry_window" in app_js


def test_voice_memo_queue_strip_present() -> None:
    html = (PUBLIC_ROOT / "index.html").read_text(encoding="utf-8")
    css = (PUBLIC_ROOT / "styles.css").read_text(encoding="utf-8")

    assert 'id="queueStrip"' in html
    assert 'class="queue-strip"' in html
    assert 'id="staleStrip"' in html
    assert ".queue-strip" in css
    assert '.queue-strip[data-tone="synced"]' in css
    assert '.queue-strip[data-tone="active"]' in css
    assert '.queue-strip[data-tone="error"]' in css
    assert ".stale-strip" in css


def test_voice_memo_synced_affirmative_present() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "All memos synced" in app_js
    assert "summarizeStaleCaptures" in app_js
    assert "renderStaleStrip" in app_js
    assert "24 * 60 * 60_000" in app_js


def test_voice_memo_persistent_storage_requested() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "requestPersistentStorage" in app_js
    assert "navigator.storage" in app_js
    assert ".persist" in app_js
    assert "requestPersistentStorage();" in app_js


def test_voice_memo_bootstrap_url_token_wins_over_stored_token() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")
    body = _function_body(app_js, "bootstrapToken")

    assert body.index("params.get('token')") < body.index("if (token)")
    assert "const fromUrlOrHash = fromUrl || fromHash;" in body
    assert "persistTokenAndScrub(fromUrlOrHash)" in body
    assert "state.sitesById.clear()" in body
    assert "state.employeesByJob.clear()" in body
    assert "switched accounts" in body


def test_voice_memo_service_worker_present() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")
    sw = render_service_worker("voice_memo")

    assert "registerServiceWorker" in app_js
    assert "stashTokenForServiceWorker" in app_js
    assert '"__token__"' in app_js or "'__token__'" in app_js
    assert 'addEventListener("sync"' in sw
    assert '"field-capture-drain"' in sw
    assert "/api/upload" in sw
    assert "response.status !== 201 && response.status !== 200" in sw
    assert '"field_capture_v1"' in sw
    assert '"captures"' in sw
    assert "UPLOAD_ORPHAN_RESET_MS" in sw
    assert 'listByStatus("uploading")' in sw
    assert "recovered_orphaned_upload" in sw
