(function () {
  "use strict";

  const INTERFACE_VERSION = "2026.06.12-offline";
  // Previous INTERFACE_VERSION values: "2026.06.12-blob-persist", "2026.06.12-resilient-sync", "2026.06.11-job-first-review", "2026.06.08-photo-limit", "2026.06.08-text-and-gating", "2026.06.06-unified-capture" (legacy static smoke tests).
  const PIPELINE_VERSION = "unified-capture-intake-v2";
  const DEFAULT_MAX_PHOTOS = 6; // fallback only; the live limit comes from /api/session.max_images
  const TOKEN_KEY = "unifiedCaptureToken";
  const SCREEN_MODE_KEY = "unifiedCaptureScreenMode";
  const RECENT_SITE_KEY = "unifiedCaptureSiteId";
  const SYNC_TAG = "unified-capture-drain";
  // Capped at 2 min: field workers want prompt sync, and the foreground heartbeat
  // (see event wiring below) picks up an expired backoff within ~20s regardless.
  const DRAIN_BACKOFF_SCHEDULE_MS = [5_000, 15_000, 30_000, 60_000, 120_000];
  const DRAIN_PERMANENT_FAILURE_HOURS = 24;
  const UPLOAD_ORPHAN_RESET_MS = 120_000;
  let token = "";

  const state = {
    photos: [],
    audio: null,
    recorder: null,
    audioChunks: [],
    audioStartedAt: 0,
    audioElapsedBeforePause: 0,
    audioResumedAt: null,
    audioStopTimer: null,
    audioTickTimer: null,
    isStartingAudio: false,
    isProcessingPhotos: false,
    isSubmitting: false,
    session: null,
    maxPhotos: DEFAULT_MAX_PHOTOS,
    sitesById: new Map(),
    formEnabled: false,
    isDraining: false,
    drainBackoffUntil: 0,
  };

  const elements = {
    statusText: document.querySelector("#statusText"),
    mySubsBtn: document.querySelector("#mySubsBtn"),
    mySubsBadge: document.querySelector("#mySubsBadge"),
    settingsMenu: document.querySelector("#settingsMenu"),
    themeColorMeta: document.querySelector("#themeColorMeta"),
    screenModeInputs: Array.from(document.querySelectorAll('input[name="screenMode"]')),
    queueStrip: document.querySelector("#queueStrip"),
    staleStrip: document.querySelector("#staleStrip"),
    captureForm: document.querySelector("#captureForm"),
    siteInput: document.querySelector("#siteInput"),
    siteGuidance: document.querySelector("#siteGuidance"),
    categoryInput: document.querySelector("#categoryInput"),
    canvas: document.querySelector("#captureCanvas"),
    thumbnailGrid: document.querySelector("#thumbnailGrid"),
    photoNoteTemplate: document.querySelector("#photoNoteTemplate"),
    photoCount: document.querySelector("#photoCount"),
    cameraInput: document.querySelector("#cameraInput"),
    fileInput: document.querySelector("#fileInput"),
    recordVoiceButton: document.querySelector("#recordVoiceButton"),
    stopVoiceButton: document.querySelector("#stopVoiceButton"),
    clearVoiceButton: document.querySelector("#clearVoiceButton"),
    voicePreview: document.querySelector("#voicePreview"),
    voiceStatus: document.querySelector("#voiceStatus"),
    voiceDuration: document.querySelector("#voiceDuration"),
    voiceSupportMessage: document.querySelector("#voiceSupportMessage"),
    notesInput: document.querySelector("#notesInput"),
    noteToggle: document.querySelector("#noteToggle"),
    noteEditorPanel: document.querySelector("#noteEditorPanel"),
    noteEditor: document.querySelector("#noteEditor"),
    submitSummary: document.querySelector("#submitSummary"),
    submitButton: document.querySelector("#submitButton"),
    successScreen: document.querySelector("#successScreen"),
    successDetail: document.querySelector("#successDetail"),
    submitAnotherButton: document.querySelector("#submitAnotherButton"),
    feedSection: document.querySelector("#feedSection"),
    feedBack: document.querySelector("#feedBack"),
    qualityCard: document.querySelector("#qualityCard"),
    submissionList: document.querySelector("#submissionList"),
    tokenPastePanel: document.querySelector("#tokenPastePanel"),
    tokenPasteInput: document.querySelector("#tokenPasteInput"),
    tokenPasteSave: document.querySelector("#tokenPasteSave"),
    tokenPasteError: document.querySelector("#tokenPasteError"),
    interfaceVersion: document.querySelector("#interfaceVersion"),
    pipelineVersion: document.querySelector("#pipelineVersion"),
  };
  const CAPTURE_PANEL_SELECTOR = "#captureForm, .photo-input-panel, .voice-panel, .note-panel";
  const SUCCESS_TAKEOVER_SELECTOR = `${CAPTURE_PANEL_SELECTOR}, #tokenPastePanel`;

  function setStatus(message, tone = "") {
    elements.statusText.textContent = message;
    if (tone) {
      elements.statusText.dataset.tone = tone;
    } else {
      delete elements.statusText.dataset.tone;
    }
  }

  function normalizedScreenMode(value) {
    return value === "light" || value === "dark" ? value : "system";
  }

  function preferredDarkMedia() {
    return window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  }

  function currentScreenMode() {
    try {
      return normalizedScreenMode(localStorage.getItem(SCREEN_MODE_KEY));
    } catch (_error) {
      return "system";
    }
  }

  function applyScreenMode(mode = currentScreenMode()) {
    const normalized = normalizedScreenMode(mode);
    const prefersDark = preferredDarkMedia()?.matches || false;
    const theme = normalized === "dark" || (normalized === "system" && prefersDark) ? "dark" : "light";
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.screenMode = normalized;
    if (elements.themeColorMeta) elements.themeColorMeta.content = theme === "dark" ? "#101416" : "#f7f8f6";
    elements.screenModeInputs.forEach((input) => {
      input.checked = input.value === normalized;
    });
  }

  function persistScreenMode(mode) {
    const normalized = normalizedScreenMode(mode);
    try {
      localStorage.setItem(SCREEN_MODE_KEY, normalized);
    } catch (_error) {}
    applyScreenMode(normalized);
  }

  function firstNameFromSession(session) {
    const name = (session?.person?.name || "").trim();
    if (!name) return "";
    if (name.includes(",")) {
      const after = name.split(",", 2)[1] || "";
      return after.trim().split(/\s+/)[0] || "";
    }
    return name.split(/\s+/)[0] || "";
  }

  function readyStatusText(session = state.session) {
    const firstName = firstNameFromSession(session);
    return firstName ? `Ready for ${firstName}` : "Ready to save a capture.";
  }

  function tokenFromHash() {
    const raw = window.location.hash || "";
    if (!raw) return "";
    const stripped = raw.startsWith("#") ? raw.slice(1) : raw;
    const params = new URLSearchParams(stripped);
    return params.get("token") || params.get("access") || (stripped.includes("=") ? "" : stripped);
  }

  function setTokenCookie(value) {
    document.cookie = `${TOKEN_KEY}=${encodeURIComponent(value)}; max-age=31536000; path=/; secure; samesite=lax`;
  }

  function clearPersistedToken() {
    token = "";
    localStorage.removeItem(TOKEN_KEY);
    document.cookie = `${TOKEN_KEY}=; max-age=0; path=/; secure; samesite=lax`;
  }

  function readTokenCookie() {
    const match = document.cookie.split("; ").find((row) => row.startsWith(`${TOKEN_KEY}=`));
    if (!match) return "";
    try {
      return decodeURIComponent(match.slice(TOKEN_KEY.length + 1));
    } catch (_error) {
      return "";
    }
  }

  function persistToken(value) {
    token = value;
    localStorage.setItem(TOKEN_KEY, value);
    setTokenCookie(value);
    stashTokenForServiceWorker(value);
    history.replaceState({}, "", window.location.pathname);
  }

  function bootstrapToken() {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("token") || "";
    const fromUrlOrHash = fromUrl || tokenFromHash();
    if (fromUrlOrHash) {
      persistToken(fromUrlOrHash);
      return true;
    }
    token = localStorage.getItem(TOKEN_KEY) || readTokenCookie();
    if (token) {
      // Ensure the cookie is present so <img src="/media/..."> requests authenticate.
      setTokenCookie(token);
      return true;
    }
    showTokenPasteUI("Paste your access token to enable capture.");
    return false;
  }

  function showTokenPasteUI(message) {
    setFormEnabled(false);
    elements.tokenPastePanel.hidden = false;
    elements.tokenPasteError.hidden = true;
    elements.tokenPasteError.textContent = "";
    setStatus(message, "warning");
  }

  function localIsoTimestamp() {
    const date = new Date();
    const offsetMinutes = -date.getTimezoneOffset();
    const sign = offsetMinutes >= 0 ? "+" : "-";
    const absoluteOffset = Math.abs(offsetMinutes);
    const pad = (value) => String(value).padStart(2, "0");
    const timezone = `${sign}${pad(Math.floor(absoluteOffset / 60))}:${pad(absoluteOffset % 60)}`;
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}${timezone}`;
  }

  async function stashTokenForServiceWorker(value) {
    if (!window.fieldCaptureDb?.isSupported() || !value) return;
    try {
      await window.fieldCaptureDb.putCapture({
        capture_id: "__token__",
        value,
        status: "__meta__",
        createdAt: localIsoTimestamp(),
      });
    } catch (_error) {
      /* Foreground drain remains available. */
    }
  }

  function fileStem(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 48) || "field-capture";
  }

  function randomSuffix() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID().slice(0, 8);
    }
    return Math.random().toString(36).slice(2, 10);
  }

  function hasAudio() {
    return Boolean(state.audio || state.recorder?.state === "recording" || state.recorder?.state === "paused");
  }

  function formatDuration(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds || 0));
    const minutes = Math.floor(seconds / 60);
    const remaining = seconds % 60;
    return `${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}`;
  }

  function activeAudioElapsedMs() {
    if (state.audio) return (state.audio.durationSeconds || 0) * 1000;
    let elapsed = state.audioElapsedBeforePause || 0;
    if (state.recorder?.state === "recording") {
      elapsed += Date.now() - (state.audioResumedAt || state.audioStartedAt || Date.now());
    }
    return elapsed;
  }

  function updateVoiceDuration() {
    elements.voiceDuration.textContent = formatDuration(activeAudioElapsedMs() / 1000);
  }

  function startDurationTicker() {
    window.clearInterval(state.audioTickTimer);
    state.audioTickTimer = window.setInterval(updateVoiceDuration, 500);
    updateVoiceDuration();
  }

  function stopDurationTicker() {
    window.clearInterval(state.audioTickTimer);
    state.audioTickTimer = null;
    updateVoiceDuration();
  }

  function setFormEnabled(enabled) {
    state.formEnabled = enabled;
    elements.siteInput.disabled = !enabled;
    elements.categoryInput.disabled = !enabled;
    elements.notesInput.disabled = !enabled;
    elements.noteToggle.disabled = !enabled;
    elements.noteEditor.disabled = !enabled;
    if (!enabled) closeNoteEditor();
    elements.fileInput.disabled = !enabled;
    elements.cameraInput.disabled = !enabled;
    renderPhotos();
    renderAudio();
    updateSubmitState();
  }

  function syncNoteAffordance() {
    const hasNote = Boolean((elements.notesInput.value || "").trim());
    elements.noteToggle.classList.toggle("is-active", hasNote);
    elements.noteToggle.setAttribute("aria-label", hasNote ? "Edit location or short note" : "Add location or short note");
  }

  function saveNoteFromEditor() {
    elements.notesInput.value = elements.noteEditor.value || "";
    syncNoteAffordance();
    updateSubmitState();
  }

  function openNoteEditor() {
    if (!state.formEnabled) return;
    elements.noteEditor.value = elements.notesInput.value || "";
    elements.noteEditorPanel.hidden = false;
    elements.noteToggle.setAttribute("aria-expanded", "true");
    window.requestAnimationFrame(() => elements.noteEditor.focus());
  }

  function closeNoteEditor() {
    if (elements.noteEditorPanel.hidden) return;
    saveNoteFromEditor();
    elements.noteEditorPanel.hidden = true;
    elements.noteToggle.setAttribute("aria-expanded", "false");
  }

  function updateSubmitState() {
    const hasNote = Boolean((elements.notesInput.value || "").trim());
    const hasAsset = state.photos.length > 0 || hasAudio() || hasNote;
    elements.submitButton.disabled = !state.formEnabled || state.isSubmitting || state.isProcessingPhotos || !state.session || !hasAsset;
    updateSubmitSummary();
  }

  function updateSubmitSummary() {
    const parts = [];
    if (state.photos.length) parts.push(`${state.photos.length} photo${state.photos.length === 1 ? "" : "s"}`);
    if (state.audio?.durationSeconds) parts.push(`${state.audio.durationSeconds}s voice note`);
    if (!parts.length && hasAudio()) parts.push("recording voice note");
    if ((elements.notesInput.value || "").trim()) parts.push("text note");
    elements.submitSummary.textContent = parts.length ? `${parts.join(" + ")} ready to save` : "Add a photo, voice note, or text note to enable submit.";
  }

  function renderSites(sites) {
    state.sitesById.clear();
    elements.siteInput.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = sites.length ? "Select site..." : "No assigned sites";
    elements.siteInput.append(placeholder);
    sites.forEach((site) => {
      const option = document.createElement("option");
      option.value = site.label || site.site_id;
      option.textContent = site.label || site.site_id;
      option.dataset.siteId = site.site_id || "";
      option.dataset.targetType = "location";
      option.dataset.targetId = site.site_id || "";
      state.sitesById.set(String(site.site_id || ""), site);
      elements.siteInput.append(option);
    });
    const recentSiteId = localStorage.getItem(RECENT_SITE_KEY) || "";
    const recent = Array.from(elements.siteInput.options).find((option) => option.dataset.siteId === recentSiteId);
    if (recent) {
      elements.siteInput.value = recent.value;
    } else if (sites.length === 1) {
      elements.siteInput.selectedIndex = 1;
    }
    renderCategoriesForSelectedSite();
  }

  function renderSiteGuidance(site) {
    const target = elements.siteGuidance;
    if (!target) return;
    const guidance = site && typeof site.capture_guidance === "string" ? site.capture_guidance.trim() : "";
    target.textContent = guidance;
    target.hidden = !guidance;
  }

  function renderCategoriesForSelectedSite() {
    const selected = elements.siteInput.selectedOptions[0];
    const siteId = selected?.dataset.siteId || "";
    if (siteId) localStorage.setItem(RECENT_SITE_KEY, siteId);
    const site = state.sitesById.get(siteId);
    renderSiteGuidance(site);
    const categories = Array.isArray(site?.display_categories) ? site.display_categories : [];
    elements.categoryInput.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = categories.length ? "Select category..." : "No categories";
    elements.categoryInput.append(placeholder);
    categories.forEach((category) => {
      const option = document.createElement("option");
      option.value = category.slug || category.label || category.name || String(category);
      option.textContent = category.label || category.name || category.slug || String(category);
      elements.categoryInput.append(option);
    });
    if (categories.length === 1) elements.categoryInput.selectedIndex = 1;
  }

  function setOfflineBanner(show) {
    const el = typeof document !== "undefined" && document.getElementById ? document.getElementById("offlineBanner") : null;
    if (el) el.hidden = !show;
  }

  // Persist the session (sites/categories/max_images) so the form can be enabled
  // offline. Same __meta__ row pattern as the SW token (stashTokenForServiceWorker).
  async function cacheSession(session) {
    if (!window.fieldCaptureDb?.isSupported() || !session) return;
    try {
      await window.fieldCaptureDb.putCapture({
        capture_id: "__session__",
        status: "__meta__",
        value: session,
        createdAt: localIsoTimestamp(),
      });
    } catch (_e) {
      /* offline fallback simply won't be available */
    }
  }

  async function loadCachedSession() {
    if (!window.fieldCaptureDb?.isSupported()) return null;
    try {
      const row = await window.fieldCaptureDb.getCapture("__session__");
      return row?.value || null;
    } catch (_e) {
      return null;
    }
  }

  function applySession(session, fromCache) {
    state.session = session;
    state.maxPhotos = Number(session.max_images) || DEFAULT_MAX_PHOTOS;
    elements.tokenPastePanel.hidden = true;
    elements.tokenPasteError.hidden = true;
    renderSites(session.sites || []);
    const enabled = Boolean(session.can_submit && (session.sites || []).length);
    setFormEnabled(enabled);
    if (!fromCache) {
      setStatus(enabled ? readyStatusText(session) : "This token cannot submit captures.", enabled ? "" : "warning");
    }
    return enabled;
  }

  async function applyCachedSessionIfAvailable() {
    const cached = await loadCachedSession();
    if (!cached) return false;
    applySession(cached, true);
    setOfflineBanner(true);
    return true;
  }

  async function loadSession() {
    if (!token) {
      showTokenPasteUI("Paste your access token to enable capture.");
      return;
    }
    try {
      const response = await fetch("/api/session", {
        headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
        cache: "no-store",
      });
      if (!response.ok) {
        if (response.status === 401) {
          // Definitive rejection — never fall back to a cached session.
          state.session = null;
          setFormEnabled(false);
          clearPersistedToken();
          showTokenPasteUI("Token is invalid, expired, or revoked.");
          return;
        }
        // Server reachable but erroring — prefer a cached session if we have one.
        if (await applyCachedSessionIfAvailable()) return;
        state.session = null;
        setFormEnabled(false);
        setStatus("Session unavailable. Capture is disabled.", "error");
        return;
      }
      const session = await response.json();
      await stashTokenForServiceWorker(token);
      await cacheSession(session);
      setOfflineBanner(false);
      applySession(session, false);
      refreshMySubmissionsBadge().catch(() => {});
    } catch (_error) {
      // Network failure / offline — run on the last-known site list if we have it.
      if (await applyCachedSessionIfAvailable()) return;
      state.session = null;
      setFormEnabled(false);
      setStatus("Could not verify session. Capture is disabled.", "error");
    }
  }

  const SEEN_ACTED_ON_KEY = "unifiedCaptureSeenActedOn";

  function loadSeenActedOn() {
    try {
      const raw = localStorage.getItem(SEEN_ACTED_ON_KEY);
      if (raw) return new Set(JSON.parse(raw));
    } catch (_error) {}
    return new Set();
  }

  function saveSeenActedOn(set) {
    try {
      localStorage.setItem(SEEN_ACTED_ON_KEY, JSON.stringify([...set]));
    } catch (_error) {}
  }

  function updateBadge(submissions) {
    if (!elements.mySubsBadge) return;
    const seen = loadSeenActedOn();
    const hasUnseen = (submissions || []).some(
      (submission) => submission.track === "B" && submission.stage === "acted_on" && !seen.has(submission.capture_id),
    );
    elements.mySubsBadge.hidden = !hasUnseen;
  }

  function markAllActedOnAsSeen(submissions) {
    const seen = loadSeenActedOn();
    (submissions || []).forEach((submission) => {
      if (submission.track === "B" && submission.stage === "acted_on") {
        seen.add(submission.capture_id);
      }
    });
    saveSeenActedOn(seen);
    if (elements.mySubsBadge) elements.mySubsBadge.hidden = true;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatRelativeTime(isoString) {
    if (!isoString) return "";
    let date;
    try {
      date = new Date(isoString);
      if (Number.isNaN(date.getTime())) return isoString;
    } catch (_error) {
      return isoString;
    }
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterdayStart = new Date(todayStart);
    yesterdayStart.setDate(yesterdayStart.getDate() - 1);
    const weekStart = new Date(todayStart);
    weekStart.setDate(weekStart.getDate() - 6);
    if (date >= todayStart) {
      return `Today ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    }
    if (date >= yesterdayStart) return "Yesterday";
    if (date >= weekStart) return date.toLocaleDateString([], { weekday: "short" });
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function renderQualityCard(qualitySummary) {
    elements.qualityCard.innerHTML = "";
    if (!qualitySummary || qualitySummary.total_processed === 0) return;
    const { total_processed, clear, flag_counts } = qualitySummary;
    const hasFlags = Object.keys(flag_counts || {}).length > 0;
    let html = '<div class="quality-headline"><span>Your photo quality</span> ';
    if (!hasFlags) {
      html += `<strong>All of your last ${total_processed} photos were clear and ready to use.</strong>`;
    } else {
      html += `<strong>${clear} of your last ${total_processed} photos were clear and ready to use.</strong>`;
    }
    html += "</div>";
    if (hasFlags) {
      const coachingTips = {
        blurry: "A few came out blurry - tap to focus, then hold still for a second before the shot.",
        motion_blur: "A few came out blurry from movement - hold the phone steady for a moment after you tap.",
        too_dark: "A few came out dark - switching on a light or using your camera flash makes them easier to use.",
        too_bright: "A few came out washed out - step back from the window or strong light.",
        glare: "A few had glare - shift your angle slightly to avoid the reflection.",
        out_of_frame: "A few had the subject cut off - back up a step so the whole area is in the shot.",
        partly_obscured: "A few had something in the way - make sure nothing is in front of what you are photographing.",
        low_resolution: "A few were low quality - use the in-app camera rather than a screenshot or forwarded image.",
        contains_people: "A few had a person in the shot - frame the area so people are not in the photo.",
        unanalyzable: "A few could not be read - re-take in better light, holding steady.",
      };
      Object.entries(flag_counts || {})
        .filter(([, count]) => count >= 3)
        .sort(([, a], [, b]) => b - a)
        .slice(0, 2)
        .forEach(([flag]) => {
          const tip = coachingTips[flag];
          if (tip) html += `<p class="quality-tip">Tip: ${escapeHtml(tip)}</p>`;
        });
    }
    elements.qualityCard.innerHTML = html;
  }

  function buildStatusPill(submission) {
    let label = "Under Review";
    let tone = "";
    if (submission.track === "A") {
      if (submission.stage === "processed") {
        label = "Analyzed";
        tone = "active";
      } else {
        label = "Analyzing...";
      }
    } else if (submission.stage === "acted_on") {
      label = submission.outcome_label || "Acted on";
      tone = "active";
    } else if (submission.stage === "reviewed") {
      label = submission.outcome_label || "Reviewed";
      tone = "done";
    }
    const toneAttr = tone ? ` data-tone="${tone}"` : "";
    return `<span class="status-pill"${toneAttr}>${escapeHtml(label)}</span>`;
  }

  function buildQualityChips(perPhotoQuality) {
    if (!Array.isArray(perPhotoQuality) || perPhotoQuality.length === 0) return "";
    const workerLabels = {
      blurry: "blurry",
      motion_blur: "blurry (movement)",
      too_dark: "too dark",
      too_bright: "too bright",
      glare: "glare",
      out_of_frame: "subject cut off",
      partly_obscured: "subject blocked",
      low_resolution: "low quality",
      contains_people: "person in shot",
      unanalyzable: "could not be read",
    };
    const counts = {};
    perPhotoQuality.forEach((photo) => {
      if ((photo.severity || "ok") === "ok" && (!photo.flags || photo.flags.length === 0)) return;
      (photo.flags || []).forEach((flag) => {
        counts[flag] = (counts[flag] || 0) + 1;
      });
    });
    return Object.entries(counts)
      .sort(([, a], [, b]) => b - a)
      .slice(0, 2)
      .map(([flag, count]) => `<span class="quality-chip">${count} ${count === 1 ? "photo" : "photos"} ${escapeHtml(workerLabels[flag] || flag)}</span>`)
      .join("");
  }

  function truncateText(value, maxLength) {
    const text = String(value || "").trim();
    if (!text) return "";
    if (text.length <= maxLength) return text;
    return `${text.slice(0, Math.max(0, maxLength - 3)).trimEnd()}...`;
  }

  function buildAnalysisLine(perPhotoQuality) {
    if (!Array.isArray(perPhotoQuality) || perPhotoQuality.length === 0) return "";
    const firstPhoto = perPhotoQuality[0] || {};
    const description = truncateText(firstPhoto.description, 140);
    if (!description) return "";
    const issues = Array.isArray(firstPhoto.possible_issues) ? firstPhoto.possible_issues : [];
    const issueChips = issues
      .filter((issue) => String(issue || "").trim())
      .slice(0, 3)
      .map((issue) => `<span class="analysis-chip">${escapeHtml(truncateText(issue, 48))}</span>`)
      .join("");
    return `<div class="sub-analysis-line"><span class="sub-analysis-description">${escapeHtml(description)}</span>${issueChips}</div>`;
  }

  function buildDetailStepper(submission) {
    if (submission.track === "A") {
      const step2Class = submission.stage === "processed" ? "step step--done" : "step step--current";
      const step2Label = submission.stage === "processed" ? "Analyzed" : "Analyzing...";
      return `<div class="stepper stepper--track-a">
        <span class="step step--done">Submitted</span>
        <span class="step-arrow">-&gt;</span>
        <span class="${step2Class}">${step2Label}</span>
      </div>`;
    }
    const outcomeLabel = escapeHtml(submission.outcome_label || "Acted on");
    if (submission.stage === "processing") {
      return `<div class="stepper stepper--track-b">
        <span class="step step--done">Submitted</span>
        <span class="step-arrow">-&gt;</span>
        <span class="step step--current">Under Review</span>
        <span class="step-arrow">-&gt;</span>
        <span class="step step--future">${outcomeLabel}</span>
      </div>`;
    }
    return `<div class="stepper stepper--track-b">
      <span class="step step--done">Submitted</span>
      <span class="step-arrow">-&gt;</span>
      <span class="step step--done">Reviewed</span>
      <span class="step-arrow">-&gt;</span>
      <span class="step step--done">${outcomeLabel}</span>
    </div>`;
  }

  function buildDetailContent(submission) {
    const workerLabels = {
      blurry: "blurry",
      motion_blur: "blurry (movement)",
      too_dark: "too dark",
      too_bright: "too bright",
      glare: "glare",
      out_of_frame: "subject cut off",
      partly_obscured: "subject blocked",
      low_resolution: "low quality",
      contains_people: "person in shot",
      unanalyzable: "could not be read",
    };
    const coachingTips = {
      blurry: "Tap to focus, then hold still for a second before the shot.",
      motion_blur: "Hold the phone steady for a moment after you tap.",
      too_dark: "Switching on a light or using flash makes the photo easier to use.",
      too_bright: "Step back from the window or strong light.",
      glare: "Shift your angle slightly to avoid the reflection.",
      out_of_frame: "Back up a step so the whole area is in the shot.",
      partly_obscured: "Make sure nothing is in front of what you are photographing.",
      low_resolution: "Use the in-app camera rather than a screenshot or forwarded image.",
      contains_people: "Frame the area so people are not in the photo.",
      unanalyzable: "Re-take in better light, holding steady.",
    };
    let html = "";
    const detailUrls = Array.isArray(submission.photo_urls) ? submission.photo_urls : [];
    if (Array.isArray(submission.per_photo_quality) && submission.per_photo_quality.length > 0) {
      html += '<div class="detail-photos">';
      submission.per_photo_quality.forEach((photo, index) => {
        const hasFlags = Array.isArray(photo.flags) && photo.flags.length > 0;
        html += '<div class="detail-photo-item">';
        if (detailUrls[index]) {
          html += `<img class="detail-thumb" src="${escapeHtml(detailUrls[index])}" alt="Photo ${index + 1}" loading="lazy">`;
        }
        if (photo.description) html += `<p class="photo-description">${escapeHtml(photo.description)}</p>`;
        if (Array.isArray(photo.possible_issues) && photo.possible_issues.length > 0) {
          html += `<ul class="photo-issues">${photo.possible_issues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>`;
        }
        if ((photo.severity || "ok") === "ok" && !hasFlags) {
          if (!photo.description) html += '<span class="photo-ok">Looks good</span>';
        } else {
          (photo.flags || []).forEach((flag) => {
            html += `<span class="photo-flag">${escapeHtml(workerLabels[flag] || flag)}</span>`;
            if (coachingTips[flag]) html += `<span class="photo-tip">${escapeHtml(coachingTips[flag])}</span>`;
          });
        }
        html += "</div>";
      });
      html += "</div>";
    }
    if (submission.note_text) {
      html += `<div class="detail-note"><span class="detail-note-label">Note</span><p class="detail-note-text">${escapeHtml(submission.note_text)}</p></div>`;
    }
    let capturedAt = submission.captured_at;
    try {
      capturedAt = new Date(submission.captured_at).toLocaleString([], {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (_error) {}
    html += `<div class="detail-meta">
      <div>${escapeHtml(submission.site_name || "")}</div>
      <div>${escapeHtml(capturedAt || "")}</div>
      <div class="detail-capture-id">${escapeHtml(submission.capture_id || "")}</div>
    </div>`;
    html += buildDetailStepper(submission);
    return html;
  }

  function toggleSubmissionDetail(row, submission) {
    document.querySelectorAll(".sub-detail").forEach((detail) => {
      if (detail.closest("li") !== row) detail.hidden = true;
    });
    let detail = row.querySelector(".sub-detail");
    if (!detail) {
      detail = document.createElement("div");
      detail.className = "sub-detail";
      detail.innerHTML = buildDetailContent(submission);
      row.append(detail);
      return;
    }
    detail.hidden = !detail.hidden;
  }

  function renderFeedMessage(message, tone = "") {
    elements.qualityCard.innerHTML = "";
    elements.submissionList.innerHTML = "";
    const item = document.createElement("li");
    item.className = "sub-empty";
    if (tone) item.dataset.tone = tone;
    item.textContent = message;
    elements.submissionList.append(item);
  }

  function renderSubmissionList(submissions) {
    elements.submissionList.innerHTML = "";
    if (!submissions || submissions.length === 0) {
      renderFeedMessage("Your submissions will show up here. Take a photo to get started.");
      return;
    }
    submissions.forEach((submission) => {
      const row = document.createElement("li");
      row.className = "sub-row";
      row.dataset.captureId = submission.capture_id || "";
      const urls = Array.isArray(submission.photo_urls) ? submission.photo_urls : [];
      const photoCount = Number(submission.photo_count || urls.length || 0);
      const parts = [`${photoCount} photo${photoCount === 1 ? "" : "s"}`];
      if (submission.has_audio) parts.push("voice note");
      const noteSnippet = submission.note_text
        ? `<div class="sub-note-snippet">${escapeHtml(submission.note_text.length > 120 ? `${submission.note_text.slice(0, 120)}...` : submission.note_text)}</div>`
        : "";
      const analysisLine = buildAnalysisLine(submission.per_photo_quality);
      const photoStrip = urls.length
        ? `<div class="sub-photo-strip">${urls.map((url) => `<img src="${escapeHtml(url)}" alt="" loading="lazy">`).join("")}</div>`
        : "";
      row.innerHTML = `
        <div class="sub-body">
          <div class="sub-meta">${escapeHtml(submission.site_name || "Submission")} &middot; ${escapeHtml(formatRelativeTime(submission.captured_at))}</div>
          <div class="sub-contents">${escapeHtml(parts.join(" / "))}</div>
          ${noteSnippet}
          ${analysisLine}
          <div class="sub-status">
            ${buildStatusPill(submission)}
            ${buildQualityChips(submission.per_photo_quality)}
          </div>
        </div>
        ${photoStrip}
      `;
      row.addEventListener("click", () => toggleSubmissionDetail(row, submission));
      elements.submissionList.append(row);
    });
  }

  async function fetchMySubmissions() {
    if (!token) {
      const error = new Error("missing_token");
      error.status = 401;
      throw error;
    }
    const response = await fetch("/api/my-submissions", {
      headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (!response.ok) {
      const error = new Error(`my_submissions_${response.status}`);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  async function refreshMySubmissionsBadge() {
    if (!token || !state.session) return;
    const data = await fetchMySubmissions();
    updateBadge(data.submissions || []);
  }

  async function showFeedView() {
    document.querySelectorAll(`${SUCCESS_TAKEOVER_SELECTOR}, #successScreen`).forEach((panel) => {
      panel.hidden = true;
    });
    elements.feedSection.hidden = false;
    renderFeedMessage("Loading submissions...");
    setStatus("My Submissions");
    try {
      const data = await fetchMySubmissions();
      const submissions = data.submissions || [];
      renderQualityCard(data.quality_summary || null);
      renderSubmissionList(submissions);
      markAllActedOnAsSeen(submissions);
    } catch (error) {
      if (error.status === 401) {
        clearPersistedToken();
        renderFeedMessage("Token is invalid, expired, or revoked. Go back and paste your access token.", "error");
      } else if (error.status === 403) {
        renderFeedMessage("This token is not authorized to view submissions.", "error");
      } else if (error.status === 503) {
        renderFeedMessage("Submissions are temporarily unavailable. Try again in a moment.", "error");
      } else {
        renderFeedMessage("Could not load submissions. Try again in a moment.", "error");
      }
    }
  }

  function hideFeedView() {
    elements.feedSection.hidden = true;
    if (!token || !state.session) {
      showTokenPasteUI("Paste your access token to enable capture.");
      return;
    }
    resetToForm();
  }

  async function decodeToCanvasSource(fileOrBlob) {
    if (typeof createImageBitmap === "function") {
      try {
        return await createImageBitmap(fileOrBlob);
      } catch (_error) {
        /* Fall back to image decode for older Safari/WebView engines. */
      }
    }
    const url = URL.createObjectURL(fileOrBlob);
    try {
      return await new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error("image_decode_failed"));
        image.src = url;
      });
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  async function normalizeImage(fileOrBlob, suggestedName) {
    const source = await decodeToCanvasSource(fileOrBlob);
    const maxSide = 1600;
    const scale = Math.min(1, maxSide / Math.max(source.width, source.height));
    const width = Math.max(1, Math.round(source.width * scale));
    const height = Math.max(1, Math.round(source.height * scale));
    const canvas = elements.canvas;
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    context.drawImage(source, 0, 0, width, height);
    if (typeof source.close === "function") source.close();
    const blob = await new Promise((resolve, reject) => {
      canvas.toBlob((result) => (result && result.size > 0 ? resolve(result) : reject(new Error("toBlob_returned_empty"))), "image/jpeg", 0.86);
    });
    const timestamp = localIsoTimestamp().replace(/[:.]/g, "-");
    return {
      filename: `${fileStem(suggestedName)}-${timestamp}.jpg`,
      mimeType: "image/jpeg",
      blob,
      previewUrl: "",
      note: "",
    };
  }

  function autoGrowPhotoNote(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = `${textarea.scrollHeight}px`;
  }

  async function addFiles(files) {
    const selected = Array.from(files).filter((file) => file.type.startsWith("image/"));
    if (!selected.length) {
      setStatus("No image selected", "warning");
      return;
    }
    const room = state.maxPhotos - state.photos.length;
    if (room <= 0) {
      setStatus(`Photo limit reached (${state.maxPhotos}). Remove one to add another.`, "warning");
      return;
    }
    const accepted = selected.slice(0, room);
    const dropped = selected.length - accepted.length;
    state.isProcessingPhotos = true;
    updateSubmitState();
    setStatus(`Processing ${accepted.length} photo${accepted.length === 1 ? "" : "s"}...`);
    try {
      for (const file of accepted) {
        state.photos.push(await normalizeImage(file, file.name));
        renderPhotos();
      }
      if (dropped > 0) setStatus(`Photo limit is ${state.maxPhotos}; ${dropped} not added.`, "warning");
      else setStatus("Photo ready.");
    } catch (_error) {
      setStatus("Could not process selected photo.", "error");
    } finally {
      state.isProcessingPhotos = false;
      elements.fileInput.value = "";
      elements.cameraInput.value = "";
      renderPhotos();
    }
  }

  function renderPhotos() {
    elements.thumbnailGrid.replaceChildren();
    state.photos.forEach((photo, index) => {
      const card = document.createElement("div");
      card.className = "thumbnail-card";
      const preview = document.createElement("div");
      preview.className = "thumbnail-preview";
      const image = document.createElement("img");
      if (!photo.previewUrl) photo.previewUrl = URL.createObjectURL(photo.blob);
      image.src = photo.previewUrl;
      image.alt = photo.filename;
      const remove = document.createElement("button");
      remove.className = "remove-photo";
      remove.type = "button";
      remove.textContent = "X";
      remove.title = "Remove photo";
      remove.disabled = !state.formEnabled || state.isSubmitting;
      remove.addEventListener("click", () => {
        if (state.photos[index]?.previewUrl) URL.revokeObjectURL(state.photos[index].previewUrl);
        state.photos.splice(index, 1);
        renderPhotos();
        setStatus("Photo removed.");
      });
      const noteField = elements.photoNoteTemplate.content.firstElementChild.cloneNode(true);
      const noteInput = noteField.querySelector(".photo-note-input");
      noteInput.value = photo.note || "";
      noteInput.disabled = !state.formEnabled || state.isSubmitting;
      noteInput.addEventListener("input", () => {
        photo.note = noteInput.value || "";
        autoGrowPhotoNote(noteInput);
      });
      preview.append(image, remove);
      card.append(preview, noteField);
      elements.thumbnailGrid.append(card);
      autoGrowPhotoNote(noteInput);
    });
    elements.photoCount.textContent = `${state.photos.length} of ${state.maxPhotos}`;
    updateSubmitState();
  }

  function renderAudio() {
    const recState = state.recorder?.state || "";
    const isRecording = recState === "recording";
    const isPaused = recState === "paused";
    const supportsAudio = window.btqRecorder?.supportsAudioRecording?.() || false;
    elements.recordVoiceButton.disabled = !state.formEnabled || state.isSubmitting || state.isStartingAudio || isRecording || !supportsAudio;
    elements.stopVoiceButton.disabled = !state.formEnabled || state.isSubmitting || !isRecording;
    elements.clearVoiceButton.disabled = !state.formEnabled || state.isSubmitting || (!state.audio && !isRecording && !isPaused);
    if (state.audio) {
      elements.voicePreview.src = state.audio.url;
      elements.voicePreview.hidden = false;
      if (!isRecording && !isPaused) elements.voiceStatus.textContent = `Ready: ${state.audio.durationSeconds}s`;
    } else {
      elements.voicePreview.removeAttribute("src");
      elements.voicePreview.hidden = true;
      if (isRecording) elements.voiceStatus.textContent = "Recording...";
      else if (isPaused) elements.voiceStatus.textContent = "Paused";
      else elements.voiceStatus.textContent = supportsAudio ? "Optional" : "Unavailable";
    }
    elements.voiceStatus.dataset.tone = isRecording ? "active" : isPaused ? "paused" : "";
    elements.voiceSupportMessage.hidden = supportsAudio;
    updateVoiceDuration();
    updateSubmitState();
  }

  async function startVoiceRecording(event) {
    event?.preventDefault();
    if (state.isStartingAudio || state.recorder?.state === "recording" || !window.btqRecorder?.supportsAudioRecording?.()) return;
    let stream = null;
    try {
      state.isStartingAudio = true;
      setStatus("Requesting microphone access...");
      renderAudio();
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = window.btqRecorder.preferredAudioMimeType();
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      clearAudio();
      state.audioChunks = [];
      state.audioStartedAt = Date.now();
      state.audioElapsedBeforePause = 0;
      state.audioResumedAt = null;
      recorder.addEventListener("dataavailable", (eventData) => {
        if (eventData.data?.size) state.audioChunks.push(eventData.data);
      });
      recorder.addEventListener("stop", () => finishVoiceRecording(recorder));
      state.recorder = recorder;
      recorder.start();
      state.audioStopTimer = window.setTimeout(stopVoiceRecording, 600_000);
      state.isStartingAudio = false;
      setStatus("Recording voice note...");
      startDurationTicker();
      renderAudio();
    } catch (error) {
      stream?.getTracks().forEach((track) => track.stop());
      state.isStartingAudio = false;
      setStatus(window.btqRecorder?.microphoneErrorMessage?.(error) || "Could not start voice recording.", "error");
      renderAudio();
    }
  }

  function stopVoiceRecording() {
    if (state.recorder?.state !== "recording") return;
    const segmentElapsed = state.audioResumedAt ? Date.now() - state.audioResumedAt : Date.now() - state.audioStartedAt;
    state.audioElapsedBeforePause = (state.audioElapsedBeforePause || 0) + segmentElapsed;
    state.audioResumedAt = null;
    window.clearTimeout(state.audioStopTimer);
    state.audioStopTimer = null;
    stopDurationTicker();
    setStatus("Stopping voice note...");
    state.recorder.stop();
    renderAudio();
  }

  function finishVoiceRecording(recorder) {
    window.clearTimeout(state.audioStopTimer);
    state.audioStopTimer = null;
    recorder.stream.getTracks().forEach((track) => track.stop());
    if (state.recorder !== recorder) return;
    const mimeType = recorder.mimeType || state.audioChunks[0]?.type || "audio/webm";
    const blob = new Blob(state.audioChunks, { type: mimeType });
    const durationSeconds = Math.max(1, Math.round((state.audioElapsedBeforePause || 0) / 1000));
    const timestamp = localIsoTimestamp().replace(/[:.]/g, "-");
    state.audio = {
      blob,
      url: URL.createObjectURL(blob),
      filename: `voice-note-${timestamp}.${window.btqRecorder.audioExtension(mimeType)}`,
      mimeType,
      durationSeconds,
    };
    state.recorder = null;
    state.audioChunks = [];
    state.audioElapsedBeforePause = 0;
    state.audioResumedAt = null;
    setStatus("Voice note recorded.");
    stopDurationTicker();
    renderAudio();
  }

  function finishActiveVoiceRecordingForSubmit() {
    const recState = state.recorder?.state;
    if (recState !== "recording" && recState !== "paused") return Promise.resolve();
    const recorder = state.recorder;
    setStatus("Finishing voice note...");
    renderAudio();
    return new Promise((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        cleanup();
        reject(new Error("Could not finish voice recording before submit."));
      }, 5000);
      const cleanup = () => {
        window.clearTimeout(timeout);
        recorder.removeEventListener("stop", handleStop);
        recorder.removeEventListener("error", handleError);
      };
      const handleStop = () => {
        cleanup();
        resolve();
      };
      const handleError = () => {
        cleanup();
        reject(new Error("Could not finish voice recording before submit."));
      };
      recorder.addEventListener("stop", handleStop);
      recorder.addEventListener("error", handleError);
      if (recorder.state === "recording") {
        const segmentElapsed = state.audioResumedAt ? Date.now() - state.audioResumedAt : Date.now() - state.audioStartedAt;
        state.audioElapsedBeforePause = (state.audioElapsedBeforePause || 0) + segmentElapsed;
        state.audioResumedAt = null;
      }
      stopDurationTicker();
      recorder.stop();
    });
  }

  function clearAudio() {
    if (state.recorder) {
      state.recorder.stream.getTracks().forEach((track) => track.stop());
      state.recorder = null;
      state.audioChunks = [];
      state.audioElapsedBeforePause = 0;
      state.audioResumedAt = null;
      window.clearTimeout(state.audioStopTimer);
      window.clearInterval(state.audioTickTimer);
      state.audioStopTimer = null;
      state.audioTickTimer = null;
    }
    if (state.audio?.url) URL.revokeObjectURL(state.audio.url);
    state.audio = null;
    state.audioElapsedBeforePause = 0;
    state.audioResumedAt = null;
    elements.voicePreview.removeAttribute("src");
    elements.voicePreview.load();
    renderAudio();
  }

  function selectedTarget() {
    const option = elements.siteInput.selectedOptions[0];
    return {
      site: elements.siteInput.value.trim(),
      siteId: option?.dataset.siteId || "",
      targetType: option?.dataset.targetType || "location",
      targetId: option?.dataset.targetId || option?.dataset.siteId || "",
    };
  }

  async function buildCaptureRecord() {
    const target = selectedTarget();
    const qcCategory = elements.categoryInput.value;
    const note = elements.notesInput.value.trim();
    if (!target.site || !target.siteId || !qcCategory) throw new Error("Site and Area / QC Category are required.");
    if (!state.photos.length && !state.audio && !note) throw new Error("Add a photo, voice note, or text note.");
    const capturedAt = localIsoTimestamp();
    const exportedAt = localIsoTimestamp();
    const suffix = randomSuffix();
    const captureId = `cap-unified-${capturedAt.replace(/[:.]/g, "-")}-${suffix}`;
    const assetKind = state.photos.length && state.audio ? "photo-voice" : state.photos.length ? "photo" : state.audio ? "voice" : "text";
    const jobId = `${exportedAt.replace(/[:.]/g, "-")}__${assetKind}-capture-${fileStem(target.site)}-${suffix}`;
    return {
      capture_id: captureId,
      status: "pending",
      attempts: 0,
      lastError: null,
      createdAt: capturedAt,
      lastTriedAt: null,
      metadata: {
        person_id: state.session?.person?.person_id || null,
        field_capture_token_id: state.session?.token?.token_id || null,
        site_id: target.targetType === "location" ? target.targetId || null : null,
        target_type: target.targetType,
        target_id: target.targetId || null,
        photo_count: state.photos.length,
        has_audio: Boolean(state.audio),
      },
      fields: {
        job_id: jobId,
        capture_id: captureId,
        site: target.site,
        site_id: target.targetType === "location" ? target.targetId || "" : "",
        target_type: target.targetType,
        target_id: target.targetId || "",
        qc_category: qcCategory,
        note,
        captured_at: capturedAt,
        exported_at: exportedAt,
      },
      // Persist raw bytes, NOT Blob objects. iOS WebKit invalidates Blobs
      // stored in IndexedDB (WebKitBlobResource error 1 / "object can not be
      // found"), permanently stranding any capture that doesn't upload
      // immediately. Read bytes here while the in-memory blob is still valid;
      // rebuild a fresh Blob at upload time. See partBlob().
      photos: await Promise.all(
        state.photos.map(async (photo) => ({
          filename: photo.filename,
          mimeType: photo.mimeType,
          note: photo.note || "",
          bytes: await photo.blob.arrayBuffer(),
        })),
      ),
      audio: state.audio
        ? {
            filename: state.audio.filename,
            mimeType: state.audio.mimeType,
            bytes: await state.audio.blob.arrayBuffer(),
            durationSeconds: state.audio.durationSeconds,
          }
        : null,
    };
  }

  function clearPhotos() {
    state.photos.forEach((photo) => {
      if (photo.previewUrl) URL.revokeObjectURL(photo.previewUrl);
    });
    state.photos = [];
    renderPhotos();
  }

  async function saveCapture() {
    if (state.isSubmitting || !state.formEnabled) return;
    state.isSubmitting = true;
    updateSubmitState();
    try {
      await finishActiveVoiceRecordingForSubmit();
      if (!elements.noteEditorPanel.hidden) closeNoteEditor();
      if (!window.fieldCaptureDb?.isSupported()) throw new Error("Local storage not available. Cannot save capture.");
      const record = await buildCaptureRecord();
      await window.fieldCaptureDb.putCapture(record);
      clearPhotos();
      clearAudio();
      elements.notesInput.value = "";
      elements.noteEditor.value = "";
      closeNoteEditor();
      syncNoteAffordance();
      elements.categoryInput.value = "";
      showSuccess(record);
      await refreshPendingCount();
      drainQueue().catch(() => {});
      requestBackgroundSync();
    } catch (error) {
      setStatus(error.message || "Could not save capture.", "error");
    } finally {
      state.isSubmitting = false;
      updateSubmitState();
    }
  }

  function showSuccess(record) {
    document.querySelectorAll(SUCCESS_TAKEOVER_SELECTOR).forEach((panel) => {
      panel.hidden = true;
    });
    elements.successDetail.textContent = "Uploading in the background. You can capture another site now.";
    elements.successScreen.hidden = false;
    setStatus("Saved locally");
  }

  function resetToForm() {
    elements.successScreen.hidden = true;
    elements.feedSection.hidden = true;
    document.querySelectorAll(CAPTURE_PANEL_SELECTOR).forEach((panel) => {
      panel.hidden = false;
    });
    renderCategoriesForSelectedSite();
    renderPhotos();
    renderAudio();
    closeNoteEditor();
    syncNoteAffordance();
    setStatus(readyStatusText());
  }

  // Rebuild a fresh Blob from persisted bytes. Records written by this version
  // store `bytes` (ArrayBuffer); legacy records may still carry a `blob` — fall
  // back to it, though an iOS-invalidated legacy blob may no longer be readable.
  function partBlob(part) {
    if (part && part.bytes) return new Blob([part.bytes], { type: part.mimeType || "application/octet-stream" });
    return part ? part.blob : null;
  }

  function photoNotesJSON(photos) {
    const notes = [];
    photos.forEach((photo, index) => {
      const note = (photo.note || "").trim();
      if (!note) return;
      notes.push({ index, filename: photo.filename, note });
    });
    return notes.length ? JSON.stringify(notes) : null;
  }

  async function uploadOneCapture(record) {
    const form = new FormData();
    const fields = record.fields || {};
    for (const [key, value] of Object.entries(fields)) {
      form.append(key, value == null ? "" : String(value));
    }
    for (const photo of record.photos || []) {
      form.append("photos", partBlob(photo), photo.filename);
    }
    const photoNotes = photoNotesJSON(record.photos || []);
    if (photoNotes) form.append("photo_notes_json", photoNotes);
    if (record.audio) {
      form.append("audio", partBlob(record.audio), record.audio.filename);
      form.append("audio_duration_seconds", String(record.audio.durationSeconds || 0));
    }
    const response = await fetch("/api/submit", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
      body: form,
    });
    if (response.status === 401) {
      const error = new Error("auth_lost");
      error.permanent = true;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(`upload_failed_${response.status}`);
      error.status = response.status;
      error.permanent = response.status >= 400 && response.status < 500 && response.status !== 404 && response.status !== 408 && response.status !== 429;
      throw error;
    }
    return response.json();
  }

  function nextBackoffMs(attempts) {
    return DRAIN_BACKOFF_SCHEDULE_MS[Math.min(attempts, DRAIN_BACKOFF_SCHEDULE_MS.length - 1)];
  }

  function captureRetryWindowStartMs(record) {
    const createdMs = new Date(record.createdAt).getTime();
    const resetMs = new Date(record.retryWindowResetAt || 0).getTime();
    const candidates = [createdMs, resetMs].filter(Number.isFinite);
    return candidates.length ? Math.max(...candidates) : Date.now();
  }

  async function requeueFailedCaptures() {
    if (!window.fieldCaptureDb?.isSupported()) return 0;
    const failed = (await window.fieldCaptureDb.listByStatus("failed")).filter((record) => record.capture_id !== "__token__");
    if (!failed.length) return 0;
    const now = localIsoTimestamp();
    for (const record of failed) {
      await window.fieldCaptureDb.updateCapture(record.capture_id, {
        status: "pending",
        attempts: 0,
        lastError: null,
        retryWindowResetAt: now,
      });
    }
    return failed.length;
  }

  async function retryFailedCaptures() {
    const count = await requeueFailedCaptures();
    if (!count) {
      await refreshPendingCount();
      return;
    }
    state.drainBackoffUntil = 0;
    setStatus(`Retrying ${count} failed capture${count === 1 ? "" : "s"}...`, "warning");
    await refreshPendingCount();
    drainQueue().catch(() => {});
  }

  async function drainQueue() {
    if (state.isDraining || !window.fieldCaptureDb?.isSupported() || !token) return;
    if (!navigator.onLine) {
      setStatus("Offline. Captures will upload when connection returns.", "warning");
      return;
    }
    if (Date.now() < state.drainBackoffUntil) return;
    state.isDraining = true;
    try {
      const orphans = (await window.fieldCaptureDb.listByStatus("uploading")).filter((record) => record.capture_id !== "__token__");
      const orphanNow = Date.now();
      for (const orphan of orphans) {
        const startedMs = new Date(orphan.lastTriedAt || orphan.createdAt).getTime();
        if (!Number.isFinite(startedMs) || orphanNow - startedMs > UPLOAD_ORPHAN_RESET_MS) {
          await window.fieldCaptureDb.updateCapture(orphan.capture_id, { status: "pending", lastError: "recovered_orphaned_upload" });
        }
      }
      const pending = (await window.fieldCaptureDb.listByStatus("pending")).filter((record) => record.capture_id !== "__token__");
      pending.sort((left, right) => (left.createdAt || "").localeCompare(right.createdAt || ""));
      for (const record of pending) {
        const ageHours = (Date.now() - captureRetryWindowStartMs(record)) / 3_600_000;
        if (ageHours > DRAIN_PERMANENT_FAILURE_HOURS) {
          await window.fieldCaptureDb.updateCapture(record.capture_id, { status: "failed", lastError: "exceeded_retry_window" });
          continue;
        }
        try {
          await window.fieldCaptureDb.updateCapture(record.capture_id, { status: "uploading", lastTriedAt: localIsoTimestamp() });
          await uploadOneCapture(record);
          await window.fieldCaptureDb.updateCapture(record.capture_id, { status: "done", lastError: null });
        } catch (error) {
          const attempts = (record.attempts || 0) + 1;
          await window.fieldCaptureDb.updateCapture(record.capture_id, {
            status: error.permanent ? "failed" : "pending",
            attempts,
            lastError: error.message || String(error),
          });
          if (error.permanent) {
            setStatus(`Capture failed: ${error.message}`, "error");
            continue;
          }
          state.drainBackoffUntil = Date.now() + nextBackoffMs(attempts - 1);
          break;
        }
      }
      const dones = await window.fieldCaptureDb.listByStatus("done");
      for (const record of dones) {
        const ageMs = Date.now() - new Date(record.lastTriedAt || record.createdAt).getTime();
        if (ageMs > 5 * 60_000) await window.fieldCaptureDb.deleteCapture(record.capture_id);
      }
    } finally {
      state.isDraining = false;
      await refreshPendingCount();
    }
  }

  async function refreshPendingCount() {
    if (!window.fieldCaptureDb?.isSupported()) return;
    const [pending, uploading, failed] = await Promise.all([
      window.fieldCaptureDb.countByStatus("pending"),
      window.fieldCaptureDb.countByStatus("uploading"),
      window.fieldCaptureDb.countByStatus("failed"),
    ]);
    renderQueueStrip(pending, uploading, failed);
    await renderStaleStrip();
  }

  function renderQueueStrip(pending, uploading, failed) {
    const total = pending + uploading + failed;
    if (total === 0) {
      elements.queueStrip.hidden = false;
      elements.queueStrip.textContent = "All captures synced";
      elements.queueStrip.dataset.tone = "synced";
      return;
    }
    const parts = [];
    if (uploading) parts.push(`Uploading ${uploading}`);
    if (pending) parts.push(`${pending} pending`);
    if (failed) parts.push(`${failed} failed`);
    elements.queueStrip.hidden = false;
    const queueText = document.createElement("span");
    queueText.textContent = parts.join(" • ");
    elements.queueStrip.replaceChildren(queueText);
    if (pending || uploading) {
      // iOS PWAs can only upload while foregrounded — tell the worker to keep it open.
      const hint = document.createElement("span");
      hint.className = "queue-hint";
      hint.textContent = " — keep this app open until synced ✓";
      queueText.appendChild(hint);
    }
    if (failed) {
      const retryButton = document.createElement("button");
      retryButton.type = "button";
      retryButton.className = "queue-retry-btn";
      retryButton.dataset.action = "retry-failed";
      retryButton.textContent = `Retry ${failed} failed`;
      elements.queueStrip.appendChild(retryButton);
    }
    elements.queueStrip.dataset.tone = failed ? "error" : uploading ? "active" : "warning";
  }

  async function renderStaleStrip() {
    const pending = window.fieldCaptureDb?.isSupported() ? await window.fieldCaptureDb.listByStatus("pending") : [];
    const stale = pending
      .filter((record) => record.capture_id !== "__token__")
      .filter((record) => Date.now() - captureRetryWindowStartMs(record) > 24 * 60 * 60_000);
    if (!stale.length) {
      elements.staleStrip.hidden = true;
      elements.staleStrip.textContent = "";
      return;
    }
    elements.staleStrip.hidden = false;
    elements.staleStrip.dataset.tone = "error";
    elements.staleStrip.textContent = `${stale.length} capture${stale.length === 1 ? "" : "s"} stuck >24h`;
  }

  async function requestPersistentStorage() {
    if (!navigator.storage?.persist) return;
    try {
      await navigator.storage.persist();
    } catch (_error) {
      /* Best effort. */
    }
  }

  async function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return null;
    try {
      return await navigator.serviceWorker.register("/sw.js?v=" + INTERFACE_VERSION);
    } catch (_error) {
      return null;
    }
  }

  async function requestBackgroundSync() {
    if (!("serviceWorker" in navigator)) return;
    const reg = await navigator.serviceWorker.ready.catch(() => null);
    if (!reg || !("sync" in reg)) return;
    try {
      await reg.sync.register(SYNC_TAG);
    } catch (_error) {
      /* Foreground drain remains the fallback. */
    }
  }

  elements.fileInput.addEventListener("change", (event) => addFiles(event.target.files));
  elements.cameraInput.addEventListener("change", (event) => addFiles(event.target.files));
  elements.siteInput.addEventListener("change", renderCategoriesForSelectedSite);
  elements.recordVoiceButton.addEventListener("click", startVoiceRecording);
  elements.recordVoiceButton.addEventListener("touchend", startVoiceRecording);
  elements.stopVoiceButton.addEventListener("click", stopVoiceRecording);
  elements.screenModeInputs.forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) persistScreenMode(input.value);
    });
  });
  document.addEventListener("click", (event) => {
    if (elements.settingsMenu.open && !elements.settingsMenu.contains(event.target)) {
      elements.settingsMenu.open = false;
    }
    if (!elements.noteEditorPanel.hidden && !elements.noteEditorPanel.contains(event.target) && !elements.noteToggle.contains(event.target)) {
      closeNoteEditor();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (elements.settingsMenu.open) elements.settingsMenu.open = false;
    closeNoteEditor();
  });
  elements.noteToggle.addEventListener("click", () => {
    if (elements.noteEditorPanel.hidden) {
      openNoteEditor();
    } else {
      closeNoteEditor();
      elements.noteToggle.focus();
    }
  });
  elements.noteEditor.addEventListener("blur", () => {
    window.setTimeout(() => {
      if (!elements.noteEditorPanel.hidden && !elements.noteEditorPanel.contains(document.activeElement)) closeNoteEditor();
    }, 0);
  });
  elements.noteEditor.addEventListener("input", () => {
    elements.notesInput.value = elements.noteEditor.value || "";
    updateSubmitState();
  });
  elements.clearVoiceButton.addEventListener("click", () => {
    if (state.audio && !window.confirm("Discard this recording?")) return;
    if (state.recorder?.state === "recording" || state.recorder?.state === "paused") state.recorder.stop();
    clearAudio();
  });
  elements.mySubsBtn.addEventListener("click", showFeedView);
  elements.feedBack.addEventListener("click", hideFeedView);
  elements.submitButton.addEventListener("click", saveCapture);
  elements.submitAnotherButton.addEventListener("click", resetToForm);
  elements.queueStrip?.addEventListener("click", (event) => {
    if (event.target?.dataset?.action === "retry-failed") {
      retryFailedCaptures().catch(() => {});
    }
  });
  elements.tokenPasteSave.addEventListener("click", () => {
    const value = (elements.tokenPasteInput.value || "").trim();
    if (!value || value.length < 8) {
      elements.tokenPasteError.textContent = "That does not look like a valid token.";
      elements.tokenPasteError.hidden = false;
      return;
    }
    persistToken(value);
    loadSession();
  });
  window.addEventListener("online", () => {
    setOfflineBanner(false);
    state.drainBackoffUntil = 0;
    drainQueue().catch(() => {});
    if (token) loadSession().catch(() => {});
  });
  window.addEventListener("offline", () => setOfflineBanner(true));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      // Foregrounding is a strong "try now" signal — clear any backoff so a worker
      // opening the app to check on pending photos triggers an immediate attempt.
      state.drainBackoffUntil = 0;
      drainQueue().catch(() => {});
    }
  });
  // Foreground heartbeat: navigator.onLine and the "online" event are unreliable
  // (especially on iOS) and do NOT fire when a weak signal becomes a usable one
  // without the network interface dropping — exactly the poor-connectivity field
  // case. Poll while visible so a stuck queue self-heals once real connectivity
  // returns. drainQueue() self-guards on isDraining/offline/backoff, so an idle
  // tick is nearly free.
  const DRAIN_HEARTBEAT_MS = 20_000;
  setInterval(() => {
    if (document.visibilityState === "visible") drainQueue().catch(() => {});
  }, DRAIN_HEARTBEAT_MS);
  const darkMedia = preferredDarkMedia();
  const handleSystemThemeChange = () => {
    if (currentScreenMode() === "system") applyScreenMode("system");
  };
  darkMedia?.addEventListener?.("change", handleSystemThemeChange);
  darkMedia?.addListener?.(handleSystemThemeChange);

  elements.interfaceVersion.textContent = INTERFACE_VERSION;
  elements.pipelineVersion.textContent = PIPELINE_VERSION;
  applyScreenMode();
  if (!window.btqRecorder?.supportsAudioRecording?.()) elements.voiceSupportMessage.hidden = false;
  setFormEnabled(false);
  renderPhotos();
  renderAudio();
  requestPersistentStorage();
  requeueFailedCaptures().then(() => refreshPendingCount()).then(() => drainQueue()).catch(() => {});
  registerServiceWorker();
  setOfflineBanner(!navigator.onLine);
  if (bootstrapToken()) loadSession();
})();
