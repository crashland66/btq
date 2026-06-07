const TOKEN_KEY = 'voiceMemoToken';
const MODE_KEY = 'voiceMemoMode';
const statusText = document.getElementById('statusText');
const recordVoiceButton = document.getElementById('recordVoiceButton');
const stopVoiceButton = document.getElementById('stopVoiceButton');
const clearVoiceButton = document.getElementById('clearVoiceButton');
const voicePreview = document.getElementById('voicePreview');
const voiceStatus = document.getElementById('voiceStatus');
const voiceSupportMessage = document.getElementById('voiceSupportMessage');
const noteInput = document.getElementById('noteInput');
const personalNoteInput = document.getElementById('personalNoteInput');
const submitButton = document.getElementById('submitButton');
const formError = document.getElementById('formError');
const lastSubmitted = document.getElementById('lastSubmitted');
const operationsFields = document.getElementById('operationsFields');
const personalFields = document.getElementById('personalFields');
const siteInput = document.getElementById('siteInput');
const employeeInput = document.getElementById('employeeInput');
const geoStatus = document.getElementById('geoStatus');
const geoSummary = document.getElementById('geoSummary');
const footerMode = document.getElementById('footerMode');
const modeInputs = Array.from(document.querySelectorAll('input[name="mode"]'));
let token = '';

const state = {
  recorder: null,
  stream: null,
  chunks: [],
  audio: null,
  recordingStartedAt: null,
  audioElapsedBeforePause: 0,
  audioResumedAt: null,
  stopTimer: null,
  submitInFlight: false,
  geolocation: null,
  geolocationDenied: false,
  sitesLoaded: false,
  employeesLoaded: false,
  sitesById: new Map(),
  activeSiteIds: new Set(),
  employeesByJob: new Map(),
  currentEmployeeSelection: new Set(),
  isDraining: false,
  pendingCount: 0,
  lastDrainAt: 0,
  drainBackoffUntil: 0
};

function tokenFromHash() {
  const raw = window.location.hash || '';
  if (!raw) return '';
  // Accept both '#token=XXX' and '#access=XXX' or just '#XXX' (raw hash).
  const stripped = raw.startsWith('#') ? raw.slice(1) : raw;
  const params = new URLSearchParams(stripped);
  return params.get('token') || params.get('access') || (stripped.includes('=') ? '' : stripped);
}

function persistTokenAndScrub(value) {
  token = value;
  localStorage.setItem(TOKEN_KEY, token);
  stashTokenForServiceWorker(token);
  history.replaceState({}, '', window.location.pathname);
}

function bootstrapToken() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get('token') || '';
  const fromHash = fromUrl ? '' : tokenFromHash();
  const fromUrlOrHash = fromUrl || fromHash;
  if (fromUrlOrHash) {
    const previous = localStorage.getItem(TOKEN_KEY) || '';
    persistTokenAndScrub(fromUrlOrHash);
    if (previous && previous !== fromUrlOrHash) {
      state.sitesLoaded = false;
      state.employeesLoaded = false;
      state.sitesById.clear();
      state.activeSiteIds.clear();
      state.employeesByJob.clear();
      state.currentEmployeeSelection.clear();
      statusText.textContent = 'New token detected - switched accounts.';
    } else {
      statusText.textContent = fromHash ? 'Token saved from link.' : 'Token saved - bookmark or add to home screen.';
    }
    return true;
  }
  token = localStorage.getItem(TOKEN_KEY) || '';
  if (token) {
    return true;
  }
  showTokenPasteUI();
  statusText.textContent = 'Paste your access token below to enable submissions.';
  return false;
}

