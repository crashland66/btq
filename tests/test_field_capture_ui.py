from __future__ import annotations

from http import HTTPStatus
from io import BytesIO
from pathlib import Path

import pytest
from field_capture.server import FieldCaptureHandler
from shared_pwa.assets import DB_JS_PATH, render_service_worker
from voice_memo.server import VoiceMemoHandler


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "project" / "field_capture" / "public"
PUBLIC_ROOT = PROJECT_ROOT


class StaticCaptureHarness:
    def __init__(self) -> None:
        self.status: HTTPStatus | None = None
        self.headers: dict[str, str] = {}
        self.wfile = BytesIO()

    def send_response(self, status: HTTPStatus) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.headers[key] = value

    def end_headers(self) -> None:
        return


def served_static_body(handler_class: type, path: str) -> bytes:
    handler = object.__new__(handler_class)
    harness = StaticCaptureHarness()
    handler.send_response = harness.send_response
    handler.send_header = harness.send_header
    handler.end_headers = harness.end_headers
    handler.wfile = harness.wfile

    assert handler.try_serve_static(path)
    assert harness.status == HTTPStatus.OK
    assert harness.headers["Content-Type"] == "application/javascript; charset=utf-8"
    return harness.wfile.getvalue()


def test_mobile_form_uses_prominent_add_photo_button_without_camera_controls() -> None:
    html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'class="brand-logo"' in html
    assert 'src="./assets/app-logo.png"' in html
    assert 'alt="Clearpath Facilities"' in html
    assert (PROJECT_ROOT / "assets" / "app-logo.png").is_file()
    assert "Photo evidence for BTQ queue review" not in html
    assert "<h1>Field Capture</h1>" not in html
    assert "Ready to submit field capture" in html
    assert html.index('id="statusText"') < html.index('id="captureForm"')
    assert 'id="clearButton"' not in html
    assert "Reset" not in html
    assert 'class="add-photo-button"' in html
    assert "Take Photo" in html
    assert "Add from Library" in html
    assert 'id="cameraInput"' in html
    assert 'capture="environment"' in html
    assert 'id="fileInput"' in html
    assert 'accept="image/*"' in html
    assert 'id="cameraPreview"' not in html
    assert 'id="cameraFallback"' not in html
    assert 'id="startCameraButton"' not in html
    assert 'id="shutterButton"' not in html
    assert "Camera ready" not in html
    assert "Start camera" not in html
    assert html.index('class="add-photo-button"') < html.index('class="details-panel note-panel"')
    assert html.index('id="thumbnailGrid"') < html.index('class="add-photo-button"')
    assert html.index('class="details-panel note-panel"') < html.index('id="exportButton"')
    assert html.index('class="details-panel note-panel"') < html.index('class="app-footer"')
    assert "thumb-section" not in html
    assert "photoCount" not in html
    assert "<h2>Photos</h2>" not in html
    assert "captured</span>" not in html
    assert "localhost" not in html


def test_mobile_form_renders_optional_voice_note_controls() -> None:
    html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")

    assert "Voice Note" in html
    assert html.index("<h2>Voice Note</h2>") < html.index('id="voiceStatus"')
    assert html.index('id="voiceStatus"') < html.index('class="tape-deck"')
    assert 'id="recordVoiceButton"' in html
    assert 'class="tape-btn tape-record"' in html
    assert 'title="Record"' in html
    assert 'id="stopVoiceButton"' in html
    assert 'id="clearVoiceButton"' in html
    assert 'id="voicePreview"' in html
    assert "<audio" in html
    assert "Voice recording is not available in this browser." in html
    assert 'id="statusText"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert "/app.js?v=20260514-01" in html
    assert 'class="app-footer"' in html
    assert "B&amp;T Field Capture" in html
    assert 'id="interfaceVersion"' in html
    assert 'id="pipelineVersion"' in html
    assert 'id="footerSite"' in html
    assert 'id="siteViewerLink"' in html
    # No actual token VALUES leak into static HTML. The literal word "token"
    # appears legitimately as a URL parameter name, cookie key suffix, and
    # variable name in the inline manifest-link bootstrap script; what
    # mustn't appear is anything resembling a real fc_* / fct_* secret.
    assert "fc_" not in html.lower()
    assert "fct_" not in html.lower()


