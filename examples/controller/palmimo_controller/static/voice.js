// Homepage picker for the voice AI modes speak in. Talks to /api/voice; a
// change applies the next time an AI mode starts. Vanilla JS, like app.js.

(function () {
  "use strict";

  const list = document.getElementById("voice-list");
  const status = document.getElementById("voice-status");
  const loadError = document.getElementById("voice-load-error");
  if (!list) return;

  const SVG_NS = "http://www.w3.org/2000/svg";
  let busy = false;

  function setStatus(text, isError) {
    status.textContent = text;
    status.classList.toggle("is-error", isError);
  }

  function checkIcon() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "voice-check");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", "20 6 9 17 4 12");
    svg.appendChild(line);
    return svg;
  }

  // Built with textContent and DOM nodes only, never HTML strings, like prompts.js.
  function render(entry) {
    const buttons = entry.voices.map(function (voice) {
      const selected = voice === entry.voice;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "voice-option";
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", selected ? "true" : "false");
      button.classList.toggle("is-selected", selected);

      const name = document.createElement("span");
      name.textContent = voice;
      button.appendChild(name);
      if (voice === entry.default_voice) {
        const tag = document.createElement("span");
        tag.className = "voice-default";
        tag.textContent = "デフォルト";
        button.appendChild(tag);
      }
      if (selected) button.appendChild(checkIcon());

      button.addEventListener("click", function () {
        if (busy || selected) return;
        save(voice);
      });
      return button;
    });
    list.replaceChildren.apply(list, buttons);
  }

  function save(voice) {
    busy = true;
    list.classList.add("is-busy");
    setStatus("…", false);
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
        setStatus(entry.voice + " に変更しました", false);
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