function showTokenPasteUI() {
  if (document.getElementById('tokenPastePanel')) return;
  const main = document.querySelector('main.app-shell') || document.body;
  const panel = document.createElement('section');
  panel.id = 'tokenPastePanel';
  panel.className = 'details-panel token-paste-panel';
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
  // Insert near the top, just after the header.
  const header = main.querySelector('.app-header');
  if (header && header.nextSibling) {
    main.insertBefore(panel, header.nextSibling);
  } else {
    main.appendChild(panel);
  }
  const input = panel.querySelector('#tokenPasteInput');
  const errorEl = panel.querySelector('#tokenPasteError');
  panel.querySelector('#tokenPasteSave').addEventListener('click', () => {
    const value = (input.value || '').trim();
    if (!value || value.length < 8) {
      errorEl.textContent = 'That does not look like a valid token.';
      errorEl.hidden = false;
      return;
    }
    persistTokenAndScrub(value);
    panel.remove();
    statusText.textContent = 'Token saved.';
    stashTokenForServiceWorker(token);
    // Re-enable inputs and reload picker data.
    loadPickers();
    renderAudio();
  });
  input.focus();
}

function currentMode() {
  return document.querySelector('input[name="mode"]:checked')?.value || 'operations';
}

function applySavedMode() {
  const saved = localStorage.getItem(MODE_KEY) || 'operations';
  const mode = saved === 'personal' ? 'personal' : 'operations';
  modeInputs.forEach((input) => {
    input.checked = input.value === mode;
  });
  updateModeVisibility();
}

function updateModeVisibility() {
  const personal = currentMode() === 'personal';
  operationsFields.hidden = personal;
  personalFields.hidden = !personal;
  footerMode.textContent = personal ? 'Personal' : 'Operations';
  localStorage.setItem(MODE_KEY, currentMode());
}

function localIsoWithOffset() {
  const date = new Date();
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const abs = Math.abs(offsetMinutes);
  const offset = `${sign}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`;
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  return `${local}${offset}`;
}

function randomSuffix() {
  // crypto.randomUUID is absent on older iOS Safari / some Android
  // WebViews (same fleet concern as the createImageBitmap fallback),
  // so degrade to Math.random — uniqueness, not cryptographic strength,
  // is the requirement.
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID().slice(0, 8);
  }
  return Math.random().toString(36).slice(2, 10);
}

// Mirror the token into IDB for the service worker. SW context does
// not have the same localStorage access as the page across all PWA
// modes, but it can read this sentinel row.
async function stashTokenForServiceWorker(value) {
  if (!window.fieldCaptureDb?.isSupported() || !value) return;
  try {
    await window.fieldCaptureDb.putCapture({
      capture_id: '__token__',
      value,
      status: '__meta__',
      createdAt: localIsoWithOffset()
    });
  } catch (_error) {
    // Foreground drain remains available.
  }
}

function supportsAudioRecording() {
  return Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
}

function preferredAudioMimeType() {
  const choices = ['audio/webm', 'audio/mp4'];
  return choices.find((mimeType) => window.MediaRecorder?.isTypeSupported?.(mimeType)) || '';
}

function audioExtension(mimeType) {
  if (mimeType.includes('mp4') || mimeType.includes('m4a')) return '.m4a';
  if (mimeType.includes('wav')) return '.wav';
  return '.webm';
}

function microphoneErrorMessage(error) {
  if (error?.name === 'NotAllowedError' || error?.name === 'SecurityError') {
    return 'Microphone access was blocked. Allow microphone access and try again.';
  }
  if (error?.name === 'NotFoundError' || error?.name === 'DevicesNotFoundError') {
    return 'No microphone was found on this device.';
  }
  if (error?.name === 'NotReadableError' || error?.name === 'TrackStartError') {
    return 'The microphone is busy or unavailable. Close other recording apps and try again.';
  }
  return 'Voice recording could not start. Try again.';
}

function renderAudio() {
  const enabled = Boolean(token) && supportsAudioRecording();
  const recState = state.recorder?.state || null;
  const isRecording = recState === 'recording';
  const isPaused = recState === 'paused';
  const isActive = isRecording || isPaused;
  recordVoiceButton.disabled = state.submitInFlight || isRecording || !enabled;
  stopVoiceButton.disabled = state.submitInFlight || !isRecording;
  clearVoiceButton.disabled = state.submitInFlight || (!isActive && !state.audio);
  submitButton.disabled = state.submitInFlight || !token;
  if (state.audio) {
    voicePreview.src = state.audio.url;
    voicePreview.hidden = false;
    if (!isActive) voiceStatus.textContent = `Ready: ${state.audio.durationSeconds}s`;
  } else {
    voicePreview.removeAttribute('src');
    voicePreview.hidden = true;
    if (!isActive) voiceStatus.textContent = 'No memo';
  }
  voiceStatus.dataset.tone = isRecording ? 'active' : isPaused ? 'paused' : '';
}