@pytest.mark.xfail(reason="superseded by prompt 26 — dynamic category population", strict=True)
def test_capture_guidance_promotes_intentional_area_photos_and_short_voice_tags() -> None:
    # Prompt 26 moved site-specific capture guidance out of static HTML and into session-rendered content.
    html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")

    assert "Capture completed logical areas, not every toilet or trash can." in html
    assert "every toilet or trash can" in html
    assert "Capture every toilet" not in html
    assert "Capture every trash can" not in html
    assert "Optional but helpful: add a short voice tag when the location is not obvious." in html
    assert "Say: Location + completed/reset/issue." in html
    assert "Locker room 130 fully reset." in html
    assert "CFO office reset, trash pulled." in html
    assert "Foreman locker room done, low on paper towels." in html
    assert "Location / short note" in html
    assert "Example: CFO office reset, trash pulled" in html
    assert html.index("Capture completed logical areas") < html.index('class="add-photo-button"')
    assert html.index("Optional but helpful") < html.index('id="recordVoiceButton"')
    assert 'id="fileInput"' in html
    assert 'id="recordVoiceButton"' in html
    assert 'id="stopVoiceButton"' in html
    assert 'id="clearVoiceButton"' in html
    assert 'id="exportButton"' in html
    assert "token" not in html.lower()


def test_javascript_keeps_file_upload_submit_flow_without_camera_state() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    assert "isProcessingPhotos: false" in app_js
    assert "isSubmitting: false" in app_js
    assert "function updateSubmitState()" in app_js
    assert "state.isProcessingPhotos = true;" in app_js
    assert "state.isProcessingPhotos = false;" in app_js
    assert "state.isSubmitting = true;" in app_js
    assert 'setStatus("Saved locally");' in app_js
    assert "function firstNameFromSession(session)" in app_js
    assert 'return `Ready for ${firstName} — ${sites[0].name}`;' in app_js
    assert 'return `Ready to submit for ${sites[0].name}`;' in app_js
    assert '"Ready to submit field capture"' in app_js
    assert 'INTERFACE_VERSION = "2026.05.14-camera"' in app_js
    assert 'PIPELINE_VERSION = "field-capture-intake-v1"' in app_js
    assert "elements.footerSite.textContent = selectedSiteLabel();" in app_js
    assert "session?.person?.first" in app_js
    assert "session?.person?.name" in app_js
    assert "updateSelectedSiteDetails" in app_js
    assert "clearButton" not in app_js
    assert "function clearCapture()" not in app_js
    assert "elements.fileInput.disabled = !enabled;" in app_js
    assert 'elements.fileInput.addEventListener("change", (event) => addFiles(event.target.files));' in app_js
    assert 'const response = await fetch("/api/submit",' in app_js
    assert "stream:" not in app_js
    assert "startCamera" not in app_js
    assert "captureFromVideo" not in app_js
    assert "openPhotoPicker" not in app_js
    assert "navigator.mediaDevices.getUserMedia({ audio: true })" in app_js
    assert "new MediaRecorder" in app_js
    assert "600_000" in app_js  # 10-minute voice recording cap (was 60_000 / 60s)
    assert "state.recorder.pause()" in app_js
    assert "state.recorder.resume()" in app_js
    assert 'state.recorder?.state === "paused"' in app_js
    assert "audioElapsedBeforePause" in app_js
    assert "Requesting microphone access..." in app_js
    assert 'elements.voiceStatus.dataset.tone = "active";' in app_js
    assert "delete elements.voiceStatus.dataset.tone;" in app_js
    assert "function microphoneErrorMessage(error)" in app_js
    assert "Microphone access was blocked. Check browser site permissions." in app_js
    assert "stream?.getTracks().forEach((track) => track.stop());" in app_js
    assert "function finishActiveVoiceRecordingForSubmit()" in app_js
    assert "await finishActiveVoiceRecordingForSubmit();" in app_js
    assert app_js.index("await finishActiveVoiceRecordingForSubmit();") < app_js.index("const record = buildCaptureRecord();")
    assert 'elements.recordVoiceButton.addEventListener("click", handleRecordVoiceEvent);' in app_js
    assert 'elements.recordVoiceButton.addEventListener("touchend", handleRecordVoiceEvent);' in app_js
    assert 'INTERFACE_VERSION = "2026.05.16-tape-deck"' in app_js
    assert 'INTERFACE_VERSION = "2026.05.14-success-screen"' in app_js
    assert 'form.append("audio", record.audio.blob, record.audio.filename);' in app_js
    assert 'form.append("audio_duration_seconds", String(record.audio.durationSeconds || 0));' in app_js
    assert "run on localhost" not in app_js


