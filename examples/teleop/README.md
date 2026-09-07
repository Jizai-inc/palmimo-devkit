# palmimo_teleop

Web-based teleoperation for Palmimo: open a page in a phone or laptop browser
on the same LAN and drive the robot, no native client, no gamepad, no
authentication. No autonomy, no LLM -- direct manual control, same as the
robot's other teleop examples, just over HTTP/WebSocket instead of a local
input device.

## Running it

```bash
uv run palmimo-teleop
```

Defaults to a real servo bus (auto-detected) and the head camera, serving on
port 8000. Add `--dry-run` to run compute-only with no hardware attached, and
`--no-camera` to skip the head camera (see [Options](#options) for the full
flag list). Then, from a browser on the same network as the robot:

- `http://<robot-ip>:8000/` -- **viewer**: a spectating page with the video in
  a framed window and a "Start piloting" link to `/pilot` (disabled while
  someone else already holds the pilot slot).
- `http://<robot-ip>:8000/pilot` -- **pilot**: full-screen video with a d-pad,
  L/R rotate buttons, Wave/Dance gesture buttons, a neck stick, and a Home
  link back to `/` to drive the robot.

Any number of viewers can watch at once; only one pilot can drive at a time.

## How it works

### Roles and the control slot

Both pages open the same `/api/control` WebSocket and announce a role as
their first message (`{"role": "pilot"}` or `{"role": "viewer"}`).
`palmimo_teleop.session.PilotSession` holds a single pilot slot: the first
`"pilot"` request gets it; a second one gets back
`{"granted": false, "reason": "busy"}` and is treated as a viewer instead.
The slot is released only when that pilot's socket disconnects -- there is no
automatic hand-off, so the next person opens `/pilot` again to claim it fresh.

Every open connection, pilot or viewer, receives the same status broadcast
twice a second: `pilot_present`, the currently resolved `motion`, the
measured `loop_fps`, `camera_ok`, `servo_attached` (whether a servo driver
was attached at startup, as opposed to running compute-only -- see
[Hardware notes](#hardware-notes)), and `robot_ok` (whether the most recent
robot control cycle completed without raising -- see
[Servo health](#servo-health)).

If a pilot's own `{"role": "pilot"}` request comes back
`{"granted": false}` (someone else already holds the slot), the pilot page
falls back to view-only: it hides the drive controls and shows a banner,
the same as opening `/` directly.

### The control loop

`palmimo_teleop.robot_loop.RobotLoop` runs on its own thread at a fixed
control rate (`--fps`, default 60) independent of the web server's async
event loop -- it is the only thread that ever calls into `Palmimo` once
connected, so WebSocket handlers only ever write to `PilotSession`, never
touch the robot directly. Each cycle:

1. Read `PilotSession.effective_input()` -- the pilot's last frame, folded
   through the deadman timer and the currently playing gesture (see below).
2. `palmimo_teleop.control.resolve_motion(move, rotate, gesture)` turns that
   into one of `Palmimo`'s motion names, or `None` for "stop" -- **gesture
   wins over rotate, which wins over translation**: holding a direction
   button and a rotate button together turns in place rather than blending
   (there is no combined gait), and a playing gesture overrides both.
3. `robot.set_motion(...)` (or `robot.stop()`) only when that resolved name
   differs from the one applied last cycle, then `robot.look(pitch=...,
   yaw=...)` with the pilot's neck stick target. The `set_motion`/`stop` call
   is edge-triggered rather than reissued every cycle because `MotionEngine`
   resets a WAVE/DANCE motion's phase to 0 every time it is (re)selected --
   calling `set_motion("wave")` on every tick would restart wave from its
   first frame forever instead of ever letting it play. Walking gaits do not
   reset on reselection, so this is unobservable for them.
4. `robot.step()` advances one control cycle and streams the result to the
   servo bus, if one is attached.

### Gestures

The pilot page has two buttons above the neck stick, **Wave** and **Dance**
(keyboard `1`/`2`), each a large tap target sized like a d-pad cell. Tapping
one sends an independent `{"play": "wave" | "dance"}` message over
`/api/control` -- separate from the per-100ms control frame -- which only the
current pilot may send; `palmimo_teleop.session.PilotSession.play` ignores it
otherwise.

A play request starts (or replaces) a gesture that then runs for
`session.GESTURE_PLAY_SECONDS` (5 seconds for both, at the time of writing)
before `effective_input()` stops reporting it and the robot loop falls back
to move/rotate. `palmimo_sdk` does not publish how long `Palmimo.wave()` or
`Palmimo.dance()` actually take to play, so this window is a fixed
placeholder rather than derived from the SDK -- `Palmimo.wave()` plays once
and then holds neutral (so outlasting it is harmless), while
`Palmimo.dance()` loops until deselected (so this window is what ends it).
Replace it with SDK introspection if a future release exposes per-motion
play length.

Tapping the gesture that is already playing stops it early instead of
restarting it (tap-to-toggle); tapping the other gesture replaces whichever
one is running. Sending a non-idle `move`/`rotate` in the regular control
frame cancels a playing gesture immediately -- the pilot's steering intent
takes priority over letting the gesture finish its window.

### Safety: the deadman timer

The pilot page sends one control frame every 100ms
(`{"move", "rotate", "neck", "seq"}`) on a fixed interval rather than only on
input change -- this doubles as the deadman heartbeat. If the server goes
**400ms** without a frame from the pilot (`session.DEADMAN_TIMEOUT_S`), the
robot loop stops translation, rotation, any playing gesture, **and the
neck** on its own -- the neck returns to center (`pitch=0, yaw=0`), it does
not hold whatever target the pilot last aimed it at, since an unattended
neck held at an extreme (e.g. fully up) is a known cause of neck-servo
overheating on real hardware. The pilot slot itself stays held as long as
the WebSocket is still open, so a brief network hiccup does not lose the
slot, only the motion. A pilot
WebSocket disconnecting outright stops the robot and releases the slot -- the
deadman timer and that disconnect handling are the only stop paths: there is
no separate emergency-stop control, since a closed WebSocket already gets the
robot to a stop faster (up to the 400ms deadman window) than a
manually-triggered one would.

The pilot page's Home link (top-right, where a STOP button might otherwise
go) is a plain link to `/`, not a control frame: leaving `/pilot` closes the
page's WebSocket, and the server's existing disconnect handling (the
`finally` block in `server.py`'s `/api/control` route) stops the robot and
frees the pilot slot the same way any other disconnect does -- no separate
"go home" logic needed.

### Servo health

`RobotLoop.robot_ok` reports whether the most recent control cycle
(`tick()`) completed without raising -- a servo bus fault (a disconnected
cable, a stalled joint) surfaces here rather than only in the log. The
status bar shows it as `servo: ok` / `servo: fault`. A failing tick does not
count toward `loop_fps`, so a servo bus failing most cycles shows a
correspondingly low fps instead of one that looks healthy, and the failure
log itself is rate-limited to roughly once per 5 seconds (logged immediately
on the first failure) rather than once per control cycle.

### Video

`palmimo_teleop.video.VideoStream` owns the head camera's open/close
lifecycle directly (not through `Palmimo`, so a camera that fails to open
never rolls back the servo connection -- see its docstring) and runs one
background thread that captures at roughly 15fps, downscales to 640x480, and
JPEG-encodes into a single shared buffer. `GET /video.mjpeg` streams that one
buffer as `multipart/x-mixed-replace` to every connected viewer, so N open
tabs cost one capture/encode, not N. With `--no-camera`, or if opening the
camera fails, the endpoint 503s and the status broadcast reports
`camera_ok: false` instead of the server failing to start.

### Shutdown

Ctrl-C / `SIGTERM` stops `uvicorn`, which runs the FastAPI `lifespan`'s
teardown: the robot loop is signaled to stop, stops the robot, and steps it
through about a second of settling (paced at the control rate, not slammed
through) before the loop thread exits; the video capture thread stops and the
camera closes; then `Palmimo.disconnect()` eases the legs back to neutral and
soft-releases the neck, the same parked shutdown every other example on this
SDK uses.

If the robot-loop or video-capture thread does not stop within its join
timeout, the corresponding teardown step (settling the robot, closing the
camera) is skipped and an error is logged instead of running it anyway --
`RobotLoop` is the only thread allowed to call into `Palmimo` once connected,
so settling through a second path while that thread might still be running
would break that invariant, and closing the camera while capture might still
be reading from it would race the two.

## Setup

This project is a member of this repository's uv workspace, so the
workspace's regular dependency sync (see
[Resolving Dependencies](../../doc/guides/installation.md#resolving-dependencies))
also covers it -- no separate `.env` or extra install step. It declares
`palmimo-sdk[hardware,vision]`, so both the servo bus and head-camera (`cv2`)
dependencies install with the rest of the workspace.

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--port` | `8000` | HTTP/WebSocket port to serve on |
| `--fps` | `60` | Robot control loop rate in cycles per second (minimum 1) |
| `--gait-speed` | `0.012` | `Palmimo` gait speed applied to every teleop-driven motion (0.001-0.05) |
| `--dry-run` | off | Compute-only: never attach a servo driver |
| `--no-camera` | off | Never attach the head camera |
| `--servo-port` | none (auto-detected) | Servo bus serial port, e.g. `/dev/ttyACM0`; ignored under `--dry-run` |

## Hardware notes

The servo bus is auto-detected and motions are LIVE by default: a
`DynamixelDriver` is probed (`connect()`/`disconnect()`) before the server
starts. The probe is retried up to `main.PROBE_RETRY_ATTEMPTS` (3) times,
`main.PROBE_RETRY_INTERVAL_S` (1s) apart -- a bus that is merely noisy right
after startup (e.g. a prior process was just killed) gets a few more chances
before the example gives up on it. Only once every attempt fails does it
degrade gracefully: it prints a one-line warning and continues compute-only,
exactly like `--dry-run`.

Compute-only is not just a log line: the pilot page shows a persistent "NO
SERVO BUS" banner for as long as `servo_attached` is `false` in the status
broadcast, and a "SERVO FAULT" banner if an attached driver's control cycle
starts failing (`robot_ok: false`) -- the viewer page's status line reflects
the same `bus:`/`servo:` state. Neither page silently runs compute-only
without saying so on screen.

**Neck axis signs are hardware-verified.**
`robot_loop.NECK_PITCH_SIGN` (`1.0`) and `robot_loop.NECK_YAW_SIGN` (`-1.0`)
were confirmed against a physical robot (2026-09): pitch tracks the stick
as-is, raw yaw comes out mirrored. If a future neck revision changes either
direction, flip the constant there (the one place both axes are applied)
rather than inverting the axis anywhere else.

As with any live motion: keep the robot in the air on first run, or after
changing `--gait-speed`, the same as any other motion in this SDK.

## Layout

The video is always shown in full (`object-fit: contain`) rather than
cropped to fill the screen, so the pilot never loses part of the frame to a
mismatched aspect ratio. Both orientations are first-class ("Game Boy"
layout, no rotation prompt):

- **Portrait**: the video sits in a fixed-width, 4:3-tall band across the
  top; the controls (d-pad/rotate group and neck/gesture group) fill the
  rest of the screen below it, side by side. Video and controls are
  normal-flow siblings, so they never overlap.
- **Landscape**: the video fills the viewport, letterboxed by `contain`; the
  controls overlay its bottom-left and bottom-right corners, matching the
  layout the device had before this feature. That keeps the video's center
  clear even when the letterbox bars are too thin to hold the controls
  without any overlap.

The pilot page's controls are sized in `vmin` (the viewport's shorter side)
rather than a fixed unit, so the same layout fits a phone in either
orientation without a separate stylesheet per orientation: rotating the
device swaps which of width/height is smaller, and `vmin` tracks whichever
that is. `html` (both pages) and the pilot page's `body` are clamped to the
visual viewport (`100dvh`) with scrolling and overscroll bounce disabled, so
a drag on the control surface can never scroll or bounce the page; the
viewer page keeps `body` scrollable for its normal document flow.

## Safety notes

**There is no authentication.** `/api/control` and `/video.mjpeg` are open to
anyone who can reach the port -- this example is built for a single trusted
LAN (e.g. the robot's own Wi-Fi AP or a home network), not for exposing the
robot over the open internet or an untrusted network. Do not port-forward
this server. `/api/control` does check the WebSocket's `Origin` header
against the request's own `Host` (rejecting a mismatch with code 1008)
since WebSocket connections are not covered by CORS and a browser tab open
to any other site on the LAN could otherwise open a control socket to this
server -- but that is a same-site check, not authentication: anyone who can
reach the port and load a page served from it (or any client that sends no
`Origin` at all, e.g. a non-browser script) can still drive.

Only one pilot can drive at a time. There is no separate emergency-stop
control: the 400ms deadman timer and the pilot-disconnect handling (see
[Safety: the deadman timer](#safety-the-deadman-timer)) are what stop the
robot when something goes wrong.

**Avoid holding the neck at an extreme for a long time.** The deadman
timer recenters the neck on its own (see above), but a pilot who keeps the
neck stick pushed fully up/down/left/right for an extended period is still
holding the neck servo at that extreme the whole time -- this is a known
cause of neck-servo overheating on real hardware. A temperature guard on
the servo itself is a follow-up on the `palmimo_sdk` side, not something
this example implements.

## Limitations

- No video/control latency compensation -- both are best-effort over
  whatever the LAN and Wi-Fi radio provide at the moment.
- The head camera is single-owner hardware (`palmimo_sdk.io.camera.HeadCamera`
  supports one open device per process); this example is the only consumer
  during a session, so it does not compose with another example that also
  wants the camera open at the same time.
- No stick trim/calibration -- the neck stick is spring-loaded and always
  starts a drag from center, but there is no way to bias where "center"
  itself points.
- The pilot page keeps no reconnect logic: a dropped WebSocket shows a
  "Disconnected" banner with a Reload button (`location.reload()`) to
  reconnect and re-claim (or re-request) the pilot slot.
- A silent disconnect (Wi-Fi walks out of range, the pilot's device sleeps,
  with no clean WebSocket close) can take up to roughly 10 seconds
  (`ws_ping_interval`/`ws_ping_timeout`, both 5s) to be noticed and free the
  pilot slot for someone else -- the robot itself stops much sooner, within
  the 400ms deadman window, since that does not depend on the ping.