function clearMessages() {
  formError.hidden = true;
  formError.textContent = '';
  lastSubmitted.hidden = true;
}

function showRefreshPickerError(message) {
  formError.textContent = '';
  const span = document.createElement('span');
  span.textContent = message;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'inline-refresh';
  button.textContent = 'Refresh';
  button.addEventListener('click', loadPickers);
  formError.append(span, ' ', button);
  formError.hidden = false;
}

function clearAudio() {
  clearMessages();
  if (state.recorder) {
    state.recorder.stream.getTracks().forEach((track) => track.stop());
    state.recorder = null;
    state.stream = null;
    state.chunks = [];
    state.audioElapsedBeforePause = 0;
    state.audioResumedAt = null;
    window.clearTimeout(state.stopTimer);
    state.stopTimer = null;
  }
  if (state.audio?.url) URL.revokeObjectURL(state.audio.url);
  state.audio = null;
  renderAudio();
}

async function fetchPicker(path, key) {
  const response = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
  if (!response.ok) throw new Error(`Picker fetch failed: ${path}`);
  const payload = await response.json();
  return payload[key] || [];
}

function sortedEmployeesForGroup(employees) {
  return [...employees].sort((left, right) => {
    const leftLast = String(left.last || '').toLowerCase();
    const rightLast = String(right.last || '').toLowerCase();
    if (leftLast !== rightLast) return leftLast.localeCompare(rightLast);
    const leftFirst = String(left.preferred_name || left.first || '').toLowerCase();
    const rightFirst = String(right.preferred_name || right.first || '').toLowerCase();
    return leftFirst.localeCompare(rightFirst);
  });
}

function buildEmployeeGroups(employees) {
  state.employeesByJob = new Map();
  employees.forEach((employee) => {
    const rawJob = String(employee.job || '').trim();
    const job = rawJob && state.activeSiteIds.has(rawJob) ? rawJob : '__unassigned__';
    const group = state.employeesByJob.get(job) || [];
    group.push(employee);
    state.employeesByJob.set(job, group);
  });
  state.employeesByJob.forEach((employeesForJob, job) => {
    state.employeesByJob.set(job, sortedEmployeesForGroup(employeesForJob));
  });
}

function alphabeticalEmployeeJobOrder() {
  return Array.from(state.employeesByJob.keys())
    .filter((job) => job !== '__unassigned__')
    .sort((left, right) => {
      const leftLabel = state.sitesById.get(left)?.label || left;
      const rightLabel = state.sitesById.get(right)?.label || right;
      return leftLabel.localeCompare(rightLabel, undefined, { sensitivity: 'base' });
    });
}

function employeeJobOrderForSite(siteId) {
  const order = alphabeticalEmployeeJobOrder();
  if (siteId && order.includes(siteId)) {
    return [siteId, ...order.filter((job) => job !== siteId), '__unassigned__'];
  }
  return [...order, '__unassigned__'];
}

function captureEmployeeSelection() {
  state.currentEmployeeSelection = new Set(Array.from(employeeInput.selectedOptions).map((option) => option.value));
}

function renderEmployeePicker(orderedJobs) {
  const selected = new Set(state.currentEmployeeSelection);
  employeeInput.innerHTML = '';
  orderedJobs.forEach((job) => {
    const employees = state.employeesByJob.get(job) || [];
    if (!employees.length) return;
    const group = document.createElement('optgroup');
    group.label = job === '__unassigned__' ? 'Unassigned' : state.sitesById.get(job)?.label || 'Unassigned';
    employees.forEach((employee) => {
      const option = document.createElement('option');
      option.value = employee.id;
      option.textContent = employee.label;
      option.selected = selected.has(employee.id);
      group.append(option);
    });
    employeeInput.append(group);
  });
}

function rerenderEmployeesForCurrentSite() {
  captureEmployeeSelection();
  renderEmployeePicker(employeeJobOrderForSite(siteInput.value));
}

