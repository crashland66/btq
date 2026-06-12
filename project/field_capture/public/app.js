const INTERFACE_VERSION = "2026.06.12-offline";
// Previous INTERFACE_VERSION = "2026.06.12-blob-persist" retained for legacy static smoke tests.
// Previous INTERFACE_VERSION = "2026.06.12-resilient-sync" retained for legacy static smoke tests.
// Previous INTERFACE_VERSION = "2026.05.28-local-first" retained for legacy static smoke tests.
// Previous INTERFACE_VERSION = "2026.05.24-multi-photo" retained for legacy static smoke tests.
// Previous INTERFACE_VERSION = "2026.05.16-inline-manifest" retained for legacy static smoke tests.
// Previous INTERFACE_VERSION = "2026.05.16-pwa-dynamic-manifest" retained for legacy static smoke tests.
// Previous INTERFACE_VERSION = "2026.05.16-pwa-token-cookie" retained for legacy static smoke tests.
// Previous INTERFACE_VERSION = "2026.05.16-tape-deck" retained for legacy static smoke tests.
// Previous INTERFACE_VERSION = "2026.05.14-success-screen" retained for legacy static smoke tests.
// Previous INTERFACE_VERSION = "2026.05.14-camera" retained for legacy static smoke tests.
// Previous INTERFACE_VERSION = "2026.05.06-intent-tags" retained for legacy static smoke tests.
const PIPELINE_VERSION = "field-capture-intake-v1";
const TOKEN_KEY = "fieldCaptureToken";
const RECENT_SITE_KEY = "fieldCaptureSiteId";
let cachedToken = "";

const state = {
  photos: [],
  audio: null,
  recorder: null,
  audioChunks: [],
  audioStartedAt: 0,
  audioElapsedBeforePause: 0,
  audioResumedAt: null,
  audioStopTimer: null,
  isStartingAudio: false,
  session: null,
  isProcessingPhotos: false,
  isSubmitting: false,
  siteViewerUrls: new Map(),
  prospectViewerUrls: new Map(),
  sitesById: new Map(),
  prospectsById: new Map(),
  prospectCategories: [],
  isDraining: false,
  drainAbort: null,
  pendingCount: 0,
  captureGated: false,
  lastDrainAt: 0,
  drainBackoffUntil: 0,
};

const elements = {
  canvas: document.querySelector("#captureCanvas"),
  fileInput: document.querySelector("#fileInput"),
  recordVoiceButton: document.querySelector("#recordVoiceButton"),
  stopVoiceButton: document.querySelector("#stopVoiceButton"),
  clearVoiceButton: document.querySelector("#clearVoiceButton"),
  voicePreview: document.querySelector("#voicePreview"),
  voiceStatus: document.querySelector("#voiceStatus"),
  voiceSupportMessage: document.querySelector("#voiceSupportMessage"),
  thumbnailGrid: document.querySelector("#thumbnailGrid"),
  exportButton: document.querySelector("#exportButton"),
  statusText: document.querySelector("#statusText"),
  queueStrip: document.querySelector("#queueStrip"),
  interfaceVersion: document.querySelector("#interfaceVersion"),
  pipelineVersion: document.querySelector("#pipelineVersion"),
  footerSite: document.querySelector("#footerSite"),
  siteViewerLink: document.querySelector("#siteViewerLink"),
  siteInput: document.querySelector("#siteInput"),
  cameraInput: document.querySelector("#cameraInput"),
  categoryInput: document.querySelector("#categoryInput"),
  captureGuidance: document.querySelector("#captureGuidance"),
  notesInput: document.querySelector("#notesInput"),
  submitSummary: document.querySelector("#submitSummary"),
  captureForm: document.querySelector("#captureForm"),
  installGate: document.querySelector("#installGate"),
  installGatePending: document.querySelector("#installGatePending"),
  successScreen: document.querySelector("#successScreen"),
  successDetail: document.querySelector("#successDetail"),
  submitAnotherButton: document.querySelector("#submitAnotherButton"),
  mySubsBtn: document.querySelector("#mySubsBtn"),
  mySubsBadge: document.querySelector("#mySubsBadge"),
  feedSection: document.querySelector("#feedSection"),
  feedBack: document.querySelector("#feedBack"),
  qualityCard: document.querySelector("#qualityCard"),
  submissionList: document.querySelector("#submissionList"),
};

function setStatus(message, tone = "") {
  elements.statusText.textContent = message;
  if (tone) {
    elements.statusText.dataset.tone = tone;
  } else {
    delete elements.statusText.dataset.tone;
  }
}

function tokenFromHash() {
  const raw = window.location.hash || "";
  if (!raw) return "";
  const stripped = raw.startsWith("#") ? raw.slice(1) : raw;
  const params = new URLSearchParams(stripped);
  return params.get("token") || params.get("access") || (stripped.includes("=") ? "" : stripped);
}

