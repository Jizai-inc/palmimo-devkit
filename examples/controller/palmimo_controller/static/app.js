// Shared frontend for viewer.html and pilot.html. No CDN, no vendored
// libraries -- this ships on an offline machine, so everything here is
// hand-rolled vanilla JS/CSS/HTML.

(function () {
  "use strict";

  // How often the pilot page sends a control frame. This also doubles as
  // the server's deadman heartbeat (see palmimo_controller.session), so it is
  // an interval, not an event-driven send -- releasing every key still has
  // to keep the socket alive with an explicit "nothing pressed" frame.
  const SEND_INTERVAL_MS = 100;
  // Keyboard neck increment rate, applied per SEND_INTERVAL_MS tick while a
  // cursor key is held.
  const NECK_KEY_RATE_PER_SEC = 1.5;

  const isPilotPage = document.body.dataset.role === "pilot";

  const statusBar = document.getElementById("status-bar");
  const disconnectedBanner = document.getElementById("disconnected-banner");
  const viewOnlyBanner = document.getElementById("view-only-banner");
  const servoBanner = document.getElementById("servo-banner");
  const servoFaultBanner = document.getElementById("servo-fault-banner");
  const controlsEl = document.getElementById("controls");
  const videoLayer = document.getElementById("video-layer");
  const pilotLink = document.getElementById("pilot-link");

  if (videoLayer) {
    videoLayer.src = "/video.mjpeg";
  }

  // A long press anywhere is a held control, never a request for the
  // browser's context menu (Android surfaces an image save menu otherwise).
  // Text fields (the homepage prompt editor) keep it, for copy and paste.
  document.addEventListener("contextmenu", function (event) {
    if (event.target.closest && event.target.closest("textarea, input")) return;
    event.preventDefault();
  });

  function renderStatusBar(status) {
    if (!statusBar) return;
    // Built with textContent, never innerHTML: `motion` and the other fields
    // arrive over an unauthenticated LAN WebSocket, so nothing from that
    // payload may be interpreted as markup.
    statusBar.replaceChildren();
    // One chip per fact: a muted label and its value. A health chip
    // (`ok` true/false) is colored and carries a dot, so a fault stands out.
    const addChip = function (label, value, ok) {
      const chip = document.createElement("span");
      chip.className = "status-chip";
      if (ok !== undefined) {
        chip.classList.add(ok ? "status-ok" : "status-bad");
        const dot = document.createElement("span");
        dot.className = "status-dot";
        dot.setAttribute("aria-hidden", "true");
        chip.appendChild(dot);
      }
      if (label) {
        const name = document.createElement("span");
        name.className = "status-label";
        name.textContent = label;
        chip.appendChild(name);
      }
      chip.appendChild(document.createTextNode(value));
      statusBar.appendChild(chip);
    };
    addChip("操縦者", status.pilot_present ? "操作中" : "なし");
    addChip("モード", status.mode === "teleop" ? "手動操作" : "AI：" + (status.ai_mode || status.mode));
    addChip("動作", status.motion || "待機中");
    addChip("", status.loop_fps.toFixed(1) + " fps");
    addChip("サーボバス", status.servo_attached ? "接続" : "未接続", status.servo_attached);
    addChip("カメラ", status.camera_ok ? "正常" : "使用不可", status.camera_ok);
    addChip("サーボ", status.robot_ok === false ? "異常" : "正常", status.robot_ok !== false);
  }

  // The pilot page dropped its own #status-bar, so a compute-only or
  // faulting servo bus needs its own always-visible banners there instead
  // of being buried in a log line.
  function renderServoBanners(status) {
    if (servoBanner) servoBanner.hidden = status.servo_attached !== false;
    if (servoFaultBanner) servoFaultBanner.hidden = status.robot_ok !== false;
  }

  // Enables/disables the viewer's "Start piloting" link based on whether a
  // pilot currently holds the control slot -- there is nothing useful for it
  // to do while someone else is driving.
  function renderPilotLinkAvailability(status) {
    if (!pilotLink) return;
    const disabled = status.pilot_present;
    pilotLink.setAttribute("aria-disabled", disabled ? "true" : "false");
    if (disabled) {
      pilotLink.addEventListener("click", preventDefault);
    } else {
      pilotLink.removeEventListener("click", preventDefault);
    }
    const caption = document.getElementById("viewer-caption");
    if (caption) {
      caption.textContent = disabled
        ? "ほかの人が操作中です。終わると操作できるようになります。"
        : "見学中です。操作するには「操作を開始」を押してください。";
    }
  }

  function preventDefault(event) {
    event.preventDefault();
  }

  function showDisconnected() {
    if (disconnectedBanner) {
      disconnectedBanner.hidden = false;
    }
  }

  const reloadButton = document.getElementById("reload-button");
  if (reloadButton) {
    reloadButton.addEventListener("click", function () {
      window.location.reload();
    });
  }

  // Falls back to view-only when this connection's own pilot request was
  // denied (someone else already holds the slot): hides the drive controls
  // (the server drops input from a non-pilot anyway), and surfaces why,
  // rather than leaving dead buttons on screen.
  function denyPilotControl() {
    if (controlsEl) controlsEl.hidden = true;
    if (viewOnlyBanner) viewOnlyBanner.hidden = false;
  }

  // Set by the pilot-only section below to render the mode bar.
  let onStatus = null;

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(protocol + "//" + window.location.host + "/api/control");

  socket.addEventListener("open", function () {
    socket.send(JSON.stringify({ role: isPilotPage ? "pilot" : "viewer" }));
  });

  socket.addEventListener("message", function (event) {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if ("pilot_present" in payload) {
      renderStatusBar(payload);
      renderPilotLinkAvailability(payload);
      renderServoBanners(payload);
      if (onStatus) onStatus(payload);
    }
    if (payload.granted === false) {
      denyPilotControl();
    }
  });

  socket.addEventListener("close", showDisconnected);
  socket.addEventListener("error", showDisconnected);

  const fullscreenButton = document.getElementById("fullscreen-button");
  if (fullscreenButton) {
    if (!document.documentElement.requestFullscreen) {
      // iPhone Safari has no Fullscreen API for non-video elements; adding
      // the page to the home screen (apple-mobile-web-app-capable) is the
      // fullscreen path there instead.
      fullscreenButton.hidden = true;
    } else {
      fullscreenButton.addEventListener("click", function () {
        if (document.fullscreenElement) {
          document.exitFullscreen();
        } else {
          document.documentElement.requestFullscreen().catch(function () {
            // Denied (e.g. not a user gesture in this browser's eyes) --
            // the page simply stays windowed.
          });
        }
      });
    }
  }

  if (!isPilotPage) {
    return;
  }

  // ---- Pilot-only input handling below ----
  //
  // Touch/mouse and keyboard are two independent input sources, each with
  // its own held-state, merged only at send time. `resolveMove`/
  // `resolveRotate`/`keyMoveSet`/`keyRotateSet` are pure functions of those
  // sets (no DOM access) precisely so the merge is easy to read and check by
  // hand: keeping one shared "pressed" set and having each source add to and
  // delete from it independently is what silently broke touch input before
  // -- every tick, the keyboard side deleted whatever direction it wasn't
  // holding, including one touch had just added.

  const touchHeld = new Set(); // "forward" | "backward" | "strafe_left" | "strafe_right"
  const touchRotateHeld = new Set(); // "left" | "right"
  const heldKeys = new Set(); // raw lowercased KeyboardEvent.key values

  let neckPitch = 0;
  let neckYaw = 0;
  let seq = 0;

  function keyMoveSet(keys) {
    const set = new Set();
    if (keys.has("w")) set.add("forward");
    if (keys.has("s")) set.add("backward");
    if (keys.has("a")) set.add("strafe_left");
    if (keys.has("d")) set.add("strafe_right");
    return set;
  }

  function keyRotateSet(keys) {
    const set = new Set();
    if (keys.has("q")) set.add("left");
    if (keys.has("e")) set.add("right");
    return set;
  }

  function resolveMove(touch, key) {
    if (touch.has("forward") || key.has("forward")) return "forward";
    if (touch.has("backward") || key.has("backward")) return "backward";
    if (touch.has("strafe_left") || key.has("strafe_left")) return "strafe_left";
    if (touch.has("strafe_right") || key.has("strafe_right")) return "strafe_right";
    return null;
  }

  function resolveRotate(touch, key) {
    const left = touch.has("left") || key.has("left");
    const right = touch.has("right") || key.has("right");
    // Both directions held cancels out, same as opposing move buttons would.
    if (left && right) return 0;
    if (left) return -1;
    if (right) return 1;
    return 0;
  }

  // A gesture is a one-off `{"play": ...}` message, independent of the
  // per-100ms control frame -- sending it more than once for the same
  // physical tap (e.g. both a `click` and a synthesized one) would toggle
  // it back off, so each button/key is wired to send exactly one `play` per
  // press.
  function sendPlay(name) {
    if (socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ play: name }));
  }

  function bindHoldButton(id, onDown, onUp) {
    const el = document.getElementById(id);
    if (!el) return;
    const start = function (event) {
      event.preventDefault();
      el.classList.add("pressed");
      onDown();
    };
    const end = function (event) {
      event.preventDefault();
      el.classList.remove("pressed");
      onUp();
    };
    el.addEventListener("touchstart", start);
    el.addEventListener("touchend", end);
    el.addEventListener("touchcancel", end);
    el.addEventListener("mousedown", start);
    el.addEventListener("mouseup", end);
    el.addEventListener("mouseleave", end);
  }

  bindHoldButton(
    "dpad-forward",
    function () {
      touchHeld.add("forward");
    },
    function () {
      touchHeld.delete("forward");
    }
  );
  bindHoldButton(
    "dpad-backward",
    function () {
      touchHeld.add("backward");
    },
    function () {
      touchHeld.delete("backward");
    }
  );
  bindHoldButton(
    "dpad-left",
    function () {
      touchHeld.add("strafe_left");
    },
    function () {
      touchHeld.delete("strafe_left");
    }
  );
  bindHoldButton(
    "dpad-right",
    function () {
      touchHeld.add("strafe_right");
    },
    function () {
      touchHeld.delete("strafe_right");
    }
  );
  bindHoldButton(
    "rotate-left",
    function () {
      touchRotateHeld.add("left");
    },
    function () {
      touchRotateHeld.delete("left");
    }
  );
  bindHoldButton(
    "rotate-right",
    function () {
      touchRotateHeld.add("right");
    },
    function () {
      touchRotateHeld.delete("right");
    }
  );
  function bindPlayButton(id, name) {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", function (event) {
      event.preventDefault();
      sendPlay(name);
    });
  }

  // Button id and keyboard shortcut for each gesture, in on-screen order.
  const GESTURES = [
    { id: "gesture-wave", name: "wave", key: "1" },
    { id: "gesture-dance", name: "dance", key: "2" },
    { id: "gesture-nod", name: "nod", key: "3" },
    { id: "gesture-clap", name: "clap", key: "4" },
    { id: "gesture-pushup", name: "pushup", key: "5" },
    { id: "gesture-body-tilt", name: "body_tilt", key: "6" },
    { id: "gesture-bow", name: "bow", key: "7" },
  ];
  GESTURES.forEach(function (gesture) {
    bindPlayButton(gesture.id, gesture.name);
  });

  // ---- AI mode switching ----
  //
  // The server owns the mode; this page only asks for a switch and renders
  // whatever the next status broadcast says. `switchPending` blocks a second
  // tap until that broadcast arrives, and the server ignores one anyway.
  // A tapped AI mode shows its starting state at once rather than after the
  // next broadcast, so the pilot sees the tap land.

  const modeIndicator = document.getElementById("mode-indicator");
  const aiButton = document.getElementById("ai-button");
  const teleopButton = document.getElementById("teleop-button");
  const modePicker = document.getElementById("mode-picker");
  const modeList = document.getElementById("mode-list");
  const modeCancel = document.getElementById("mode-cancel");
  const noticeBanner = document.getElementById("notice-banner");
  const modeLabels = {};
  let aiAvailable = false;
  let currentMode = "teleop";
  let switchPending = false;
  let pendingTimer = null;
  let liveFlashTimer = null;
  let lastStatus = null;

  // A switch that fails at once (no key, no prompt) never changes the
  // broadcast mode, so the pending state also ends on its own.
  const SWITCH_PENDING_MS = 2500;
  const LIVE_FLASH_MS = 1200;

  function setPending(pending) {
    switchPending = pending;
    clearTimeout(pendingTimer);
    if (pending) {
      pendingTimer = setTimeout(function () {
        switchPending = false;
        if (lastStatus) onStatus(lastStatus);
      }, SWITCH_PENDING_MS);
    }
  }

  function sendAi(message) {
    if (socket.readyState !== WebSocket.OPEN) return;
    setPending(true);
    socket.send(JSON.stringify(message));
  }

  function renderMode(mode, name) {
    const inTeleop = mode === "teleop";
    const starting = mode === "ai_starting";
    if (inTeleop) {
      modeIndicator.textContent = "手動操作";
    } else if (starting) {
      modeIndicator.textContent = "AI起動中：" + name;
    } else {
      modeIndicator.textContent = "AI：" + name;
    }
    document.body.classList.toggle("ai-active", !inTeleop);
    document.body.classList.toggle("ai-starting", starting);
    aiButton.hidden = !inTeleop;
    teleopButton.hidden = inTeleop;
    if (!inTeleop) modePicker.hidden = true;
  }

  function flashLive() {
    document.body.classList.add("ai-live-flash");
    clearTimeout(liveFlashTimer);
    liveFlashTimer = setTimeout(function () {
      document.body.classList.remove("ai-live-flash");
    }, LIVE_FLASH_MS);
  }

  function renderModeList(modes) {
    modeList.replaceChildren();
    modes.forEach(function (mode) {
      modeLabels[mode.id] = mode.label;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = mode.label;
      button.addEventListener("click", function () {
        modePicker.hidden = true;
        if (switchPending || currentMode !== "teleop") return;
        sendAi({ ai: "start", mode: mode.id });
        if (switchPending) {
          noticeBanner.hidden = true;
          renderMode("ai_starting", mode.label);
        }
      });
      modeList.appendChild(button);
    });
  }

  fetch("/api/modes")
    .then(function (response) {
      return response.json();
    })
    .then(function (body) {
      aiAvailable = body.ai_available;
      renderModeList(body.modes);
    })
    .catch(function () {
      // Mode list unavailable: the AI button stays inert, teleop is unaffected.
    });

  aiButton.addEventListener("click", function () {
    if (switchPending || currentMode !== "teleop") return;
    if (!aiAvailable) {
      noticeBanner.textContent = "AIは使用できません：ポータルでAPIキーを設定してください";
      noticeBanner.hidden = false;
      return;
    }
    modePicker.hidden = false;
  });
  modeCancel.addEventListener("click", function () {
    modePicker.hidden = true;
  });
  teleopButton.addEventListener("click", function () {
    sendAi({ ai: "stop" });
  });

  onStatus = function (status) {
    lastStatus = status;
    const previousMode = currentMode;
    if (status.mode !== currentMode) setPending(false);
    currentMode = status.mode;
    aiAvailable = status.ai_available;
    // Keep the optimistic starting state until the server's mode moves.
    if (switchPending) return;
    const inTeleop = status.mode === "teleop";
    renderMode(status.mode, modeLabels[status.ai_mode] || status.ai_mode || "");
    if (status.mode === "ai" && previousMode !== "ai") flashLive();
    if (status.notice && inTeleop) {
      noticeBanner.textContent = status.notice;
      noticeBanner.hidden = false;
    } else if (!inTeleop) {
      noticeBanner.hidden = true;
    }
  };

  // Keyboard: WASD move, Q/E rotate, cursor keys increment the neck, c
  // centers it, 1-7 play a gesture. `event.repeat` guards the gesture keys
  // so holding the key down doesn't fire `play` (and toggle the gesture
  // back off) on every OS-level auto-repeat keydown.
  window.addEventListener("keydown", function (event) {
    heldKeys.add(event.key.toLowerCase());
    if (event.key === "c" || event.key === "C") {
      neckPitch = 0;
      neckYaw = 0;
    }
    if (!event.repeat) {
      GESTURES.forEach(function (gesture) {
        if (event.key === gesture.key) sendPlay(gesture.name);
      });
    }
  });
  window.addEventListener("keyup", function (event) {
    heldKeys.delete(event.key.toLowerCase());
  });

  function applyNeckKeys() {
    const step = NECK_KEY_RATE_PER_SEC * (SEND_INTERVAL_MS / 1000);
    if (heldKeys.has("arrowup")) neckPitch -= step;
    if (heldKeys.has("arrowdown")) neckPitch += step;
    if (heldKeys.has("arrowleft")) neckYaw -= step;
    if (heldKeys.has("arrowright")) neckYaw += step;
    neckPitch = Math.max(-1, Math.min(1, neckPitch));
    neckYaw = Math.max(-1, Math.min(1, neckYaw));
  }

  // Neck stick: spring-loaded -- follows the touch/mouse while dragging and
  // snaps back to {0, 0} on release, like a real analog stick. `c` also
  // recenters it from the keyboard.
  const stick = document.getElementById("neck-stick");
  const handle = document.getElementById("neck-stick-handle");
  let dragging = false;

  function setHandlePosition(x, y) {
    const radius = stick.clientWidth / 2;
    handle.style.left = radius + x * radius + "px";
    handle.style.top = radius + y * radius + "px";
  }

  function stickPointFromEvent(event) {
    const rect = stick.getBoundingClientRect();
    const point = event.touches ? event.touches[0] : event;
    const radius = rect.width / 2;
    let x = (point.clientX - rect.left - radius) / radius;
    let y = (point.clientY - rect.top - radius) / radius;
    const magnitude = Math.hypot(x, y);
    if (magnitude > 1) {
      x /= magnitude;
      y /= magnitude;
    }
    return { x: x, y: y };
  }

  if (stick && handle) {
    const onMove = function (event) {
      if (!dragging) return;
      event.preventDefault();
      const point = stickPointFromEvent(event);
      neckYaw = point.x;
      neckPitch = point.y;
      setHandlePosition(point.x, point.y);
    };
    const onStart = function (event) {
      dragging = true;
      onMove(event);
    };
    const onEnd = function () {
      dragging = false;
      neckPitch = 0;
      neckYaw = 0;
      setHandlePosition(0, 0);
    };
    stick.addEventListener("touchstart", onStart);
    stick.addEventListener("touchmove", onMove);
    stick.addEventListener("touchend", onEnd);
    stick.addEventListener("touchcancel", onEnd);
    stick.addEventListener("mousedown", onStart);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onEnd);
  }

  function sendFrame() {
    if (socket.readyState !== WebSocket.OPEN) {
      return;
    }
    seq += 1;
    socket.send(
      JSON.stringify({
        move: resolveMove(touchHeld, keyMoveSet(heldKeys)),
        rotate: resolveRotate(touchRotateHeld, keyRotateSet(heldKeys)),
        neck: { pitch: neckPitch, yaw: neckYaw },
        seq: seq,
      })
    );
  }

  // Losing focus or backgrounding the tab must not leave a stale direction
  // -- or a stale neck target -- held server-side until the next 100ms tick
  // happens to notice, so this clears every input source (including the
  // neck stick, which otherwise keeps whatever off-center target it had
  // when focus was lost) and pushes the release immediately. A gesture
  // already playing is left alone: it is server-timed
  // (`GESTURE_PLAY_SECONDS`), not held client-side, so there is nothing
  // here to release.
  function clearAllInputsAndSendIdle() {
    touchHeld.clear();
    touchRotateHeld.clear();
    heldKeys.clear();
    dragging = false;
    neckPitch = 0;
    neckYaw = 0;
    if (stick && handle) setHandlePosition(0, 0);
    sendFrame();
  }

  window.addEventListener("blur", clearAllInputsAndSendIdle);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) clearAllInputsAndSendIdle();
  });

  setInterval(function () {
    applyNeckKeys();
    sendFrame();
  }, SEND_INTERVAL_MS);
})();