async function loadPickers() {
  if (!token) return;
  try {
    const [sites, employees] = await Promise.all([
      fetchPicker('/api/sites', 'sites'),
      fetchPicker('/api/employees', 'employees')
    ]);
    state.sitesById = new Map(sites.map((site) => [String(site.id), site]));
    state.activeSiteIds = new Set(sites.map((site) => String(site.id)));
    siteInput.innerHTML = '<option value="">(no site)</option>';
    sites.forEach((site) => {
      const option = document.createElement('option');
      option.value = site.id;
      option.textContent = site.label;
      siteInput.append(option);
    });
    buildEmployeeGroups(employees);
    renderEmployeePicker(employeeJobOrderForSite(siteInput.value));
    state.sitesLoaded = true;
    state.employeesLoaded = true;
    formError.hidden = true;
  } catch (error) {
    state.sitesLoaded = false;
    state.employeesLoaded = false;
    formError.textContent = 'Site and employee lists could not load. Personal mode still works.';
    formError.hidden = false;
  }
}

function captureGeolocation({ refresh = false } = {}) {
  if (state.geolocationDenied || !navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    (position) => {
      state.geolocation = {
        lat: position.coords.latitude,
        lng: position.coords.longitude,
        accuracy_m: position.coords.accuracy,
        captured_at: new Date().toISOString()
      };
      geoSummary.textContent = `${state.geolocation.lat.toFixed(2)}, ${state.geolocation.lng.toFixed(2)} (±${Math.round(state.geolocation.accuracy_m)}m)`;
      geoStatus.hidden = false;
    },
    () => {
      state.geolocationDenied = true;
      geoStatus.hidden = true;
    },
    { enableHighAccuracy: false, timeout: refresh ? 5000 : 10000, maximumAge: 60000 }
  );
}

async function startVoiceRecording(event) {
  event?.preventDefault();
  clearMessages();
  if (!state.geolocation && !state.geolocationDenied) captureGeolocation();
  // Resume a paused recording instead of starting a new one.
  if (state.recorder?.state === 'paused') {
    state.recorder.resume();
    const remaining = Math.max(60_000, 600_000 - (state.audioElapsedBeforePause || 0));
    state.stopTimer = window.setTimeout(stopVoiceRecording, remaining);
    state.audioResumedAt = Date.now();
    voiceStatus.textContent = 'Recording...';
    statusText.textContent = 'Recording voice memo (up to 10 min)...';
    renderAudio();
    return;
  }
  if (!token || !supportsAudioRecording() || state.recorder?.state === 'recording') return;
  clearAudio();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = preferredAudioMimeType();
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    state.stream = stream;
    state.recorder = recorder;
    state.chunks = [];
    state.recordingStartedAt = Date.now();
    state.audioElapsedBeforePause = 0;
    state.audioResumedAt = null;
    recorder.addEventListener('dataavailable', (chunkEvent) => {
      if (chunkEvent.data?.size) state.chunks.push(chunkEvent.data);
    });
    recorder.addEventListener('stop', () => finishVoiceRecording(recorder), { once: true });
    recorder.start();
    // 10 min cap, was 60s — see field_capture/public/app.js fix.
    state.stopTimer = window.setTimeout(stopVoiceRecording, 600000);
    voiceStatus.textContent = 'Recording...';
    statusText.textContent = 'Recording voice memo (up to 10 min)...';
    renderAudio();
  } catch (error) {
    statusText.textContent = microphoneErrorMessage(error);
    renderAudio();
  }
}

function stopVoiceRecording() {
  if (state.recorder?.state === 'recording') {
    const segmentElapsed = state.audioResumedAt
      ? Date.now() - state.audioResumedAt
      : Date.now() - (state.recordingStartedAt || Date.now());
    state.audioElapsedBeforePause = (state.audioElapsedBeforePause || 0) + segmentElapsed;
    state.recorder.pause();
    window.clearTimeout(state.stopTimer);
    state.stopTimer = null;
    voiceStatus.textContent = 'Paused — press record to resume';
    statusText.textContent = 'Voice memo paused — press record to resume.';
    renderAudio();
  }
}