def test_ready_message_uses_first_name_only_and_keeps_fallbacks() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'const explicit = (session?.person?.first || "").trim();' in app_js
    assert "if (explicit) return explicit;" in app_js
    assert 'const name = (session?.person?.name || "").trim();' in app_js
    assert "if (name.includes(\",\"))" in app_js
    assert "return name.split(/\\s+/)[0] || \"\";" in app_js
    assert "Ready for ${firstName}" in app_js
    assert "Ready for ${name}" not in app_js
    assert "Ready for ${session.person.name}" not in app_js
    assert "Ready to submit for ${sites[0].name}" in app_js
    assert "Ready to submit field capture" in app_js
    assert "bootstrapToken()" in app_js
    assert "tokenFromHash()" in app_js
    assert "elements.statusText.textContent = message;" in app_js


def test_app_js_prefers_explicit_first_name_field() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    assert app_js.index("session?.person?.first") < app_js.index("session?.person?.name")
    assert 'const explicit = (session?.person?.first || "").trim();' in app_js
    assert "if (explicit) return explicit;" in app_js


def test_app_js_includes_view_captures_link_logic() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    assert "siteViewerLink: document.querySelector(\"#siteViewerLink\")" in app_js
    assert "site.viewer_url" in app_js
    assert "state.siteViewerUrls.set(site.name, site.viewer_url);" in app_js
    assert "function updateSiteViewerLink()" in app_js
    assert "elements.siteViewerLink.href = viewerUrl;" in app_js
    assert "elements.siteViewerLink.hidden = false;" in app_js
    assert "elements.siteViewerLink.hidden = true;" in app_js
    assert 'elements.siteInput.addEventListener("change", updateSelectedSiteDetails);' in app_js


def test_index_html_includes_site_viewer_link_element() -> None:
    html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")

    assert '<a id="siteViewerLink"' in html
    assert 'class="site-viewer-link"' in html
    assert "View captures" in html


def test_note_and_submit_share_mobile_row() -> None:
    css = (PROJECT_ROOT / "styles.css").read_text(encoding="utf-8")

    assert ".note-submit-row {" in css
    assert "grid-template-columns: minmax(0, 1fr) minmax(142px, auto);" in css
    assert "align-items: center;" in css
    assert ".thumbnail-grid:empty" in css
    assert "display: none;" in css
    assert "width: 100%;" in css
    assert "min-height: 60px;" in css
    assert "env(safe-area-inset-bottom)" in css
    assert ".app-footer" in css
    assert "font-size: 11px;" in css
    assert ".bottom-tap-buffer" not in css
    assert "@media (max-width: 360px)" in css


def test_add_photo_button_keeps_large_tap_target_with_tape_deck() -> None:
    css = (PROJECT_ROOT / "styles.css").read_text(encoding="utf-8")

    tape_record_rule = css[css.index(".tape-record {") : css.index(".tape-record:not(:disabled):hover")]
    add_photo_rule = css[css.index(".add-photo-button {") : css.index(".add-photo-button:hover")]

    assert "width: 48px;" in tape_record_rule
    assert "height: 48px;" in tape_record_rule
    assert "min-height: 46px;" in add_photo_rule
    assert "font-size: 16px;" in add_photo_rule
    assert "font-weight: 800;" in add_photo_rule


def test_tape_deck_markup_present_in_field_capture_index() -> None:
    html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'class="tape-deck"' in html
    assert "tape-record" in html
    assert "tape-stop" in html
    assert "tape-clear" in html


def test_clear_voice_button_handler_has_confirm_guard() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")
    handler_start = app_js.index('elements.clearVoiceButton.addEventListener("click"')
    handler_window = app_js[handler_start : handler_start + 240]

    assert "clearVoiceButton.addEventListener" in handler_window
    assert "window.confirm(" in handler_window


def test_brand_green_matches_bt_logo_accent_without_white_text() -> None:
    css = (PROJECT_ROOT / "styles.css").read_text(encoding="utf-8")

    assert "--accent: #6fd064;" in css
    assert "--accent-dark: #57b94f;" in css
    assert "--accent-ink: #071b36;" in css
    assert "--accent-soft: rgba(111, 208, 100, 0.18);" in css
    assert "opacity: 0.68;" in css
    assert "rgba(47, 111, 87" not in css
    assert "#2f6f57" not in css
    assert "#245744" not in css


