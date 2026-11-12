// Homepage picker for the voice AI modes speak in. Talks to /api/voice; a
// change applies the next time an AI mode starts. Each voice's play button
// asks /api/voice/preview to play its sample on the robot's speaker.
// Vanilla JS, like app.js.

(function () {
  "use strict";

  const list = document.getElementById("voice-list");
  const status = document.getElementById("voice-status");
  const loadError = document.getElementById("voice-load-error");
  if (!list) return;

  const SVG_NS = "http://www.w3.org/2000/svg";
  let busy = false;
  let playingButton = null;
  let playingTimer = null;

  function setStatus(text, isError) {
    status.textContent = text;
    status.classList.toggle("is-error", isError);
  }

  function checkIcon() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "choice-check");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", "20 6 9 17 4 12");
    svg.appendChild(line);
    return svg;
  }

  function playIcon() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const triangle = document.createElementNS(SVG_NS, "polygon");
    triangle.setAttribute("points", "7 4 20 12 7 20 7 4");
    svg.appendChild(triangle);
    return svg;
  }

  function markPlaying(button, seconds) {
    clearTimeout(playingTimer);
    if (playingButton) playingButton.classList.remove("is-playing");
    playingButton = button;
    if (!button) return;
    button.classList.add("is-playing");
    playingTimer = setTimeout(function () {
      button.classList.remove("is-playing");
      if (playingButton === button) playingButton = null;
    }, seconds * 1000);
  }

  // What the pilot reads for the preview refusals the server can give.
  const PREVIEW_ERRORS = {
    409: "AIモード中は試聴できません",
    503: "この声のサンプルがありません",
  };

  function preview(voice, button) {
    button.disabled = true;
    setStatus("", false);
    fetch("/api/voice/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice: voice }),
    })
      .then(function (response) {
        return response.json().then(function (payload) {
          if (!response.ok) {
            throw new Error(PREVIEW_ERRORS[response.status] || "再生できませんでした");
          }
          return payload;
        });
      })
      .then(function (payload) {
        markPlaying(button, payload.seconds);
      })
      .catch(function (err) {
        markPlaying(null, 0);
        setStatus("エラー：" + err.message, true);
      })
      .finally(function () {
        button.disabled = false;
      });
  }

  // Built with textContent and DOM nodes only, never HTML strings, like prompts.js.
  function render(entry) {
    const rows = entry.voices.map(function (voice) {
      const selected = voice === entry.voice;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "choice-option";
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", selected ? "true" : "false");
      button.classList.toggle("is-selected", selected);

      const name = document.createElement("span");
      name.textContent = voice;
      button.appendChild(name);
      if (voice === entry.default_voice) {
        const tag = document.createElement("span");
        tag.className = "choice-default";
        tag.textContent = "デフォルト";
        button.appendChild(tag);
      }
      if (selected) button.appendChild(checkIcon());

      button.addEventListener("click", function () {
        if (busy || selected) return;
        save(voice);
      });

      const play = document.createElement("button");
      play.type = "button";
      play.className = "voice-play";
      play.setAttribute("aria-label", voice + " を試聴");
      play.title = "試聴";
      play.appendChild(playIcon());
      play.addEventListener("click", function () {
        preview(voice, play);
      });

      const row = document.createElement("div");
      row.className = "voice-row";
      row.classList.toggle("is-selected", selected);
      row.append(button, play);
      return row;
    });
    markPlaying(null, 0);
    list.replaceChildren.apply(list, rows);
  }

  function save(voice) {
    busy = true;
    list.classList.add("is-busy");
    setStatus("", false);
    fetch("/api/voice", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice: voice }),
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
        render(entry);
      })
      .catch(function (err) {
        setStatus("エラー：" + err.message, true);
      })
      .finally(function () {
        busy = false;
        list.classList.remove("is-busy");
      });
  }

  fetch("/api/voice")
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(render)
    .catch(function () {
      if (loadError) loadError.hidden = false;
    });
})();