// iOS standalone PWAs have isolated localStorage from Safari for the same
// origin — but cookies ARE shared. Mirror the token into both so the
// Add-to-Home-Screen flow keeps working: Safari saves the token to both;
// the standalone PWA reads it back from the cookie when its localStorage
// is empty on first launch.
function setTokenCookie(value) {
  // 1 year, root path, HTTPS-only, Lax so top-level navigations still send it.
  document.cookie = `${TOKEN_KEY}=${encodeURIComponent(value)}; max-age=31536000; path=/; secure; samesite=lax`;
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

function clearTokenCookie() {
  document.cookie = `${TOKEN_KEY}=; max-age=0; path=/; secure; samesite=lax`;
}

function isIosDevice() {
  const ua = navigator.userAgent || "";
  if (/iPad|iPhone|iPod/.test(ua)) return true;
  // iPadOS 13+ reports as MacIntel; disambiguate with touch points.
  return navigator.platform === "MacIntel" && (navigator.maxTouchPoints || 0) > 1;
}

function isStandaloneDisplay() {
  const mm = typeof window.matchMedia === "function"
    ? window.matchMedia("(display-mode: standalone)").matches
    : false;
  return mm || navigator.standalone === true;
}

// Replace the static <link rel="manifest"> with one whose URL carries the
// token. Backend serves the manifest dynamically with start_url=/?token=X
// when the manifest URL itself includes the token — this is what survives
// the iOS Safari → standalone-PWA storage isolation, because the manifest
// is captured into the home-screen icon at install time.
function updateManifestLinkForToken(token) {
  if (!token) return;
  const replacement = document.createElement("link");
  replacement.rel = "manifest";
  replacement.href = `/manifest.webmanifest?token=${encodeURIComponent(token)}`;
  const existing = document.querySelector('link[rel="manifest"]');
  if (existing) {
    existing.replaceWith(replacement);
  } else {
    document.head.appendChild(replacement);
  }
}

function persistTokenAndScrub(value) {
  cachedToken = value;
  localStorage.setItem(TOKEN_KEY, cachedToken);
  setTokenCookie(cachedToken);
  updateManifestLinkForToken(cachedToken);
  history.replaceState({}, "", window.location.pathname);
}

function clearStoredToken() {
  cachedToken = "";
  localStorage.removeItem(TOKEN_KEY);
  clearTokenCookie();
  state.session = null;
}

function bootstrapToken() {
  // URL wins over any stored token so Jordan can test new tokens by visiting
  // /?token=<NEW> without first clearing the device. If the URL token differs
  // from the cached value, treat it as a fresh user and clear any per-session
  // state from the previous token.
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("token") || "";
  const fromHash = fromUrl ? "" : tokenFromHash();
  const fromUrlOrHash = fromUrl || fromHash;
  if (fromUrlOrHash) {
    const previous = localStorage.getItem(TOKEN_KEY) || readTokenCookie();
    persistTokenAndScrub(fromUrlOrHash);
    if (previous && previous !== fromUrlOrHash) {
      state.session = null;
      setStatus("New token detected - switched accounts.");
    } else {
      setStatus(fromHash ? "Token saved from link." : "Token saved - bookmark or add to home screen.");
    }
    return true;
  }
  cachedToken = localStorage.getItem(TOKEN_KEY) || "";
  if (!cachedToken) {
    // iOS PWA isolation fallback: localStorage may be empty on first
    // standalone launch even though the cookie persists from Safari.
    cachedToken = readTokenCookie();
    if (cachedToken) {
      localStorage.setItem(TOKEN_KEY, cachedToken);
    }
  }
  if (cachedToken) {
    // Re-fresh the manifest link so a subsequent Add-to-Home-Screen
    // re-captures with the token baked into start_url.
    updateManifestLinkForToken(cachedToken);
    return true;
  }
  showTokenPasteUI();
  setStatus("Paste your access token below to enable submissions.", "warning");
  return false;
}

function showTokenPasteUI() {
  if (document.getElementById("tokenPastePanel")) return;
  const main = document.querySelector("main.app-shell") || document.body;
  const panel = document.createElement("section");
  panel.id = "tokenPastePanel";
  panel.className = "details-panel token-paste-panel";
  panel.innerHTML = `
    <p class="token-paste-context">If you don't have an access token, this is a demo of an internal field-operations tool. Real users get a tokenized link from their administrator.</p>
    <p class="token-paste-help">First-time setup on this device. Paste the access token you were given, then tap Save.</p>
    <label class="token-paste-field">
      <span>Access token</span>
      <input id="tokenPasteInput" type="password" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
    </label>
    <button id="tokenPasteSave" class="primary-button" type="button">Save token</button>
    <p id="tokenPasteError" class="form-error" role="alert" hidden></p>
  `;
  const header = main.querySelector(".app-header");
  if (header && header.nextSibling) {
    main.insertBefore(panel, header.nextSibling);
  } else {
    main.appendChild(panel);
  }
  const input = panel.querySelector("#tokenPasteInput");
  const errorEl = panel.querySelector("#tokenPasteError");
  panel.querySelector("#tokenPasteSave").addEventListener("click", () => {
    const value = (input.value || "").trim();
    if (!value || value.length < 8) {
      errorEl.textContent = "That does not look like a valid token.";
      errorEl.hidden = false;
      return;
    }
    persistTokenAndScrub(value);
    panel.remove();
    setStatus("Token saved.");
    loadSessionAndSites();
    renderAudio();
  });
  input.focus();
}

function siteLabelFromOption(option) {
  if (!option || !option.value) {
    return "Not loaded";
  }
  const siteId = option.dataset.siteId || "";
  return siteId ? `${siteId} - ${option.value}` : option.value;
}

function selectedSiteLabel() {
  return siteLabelFromOption(elements.siteInput.selectedOptions[0]);
}

function firstNameFromSession(session) {
  const explicit = (session?.person?.first || "").trim();
  if (explicit) return explicit;
  const name = (session?.person?.name || "").trim();
  if (!name) return "";
  if (name.includes(",")) {
    const after = name.split(",", 2)[1] || "";
    const firstToken = after.trim().split(/\s+/)[0] || "";
    if (firstToken) return firstToken;
  }
  return name.split(/\s+/)[0] || "";
}

function readyMessageForSession(session) {
  const sites = session?.sites || [];
  if (sites.length === 1 && sites[0]?.name) {
    const firstName = firstNameFromSession(session);
    if (firstName) {
      return `Ready for ${firstName} — ${sites[0].name}`;
    }
    return `Ready to submit for ${sites[0].name}`;
  }
  return "Ready to submit field capture";
}

function updateFooterSite() {
  elements.footerSite.textContent = selectedSiteLabel();
}

function updateSiteViewerLink() {
  const selectedOption = elements.siteInput.selectedOptions[0];
  const selectedSiteName = selectedOption?.value || "";
  const targetType = selectedOption?.dataset.targetType || "";
  let viewerUrl = "";
  if (targetType === "location") {
    viewerUrl = state.siteViewerUrls.get(selectedSiteName) || "";
  } else if (targetType === "prospect") {
    viewerUrl = state.prospectViewerUrls.get(selectedSiteName) || "";
  }
  if (selectedSiteName && viewerUrl) {
    elements.siteViewerLink.href = viewerUrl;
    elements.siteViewerLink.hidden = false;
  } else {
    elements.siteViewerLink.href = "#";
    elements.siteViewerLink.hidden = true;
  }
}

function updateSelectedSiteDetails() {
  updateFooterSite();
  updateSiteViewerLink();
  renderSelectedSiteTuning();
  const selectedSiteId = elements.siteInput.selectedOptions[0]?.dataset.siteId || "";
  if (selectedSiteId) {
    localStorage.setItem(RECENT_SITE_KEY, selectedSiteId);
  }
}

function setFormEnabled(enabled) {
  elements.fileInput.disabled = !enabled;
  elements.cameraInput.disabled = !enabled;
  elements.recordVoiceButton.disabled = !enabled || !supportsAudioRecording();
  elements.siteInput.disabled = !enabled;
  elements.categoryInput.disabled = !enabled;
  elements.notesInput.disabled = !enabled;
  renderPhotos();
  renderAudio();
}

function updateSubmitState() {
  elements.exportButton.disabled = state.captureGated || state.photos.length === 0 || !state.session || state.isProcessingPhotos || state.isSubmitting;
}

function updateSubmitSummary() {
  if (state.photos.length === 0) {
    elements.submitSummary.hidden = true;
    return;
  }
  const parts = [`${state.photos.length} photo${state.photos.length === 1 ? "" : "s"}`];
  if (state.audio?.durationSeconds) {
    parts.push(`${state.audio.durationSeconds}s voice note`);
  }
  elements.submitSummary.textContent = `${parts.join(" + ")} — ready to submit`;
  elements.submitSummary.hidden = false;
}

function selectedSiteFromSession() {
  const selectedOption = elements.siteInput.selectedOptions[0];
  const siteId = selectedOption?.dataset.siteId || "";
  return siteId ? state.sitesById.get(siteId) || null : null;
}

function renderSelectedSiteTuning() {
  const selectedOption = elements.siteInput.selectedOptions[0];
  if (selectedOption?.dataset.targetType === "prospect") {
    renderCaptureGuidance("");
    renderDisplayCategories(state.prospectCategories || []);
    return;
  }
  const site = selectedSiteFromSession();
  renderCaptureGuidance(site?.capture_guidance || "");
  renderDisplayCategories(site?.display_categories || []);
}

function renderCaptureGuidance(text) {
  if (state.captureGated) {
    elements.captureGuidance.textContent = "";
    elements.captureGuidance.hidden = true;
    return;
  }
  const guidance = (text || "").trim();
  if (!guidance) {
    elements.captureGuidance.textContent = "";
    elements.captureGuidance.hidden = true;
    return;
  }
  elements.captureGuidance.textContent = guidance;
  elements.captureGuidance.hidden = false;
}

function renderDisplayCategories(list) {
  const placeholder = elements.categoryInput.querySelector('option[value=""]') || document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select category…";
  elements.categoryInput.replaceChildren(placeholder);
  if (Array.isArray(list)) {
    list.forEach((item) => {
      const label = (item?.label || "").trim();
      const canonical = (item?.canonical || "").trim();
      if (!label || !canonical) return;
      const option = document.createElement("option");
      option.value = canonical;
      option.textContent = label;
      elements.categoryInput.append(option);
    });
  }
  elements.categoryInput.value = "";
  updateSubmitState();
}

function renderSites(sites, prospects = []) {
  elements.siteInput.replaceChildren();
  state.siteViewerUrls = new Map();
  state.prospectViewerUrls = new Map();
  state.sitesById = new Map();
  state.prospectsById = new Map();
  state.prospectCategories = sites.find((site) => Array.isArray(site.display_categories))?.display_categories || [];
  if (!sites.length && !prospects.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No assigned sites";
    elements.siteInput.append(option);
    updateSelectedSiteDetails();
    return;
  }
  const siteGroup = document.createElement("optgroup");
  siteGroup.label = "Sites";
  sites.forEach((site) => {
    const option = document.createElement("option");
    option.value = site.name;
    option.dataset.siteId = site.site_id;
    option.dataset.targetType = "location";
    option.dataset.targetId = site.site_id;
    option.textContent = site.account ? `${site.name} (${site.account})` : site.name;
    state.sitesById.set(site.site_id, site);
    if (site.viewer_url) {
      state.siteViewerUrls.set(site.name, site.viewer_url);
    }
    siteGroup.append(option);
  });
  if (sites.length) {
    elements.siteInput.append(siteGroup);
  }
  if (Array.isArray(prospects) && prospects.length) {
    const prospectGroup = document.createElement("optgroup");
    prospectGroup.label = "Prospects";
    prospects.forEach((prospect) => {
      const option = document.createElement("option");
      option.value = prospect.name;
      option.dataset.prospectId = prospect.prospect_id;
      option.dataset.targetType = "prospect";
      option.dataset.targetId = prospect.prospect_id;
      option.textContent = prospect.account ? `${prospect.name} (${prospect.account})` : prospect.name;
      state.prospectsById.set(prospect.prospect_id, prospect);
      if (prospect.viewer_url) {
        state.prospectViewerUrls.set(prospect.name, prospect.viewer_url);
      }
      prospectGroup.append(option);
    });
    elements.siteInput.append(prospectGroup);
  }
  if (sites.length + prospects.length === 1) {
    elements.siteInput.selectedIndex = 0;
  } else {
    const recentSiteId = localStorage.getItem(RECENT_SITE_KEY);
    if (recentSiteId) {
      const matchingOption = Array.from(elements.siteInput.options).find(
        (opt) => opt.dataset.siteId === recentSiteId,
      );
      if (matchingOption) {
        elements.siteInput.value = matchingOption.value;
      }
    }
  }
  updateSelectedSiteDetails();
}

function setOfflineBanner(show) {
  const el = typeof document !== "undefined" && document.getElementById ? document.getElementById("offlineBanner") : null;
  if (el) el.hidden = !show;
}

// Persist the session (sites/prospects/can_submit) so the form can be enabled
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
  renderSites(session.sites || [], session.prospects || []);
  const enabled = Boolean(session.token?.can_submit && (session.sites?.length || session.prospects?.length));
  setFormEnabled(enabled);
  if (!fromCache) {
    setStatus(
      enabled
        ? readyMessageForSession(session)
        : session.sites?.length || session.prospects?.length
          ? "This link can view assigned sites but cannot submit captures"
          : "No assigned sites",
      enabled ? "" : "warning",
    );
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

async function loadSessionAndSites() {
  if (!cachedToken) {
    setFormEnabled(false);
    showTokenPasteUI();
    setStatus("Paste your access token below to enable submissions.", "warning");
    return;
  }
  try {
    const response = await fetch("/api/session", {
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${cachedToken}`,
      },
      cache: "no-store",
    });
    if (!response.ok) {
      if (response.status === 401) {
        // Definitive rejection — never fall back to a cached session.
        clearStoredToken();
        showTokenPasteUI();
        setFormEnabled(false);
        setStatus("Token is invalid, expired, or revoked", "error");
        return;
      }
      // Server reachable but erroring — prefer a cached session if we have one.
      if (await applyCachedSessionIfAvailable()) return;
      setFormEnabled(false);
      setStatus("Session unavailable. Capture is disabled.", "error");
      return;
    }
    const session = await response.json();
    stashTokenForServiceWorker(cachedToken);
    await cacheSession(session);
    setOfflineBanner(false);
    applySession(session, false);
  } catch (error) {
    // Network failure / offline — run on the last-known site list if we have it.
    if (await applyCachedSessionIfAvailable()) return;
    setFormEnabled(false);
    setStatus("Could not verify token", "error");
  }
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

// Mirror the token into IDB for the service worker.
// SW context has no localStorage / cookies access pattern
// that matches all platforms; IDB is uniformly readable.
async function stashTokenForServiceWorker(token) {
  if (!window.fieldCaptureDb?.isSupported() || !token) return;
  try {
    // Use the captures store with a sentinel key. The store has
    // keyPath: "capture_id" so the sentinel record carries a
    // matching field. SW reads from {capture_id: "__token__"}.
    await window.fieldCaptureDb.putCapture({
      capture_id: "__token__",
      value: token,
      status: "__meta__",
      createdAt: localIsoTimestamp(),
    });
  } catch (_e) {
    // Non-fatal — foreground drain still works.
  }
}

function renderPhotos() {
  elements.thumbnailGrid.replaceChildren();
  state.photos.forEach((photo, index) => {
    const card = document.createElement("div");
    card.className = "thumbnail-card";

    const image = document.createElement("img");
    if (!photo.previewUrl) {
      photo.previewUrl = URL.createObjectURL(photo.blob);
    }
    image.src = photo.previewUrl;
    image.alt = photo.filename;

    const button = document.createElement("button");
    button.className = "remove-photo";
    button.type = "button";
    button.textContent = "X";
    button.title = "Remove photo";
    button.addEventListener("click", () => {
      if (state.photos[index]?.previewUrl) {
        URL.revokeObjectURL(state.photos[index].previewUrl);
      }
      state.photos.splice(index, 1);
      renderPhotos();
      setStatus("Photo removed");
    });

    card.append(image, button);
    elements.thumbnailGrid.append(card);
  });
  updateSubmitState();
  updateSubmitSummary();
}

function clearPhotos() {
  state.photos.forEach((photo) => {
    if (photo.previewUrl) {
      URL.revokeObjectURL(photo.previewUrl);
    }
  });
  state.photos = [];
}

function supportsAudioRecording() {
  return Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
}

function renderAudio() {
  if (!supportsAudioRecording()) {
    elements.voiceSupportMessage.hidden = false;
    elements.recordVoiceButton.disabled = true;
    elements.stopVoiceButton.disabled = true;
    elements.clearVoiceButton.disabled = true;
    elements.voiceStatus.textContent = "Unavailable";
    updateSubmitSummary();
    return;
  }
  const recState = state.recorder?.state || null;
  const isRecording = recState === "recording";
  const isPaused = recState === "paused";
  const isActive = isRecording || isPaused;
  elements.voiceSupportMessage.hidden = true;
  elements.recordVoiceButton.disabled = !state.session || isRecording || state.isStartingAudio;
  elements.stopVoiceButton.disabled = !isRecording;
  elements.clearVoiceButton.disabled = (!state.audio && !isActive) || state.isStartingAudio;
  elements.voiceStatus.textContent = state.isStartingAudio ? "Requesting microphone..." : isRecording ? "Recording..." : isPaused ? "Paused — press record to resume" : state.audio ? "Ready" : "Optional";
  if (state.isStartingAudio || isRecording) {
    elements.voiceStatus.dataset.tone = "active";
  } else if (isPaused) {
    elements.voiceStatus.dataset.tone = "paused";
  } else {
    delete elements.voiceStatus.dataset.tone;
  }
  elements.voicePreview.hidden = !state.audio;
  if (state.audio && elements.voicePreview.src !== state.audio.url) {
    elements.voicePreview.src = state.audio.url;
  }
  updateSubmitSummary();
}

function showSuccessScreen(result, captureId) {
  if (result.saved_locally) {
    elements.successDetail.textContent = "Uploading in the background. You can capture another site now.";
    document.querySelectorAll(".quick-fields, .photo-input-panel, .voice-panel, .note-panel").forEach((el) => {
      el.hidden = true;
    });
    elements.captureGuidance.hidden = true;
    elements.successScreen.hidden = false;
    setStatus("Saved locally");
    return;
  }
  const photoCount = typeof result.photo_count === "number" ? result.photo_count : state.photos.length;
  const timestamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const parts = [`${photoCount} photo${photoCount === 1 ? "" : "s"}`];
  if (result.has_audio) {
    parts.push("voice note");
  }
  parts.push(timestamp);
  if (captureId) {
    parts.push(captureId);
  }
  elements.successDetail.textContent = parts.join(" · ");
  document.querySelectorAll(".quick-fields, .photo-input-panel, .voice-panel, .note-panel").forEach((el) => {
    el.hidden = true;
  });
  elements.captureGuidance.hidden = true;
  elements.successScreen.hidden = false;
  setStatus("Submitted successfully");
}

function resetToForm() {
  if (state.captureGated) {
    showInstallGate();
    return;
  }
  elements.successScreen.hidden = true;
  document.querySelectorAll(".quick-fields, .photo-input-panel, .voice-panel, .note-panel").forEach((el) => {
    el.hidden = false;
  });
  renderSelectedSiteTuning();
  elements.notesInput.value = "";
  elements.categoryInput.value = "";
  renderPhotos();
  renderAudio();
  setStatus(state.session ? readyMessageForSession(state.session) : "Ready to submit field capture");
}

function fileStem(value) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48) || "field-photo";
}

function randomSuffix() {
  // crypto.randomUUID is absent on older iOS Safari / some Android
  // WebViews (same fleet concern as the createImageBitmap fallback),
  // so degrade to Math.random — uniqueness, not cryptographic strength,
  // is the requirement.
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID().slice(0, 8);
  }
  return Math.random().toString(36).slice(2, 10);
}

async function decodeToCanvasSource(fileOrBlob) {
  if (typeof createImageBitmap === "function") {
    try {
      return await createImageBitmap(fileOrBlob);
    } catch (_e) {
      // Fall through to <img> decode for older Safari/WebView engines.
    }
  }
  const url = URL.createObjectURL(fileOrBlob);
  try {
    const img = await new Promise((resolve, reject) => {
      const i = new Image();
      i.onload = () => resolve(i);
      i.onerror = () => reject(new Error("image_decode_failed"));
      i.src = url;
    });
    return img;
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
    canvas.toBlob(
      (result) => (result && result.size > 0 ? resolve(result) : reject(new Error("toBlob_returned_empty"))),
      "image/jpeg",
      0.86,
    );
  });
  const timestamp = localIsoTimestamp().replace(/[:.]/g, "-");
  return {
    filename: `${fileStem(suggestedName)}-${timestamp}.jpg`,
    mimeType: "image/jpeg",
    blob,
  };
}

function preferredAudioMimeType() {
  const candidates = ["audio/webm", "audio/mp4"];
  return candidates.find((mimeType) => MediaRecorder.isTypeSupported?.(mimeType)) || "";
}

function audioExtension(mimeType) {
  if (mimeType.includes("mp4") || mimeType.includes("m4a")) {
    return "m4a";
  }
  if (mimeType.includes("wav")) {
    return "wav";
  }
  return "webm";
}

async function startVoiceRecording() {
  // Resume a paused recording instead of starting a new one.
  if (state.recorder?.state === "paused") {
    state.recorder.resume();
    // Re-arm the auto-stop timer for the remaining budget.
    // Track total recording time across pauses via state.audioElapsedBeforePause.
    const remaining = Math.max(60_000, 600_000 - (state.audioElapsedBeforePause || 0));
    state.audioStopTimer = window.setTimeout(stopVoiceRecording, remaining);
    state.audioResumedAt = Date.now();
    setStatus("Recording voice note (up to 10 min)...");
    renderAudio();
    return;
  }
  if (state.isStartingAudio || state.recorder?.state === "recording") {
    return;
  }
  if (!supportsAudioRecording()) {
    renderAudio();
    return;
  }
  let stream = null;
  try {
    state.isStartingAudio = true;
    setStatus("Requesting microphone access...");
    renderAudio();
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = preferredAudioMimeType();
    const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    clearAudio();
    state.audioChunks = [];
    state.audioStartedAt = Date.now();
    state.audioElapsedBeforePause = 0;
    state.audioResumedAt = null;
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size > 0) {
        state.audioChunks.push(event.data);
      }
    });
    recorder.addEventListener("stop", () => finishVoiceRecording(recorder));
    state.recorder = recorder;
    recorder.start();
    // Auto-stop at 10 minutes. Was 60s — silently truncated longer
    // recordings, which from the user's perspective looked like the
    // audio was being lost. 10 min keeps the typical voice-memo well
    // under the server's 10MB per-audio cap (~480 KB/min observed).
    state.audioStopTimer = window.setTimeout(stopVoiceRecording, 600_000);
    state.isStartingAudio = false;
    setStatus("Recording voice note (up to 10 min)...");
    renderAudio();
  } catch (error) {
    stream?.getTracks().forEach((track) => track.stop());
    state.isStartingAudio = false;
    setStatus(microphoneErrorMessage(error), "error");
    renderAudio();
  }
}

function microphoneErrorMessage(error) {
  const name = error?.name || "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Microphone access was blocked. Check browser site permissions.";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "No microphone is available to this browser.";
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return "The microphone is busy or unavailable.";
  }
  return "Could not start voice recording on this browser.";
}

function stopVoiceRecording() {
  if (state.recorder?.state === "recording") {
    const segmentElapsed = state.audioResumedAt
      ? Date.now() - state.audioResumedAt
      : Date.now() - state.audioStartedAt;
    state.audioElapsedBeforePause = (state.audioElapsedBeforePause || 0) + segmentElapsed;
    state.recorder.pause();
    window.clearTimeout(state.audioStopTimer);
    state.audioStopTimer = null;
    setStatus("Voice note paused — press record to resume.", "paused");
    renderAudio();
  }
}

function finishActiveVoiceRecordingForSubmit() {
  const recState = state.recorder?.state;
  if (recState !== "recording" && recState !== "paused") {
    return Promise.resolve();
  }
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
    recorder.stop();
  });
}

function finishVoiceRecording(recorder) {
  window.clearTimeout(state.audioStopTimer);
  state.audioStopTimer = null;
  recorder.stream.getTracks().forEach((track) => track.stop());
  if (state.recorder !== recorder) {
    return;
  }
  const mimeType = recorder.mimeType || state.audioChunks[0]?.type || "audio/webm";
  const blob = new Blob(state.audioChunks, { type: mimeType });
  // No upper cap on reported duration: the auto-stop timer already
  // bounds actual recording length (see startVoiceRecording).
  const durationSeconds = Math.max(1, Math.round((Date.now() - state.audioStartedAt) / 1000));
  const timestamp = localIsoTimestamp().replace(/[:.]/g, "-");
  state.audio = {
    blob,
    url: URL.createObjectURL(blob),
    filename: `voice-note-${timestamp}.${audioExtension(mimeType)}`,
    mimeType,
    durationSeconds,
  };
  state.recorder = null;
  state.audioChunks = [];
  state.audioElapsedBeforePause = 0;
  state.audioResumedAt = null;
  setStatus("Voice note recorded");
  renderAudio();
}

function clearAudio() {
  if (state.recorder) {
    state.recorder.stream.getTracks().forEach((track) => track.stop());
    state.recorder = null;
    state.audioChunks = [];
    state.audioElapsedBeforePause = 0;
    state.audioResumedAt = null;
    window.clearTimeout(state.audioStopTimer);
    state.audioStopTimer = null;
  }
  if (state.audio?.url) {
    URL.revokeObjectURL(state.audio.url);
  }
  state.audio = null;
  elements.voicePreview.removeAttribute("src");
  elements.voicePreview.load();
  renderAudio();
}

async function addFiles(files) {
  const selected = Array.from(files).filter((file) => file.type.startsWith("image/"));
  if (!selected.length) {
    setStatus("No image selected");
    return;
  }
  state.isProcessingPhotos = true;
  updateSubmitState();
  setStatus(`Processing ${selected.length} photo${selected.length === 1 ? "" : "s"}...`);
  try {
    for (const file of selected) {
      const photo = await normalizeImage(file, file.name);
      state.photos.push(photo);
      renderPhotos();
    }
    setStatus("Photo ready");
  } catch (error) {
    setStatus("Could not process selected photo", "error");
  } finally {
    state.isProcessingPhotos = false;
    elements.fileInput.value = "";
    elements.cameraInput.value = "";
    renderPhotos();
  }
}

async function buildCaptureRecord() {
  const site = elements.siteInput.value.trim();
  const qcCategory = elements.categoryInput.value;
  const note = elements.notesInput.value.trim();
  if (!site || !qcCategory) {
    throw new Error("Site and Area / QC Category are required.");
  }
  if (!state.photos.length) {
    throw new Error("Add at least one photo.");
  }
  const capturedAt = localIsoTimestamp();
  const exportedAt = localIsoTimestamp();
  const suffix = randomSuffix();
  const captureId = `cap-photo-${capturedAt.replace(/[:.]/g, "-")}-${suffix}`;
  const jobId = `${exportedAt.replace(/[:.]/g, "-")}__photo-capture-${fileStem(site)}-${suffix}`;
  const selectedSite = elements.siteInput.selectedOptions[0];
  const targetType = selectedSite?.dataset.targetType || "location";
  const targetId = selectedSite?.dataset.targetId || selectedSite?.dataset.siteId || "";
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
      site_id: targetType === "location" ? targetId || null : null,
      target_type: targetType,
      target_id: targetId || null,
    },
    fields: {
      job_id: jobId,
      capture_id: captureId,
      site,
      site_id: targetType === "location" ? targetId || "" : "",
      target_type: targetType,
      target_id: targetId || "",
      qc_category: qcCategory,
      note,
      captured_at: capturedAt,
      exported_at: exportedAt,
    },
    // Persist raw bytes, NOT Blob objects. iOS WebKit invalidates Blobs stored
    // in IndexedDB (WebKitBlobResource error 1 / "object can not be found"),
    // which permanently strands any capture that doesn't upload immediately.
    // Read the bytes here while the in-memory blob is still valid; rebuild a
    // fresh Blob at upload time. See partBlob().
    photos: await Promise.all(
      state.photos.map(async (photo) => ({
        filename: photo.filename,
        mimeType: photo.mimeType,
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

function isQuotaError(error) {
  if (!error) return false;
  // Standard DOMException name, plus legacy Firefox.
  return error.name === "QuotaExceededError" ||
    error.name === "NS_ERROR_DOM_QUOTA_REACHED" ||
    error.code === 22;   // legacy numeric QUOTA_EXCEEDED_ERR
}

function isStorageRestartedError(error) {
  if (!error) return false;
  return error.name === "StorageRestartedError" ||
    error.name === "InvalidStateError" ||
    ((error.message || "").toLowerCase().includes("connection is closing"));
}

async function saveCapture() {
  if (state.captureGated) return;
  if (state.isSubmitting) return;
  if (!window.fieldCaptureDb?.isSupported()) {
    setStatus("Local storage not available — cannot save capture.", "error");
    return;
  }
  try {
    state.isSubmitting = true;
    updateSubmitState();
    await finishActiveVoiceRecordingForSubmit();
    const record = await buildCaptureRecord();
    await window.fieldCaptureDb.putCapture(record);
    clearPhotos();
    clearAudio();
    renderPhotos();
    showSuccessScreen({ saved_locally: true }, record.capture_id);
    await refreshPendingCount();
    drainQueue().catch(() => {
      /* drain reports its own errors via status text */
    });
    requestBackgroundSync();  // Android-only bonus; no-op on iOS Safari
  } catch (error) {
    if (isQuotaError(error)) {
      setStatus("Phone storage full — free up space, then re-save this capture.", "error");
    } else if (isStorageRestartedError(error)) {
      setStatus("Local storage restarted — tap Save again.", "error");
    } else {
      setStatus(error.message, "error");
    }
    renderPhotos();
  } finally {
    state.isSubmitting = false;
    updateSubmitState();
  }
}

function handleRecordVoiceEvent(event) {
  event.preventDefault();
  startVoiceRecording();
}

// ─── My Submissions feed ────────────────────────────────────────────────────

const SEEN_ACTED_ON_KEY = "seenActedOn";

function loadSeenActedOn() {
  try {
    const raw = localStorage.getItem(SEEN_ACTED_ON_KEY);
    if (raw) return new Set(JSON.parse(raw));
  } catch (_e) {}
  return new Set();
}

function saveSeenActedOn(set) {
  try {
    localStorage.setItem(SEEN_ACTED_ON_KEY, JSON.stringify([...set]));
  } catch (_e) {}
}

function updateBadge(submissions) {
  const seen = loadSeenActedOn();
  const hasUnseen = submissions.some(
    (s) => s.track === "B" && s.stage === "acted_on" && !seen.has(s.capture_id),
  );
  elements.mySubsBadge.hidden = !hasUnseen;
}

function markAllActedOnAsSeen(submissions) {
  const seen = loadSeenActedOn();
  submissions.forEach((s) => {
    if (s.track === "B" && s.stage === "acted_on") {
      seen.add(s.capture_id);
    }
  });
  saveSeenActedOn(seen);
  elements.mySubsBadge.hidden = true;
}

function formatRelativeTime(isoStr) {
  if (!isoStr) return "";
  let date;
  try {
    date = new Date(isoStr);
    if (isNaN(date.getTime())) return isoStr;
  } catch (_e) {
    return isoStr;
  }
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterdayStart = new Date(todayStart);
  yesterdayStart.setDate(yesterdayStart.getDate() - 1);
  const weekStart = new Date(todayStart);
  weekStart.setDate(weekStart.getDate() - 6);

  if (date >= todayStart) {
    return "Today " + date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  if (date >= yesterdayStart) {
    return "Yesterday";
  }
  if (date >= weekStart) {
    return date.toLocaleDateString([], { weekday: "short" });
  }
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
    const COACHING_TIPS = {
      blurry: "A few came out blurry — tap to focus, then hold still for a second before the shot.",
      motion_blur: "A few came out blurry from movement — hold the phone steady for a moment after you tap.",
      too_dark: "A few came out dark — switching on a light or using your camera flash makes them much easier to use.",
      too_bright: "A few came out washed out — step back from the window or strong light.",
      glare: "A few had glare — shift your angle slightly to avoid the reflection.",
      out_of_frame: "A few had the subject cut off — back up a step so the whole area is in the shot.",
      partly_obscured: "A few had something in the way — make sure nothing is in front of what you're photographing.",
      low_resolution: "A few were low quality — use the in-app camera rather than a screenshot or forwarded image.",
      contains_people: "A few had a person in the shot — frame the area so people aren't in the photo.",
      unanalyzable: "A few couldn't be read — re-take in better light, holding steady.",
    };
    const THRESHOLD = 3;
    const MAX_TIPS = 2;
    const sorted = Object.entries(flag_counts)
      .filter(([, count]) => count >= THRESHOLD)
      .sort(([, a], [, b]) => b - a)
      .slice(0, MAX_TIPS);
    for (const [flag] of sorted) {
      const tip = COACHING_TIPS[flag];
      if (tip) {
        html += `<p class="quality-tip">Tip: ${tip}</p>`;
      }
    }
  }

  elements.qualityCard.innerHTML = html;
}

function buildStatusPill(sub) {
  let label;
  let tone = "";
  if (sub.track === "A") {
    if (sub.stage === "processed") {
      label = "Analyzed";
      tone = "active";
    } else {
      label = "Analyzing…";
    }
  } else {
    // Track B
    if (sub.stage === "acted_on") {
      label = sub.outcome_label || "Acted on";
      tone = "active";
    } else if (sub.stage === "reviewed") {
      label = sub.outcome_label || "Reviewed";
      tone = "done";
    } else {
      label = "Under Review";
    }
  }
  const toneAttr = tone ? ` data-tone="${tone}"` : "";
  return `<span class="status-pill"${toneAttr}>${label}</span>`;
}

function buildQualityChips(perPhotoQuality) {
  if (!Array.isArray(perPhotoQuality) || perPhotoQuality.length === 0) return "";
  const WORKER_LABELS = {
    blurry: "blurry",
    motion_blur: "blurry (movement)",
    too_dark: "too dark",
    too_bright: "too bright",
    glare: "glare",
    out_of_frame: "subject cut off",
    partly_obscured: "subject blocked",
    low_resolution: "low quality",
    contains_people: "person in shot",
    unanalyzable: "couldn't be read",
  };
  const counts = {};
  for (const photo of perPhotoQuality) {
    const severity = photo.severity || "ok";
    if (severity === "ok" && (!photo.flags || photo.flags.length === 0)) continue;
    for (const flag of photo.flags || []) {
      counts[flag] = (counts[flag] || 0) + 1;
    }
  }
  const sorted = Object.entries(counts).sort(([, a], [, b]) => b - a).slice(0, 2);
  return sorted
    .map(([flag, count]) => {
      const workerLabel = WORKER_LABELS[flag] || flag;
      const plural = count === 1 ? "photo" : "photos";
      return `<span class="quality-chip">${count} ${plural} ${workerLabel}</span>`;
    })
    .join("");
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function buildDetailStepper(sub) {
  if (sub.track === "A") {
    // 2-step: Submitted → Analyzed (or Analyzing…)
    const step2Cls = sub.stage === "processed" ? "step step--done" : "step step--current";
    const step2Label = sub.stage === "processed" ? "Analyzed ✓" : "Analyzing…";
    return `<div class="stepper stepper--track-a">
      <span class="step step--done">Submitted ✓</span>
      <span class="step-arrow">→</span>
      <span class="${step2Cls}">${step2Label}</span>
    </div>`;
  }

  // Track B — 3 steps: Submitted → Reviewed → [outcome]
  // stage: "processing" | "reviewed" | "acted_on"
  const outcomeLabel = sub.outcome_label || "Acted on";

  if (sub.stage === "processing") {
    return `<div class="stepper stepper--track-b">
      <span class="step step--done">Submitted ✓</span>
      <span class="step-arrow">→</span>
      <span class="step step--current">Under Review</span>
      <span class="step-arrow">→</span>
      <span class="step step--future">${outcomeLabel}</span>
    </div>`;
  }
  if (sub.stage === "reviewed") {
    // No action needed — terminal at "Reviewed"
    return `<div class="stepper stepper--track-b">
      <span class="step step--done">Submitted ✓</span>
      <span class="step-arrow">→</span>
      <span class="step step--done">Reviewed ✓</span>
      <span class="step-arrow">→</span>
      <span class="step step--done">${outcomeLabel} ✓</span>
    </div>`;
  }
  // acted_on
  return `<div class="stepper stepper--track-b">
    <span class="step step--done">Submitted ✓</span>
    <span class="step-arrow">→</span>
    <span class="step step--done">Reviewed ✓</span>
    <span class="step-arrow">→</span>
    <span class="step step--done">${outcomeLabel} ✓</span>
  </div>`;
}

function buildDetailContent(sub) {
  let html = "";

  // Photos — vision analysis (Track A) or quality flags only (Track B)
  if (Array.isArray(sub.per_photo_quality) && sub.per_photo_quality.length > 0) {
    const WORKER_LABELS = {
      blurry: "blurry",
      motion_blur: "blurry (movement)",
      too_dark: "too dark",
      too_bright: "too bright",
      glare: "glare",
      out_of_frame: "subject cut off",
      partly_obscured: "subject blocked",
      low_resolution: "low quality",
      contains_people: "person in shot",
      unanalyzable: "couldn't be read",
    };
    const COACHING_TIPS = {
      blurry: "A few came out blurry — tap to focus, then hold still for a second before the shot.",
      motion_blur: "A few came out blurry from movement — hold the phone steady for a moment after you tap.",
      too_dark: "A few came out dark — switching on a light or using your camera flash makes them much easier to use.",
      too_bright: "A few came out washed out — step back from the window or strong light.",
      glare: "A few had glare — shift your angle slightly to avoid the reflection.",
      out_of_frame: "A few had the subject cut off — back up a step so the whole area is in the shot.",
      partly_obscured: "A few had something in the way — make sure nothing is in front of what you're photographing.",
      low_resolution: "A few were low quality — use the in-app camera rather than a screenshot or forwarded image.",
      contains_people: "A few had a person in the shot — frame the area so people aren't in the photo.",
      unanalyzable: "A few couldn't be read — re-take in better light, holding steady.",
    };
    const detailUrls = Array.isArray(sub.photo_urls) ? sub.photo_urls : [];
    html += '<div class="detail-photos">';
    sub.per_photo_quality.forEach((photo, i) => {
      html += '<div class="detail-photo-item">';
      const photoUrl = detailUrls[i] || "";
      if (photoUrl) {
        html += `<img class="detail-thumb" src="${photoUrl}" alt="Photo ${i + 1}" loading="lazy" />`;
      }
      const hasFlags = Array.isArray(photo.flags) && photo.flags.length > 0;
      // Vision description (Track A: always show when present)
      if (photo.description) {
        html += `<p class="photo-description">${escapeHtml(photo.description)}</p>`;
      }
      // Possible issues spotted by vision
      if (Array.isArray(photo.possible_issues) && photo.possible_issues.length > 0) {
        html += '<ul class="photo-issues">';
        for (const issue of photo.possible_issues) {
          html += `<li>${escapeHtml(issue)}</li>`;
        }
        html += "</ul>";
      }
      // Quality flags
      if (photo.severity === "ok" && !hasFlags) {
        if (!photo.description) html += '<span class="photo-ok">Looks good</span>';
      } else {
        for (const flag of photo.flags || []) {
          const workerLabel = WORKER_LABELS[flag] || flag;
          const tip = COACHING_TIPS[flag] || "";
          html += `<span class="photo-flag">${workerLabel}</span>`;
          if (tip) html += `<span class="photo-tip">${tip}</span>`;
        }
      }
      html += "</div>";
    });
    html += "</div>";
    // Note: audio URL cannot be constructed from API data alone — player omitted (known gap for v1)
  }

  // Note text
  if (sub.note_text) {
    html += `<div class="detail-note"><span class="detail-note-label">Note</span><p class="detail-note-text">${escapeHtml(sub.note_text)}</p></div>`;
  }

  // Meta
  let capturedFmt = sub.captured_at;
  try {
    capturedFmt = new Date(sub.captured_at).toLocaleString([], {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch (_e) {}
  html += `<div class="detail-meta">`;
  html += `<div>${sub.site_name}</div>`;
  html += `<div>${capturedFmt}</div>`;
  html += `<div class="detail-capture-id">${sub.capture_id}</div>`;
  html += `</div>`;

  // Stepper
  html += buildDetailStepper(sub);

  return html;
}

function retargetOptionsHtml(currentType, currentId) {
  const options = [];
  for (const site of state.session?.sites || []) {
    const id = String(site.site_id || "");
    if (!id || (currentType === "location" && id === currentId)) continue;
    const label = site.name || site.label || id;
    options.push(`<option value="location:${escapeHtml(id)}" data-target-type="location" data-target-id="${escapeHtml(id)}">${escapeHtml(label)} (location)</option>`);
  }
  for (const prospect of state.session?.prospects || []) {
    const id = String(prospect.prospect_id || "");
    if (!id || (currentType === "prospect" && id === currentId)) continue;
    const label = prospect.name || id;
    options.push(`<option value="prospect:${escapeHtml(id)}" data-target-type="prospect" data-target-id="${escapeHtml(id)}">${escapeHtml(label)} (prospect)</option>`);
  }
  return options.join("");
}

function renderRetargetPreview(panel, sub, preview) {
  const current = preview.current_target || {};
  const candidates = preview.downstream?.candidates || [];
  const candidateLines = candidates.length
    ? candidates.map((candidate) => `<li>${escapeHtml(candidate.stage || "unknown")} ${escapeHtml(candidate.kind || "candidate")} - ${candidate.auto_action_on_retarget === "withdraw" ? "will be auto-withdrawn" : "no automatic change"}</li>`).join("")
    : "<li>No pending candidates</li>";
  const warnings = (preview.warnings || []).map((warning) => `<div class="retarget-warning">${escapeHtml(warning)}</div>`).join("");
  const choose = preview.retargetable
    ? '<button class="retarget-choose" type="button">Choose new site / prospect</button>'
    : '<div class="retarget-locked">Acted on - cannot retarget.</div>';
  panel.innerHTML = `
    <div class="retarget-panel-inner">
      <div><strong>Current target:</strong> ${escapeHtml(current.label || sub.site_name || "")} (${escapeHtml(current.target_type || sub.target_type || "location")})</div>
      <div><strong>Stage:</strong> ${escapeHtml(preview.stage || sub.stage || "")}</div>
      ${warnings}
      <div class="retarget-effects">Downstream effects of retargeting:</div>
      <ul class="retarget-list">
        ${candidateLines}
        <li>No journal projections yet</li>
        <li>No exported references</li>
      </ul>
      <div class="retarget-actions">
        ${choose}
        <button class="retarget-cancel" type="button">Cancel</button>
      </div>
    </div>
  `;
}

function renderRetargetPicker(panel, sub, preview) {
  const current = preview.current_target || {};
  const options = retargetOptionsHtml(current.target_type || sub.target_type || "location", current.target_id || sub.target_id || "");
  panel.innerHTML = `
    <div class="retarget-panel-inner">
      <label class="retarget-picker-label">Choose new target
        <select class="retarget-picker">${options}</select>
      </label>
      <div class="retarget-actions">
        <button class="retarget-next" type="button"${options ? "" : " disabled"}>Continue</button>
        <button class="retarget-back" type="button">Back</button>
      </div>
    </div>
  `;
}

function renderRetargetConfirm(panel, sub, preview, selected) {
  const current = preview.current_target || {};
  const pendingCount = (preview.downstream?.candidates || []).filter((candidate) => candidate.auto_action_on_retarget === "withdraw").length;
  panel.innerHTML = `
    <div class="retarget-panel-inner">
      <div class="retarget-confirm-title">Retarget ${escapeHtml(sub.capture_id)}</div>
      <div>From: ${escapeHtml(current.label || sub.site_name || "")} (${escapeHtml(current.target_type || sub.target_type || "location")})</div>
      <div>To: ${escapeHtml(selected.label)} (${escapeHtml(selected.type)}${selected.type === "location" ? `, site ${escapeHtml(selected.id)}` : ""})</div>
      <div class="retarget-effects">This will:</div>
      <ul class="retarget-list">
        <li>Move the capture and its media to the new target</li>
        <li>Auto-withdraw ${pendingCount} pending candidate${pendingCount === 1 ? "" : "s"}</li>
      </ul>
      <div class="retarget-actions">
        <button class="retarget-confirm" type="button">Confirm retarget</button>
        <button class="retarget-back" type="button">Back</button>
      </div>
    </div>
  `;
}

async function openRetargetPanel(li, sub) {
  let panel = li.querySelector(".retarget-panel");
  if (!panel) {
    panel = document.createElement("div");
    panel.className = "retarget-panel";
    panel.addEventListener("click", (event) => event.stopPropagation());
    li.append(panel);
  }
  panel.innerHTML = '<div class="retarget-panel-inner">Loading downstream effects...</div>';
  try {
    const response = await fetch(`/api/retarget-preview?capture_id=${encodeURIComponent(sub.capture_id)}`, {
      headers: { Authorization: `Bearer ${cachedToken}`, Accept: "application/json" },
      cache: "no-store",
    });
    const preview = await response.json();
    if (!response.ok) {
      panel.innerHTML = `<div class="retarget-error">${escapeHtml(preview.message || preview.error || "Retarget preview failed")}</div>`;
      return;
    }
    const showPicker = () => {
      renderRetargetPicker(panel, sub, preview);
      panel.querySelector(".retarget-back")?.addEventListener("click", (backEvent) => {
        backEvent.stopPropagation();
        showPreview();
      });
      panel.querySelector(".retarget-next")?.addEventListener("click", (nextEvent) => {
        nextEvent.stopPropagation();
        const picker = panel.querySelector(".retarget-picker");
        const option = picker?.selectedOptions?.[0];
        const selected = {
          type: option?.dataset.targetType || "",
          id: option?.dataset.targetId || "",
          label: option?.textContent?.replace(/\s+\((location|prospect)\)\s*$/, "") || "",
        };
        renderRetargetConfirm(panel, sub, preview, selected);
        panel.querySelector(".retarget-back")?.addEventListener("click", (backEvent) => {
          backEvent.stopPropagation();
          showPicker();
        });
        panel.querySelector(".retarget-confirm")?.addEventListener("click", async (confirmEvent) => {
          confirmEvent.stopPropagation();
          await confirmRetarget(panel, li, sub, selected);
        });
      });
    };
    const showPreview = () => {
      renderRetargetPreview(panel, sub, preview);
      panel.querySelector(".retarget-cancel")?.addEventListener("click", (event) => {
        event.stopPropagation();
        panel.remove();
      });
      panel.querySelector(".retarget-choose")?.addEventListener("click", (event) => {
        event.stopPropagation();
        showPicker();
      });
    };
    showPreview();
  } catch (_e) {
    panel.innerHTML = '<div class="retarget-error">Retarget preview failed</div>';
  }
}

async function confirmRetarget(panel, li, sub, selected) {
  try {
    const response = await fetch("/api/retarget", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cachedToken}`,
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        capture_id: sub.capture_id,
        new_target_type: selected.type,
        new_target_id: selected.id,
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      panel.innerHTML = `<div class="retarget-error">${escapeHtml(payload.message || payload.error || "Retarget failed")}</div>`;
      return;
    }
    sub.target_type = selected.type;
    sub.target_id = selected.id;
    sub.site_name = selected.label;
    li.querySelector(".sub-meta").textContent = `${selected.label} · ${formatRelativeTime(sub.captured_at)}`;
    panel.innerHTML = `<div class="retarget-success">Queued - will appear under ${escapeHtml(selected.label)} shortly.</div>`;
  } catch (_e) {
    panel.innerHTML = '<div class="retarget-error">Retarget failed</div>';
  }
}

function renderSubmissionList(submissions) {
  elements.submissionList.innerHTML = "";
  if (!submissions || submissions.length === 0) {
    const li = document.createElement("li");
    li.className = "sub-empty";
    li.textContent = "Your submissions will show up here. Take a photo to get started.";
    elements.submissionList.append(li);
    return;
  }

  submissions.forEach((sub) => {
    const li = document.createElement("li");
    li.className = "sub-row";
    li.dataset.captureId = sub.capture_id;

    const urls = Array.isArray(sub.photo_urls) ? sub.photo_urls : [];

    const parts = [`${sub.photo_count} photo${sub.photo_count === 1 ? "" : "s"}`];
    if (sub.has_audio) parts.push("voice note");
    const contentsText = parts.join(" · ");

    const qualityChips = buildQualityChips(sub.per_photo_quality);
    const relTime = formatRelativeTime(sub.captured_at);

    const noteSnippet = sub.note_text
      ? `<div class="sub-note-snippet">${escapeHtml(sub.note_text.length > 120 ? sub.note_text.slice(0, 120) + "…" : sub.note_text)}</div>`
      : "";

    const photoStripHtml = urls.length
      ? `<div class="sub-photo-strip">${urls.map(u => `<img src="${u}" alt="" loading="lazy" />`).join("")}</div>`
      : "";
    const retargetControl = sub.retargetable
      ? `<button class="row-retarget-btn"
          data-capture-id="${escapeHtml(sub.capture_id)}"
          data-current-target-type="${escapeHtml(sub.target_type || "location")}"
          data-current-target-id="${escapeHtml(sub.target_id || "")}"
          type="button">Wrong site? Change…</button>`
      : sub.stage === "acted_on"
        ? '<div class="retarget-readonly">Acted on - cannot retarget.</div>'
        : "";

    li.innerHTML = `
      <div class="sub-body">
        <div class="sub-meta">${sub.site_name} · ${relTime}</div>
        <div class="sub-contents">${contentsText}</div>
        ${noteSnippet}
        <div class="sub-status">
          ${buildStatusPill(sub)}
          ${qualityChips}
        </div>
        ${retargetControl}
      </div>
      ${photoStripHtml}
    `;

    li.querySelector(".row-retarget-btn")?.addEventListener("click", (event) => {
      event.stopPropagation();
      openRetargetPanel(li, sub);
    });
    li.addEventListener("click", () => toggleDetail(li, sub));
    elements.submissionList.append(li);
  });
}

function toggleDetail(li, sub) {
  // Close any other open detail
  document.querySelectorAll(".sub-detail").forEach((el) => {
    if (el.closest("li") !== li) {
      el.hidden = true;
    }
  });

  let detail = li.querySelector(".sub-detail");
  if (!detail) {
    detail = document.createElement("div");
    detail.className = "sub-detail";
    detail.innerHTML = buildDetailContent(sub);
    li.append(detail);
  } else {
    detail.hidden = !detail.hidden;
  }
}

const FORM_PANEL_SELECTORS = ".quick-fields, .photo-input-panel, .voice-panel, .note-panel";

async function showFeedView() {
  if (state.captureGated) {
    await showInstallGate();
    return;
  }
  // Fetch data
  let submissions = [];
  let qualitySummary = null;
  if (cachedToken) {
    try {
      const response = await fetch("/api/my-submissions", {
        headers: { Authorization: `Bearer ${cachedToken}`, Accept: "application/json" },
        cache: "no-store",
      });
      if (response.ok) {
        const data = await response.json();
        submissions = data.submissions || [];
        qualitySummary = data.quality_summary || null;
      }
    } catch (_e) {}
  }

  renderQualityCard(qualitySummary);
  renderSubmissionList(submissions);
  markAllActedOnAsSeen(submissions);

  // Show feed, hide form panels and success screen
  document.querySelectorAll(FORM_PANEL_SELECTORS).forEach((el) => { el.hidden = true; });
  elements.captureGuidance.hidden = true;
  elements.successScreen.hidden = true;
  elements.feedSection.hidden = false;
}

function hideFeedView() {
  if (state.captureGated) {
    showInstallGate();
    return;
  }
  elements.feedSection.hidden = true;
  elements.successScreen.hidden = true;
  document.querySelectorAll(FORM_PANEL_SELECTORS).forEach((el) => { el.hidden = false; });
  renderSelectedSiteTuning();
}

// Update the badge after a successful session load
const _originalLoadSessionAndSites = loadSessionAndSites;

// ─── End My Submissions feed ────────────────────────────────────────────────

// Capped at 2 min: field workers want prompt sync, and the foreground heartbeat
// (see below) means an expired backoff is picked up within ~20s regardless.
const DRAIN_BACKOFF_SCHEDULE_MS = [5_000, 15_000, 30_000, 60_000, 120_000];
const DRAIN_PERMANENT_FAILURE_HOURS = 24;
// A record left in "uploading" longer than this was orphaned by a tab/browser
// that died mid-upload — requeue it instead of leaving it stuck forever.
const UPLOAD_ORPHAN_RESET_MS = 120_000;

function nextBackoffMs(attempts) {
  // attempts is the count BEFORE this failure increments it.
  const index = Math.min(attempts, DRAIN_BACKOFF_SCHEDULE_MS.length - 1);
  return DRAIN_BACKOFF_SCHEDULE_MS[index];
}

function captureRetryWindowStartMs(record) {
  const createdMs = new Date(record.createdAt).getTime();
  const resetMs = new Date(record.retryWindowResetAt || 0).getTime();
  const candidates = [createdMs, resetMs].filter(Number.isFinite);
  return candidates.length ? Math.max(...candidates) : Date.now();
}

async function requeueFailedCaptures() {
  if (!window.fieldCaptureDb?.isSupported()) return 0;
  const failed = (await window.fieldCaptureDb.listByStatus("failed"))
    .filter((record) => record.capture_id !== "__token__");
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

// Rebuild a fresh Blob from persisted bytes. Records written by this version
// store `bytes` (ArrayBuffer); legacy records may still carry a `blob` — fall
// back to it, though an iOS-invalidated legacy blob may no longer be readable.
function partBlob(part) {
  if (part && part.bytes) return new Blob([part.bytes], { type: part.mimeType || "application/octet-stream" });
  return part ? part.blob : null;
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
  if (record.audio) {
    form.append("audio", partBlob(record.audio), record.audio.filename);
    form.append("audio_duration_seconds", String(record.audio.durationSeconds || 0));
  }
  const response = await fetch("/api/submit", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${cachedToken}`,
      Accept: "application/json",
    },
    body: form,
  });
  if (response.status === 401) {
    // Auth lost — stop draining and prompt for re-auth.
    clearStoredToken();
    showTokenPasteUI();
    const err = new Error("auth_lost");
    err.permanent = true;
    throw err;
  }
  if (!response.ok) {
    let message = `upload_failed_${response.status}`;
    try {
      const body = await response.json();
      if (body?.message) message = body.message;
    } catch (_e) {}
    const err = new Error(message);
    err.status = response.status;
    // 4xx other than 401 means the request itself is bad — won't fix on retry.
    err.permanent = response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429;
    throw err;
  }
  return response.json();
}

async function drainQueue() {
  if (state.isDraining) return;
  if (!window.fieldCaptureDb?.isSupported()) return;
  if (!navigator.onLine) {
    setStatus("Offline — captures will upload when connection returns.", "warning");
    return;
  }
  if (Date.now() < state.drainBackoffUntil) return;
  state.isDraining = true;
  try {
    // Recover orphans: records stuck in "uploading" from a prior session whose
    // tab died mid-flight (the done/pending/failed transition never ran). No drain
    // is in progress here (isDraining guard above), so anything still "uploading"
    // past the reset window is orphaned — requeue it as pending so it retries.
    const orphans = (await window.fieldCaptureDb.listByStatus("uploading"))
      .filter((record) => record.capture_id !== "__token__");
    const orphanNow = Date.now();
    for (const orphan of orphans) {
      const startedMs = new Date(orphan.lastTriedAt || orphan.createdAt).getTime();
      if (!Number.isFinite(startedMs) || orphanNow - startedMs > UPLOAD_ORPHAN_RESET_MS) {
        await window.fieldCaptureDb.updateCapture(orphan.capture_id, {
          status: "pending",
          lastError: "recovered_orphaned_upload",
        });
      }
    }
    const pending = (await window.fieldCaptureDb.listByStatus("pending"))
      .filter((record) => record.capture_id !== "__token__");
    pending.sort((a, b) => (a.createdAt || "").localeCompare(b.createdAt || ""));
    for (const record of pending) {
      const ageHours = (Date.now() - captureRetryWindowStartMs(record)) / 3_600_000;
      if (ageHours > DRAIN_PERMANENT_FAILURE_HOURS) {
        await window.fieldCaptureDb.updateCapture(record.capture_id, {
          status: "failed",
          lastError: "exceeded_retry_window",
        });
        continue;
      }
      try {
        await window.fieldCaptureDb.updateCapture(record.capture_id, {
          status: "uploading",
          lastTriedAt: localIsoTimestamp(),
        });
        await uploadOneCapture(record);
        await window.fieldCaptureDb.updateCapture(record.capture_id, {
          status: "done",
          lastError: null,
        });
      } catch (error) {
        const attempts = (record.attempts || 0) + 1;
        await window.fieldCaptureDb.updateCapture(record.capture_id, {
          status: error.permanent ? "failed" : "pending",
          attempts,
          lastError: error.message || String(error),
        });
        if (error.permanent) {
          // Surface the failure but keep draining the rest.
          setStatus(`Capture failed: ${error.message}`, "error");
          continue;
        }
        state.drainBackoffUntil = Date.now() + nextBackoffMs(attempts - 1);
        // Stop the loop — try again after backoff.
        break;
      }
    }
    // Sweep done rows older than 5 minutes to keep IDB lean.
    const dones = await window.fieldCaptureDb.listByStatus("done");
    for (const record of dones) {
      const ageMs = Date.now() - new Date(record.lastTriedAt || record.createdAt).getTime();
      if (ageMs > 5 * 60_000) {
        await window.fieldCaptureDb.deleteCapture(record.capture_id);
      }
    }
  } finally {
    state.isDraining = false;
    state.lastDrainAt = Date.now();
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
  state.pendingCount = pending + uploading;
  renderQueueStrip(pending, uploading, failed);
  await renderStaleStrip();
}

function renderQueueStrip(pending, uploading, failed) {
  const strip = elements.queueStrip;
  if (!strip) return;
  const total = pending + uploading + failed;
  if (total === 0) {
    // Affirmative synced state — explicit, not silent.
    strip.hidden = false;
    strip.textContent = "All captures synced ✓";
    strip.dataset.tone = "synced";
    return;
  }
  const parts = [];
  if (uploading) parts.push(`Uploading ${uploading}`);
  if (pending) parts.push(`${pending} pending`);
  if (failed) parts.push(`${failed} failed`);
  strip.hidden = false;
  const queueText = document.createElement("span");
  queueText.textContent = parts.join(" • ");
  strip.replaceChildren(queueText);
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
    strip.appendChild(retryButton);
  }
  strip.dataset.tone = failed ? "error" : uploading ? "active" : "warning";
}

async function summarizeStaleCaptures() {
  if (!window.fieldCaptureDb?.isSupported()) return [];
  const pending = await window.fieldCaptureDb.listByStatus("pending");
  const cutoffMs = 24 * 60 * 60_000;
  const now = Date.now();
  return pending
    .filter((r) => r.capture_id !== "__token__")
    .filter((r) => now - captureRetryWindowStartMs(r) > cutoffMs)
    .map((r) => ({
      capture_id: r.capture_id,
      createdAt: r.createdAt,
      site: r.fields?.site || "(unknown site)",
      ageHours: Math.floor((now - captureRetryWindowStartMs(r)) / 3_600_000),
    }));
}

async function renderStaleStrip() {
  const strip = document.querySelector("#staleStrip");
  if (!strip) return;
  const stale = await summarizeStaleCaptures();
  if (!stale.length) {
    strip.hidden = true;
    strip.textContent = "";
    return;
  }
  const lines = stale.map((s) => `• ${s.site} — ${s.ageHours}h old (${s.createdAt.slice(0, 10)})`);
  strip.hidden = false;
  strip.textContent = `${stale.length} capture${stale.length === 1 ? "" : "s"} stuck >24h:\n${lines.join("\n")}`;
  strip.dataset.tone = "error";
}

async function requestPersistentStorage() {
  // iOS Safari evicts non-persistent IDB after ~7 days
  // of non-use. For installed home-screen PWAs, Safari
  // and Chrome will often grant persistence on request,
  // which prevents background eviction. No-ops cleanly
  // if the API is missing (older browsers).
  if (!navigator.storage?.persist) return false;
  try {
    return await navigator.storage.persist();
  } catch (_e) {
    return false;
  }
}

async function showInstallGate() {
  state.captureGated = true;
  elements.captureForm.hidden = true;
  document.querySelectorAll(".photo-input-panel, .voice-panel, .note-panel").forEach((el) => {
    el.hidden = true;
  });
  elements.captureGuidance.hidden = true;
  elements.successScreen.hidden = true;
  elements.feedSection.hidden = true;
  elements.installGate.hidden = false;
  elements.installGatePending.hidden = true;
  elements.installGatePending.textContent = "";
  updateSubmitState();

  if (!window.fieldCaptureDb?.isSupported()) return;
  try {
    const pending = await window.fieldCaptureDb.listByStatus("pending");
    const count = pending.filter((record) => record.capture_id !== "__token__").length;
    if (count > 0) {
      elements.installGatePending.textContent = `You have ${count} unsent capture(s) on this phone. Open the app from your Home Screen icon to send them.`;
      elements.installGatePending.hidden = false;
    }
  } catch (_e) {
    elements.installGatePending.hidden = true;
    elements.installGatePending.textContent = "";
  }
}

async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return null;
  try {
    const reg = await navigator.serviceWorker.register("/sw.js?v=" + INTERFACE_VERSION);
    return reg;
  } catch (_e) {
    return null;
  }
}

async function requestBackgroundSync() {
  if (!("serviceWorker" in navigator)) return;
  const reg = await navigator.serviceWorker.ready.catch(() => null);
  if (!reg || !("sync" in reg)) return; // iOS Safari path
  try {
    await reg.sync.register("field-capture-drain");
  } catch (_e) {
    // Sync registration can throw if the user denied permission
    // or the browser is in a restricted mode. Foreground drain
    // remains the fallback.
  }
}

elements.fileInput.addEventListener("change", (event) => addFiles(event.target.files));
elements.cameraInput.addEventListener("change", (event) => addFiles(event.target.files));
elements.recordVoiceButton.addEventListener("click", handleRecordVoiceEvent);
elements.recordVoiceButton.addEventListener("touchend", handleRecordVoiceEvent);
elements.stopVoiceButton.addEventListener("click", stopVoiceRecording);
elements.clearVoiceButton.addEventListener("click", () => {
  if (state.audio && !window.confirm("Discard this recording?")) {
    return;
  }
  if (state.recorder?.state === "paused" || state.recorder?.state === "recording") {
    state.recorder.stop();
  }
  clearAudio();
  state.audioElapsedBeforePause = 0;
  state.audioResumedAt = null;
  renderAudio();
});
elements.exportButton.addEventListener("click", saveCapture);
elements.siteInput.addEventListener("change", updateSelectedSiteDetails);
elements.submitAnotherButton.addEventListener("click", resetToForm);
elements.mySubsBtn.addEventListener("click", showFeedView);
elements.feedBack.addEventListener("click", hideFeedView);
elements.queueStrip?.addEventListener("click", (event) => {
  if (event.target?.dataset?.action === "retry-failed") {
    retryFailedCaptures().catch(() => {});
  }
});
window.addEventListener("online", () => {
  setOfflineBanner(false);
  state.drainBackoffUntil = 0;
  drainQueue().catch(() => {});
  if (cachedToken) loadSessionAndSites().catch(() => {});
});
window.addEventListener("offline", () => setOfflineBanner(true));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    // Foregrounding is a strong "try now" signal — clear any backoff so a worker
    // opening the app to check on pending photos triggers an immediate attempt
    // instead of waiting out the remaining backoff.
    state.drainBackoffUntil = 0;
    drainQueue().catch(() => {});
  }
});
// Foreground heartbeat: navigator.onLine and the "online" event are unreliable
// (especially on iOS) and do NOT fire when a weak signal becomes a usable one
// without the network interface dropping — which is exactly the poor-connectivity
// field case. Poll while visible so a stuck queue self-heals the moment real
// connectivity returns. drainQueue() self-guards on isDraining/offline/backoff,
// so an idle tick is nearly free.
const DRAIN_HEARTBEAT_MS = 20_000;
setInterval(() => {
  if (document.visibilityState === "visible") drainQueue().catch(() => {});
}, DRAIN_HEARTBEAT_MS);

(async () => {
  await requestPersistentStorage();
  if (isIosDevice() && !isStandaloneDisplay()) {
    await showInstallGate();
  }
  // Always still drain — flushing a pre-gate queue while online is
  // strictly beneficial and does not create new captures.
  await requeueFailedCaptures().catch(() => 0);
  await refreshPendingCount().catch(() => {});
  drainQueue().catch(() => {});
})();
// Register once at boot. Don't await — the page is usable while
// the SW activates.
registerServiceWorker();

elements.interfaceVersion.textContent = INTERFACE_VERSION;
elements.pipelineVersion.textContent = PIPELINE_VERSION;
updateSelectedSiteDetails();
renderPhotos();
renderAudio();
setFormEnabled(false);
setOfflineBanner(!navigator.onLine);
if (bootstrapToken()) {
  loadSessionAndSites();
  // Refresh badge independently — non-blocking, silent on failure
  if (cachedToken) {
    fetch("/api/my-submissions", {
      headers: { Authorization: `Bearer ${cachedToken}`, Accept: "application/json" },
      cache: "no-store",
    })
      .then((r) => r.ok ? r.json() : null)
      .then((data) => { if (data) updateBadge(data.submissions || []); })
      .catch(() => {});
  }
}
