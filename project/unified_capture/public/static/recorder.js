(function () {
  "use strict";

  function supportsAudioRecording() {
    return Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  }

  function preferredAudioMimeType() {
    const choices = ["audio/webm", "audio/mp4"];
    return choices.find((mimeType) => window.MediaRecorder?.isTypeSupported?.(mimeType)) || "";
  }

  function audioExtension(mimeType) {
    if (mimeType.includes("mp4") || mimeType.includes("m4a")) return "m4a";
    if (mimeType.includes("wav")) return "wav";
    return "webm";
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

  window.btqRecorder = {
    supportsAudioRecording,
    preferredAudioMimeType,
    audioExtension,
    microphoneErrorMessage,
  };
})();
