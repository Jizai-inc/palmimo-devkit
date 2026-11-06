// Homepage editor for the AI mode prompts. Talks to /api/prompts; a saved
// prompt applies the next time that mode starts. Vanilla JS, no libraries,
// like app.js.

(function () {
  "use strict";

  const list = document.getElementById("prompt-list");
  const loadError = document.getElementById("prompt-load-error");
  if (!list) return;

  const HEADERS = { "Content-Type": "application/json" };

  function request(method, modeId, body) {
    return fetch("/api/prompts/" + encodeURIComponent(modeId), {
      method: method,
      headers: HEADERS,
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok) {
          throw new Error(typeof payload.detail === "string" ? payload.detail : "HTTP " + response.status);
        }
        return payload;
      });
    });
  }

  // Built with textContent/.value only, never HTML strings: prompt text is free
  // text anyone on the LAN can write, so none of it may become markup.
  function renderCard(entry) {
    const card = document.createElement("details");
    card.className = "prompt-card";

    const summary = document.createElement("summary");
    const label = document.createElement("span");
    label.className = "prompt-label";
    label.textContent = entry.label;
    const badge = document.createElement("span");
    badge.className = "prompt-badge";
    summary.append(label, badge);

    const textarea = document.createElement("textarea");
    textarea.className = "prompt-text";
    textarea.rows = 12;
    textarea.spellcheck = false;
    textarea.setAttribute("aria-label", entry.label + " のプロンプト");

    const actions = document.createElement("div");
    actions.className = "prompt-actions";
    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.className = "prompt-save";
    saveButton.textContent = "保存";
    const resetButton = document.createElement("button");
    resetButton.type = "button";
    resetButton.className = "prompt-reset";
    resetButton.textContent = "デフォルトに戻す";
    const status = document.createElement("span");
    status.className = "prompt-status";
    status.setAttribute("role", "status");
    actions.append(saveButton, resetButton, status);

    card.append(summary, textarea, actions);

    function show(current) {
      textarea.value = current.text;
      badge.textContent = current.custom ? "カスタム" : "デフォルト";
      badge.classList.toggle("is-custom", current.custom);
      resetButton.disabled = !current.custom;
    }

    function setStatus(text, isError) {
      status.textContent = text;
      status.classList.toggle("is-error", isError);
    }

    function run(promise, doneText) {
      saveButton.disabled = true;
      resetButton.disabled = true;
      setStatus("…", false);
      promise
        .then(function (updated) {
          show(updated);
          setStatus(doneText, false);
        })
        .catch(function (err) {
          setStatus("エラー：" + err.message, true);
          resetButton.disabled = false;
        })
        .finally(function () {
          saveButton.disabled = false;
        });
    }

    saveButton.addEventListener("click", function () {
      run(request("PUT", entry.id, { text: textarea.value }), "保存しました");
    });
    resetButton.addEventListener("click", function () {
      if (!window.confirm(entry.label + " のプロンプトをデフォルトに戻しますか？")) return;
      run(request("DELETE", entry.id), "デフォルトに戻しました");
    });

    show(entry);
    return card;
  }

  fetch("/api/prompts")
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(function (entries) {
      list.replaceChildren.apply(list, entries.map(renderCard));
    })
    .catch(function () {
      if (loadError) loadError.hidden = false;
    });
})();