def test_recording_status_is_prominent_in_voice_header() -> None:
    css = (PROJECT_ROOT / "styles.css").read_text(encoding="utf-8")

    assert ".capture-guidance" in css
    assert ".voice-guidance p" in css
    assert ".voice-guidance {" in css
    assert ".voice-panel .section-title" in css
    assert "#voiceStatus {" in css
    assert "margin-left: auto;" in css
    assert "text-align: right;" in css
    assert '#voiceStatus[data-tone="active"]' in css
    assert "color: var(--danger);" in css


def test_submitting_status_is_prominent() -> None:
    css = (PROJECT_ROOT / "styles.css").read_text(encoding="utf-8")

    status_rule = css[css.index(".status-text {") : css.index("#voiceStatus {")]
    active_rule = css[css.index('.status-text[data-tone="active"]') : css.index('.status-text[data-tone="error"]')]

    assert "min-height: 44px;" in status_rule
    assert "display: flex;" in status_rule
    assert "font-weight: 800;" in status_rule
    assert "color: var(--text);" in status_rule
    assert 'data-tone="active"' in css
    assert "background: var(--accent);" in active_rule
    assert "color: var(--accent-ink);" in active_rule
    assert "box-shadow: 0 0 0 4px var(--accent-soft);" in active_rule


def test_index_html_replaces_hardcoded_category_options_with_placeholder() -> None:
    html = (PUBLIC_ROOT / "index.html").read_text(encoding="utf-8")
    category_select = html[html.index('id="categoryInput"') : html.index("</select>", html.index('id="categoryInput"'))]

    assert '<option value="">Select category…</option>' in category_select
    assert 'value="Entryways / Lobby / Doorways"' not in category_select
    assert 'value="Other"' not in category_select


def test_index_html_includes_capture_guidance_element() -> None:
    html = (PUBLIC_ROOT / "index.html").read_text(encoding="utf-8")

    assert '<div id="captureGuidance" class="capture-guidance" hidden></div>' in html
    assert html.index('id="captureGuidance"') < html.index('class="photo-input-panel"')


def test_app_js_builds_sites_by_id_map_from_session() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "sitesById: new Map()" in app_js
    assert "state.sitesById = new Map();" in app_js
    assert "state.sitesById.set(site.site_id, site);" in app_js
    assert "return siteId ? state.sitesById.get(siteId) || null : null;" in app_js


def test_capture_id_and_job_id_include_random_suffix() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")
    html = (PUBLIC_ROOT / "index.html").read_text(encoding="utf-8")

    assert "function randomSuffix()" in app_js
    assert "crypto.randomUUID" in app_js
    assert "Math.random().toString(36).slice(2, 10)" in app_js
    assert "const suffix = randomSuffix();" in app_js
    assert 'const captureId = `cap-photo-${capturedAt.replace(/[:.]/g, "-")}-${suffix}`;' in app_js
    assert 'const jobId = `${exportedAt.replace(/[:.]/g, "-")}__photo-capture-${fileStem(site)}-${suffix}`;' in app_js
    assert '<script src="/app.js?v=20260529-05"></script>' in html
    assert "<!-- Previous asset path: /app.js?v=20260529-04 -->" in html
    assert "<!-- Previous asset path: /app.js?v=20260529-03 -->" in html
    assert "<!-- Previous asset path: /app.js?v=20260529-02 -->" in html
    assert "<!-- Previous asset path: /app.js?v=20260529-01 -->" in html
    assert "<!-- Previous asset path: /app.js?v=20260528-01 -->" in html


def test_app_js_falls_back_when_create_image_bitmap_decode_is_unavailable() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "async function decodeToCanvasSource(fileOrBlob)" in app_js
    assert 'if (typeof createImageBitmap === "function") {' in app_js
    assert "try {" in app_js
    assert "return await createImageBitmap(fileOrBlob);" in app_js
    assert "} catch (_e) {" in app_js
    assert "const url = URL.createObjectURL(fileOrBlob);" in app_js
    assert "const i = new Image();" in app_js
    assert "i.onload = () => resolve(i);" in app_js
    assert 'i.onerror = () => reject(new Error("image_decode_failed"));' in app_js
    assert "i.src = url;" in app_js
    assert "} finally {" in app_js
    assert "URL.revokeObjectURL(url);" in app_js
    assert "const source = await decodeToCanvasSource(fileOrBlob);" in app_js
    assert "context.drawImage(source, 0, 0, width, height);" in app_js
    assert 'if (typeof source.close === "function") source.close();' in app_js
    assert "bitmap.close();" not in app_js