function finishVoiceRecording(recorder) {
  window.clearTimeout(state.stopTimer);
  state.stream?.getTracks().forEach((track) => track.stop());
  const mimeType = recorder.mimeType || preferredAudioMimeType() || 'audio/webm';
  const blob = new Blob(state.chunks, { type: mimeType });
  const elapsed = state.recordingStartedAt ? Math.round((Date.now() - state.recordingStartedAt) / 1000) : 1;
  const durationSeconds = Math.max(1, elapsed);
  const isoTimestamp = new Date().toISOString().replace(/[:.]/g, '-');
  state.audio = {
    blob,
    url: URL.createObjectURL(blob),
    filename: `voice-memo-${isoTimestamp}${audioExtension(mimeType)}`,
    mimeType,
    durationSeconds
  };
  state.recorder = null;
  state.stream = null;
  state.chunks = [];
  state.audioElapsedBeforePause = 0;
  state.audioResumedAt = null;
  statusText.textContent = 'Memo ready to save.';
  renderAudio();
}

function finishActiveVoiceRecordingForSubmit() {
  return new Promise((resolve, reject) => {
    if (state.recorder?.state !== 'recording' && state.recorder?.state !== 'paused') {
      resolve();
      return;
    }
    const recorder = state.recorder;
    const timeout = window.setTimeout(() => {
      recorder.removeEventListener('stop', handleStop);
      reject(new Error('Timed out while saving the voice memo.'));
    }, 5000);
    function handleStop() {
      window.clearTimeout(timeout);
      resolve();
    }
    recorder.addEventListener('stop', handleStop, { once: true });
    recorder.stop();
  });
}

function selectedEmployeeSlugs() {
  return Array.from(employeeInput.selectedOptions).map((option) => option.value).join(',');
}

function memoNoteForMode(mode) {
  return mode === 'personal' ? personalNoteInput.value : noteInput.value;
}

function clearMemoNoteForMode(mode) {
  if (mode === 'personal') {
    personalNoteInput.value = '';
  } else {
    noteInput.value = '';
  }
}

function buildMemoRecord() {
  if (!state.audio) {
    throw new Error('Record a voice memo before saving');
  }
  const capturedAt = localIsoWithOffset();
  const captureId = `cap-voice-${capturedAt.replace(/[:.]/g, '-')}-${randomSuffix()}`;
  const mode = currentMode();
  const fields = {
    capture_id: captureId,
    note: memoNoteForMode(mode),
    captured_at: capturedAt,
    duration_seconds: String(state.audio.durationSeconds),
    mode
  };
  if (mode === 'operations') {
    fields.site_id = siteInput.value;
    fields.employee_slugs = selectedEmployeeSlugs();
  }
  if (state.geolocation) {
    fields.geolocation_lat = String(state.geolocation.lat);
    fields.geolocation_lng = String(state.geolocation.lng);
    fields.geolocation_accuracy_m = String(state.geolocation.accuracy_m);
    fields.geolocation_captured_at = state.geolocation.captured_at;
  }
  return {
    capture_id: captureId,
    status: 'pending',
    attempts: 0,
    lastError: null,
    createdAt: capturedAt,
    lastTriedAt: null,
    metadata: {
      mode,
      site_id: mode === 'operations' && siteInput.value ? siteInput.value : null,
      employee_slugs: mode === 'operations' ? selectedEmployeeSlugs() : ''
    },
    fields,
    photos: [],
    audio: {
      filename: state.audio.filename,
      mimeType: state.audio.mimeType,
      blob: state.audio.blob,
      durationSeconds: state.audio.durationSeconds
    }
  };
}

function showLocalSaveSuccess(record) {
  const duration = record.audio?.durationSeconds || 0;
  clearAudio();
  clearMemoNoteForMode(record.fields?.mode || currentMode());
  const time = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  lastSubmitted.textContent = `Voice memo saved locally at ${time} (${duration}s)`;
  lastSubmitted.hidden = false;
  statusText.textContent = 'Voice memo saved locally';
  renderAudio();
}

