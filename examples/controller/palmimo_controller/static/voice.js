// Homepage picker for the voice AI modes speak in. Talks to /api/voice; a
// change applies the next time an AI mode starts. Vanilla JS, like app.js.

(function () {
  "use strict";

  const select = document.getElementById("voice-select");
  const status = document.getElementById("voice-status");
  const loadError = document.getElementById("voice-load-error");
  if (!select) return;

  function setStatus(text, isError) {
    status.textContent = text;
    status.classList.toggle("is-error", isError);
  }

  // Built with textContent/.value only, never HTML strings, like prompts.js.
  function render(entry) {
    const options = entry.voices.map(function (voice) {
      const option = document.createElement("option");
      option.value = voice;
      option.textContent = voice === entry.default_voice ? voice + "（デフォルト）" : voice;
      return option;
    });
    select.replaceChildren.apply(select, options);
    select.value = entry.voice;
    select.disabled = false;
  }

  select.addEventListener("change", function () {
    select.disabled = true;
    setStatus("…", false);
    fetch("/api/voice", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice: select.value }),
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
        setStatus("保存しました", false);
      })
      .catch(function (err) {
        select.disabled = false;
        setStatus("エラー：" + err.message, true);
      });
  });

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