def test_app_js_renders_capture_guidance_text_from_session() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "captureGuidance: document.querySelector(\"#captureGuidance\")" in app_js
    assert "function renderCaptureGuidance(text)" in app_js
    assert "elements.captureGuidance.textContent = guidance;" in app_js
    assert "elements.captureGuidance.hidden = false;" in app_js
    assert "elements.captureGuidance.hidden = true;" in app_js
    assert "renderCaptureGuidance(site?.capture_guidance || \"\");" in app_js


def test_app_js_renders_display_categories_from_session() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "function renderDisplayCategories(list)" in app_js
    assert "elements.categoryInput.replaceChildren(placeholder);" in app_js
    assert "option.value = canonical;" in app_js
    assert "option.textContent = label;" in app_js
    assert "renderDisplayCategories(site?.display_categories || []);" in app_js
    assert "const qcCategory = elements.categoryInput.value;" in app_js
    assert "qc_category: qcCategory" in app_js
    assert "for (const [key, value] of Object.entries(fields))" in app_js


def test_app_js_falls_back_silently_when_session_fields_absent() -> None:
    app_js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "renderCaptureGuidance(site?.capture_guidance || \"\");" in app_js
    assert "renderDisplayCategories(site?.display_categories || []);" in app_js
    assert "if (Array.isArray(list))" in app_js
    assert "if (!label || !canonical) return;" in app_js


def test_styles_css_includes_capture_guidance_selector() -> None:
    css = (PUBLIC_ROOT / "styles.css").read_text(encoding="utf-8")
    rule = css[css.index(".capture-guidance {") : css.index(".voice-guidance {")]

    assert ".capture-guidance {" in css
    assert "border-left: 4px solid var(--accent);" in rule
    assert "background: var(--surface);" in rule
    assert "font-weight: 800;" in rule


def test_indexeddb_capture_store_module_present() -> None:
    db_js = DB_JS_PATH.read_text(encoding="utf-8")

    # Module surface
    assert "window.fieldCaptureDb" in db_js
    assert 'DB_NAME = "field_capture_v1"' in db_js
    assert "DB_VERSION = 1" in db_js
    assert 'STORE = "captures"' in db_js
    assert "onversionchange" in db_js
    assert "isConnectionClosingError" in db_js
    assert "withConnection" in db_js
    assert "StorageRestartedError" in db_js or "local_storage_restarted" in db_js

    # Schema: keyPath + indexes
    assert 'keyPath: "capture_id"' in db_js
    assert 'createIndex("status"' in db_js
    assert 'createIndex("createdAt"' in db_js

    # Public functions
    for fn in (
        "putCapture",
        "getCapture",
        "listAll",
        "listByStatus",
        "deleteCapture",
        "updateCapture",
        "countByStatus",
        "isSupported",
    ):
        assert fn in db_js, f"db.js missing public function {fn}"

    # The whole point: store Blobs, not base64. The module
    # never references dataUrl or toDataURL.
    assert "dataUrl" not in db_js
    assert "toDataURL" not in db_js


def test_indexeddb_db_js_loaded_before_app_js() -> None:
    html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")

    assert "/db.js" in html
    # Order matters: db.js initializes window.fieldCaptureDb
    # which app.js will read at boot in prompt 148.
    assert html.index('<script src="/db.js') < html.index('<script src="/app.js')


def test_local_first_capture_uses_blob_pipeline() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    # Photos are stored as Blobs, not base64
    assert "canvas.toBlob(" in app_js
    assert "blob," in app_js  # photo record shape uses .blob
    # The old data-URL path is gone
    assert "toDataURL" not in app_js
    assert "dataUrlToBlob" not in app_js
    assert "data_url" not in app_js

    # IDB is wired in
    assert "window.fieldCaptureDb" in app_js
    assert "putCapture(" in app_js
    assert "listByStatus(" in app_js
    assert "updateCapture(" in app_js


def test_local_first_save_replaces_submit_flow() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")

    # New entry point
    assert "function saveCapture" in app_js or "async function saveCapture" in app_js
    assert "buildCaptureRecord" in app_js
    # Old entry point is gone
    assert "function submitCapture" not in app_js
    assert "async function submitCapture" not in app_js
    assert "function buildJob" not in app_js
    # Button label updated
    assert ">Save capture<" in html
    assert ">Submit capture<" not in html
    assert ">Saved locally<" in html


