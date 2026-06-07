(function () {
  "use strict";

  function supportsAudioRecording() {
    return Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
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

  function localIsoTimestamp() {
    const date = new Date();
    const offsetMinutes = -date.getTimezoneOffset();
    const sign = offsetMinutes >= 0 ? "+" : "-";
    const absoluteOffset = Math.abs(offsetMinutes);
    const pad = (value) => String(value).padStart(2, "0");
    const timezone = `${sign}${pad(Math.floor(absoluteOffset / 60))}:${pad(absoluteOffset % 60)}`;
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}${timezone}`;
  }

  function randomSuffix(length) {
    const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
    const bytes = new Uint8Array(length);
    if (window.crypto?.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    } else {
      for (let i = 0; i < length; i += 1) {
        bytes[i] = Math.floor(Math.random() * 256);
      }
    }
    return Array.from(bytes, (byte) => alphabet[byte % alphabet.length]).join("");
  }

  function newCaptureId() {
    return `cap-tapedeck-${Date.now()}-${randomSuffix(8)}`;
  }

  function ensureCaptureId(form) {
    let input = form.querySelector('input[name="capture_id"]');
    if (!input) {
      input = document.createElement("input");
      input.type = "hidden";
      input.name = "capture_id";
      form.appendChild(input);
    }
    if (!input.value) {
      input.value = newCaptureId();
    }
    return input.value;
  }

  function relativeTime(isoString) {
    const timestamp = Date.parse(isoString || "");
    if (!Number.isFinite(timestamp)) {
      return "unknown time";
    }
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    if (seconds < 60) return "just now";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
    const days = Math.floor(hours / 24);
    return `${days} day${days === 1 ? "" : "s"} ago`;
  }

  // Safety-net stash. Recorder is form-bound, not SPA, so
  // we use the parent page's hidden inputs as the
  // serializable form. On next page load this stash, if
  // present, surfaces a "Retry pending capture" banner.
  async function stashFailedSubmit(form, formData, audioBlob, audioFilename, audioMimeType) {
    if (!window.fieldCaptureDb?.isSupported()) return;
    const fields = {};
    for (const [key, value] of formData.entries()) {
      if (key === "audio") continue;
      if (typeof value === "string") fields[key] = value;
    }
    const captureId = formData.get("capture_id") || `cap-tapedeck-${Date.now()}`;
    await window.fieldCaptureDb.putCapture({
      capture_id: captureId,
      status: "pending",
      attempts: 0,
      lastError: null,
      createdAt: new Date().toISOString(),
      lastTriedAt: null,
      metadata: { source: "ops_dashboard_tapedeck", action_url: form.action },
      fields,
      photos: [],
      audio: audioBlob ? { filename: audioFilename, mimeType: audioMimeType, blob: audioBlob, durationSeconds: 0 } : null,
    });
  }

  async function uploadOneSafetyNetCapture(record) {
    const form = new FormData();
    const fields = record.fields || {};
    for (const [key, value] of Object.entries(fields)) {
      form.append(key, value == null ? "" : String(value));
    }
    if (record.audio) {
      form.set("audio", record.audio.blob, record.audio.filename);
    }
    const response = await fetch(record.metadata?.action_url || "/", {
      method: "POST",
      body: form,
      redirect: "follow",
    });
    if (!response.ok && (response.status < 300 || response.status >= 400)) {
      throw new Error(`upload_failed_${response.status}`);
    }
    await window.fieldCaptureDb.updateCapture(record.capture_id, {
      status: "done",
      lastError: null,
      lastTriedAt: new Date().toISOString(),
    });
    await window.fieldCaptureDb.deleteCapture(record.capture_id);
  }

  function renderSafetyNetBanner(records) {
    const banner = document.getElementById("opsRecorderRetryBanner");
    if (!banner) return;
    const pending = records || [];
    if (!pending.length) {
      banner.hidden = true;
      banner.replaceChildren();
      return;
    }
    const oldest = pending.slice().sort((a, b) => String(a.createdAt || "").localeCompare(String(b.createdAt || "")))[0];
    const label = `${pending.length} pending voice capture${pending.length === 1 ? "" : "s"} from ${relativeTime(oldest.createdAt)}`;
    const text = document.createElement("span");
    text.textContent = `${label} — `;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "Retry";
    const discard = document.createElement("button");
    discard.type = "button";
    discard.textContent = "Discard";
    banner.replaceChildren(text, retry, document.createTextNode(" "), discard);
    banner.hidden = false;

    retry.addEventListener("click", async () => {
      retry.disabled = true;
      discard.disabled = true;
      for (const record of pending) {
        try {
          await window.fieldCaptureDb.updateCapture(record.capture_id, {
            status: "pending",
            attempts: Number(record.attempts || 0) + 1,
            lastTriedAt: new Date().toISOString(),
          });
          await uploadOneSafetyNetCapture(record);
        } catch (error) {
          await window.fieldCaptureDb.updateCapture(record.capture_id, {
            status: "pending",
            attempts: Number(record.attempts || 0) + 1,
            lastError: error?.message || "retry_failed",
            lastTriedAt: new Date().toISOString(),
          });
        }
      }
      drainSafetyNet().catch(() => {});
    });

    discard.addEventListener("click", async () => {
      if (!window.confirm("Discard pending voice capture?")) {
        return;
      }
      discard.disabled = true;
      retry.disabled = true;
      for (const record of pending) {
        await window.fieldCaptureDb.deleteCapture(record.capture_id);
      }
      drainSafetyNet().catch(() => {});
    });
  }

  async function drainSafetyNet() {
    if (!window.fieldCaptureDb?.isSupported()) return;
    const pending = await window.fieldCaptureDb.listByStatus("pending");
    const ours = pending.filter((r) => r.metadata?.source === "ops_dashboard_tapedeck");
    renderSafetyNetBanner(ours);
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

  function attachRecorder({
    form,
    recordButton,
    stopButton,
    clearButton,
    preview,
    statusEl,
    supportEl,
    hiddenSiteRedirect,
  }) {
    if (!form || !recordButton || !stopButton || !clearButton || !preview || !statusEl || !supportEl) {
      return;
    }

    let recorder = null;
    let audioChunks = [];
    let audioStartedAt = null;
    let audioElapsedBeforePause = 0;
    let audioResumedAt = null;
    let audio = null;
    let isStarting = false;
    let stopTimer = null;
    let isSubmitting = false;

    function setStatus(text, tone) {
      statusEl.textContent = text;
      if (tone) {
        statusEl.dataset.tone = tone;
      } else {
        delete statusEl.dataset.tone;
      }
    }

    if (!supportsAudioRecording()) {
      supportEl.hidden = false;
      recordButton.disabled = true;
      stopButton.disabled = true;
      clearButton.disabled = true;
      setStatus("Unavailable");
      return;
    }

    function renderAudio() {
      if (!supportsAudioRecording()) {
        supportEl.hidden = false;
        recordButton.disabled = true;
        stopButton.disabled = true;
        clearButton.disabled = true;
        setStatus("Unavailable");
        return;
      }
      const recState = recorder?.state || null;
      const isRecording = recState === "recording";
      const isPaused = recState === "paused";
      const isActive = isRecording || isPaused;
      supportEl.hidden = true;
      recordButton.disabled = isRecording || isStarting || isSubmitting;
      stopButton.disabled = !isRecording;
      clearButton.disabled = (!isActive && !audio) || isStarting || isSubmitting;
      const statusText = isStarting
        ? "Requesting microphone…"
        : isRecording
          ? "Recording…"
          : isPaused
            ? "Paused — press record to resume"
            : audio
              ? "Ready"
              : "Optional";
      if (isStarting || isRecording) {
        setStatus(statusText, "active");
      } else if (isPaused) {
        setStatus(statusText, "paused");
      } else {
        setStatus(statusText);
      }
      preview.hidden = !audio;
      if (audio && preview.src !== audio.url) {
        preview.src = audio.url;
      }
    }

    function clearAudio() {
      if (recorder) {
        recorder.stream.getTracks().forEach((track) => track.stop());
        recorder = null;
        audioChunks = [];
        audioElapsedBeforePause = 0;
        audioResumedAt = null;
        window.clearTimeout(stopTimer);
        stopTimer = null;
      }
      if (audio?.url) {
        URL.revokeObjectURL(audio.url);
      }
      audio = null;
      preview.removeAttribute("src");
      preview.load();
      renderAudio();
    }

    function finishRecording(stoppedRecorder) {
      window.clearTimeout(stopTimer);
      stopTimer = null;
      stoppedRecorder.stream.getTracks().forEach((track) => track.stop());
      const mimeType = stoppedRecorder.mimeType || audioChunks[0]?.type || "audio/webm";
      const blob = new Blob(audioChunks, { type: mimeType });
      const timestamp = localIsoTimestamp().replace(/[:.]/g, "-");
      audio = {
        blob,
        url: URL.createObjectURL(blob),
        filename: `voice-note-${timestamp}.${audioExtension(mimeType)}`,
        mimeType,
      };
      recorder = null;
      audioChunks = [];
      audioElapsedBeforePause = 0;
      audioResumedAt = null;
      setStatus("Voice note recorded");
      renderAudio();
    }

    async function startRecording() {
      // Resume a paused recording instead of starting a new one.
      if (recorder?.state === "paused") {
        recorder.resume();
        const remaining = Math.max(60_000, 600_000 - (audioElapsedBeforePause || 0));
        stopTimer = window.setTimeout(stopRecording, remaining);
        audioResumedAt = Date.now();
        setStatus("Recording (up to 10 min)…", "active");
        renderAudio();
        return;
      }
      if (isStarting || recorder?.state === "recording") {
        return;
      }
      if (!supportsAudioRecording()) {
        renderAudio();
        return;
      }
      let stream = null;
      try {
        isStarting = true;
        setStatus("Requesting microphone access…", "active");
        renderAudio();
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const mimeType = preferredAudioMimeType();
        const nextRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
        clearAudio();
        audioChunks = [];
        audioStartedAt = Date.now();
        audioElapsedBeforePause = 0;
        audioResumedAt = null;
        nextRecorder.addEventListener("dataavailable", (event) => {
          if (event.data.size > 0) {
            audioChunks.push(event.data);
          }
        });
        nextRecorder.addEventListener("stop", () => finishRecording(nextRecorder));
        recorder = nextRecorder;
        nextRecorder.start();
        // 10 min cap, was 60s — see field_capture/public/app.js fix.
        stopTimer = window.setTimeout(stopRecording, 600_000);
        isStarting = false;
        setStatus("Recording (up to 10 min)…", "active");
        renderAudio();
      } catch (error) {
        stream?.getTracks().forEach((track) => track.stop());
        isStarting = false;
        setStatus(microphoneErrorMessage(error));
        renderAudio();
      }
    }

    function stopRecording() {
      if (recorder?.state === "recording") {
        const segmentElapsed = audioResumedAt
          ? Date.now() - audioResumedAt
          : Date.now() - (audioStartedAt || Date.now());
        audioElapsedBeforePause = (audioElapsedBeforePause || 0) + segmentElapsed;
        recorder.pause();
        window.clearTimeout(stopTimer);
        stopTimer = null;
        setStatus("Paused — press record to resume", "paused");
        renderAudio();
      }
    }

    function finishActiveForSubmit() {
      if (recorder?.state !== "recording" && recorder?.state !== "paused") {
        return Promise.resolve();
      }
      const activeRecorder = recorder;
      setStatus("Finishing voice note…", "active");
      renderAudio();
      return new Promise((resolve, reject) => {
        const timeout = window.setTimeout(() => {
          cleanup();
          reject(new Error("Could not finish voice recording before submit."));
        }, 5000);
        const cleanup = () => {
          window.clearTimeout(timeout);
          activeRecorder.removeEventListener("stop", handleStop);
          activeRecorder.removeEventListener("error", handleError);
        };
        const handleStop = () => {
          cleanup();
          resolve();
        };
        const handleError = () => {
          cleanup();
          reject(new Error("Could not finish voice recording before submit."));
        };
        activeRecorder.addEventListener("stop", handleStop);
        activeRecorder.addEventListener("error", handleError);
        activeRecorder.stop();
      });
    }

    function handleRecordEvent(event) {
      event.preventDefault();
      startRecording();
    }

    recordButton.addEventListener("click", handleRecordEvent);
    recordButton.addEventListener("touchend", handleRecordEvent);
    stopButton.addEventListener("click", stopRecording);
    clearButton.addEventListener("click", () => {
      if (audio && !window.confirm("Discard this recording?")) {
        return;
      }
      clearAudio();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      let formData = null;
      let audioBlob = null;
      let audioFilename = "";
      let audioMimeType = "";
      try {
        isSubmitting = true;
        await finishActiveForSubmit();
        ensureCaptureId(form);
        formData = new FormData(form);
        if (audio) {
          audioBlob = audio.blob;
          audioFilename = audio.filename;
          audioMimeType = audio.mimeType;
          formData.set("audio", new File([audio.blob], audio.filename, { type: audio.mimeType }));
        }
        const response = await fetch(form.action, { method: "POST", body: formData, redirect: "follow" });
        const responseUrl = new URL(response.url || form.action, window.location.href);
        const redirectedToError = responseUrl.searchParams.has("error");
        if ((response.ok || (response.status >= 300 && response.status < 400)) && !redirectedToError) {
          window.location.assign(response.url || hiddenSiteRedirect?.successUrl || "/");
          return;
        }
        try {
          await stashFailedSubmit(form, formData, audioBlob, audioFilename, audioMimeType);
        } catch (_stashError) {}
        drainSafetyNet().catch(() => {});
        setStatus("Submit failed — try again");
      } catch (_error) {
        if (formData) {
          try {
            await stashFailedSubmit(form, formData, audioBlob, audioFilename, audioMimeType);
          } catch (_stashError) {}
          drainSafetyNet().catch(() => {});
        }
        setStatus("Submit failed — try again");
      } finally {
        isSubmitting = false;
        renderAudio();
      }
    });

    renderAudio();
  }

  window.attachRecorder = attachRecorder;
  window.opsDashboardRecorderSafetyNet = { stashFailedSubmit, drainSafetyNet };
  document.addEventListener("DOMContentLoaded", () => {
    drainSafetyNet().catch(() => {});
  });
})();
