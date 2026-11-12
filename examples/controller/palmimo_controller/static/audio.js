// Homepage speaker volume and mic gain sliders, plus the mic input meter.
// Talks to /api/audio; a change applies to the running AI mode at once. The
// meter is fed by app.js's "palmimo:mic-level" event. Vanilla JS, like app.js.

(function () {
  "use strict";

  const speaker = document.getElementById("speaker-volume");
  const mic = document.getElementById("mic-gain");
  if (!speaker || !mic) return;

  const speakerValue = document.getElementById("speaker-volume-value");
  const micValue = document.getElementById("mic-gain-value");
  const status = document.getElementById("audio-status");
  const loadError = document.getElementById("audio-load-error");
  const meter = document.getElementById("mic-meter");
  const meterFill = document.getElementById("mic-meter-fill");
  const meterHint = document.getElementById("mic-meter-hint");

  // Wait this long after the slider stops moving before saving, so a drag
  // sends one request instead of one per step.
  const SAVE_DELAY_MS = 150;
  // The server sends levels 10 times a second while an AI mode has the mic
  // open; this long without one means the mic closed.
  const METER_IDLE_MS = 1000;

  let saveTimer = null;
  let idleTimer = null;

  function showValues() {
    speakerValue.textContent = speaker.value + "%";
    micValue.textContent = mic.value + "%";
  }

  function setStatus(text, isError) {
    status.textContent = text;
    status.classList.toggle("is-error", isError);
  }

  function apply(entry) {
    speaker.min = entry.speaker_volume_range[0];
    speaker.max = entry.speaker_volume_range[1];
    mic.min = entry.mic_gain_range[0];
    mic.max = entry.mic_gain_range[1];
    speaker.value = entry.speaker_volume;
    mic.value = entry.mic_gain;
    showValues();
  }

  function save() {
    setStatus("…", false);
    fetch("/api/audio", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speaker_volume: Number(speaker.value), mic_gain: Number(mic.value) }),
    })
      .then(function (response) {
        return response.json().then(function (payload) {
          if (!response.ok) {
            throw new Error(typeof payload.detail === "string" ? payload.detail : "HTTP " + response.status);
          }
          return payload;
        });
      })
      .then(function (entry) {
        // A newer drag may already be waiting to save; don't snap the slider back under it.
        if (saveTimer === null) apply(entry);
        setStatus("保存しました", false);
      })
      .catch(function (err) {
        setStatus("エラー：" + err.message, true);
      });
  }

  function onInput() {
    showValues();
    if (saveTimer !== null) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      saveTimer = null;
      save();
    }, SAVE_DELAY_MS);
  }

  function setMeter(level) {
    const percent = Math.round(Math.max(0, Math.min(1, level)) * 100);
    meterFill.style.width = percent + "%";
    meter.setAttribute("aria-valuenow", String(percent));
  }

  function goIdle() {
    idleTimer = null;
    setMeter(0);
    meter.classList.add("is-idle");
    meterHint.hidden = false;
  }

  window.addEventListener("palmimo:mic-level", function (event) {
    const level = Number(event.detail);
    if (!Number.isFinite(level)) return;
    meter.classList.remove("is-idle");
    meterHint.hidden = true;
    setMeter(level);
    if (idleTimer !== null) clearTimeout(idleTimer);
    idleTimer = setTimeout(goIdle, METER_IDLE_MS);
  });

  speaker.addEventListener("input", onInput);
  mic.addEventListener("input", onInput);

  fetch("/api/audio")
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(function (entry) {
      apply(entry);
      speaker.disabled = false;
      mic.disabled = false;
    })
    .catch(function () {
      if (loadError) loadError.hidden = false;
    });
})();