def test_save_capture_surfaces_actionable_quota_error() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    assert "function isQuotaError(error)" in app_js
    assert 'error.name === "QuotaExceededError"' in app_js
    assert 'error.name === "NS_ERROR_DOM_QUOTA_REACHED"' in app_js
    assert "error.code === 22" in app_js
    assert "if (isQuotaError(error))" in app_js
    assert 'setStatus("Phone storage full — free up space, then re-save this capture.", "error");' in app_js
    assert "function isStorageRestartedError(error)" in app_js
    assert 'error.name === "StorageRestartedError"' in app_js
    assert 'setStatus("Local storage restarted — tap Save again.", "error");' in app_js


def test_drain_loop_and_triggers_present() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    assert "function drainQueue" in app_js or "async function drainQueue" in app_js
    assert "uploadOneCapture" in app_js
    # Backoff schedule constant present so failures don't busy-loop
    assert "DRAIN_BACKOFF_SCHEDULE_MS" in app_js
    assert "DRAIN_PERMANENT_FAILURE_HOURS" in app_js
    # Triggers
    assert 'addEventListener("online"' in app_js
    assert 'addEventListener("visibilitychange"' in app_js
    # Auth-lost handling
    assert "clearStoredToken" in app_js
    # Idempotent retries via the existing capture_id
    assert 'fields.append("capture_id"' in app_js or 'form.append("capture_id"' in app_js or \
        'capture_id: captureId' in app_js  # carried via fields object
    # Orphan recovery: a capture left in "uploading" by a tab that died
    # mid-flight must be requeued, or it stays stuck in the queue strip
    # forever (drainQueue only re-reads "pending").
    assert "UPLOAD_ORPHAN_RESET_MS" in app_js
    assert 'listByStatus("uploading")' in app_js
    assert "recovered_orphaned_upload" in app_js


def test_queue_strip_present_in_html_and_css() -> None:
    html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "styles.css").read_text(encoding="utf-8")

    assert 'id="queueStrip"' in html
    assert 'class="queue-strip"' in html
    assert ".queue-strip" in css
    assert '[data-tone="active"]' in css
    assert '[data-tone="error"]' in css
    # Synced affordance must be visible, not silent
    assert '[data-tone="synced"]' in css
    assert "id=\"staleStrip\"" in html or "id='staleStrip'" in html
    assert ".stale-strip" in css


def test_synced_affirmative_and_stale_warning_logic_present() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    # Empty state surfaces an affirmative message
    assert "All captures synced" in app_js
    # Stale warning helper exists
    assert "summarizeStaleCaptures" in app_js
    assert "renderStaleStrip" in app_js
    # 24h threshold present (matches DRAIN_PERMANENT_FAILURE_HOURS
    # ceiling but is the WARNING threshold, not the failure
    # threshold; both should be in the file).
    assert "24 * 60 * 60_000" in app_js


def test_persistent_storage_requested_on_boot() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    assert "requestPersistentStorage" in app_js
    assert "navigator.storage" in app_js
    assert ".persist" in app_js


def test_ios_install_gate_detection_and_save_guard_present() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    assert "function isIosDevice()" in app_js
    assert "/iPad|iPhone|iPod/.test(ua)" in app_js
    assert 'navigator.platform === "MacIntel" && (navigator.maxTouchPoints || 0) > 1' in app_js
    assert "function isStandaloneDisplay()" in app_js
    assert 'window.matchMedia("(display-mode: standalone)").matches' in app_js
    assert "navigator.standalone === true" in app_js
    assert "captureGated: false" in app_js
    assert "async function showInstallGate()" in app_js
    assert "state.captureGated = true;" in app_js
    assert 'window.fieldCaptureDb.listByStatus("pending")' in app_js
    assert "installGatePending.textContent" in app_js
    assert "You have ${count} unsent capture(s) on this phone." in app_js
    assert "if (state.captureGated) return;" in app_js


