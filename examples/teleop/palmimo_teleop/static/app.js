// Shared frontend for viewer.html and pilot.html. No CDN, no vendored
// libraries -- this ships on an offline machine, so everything here is
// hand-rolled vanilla JS/CSS/HTML.

(function () {
  "use strict";

  // How often the pilot page sends a control frame. This also doubles as
  // the server's deadman heartbeat (see palmimo_teleop.session), so it is
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
  document.addEventListener("contextmenu", function (event) {
    event.preventDefault();
  });

  function renderStatusBar(status) {
    if (!statusBar) return;
    // Built with textContent, never innerHTML: `motion` and the other fields
    // arrive over an unauthenticated LAN WebSocket, so nothing from that
    // payload may be interpreted as markup.
    statusBar.replaceChildren();
    const addCell = function (text, className) {
      const span = document.createElement("span");
      span.textContent = text;
      if (className) span.className = className;
      statusBar.appendChild(span);
    };
    const okOrBad = function (ok) {
      return ok ? "status-ok" : "status-bad";
    };
    addCell(status.pilot_present ? "pilot: connected" : "pilot: none");
    addCell("motion: " + (status.motion || "idle"));
    addCell("fps: " + status.loop_fps.toFixed(1));
    addCell("bus: " + (status.servo_attached ? "attached" : "compute-only"), okOrBad(status.servo_attached));
    addCell("camera: " + (status.camera_ok ? "ok" : "unavailable"), okOrBad(status.camera_ok));
    addCell("servo: " + (status.robot_ok === false ? "fault" : "ok"), okOrBad(status.robot_ok !== false));
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
        ? "Someone is piloting right now -- you can start once they leave."
        : "You are spectating. Press Start piloting to take control.";
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

  bindPlayButton("gesture-wave", "wave");
  bindPlayButton("gesture-dance", "dance");

  // Keyboard: WASD move, Q/E rotate, cursor keys increment the neck, c
  // centers it, 1/2 play a gesture. `event.repeat` guards the gesture keys
  // so holding the key down doesn't fire `play` (and toggle the gesture
  // back off) on every OS-level auto-repeat keydown.
  window.addEventListener("keydown", function (event) {
    heldKeys.add(event.key.toLowerCase());
    if (event.key === "c" || event.key === "C") {
      neckPitch = 0;
      neckYaw = 0;
    }
    if (!event.repeat) {
      if (event.key === "1") sendPlay("wave");
      if (event.key === "2") sendPlay("dance");
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
  // held server-side until the next 100ms tick happens to notice -- clear
  // every input source and push the release immediately. A gesture already
  // playing is left alone: it is server-timed (`GESTURE_PLAY_SECONDS`), not
  // held client-side, so there is nothing here to release.
  function clearAllInputsAndSendIdle() {
    touchHeld.clear();
    touchRotateHeld.clear();
    heldKeys.clear();
    dragging = false;
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