async function saveMemo() {
  clearMessages();
  captureGeolocation({ refresh: true });
  state.submitInFlight = true;
  renderAudio();
  try {
    await finishActiveVoiceRecordingForSubmit();
    if (!window.fieldCaptureDb?.isSupported()) {
      throw new Error('Local storage not available - cannot save voice memo.');
    }
    const record = buildMemoRecord();
    await window.fieldCaptureDb.putCapture(record);
    showLocalSaveSuccess(record);
    await refreshPendingCount();
    drainQueue().catch(() => {});
    requestBackgroundSync();
  } catch (error) {
    formError.textContent = error.message || 'Network error. Try again.';
    formError.hidden = false;
  }
  state.submitInFlight = false;
  renderAudio();
}

const DRAIN_BACKOFF_SCHEDULE_MS = [5_000, 30_000, 120_000, 600_000, 3_600_000];
const DRAIN_PERMANENT_FAILURE_HOURS = 24;

function nextBackoffMs(attempts) {
  const index = Math.min(attempts, DRAIN_BACKOFF_SCHEDULE_MS.length - 1);
  return DRAIN_BACKOFF_SCHEDULE_MS[index];
}

async function uploadOneCapture(record) {
  const form = new FormData();
  const fields = record.fields || {};
  for (const [key, value] of Object.entries(fields)) {
    form.append(key, value == null ? '' : String(value));
  }
  if (record.audio) {
    form.append('audio', record.audio.blob, record.audio.filename);
  }
  const response = await fetch('/api/upload', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/json'
    },
    body: form
  });
  if (response.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    token = '';
    statusText.textContent = 'Token expired or invalid. Reopen the tokenized URL.';
    const err = new Error('auth_lost');
    err.permanent = true;
    throw err;
  }
  if (response.status !== 201 && response.status !== 200) {
    let message = `upload_failed_${response.status}`;
    let code = '';
    try {
      const payload = await response.json();
      code = payload.error || '';
      message = payload.message || payload.error || message;
    } catch (_error) {}
    if (response.status === 400 && (code === 'unknown_site_id' || code === 'unknown_employee_slug')) {
      showRefreshPickerError('Site/employee list out of date - refresh the page');
    }
    const err = new Error(message);
    err.status = response.status;
    err.permanent = response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429;
    throw err;
  }
  return response.json();
}

async function drainQueue() {
  if (state.isDraining) return;
  if (!window.fieldCaptureDb?.isSupported()) return;
  if (!navigator.onLine) {
    statusText.textContent = 'Offline - memos will upload when connection returns.';
    return;
  }
  if (Date.now() < state.drainBackoffUntil) return;
  state.isDraining = true;
  try {
    const pending = (await window.fieldCaptureDb.listByStatus('pending'))
      .filter((record) => record.capture_id !== '__token__');
    pending.sort((a, b) => (a.createdAt || '').localeCompare(b.createdAt || ''));
    for (const record of pending) {
      const ageHours = (Date.now() - new Date(record.createdAt).getTime()) / 3_600_000;
      if (ageHours > DRAIN_PERMANENT_FAILURE_HOURS) {
        await window.fieldCaptureDb.updateCapture(record.capture_id, {
          status: 'failed',
          lastError: 'exceeded_retry_window'
        });
        continue;
      }
      try {
        await window.fieldCaptureDb.updateCapture(record.capture_id, {
          status: 'uploading',
          lastTriedAt: localIsoWithOffset()
        });
        await uploadOneCapture(record);
        await window.fieldCaptureDb.updateCapture(record.capture_id, {
          status: 'done',
          lastError: null
        });
      } catch (error) {
        const attempts = (record.attempts || 0) + 1;
        await window.fieldCaptureDb.updateCapture(record.capture_id, {
          status: error.permanent ? 'failed' : 'pending',
          attempts,
          lastError: error.message || String(error)
        });
        if (error.permanent) {
          statusText.textContent = `Memo upload failed: ${error.message}`;
          continue;
        }
        state.drainBackoffUntil = Date.now() + nextBackoffMs(attempts - 1);
        break;
      }
    }
    const dones = await window.fieldCaptureDb.listByStatus('done');
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
    window.fieldCaptureDb.countByStatus('pending'),
    window.fieldCaptureDb.countByStatus('uploading'),
    window.fieldCaptureDb.countByStatus('failed')
  ]);
  state.pendingCount = pending + uploading;
  renderQueueStrip(pending, uploading, failed);
  await renderStaleStrip();
}