def test_ios_install_gate_markup_present_as_app_shell_peer() -> None:
    html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")
    app_shell = html[html.index('<main class="app-shell">') : html.index("</main>")]

    assert 'id="installGate"' in app_shell
    assert 'class="details-panel install-gate"' in app_shell
    assert "Add to Home Screen to keep your captures safe" in app_shell
    assert "Saved captures live only on this phone until they upload." in app_shell
    assert "Add to Home Screen" in app_shell
    assert 'id="installGatePending"' in app_shell
    assert 'id="successScreen"' in app_shell
    assert app_shell.index('id="successScreen"') < app_shell.index('id="installGate"')
    assert '<script src="/app.js?v=20260529-05"></script>' in html


def test_boot_drain_runs_before_form_is_actionable() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    # The boot sequence awaits persistence, gates iOS browser tabs,
    # then refreshes the queue before kicking the foreground drain.
    assert "refreshPendingCount()" in app_js
    assert "await requestPersistentStorage()" in app_js
    assert "\nrequestPersistentStorage();" not in app_js
    assert "if (isIosDevice() && !isStandaloneDisplay())" in app_js
    assert "await showInstallGate();" in app_js
    assert "await refreshPendingCount().catch(() => {});" in app_js
    assert "drainQueue().catch(() => {});" in app_js


def test_interface_version_bumped_for_local_first() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")
    assert 'INTERFACE_VERSION = "2026.05.28-local-first"' in app_js
    # The previous version is preserved as a comment
    assert "2026.05.24-multi-photo" in app_js


def test_service_worker_file_present_and_drain_only() -> None:
    sw = render_service_worker("field_capture")

    # Sync event handler
    assert 'addEventListener("sync"' in sw
    assert '"field-capture-drain"' in sw

    # Reuses the same IDB schema as db.js
    assert '"field_capture_v1"' in sw
    assert '"captures"' in sw

    # No app-shell caching at this stage — the SW is
    # deliberately drain-only.
    assert 'addEventListener("fetch"' not in sw
    assert "caches.open" not in sw
    assert "addAll(" not in sw

    # Upload contract matches the foreground path
    assert "/api/submit" in sw
    assert "Bearer" in sw
    assert "FormData" in sw

    # Backoff + permanent-failure constants present
    assert "DRAIN_BACKOFF_SCHEDULE_MS" in sw
    assert "DRAIN_PERMANENT_FAILURE_HOURS" in sw

    # Orphan recovery: the SW can be killed mid-sync, leaving a record in
    # "uploading". It must requeue such rows or they stay stuck forever
    # (drainQueue only re-reads "pending").
    assert "UPLOAD_ORPHAN_RESET_MS" in sw
    assert 'listByStatus("uploading")' in sw
    assert "recovered_orphaned_upload" in sw


def test_service_worker_shared_source_renders_per_product() -> None:
    field_sw = render_service_worker("field_capture")
    voice_sw = render_service_worker("voice_memo")

    assert served_static_body(FieldCaptureHandler, "/sw.js").decode("utf-8") == field_sw
    assert served_static_body(VoiceMemoHandler, "/sw.js").decode("utf-8") == voice_sw
    assert "/api/submit" in field_sw
    assert "/api/upload" in voice_sw
    assert "if (!response.ok)" in field_sw
    assert "if (response.status !== 201 && response.status !== 200)" in voice_sw
    assert "UPLOAD_ORPHAN_RESET_MS" in field_sw
    assert "UPLOAD_ORPHAN_RESET_MS" in voice_sw
    assert "recovered_orphaned_upload" in field_sw
    assert "recovered_orphaned_upload" in voice_sw

    normalized_field = (
        field_sw.replace("Field-capture", "__PRODUCT__")
        .replace("/api/submit", "__API_ENDPOINT__")
        .replace("!response.ok", "__SUCCESS_CHECK__")
    )
    normalized_voice = (
        voice_sw.replace("Voice-memo", "__PRODUCT__")
        .replace("/api/upload", "__API_ENDPOINT__")
        .replace("response.status !== 201 && response.status !== 200", "__SUCCESS_CHECK__")
    )
    assert normalized_field == normalized_voice


def test_app_registers_service_worker_and_requests_sync() -> None:
    app_js = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")

    assert "registerServiceWorker" in app_js
    assert 'navigator.serviceWorker.register("/sw.js' in app_js
    assert "requestBackgroundSync" in app_js
    assert '"field-capture-drain"' in app_js

    # iOS guard: if reg.sync is missing, skip silently
    assert '"sync" in reg' in app_js

    # Token stash for SW context
    assert "stashTokenForServiceWorker" in app_js
    assert '"__token__"' in app_js