function renderQueueStrip(pending, uploading, failed) {
  const strip = document.querySelector('#queueStrip');
  if (!strip) return;
  const total = pending + uploading + failed;
  if (total === 0) {
    strip.hidden = false;
    strip.textContent = 'All memos synced ✓';
    strip.dataset.tone = 'synced';
    return;
  }
  const parts = [];
  if (uploading) parts.push(`Uploading ${uploading}`);
  if (pending) parts.push(`${pending} pending`);
  if (failed) parts.push(`${failed} failed`);
  strip.hidden = false;
  strip.textContent = parts.join(' • ');
  strip.dataset.tone = failed ? 'error' : uploading ? 'active' : 'warning';
}

async function summarizeStaleCaptures() {
  if (!window.fieldCaptureDb?.isSupported()) return [];
  const pending = await window.fieldCaptureDb.listByStatus('pending');
  const cutoffMs = 24 * 60 * 60_000;
  const now = Date.now();
  return pending
    .filter((r) => r.capture_id !== '__token__')
    .filter((r) => now - new Date(r.createdAt).getTime() > cutoffMs)
    .map((r) => ({
      capture_id: r.capture_id,
      createdAt: r.createdAt,
      mode: r.fields?.mode || 'voice memo',
      ageHours: Math.floor((now - new Date(r.createdAt).getTime()) / 3_600_000)
    }));
}

async function renderStaleStrip() {
  const strip = document.querySelector('#staleStrip');
  if (!strip) return;
  const stale = await summarizeStaleCaptures();
  if (!stale.length) {
    strip.hidden = true;
    strip.textContent = '';
    return;
  }
  const lines = stale.map((s) => `• ${s.mode} - ${s.ageHours}h old (${s.createdAt.slice(0, 10)})`);
  strip.hidden = false;
  strip.textContent = `${stale.length} memo${stale.length === 1 ? '' : 's'} stuck >24h:\n${lines.join('\n')}`;
  strip.dataset.tone = 'error';
}

async function requestPersistentStorage() {
  if (!navigator.storage?.persist) return false;
  try {
    return await navigator.storage.persist();
  } catch (_error) {
    return false;
  }
}

async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return null;
  try {
    return await navigator.serviceWorker.register('/sw.js?v=20260528-01');
  } catch (_error) {
    return null;
  }
}

async function requestBackgroundSync() {
  if (!('serviceWorker' in navigator)) return;
  const reg = await navigator.serviceWorker.ready.catch(() => null);
  if (!reg || !('sync' in reg)) return;
  try {
    await reg.sync.register('field-capture-drain');
  } catch (_error) {
    // Foreground drain remains the fallback.
  }
}

recordVoiceButton.addEventListener('click', startVoiceRecording);
recordVoiceButton.addEventListener('touchend', startVoiceRecording);
stopVoiceButton.addEventListener('click', stopVoiceRecording);
clearVoiceButton.addEventListener('click', () => {
  if (state.audio && !window.confirm('Discard this recording?')) {
    return;
  }
  clearAudio();
});
submitButton.addEventListener('click', saveMemo);
noteInput.addEventListener('input', clearMessages);
personalNoteInput.addEventListener('input', clearMessages);
siteInput.addEventListener('change', rerenderEmployeesForCurrentSite);
employeeInput.addEventListener('change', captureEmployeeSelection);
modeInputs.forEach((input) => input.addEventListener('change', updateModeVisibility));

if (!supportsAudioRecording()) voiceSupportMessage.hidden = false;
applySavedMode();
requestPersistentStorage();
refreshPendingCount()
  .then(() => drainQueue())
  .catch(() => {});
registerServiceWorker();
if (bootstrapToken()) {
  stashTokenForServiceWorker(token);
  loadPickers();
}
renderAudio();
window.addEventListener('online', () => {
  state.drainBackoffUntil = 0;
  drainQueue().catch(() => {});
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    drainQueue().catch(() => {});
  }
});
