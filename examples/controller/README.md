# palmimo_controller

Palmimo Controller: open a page in a phone or laptop browser on the same LAN
and drive the robot by hand, then switch it to an AI voice mode with one tap
and back again. Manual control is the same as the `teleop` example; AI modes
add a voice conversation (OpenAI Realtime) in which the robot speaks as a
preset character and moves on its own. Teleop needs no API key and no
network beyond the LAN; AI modes need `OPENAI_API_KEY`.

## Running it

From this directory (`examples/controller/`):

```bash
uv sync
OPENAI_API_KEY=sk-... uv run palmimo-controller
```

Leave `OPENAI_API_KEY` unset to run teleop only; the AI button then says why
AI is unavailable instead of starting.

Defaults to a real servo bus (auto-detected) and the head camera, serving on
port 8010. Add `--dry-run` to run compute-only with no hardware attached, and
`--no-camera` to skip the head camera (see [Options](#options) for the full
flag list). Then, from a browser on the same network as the robot:

- `http://<robot-ip>:8010/` -- **viewer**: a spectating page with the video in
  a framed window, a "Start piloting" link to `/pilot` (disabled while
  someone else already holds the pilot slot), and below it the editor for
  the AI mode prompts (see [Editing prompts](#editing-prompts)).
- `http://<robot-ip>:8010/pilot` -- **pilot**: full-screen video with a d-pad,
  L/R rotate buttons, seven gesture buttons plus the AI mode button, a neck
  stick, and a Home link back to `/` to drive the robot. The mode bar at the
  top shows the current mode and the way back to manual control.

Any number of viewers can watch at once; only one pilot can drive at a time.

## AI modes

Two preset modes ship with the app, each one prompt in
`palmimo_controller/modes/`:

| Mode | Prompt | Character |
|---|---|---|
| Default | `default.md` | Neutral, polite standard Japanese |
| Kansai-ben | `kansai.md` | The same robot, speaking Kansai dialect |

On the pilot page, tap the AI mode button in the mode bar, pick a mode, and
the robot starts listening and talking. The same spot then holds the
return-to-teleop button; tapping it stops speech and AI motion at once.

How a switch works:

- **The robot connection never changes.** `RobotLoop` is the only thread
  that drives `Palmimo`, in every mode. An AI mode only changes which input
  `PilotSession.effective_input()` returns: the pilot's d-pad in teleop,
  nothing while the AI session connects, and the AI's latest motion intent
  once it is live. Going live starts with one nod
  (`session.AI_START_NOD_SECONDS`), which the AI's first motion replaces.
- **The switch shows its progress.** The pilot page shows a spinner from the
  tap until the session is live. When a face display is attached, the face
  cycles through `session.STARTING_FACES` (thinking, idea, smile) while the
  session connects, shows a happy face when it goes live, and a sad face when
  a switch fails, before returning to its neutral face.
  `PilotSession.face_cue()` decides the face, and `RobotLoop` sends it only
  when it changes. A face that stops answering is logged and skipped; it never
  counts as a servo fault, so it cannot end an AI session.
- **The AI moves the robot through tools** (`ai/tools.py`): `walk`, `turn`,
  `gesture`, `look`, and `stop`. Each records an intent with a time limit of
  at most `session.AI_INTENT_MAX_SECONDS` (5 s), after which the robot stands
  still again, so a model never keeps the robot walking.
- **One switch at a time.** A switch is only accepted from teleop, so a
  double tap cannot start two AI sessions.
- **Failures fall back to teleop.** A missing or rejected key, an exhausted
  quota, a network drop, or a start slower than `START_TIMEOUT_S` (8 s) ends
  the AI session and returns to teleop with a plain-language message on the
  pilot page. A failing servo bus also stops the AI session, so the account
  is not billed for a robot that cannot move. The pilot disconnecting stops
  it too: with nobody holding the controls, nobody could tap back.
- **Prompts are read at mode start**, not at app start, through
  `modes.PromptSource`. `EditablePrompts` reads an edit saved from the
  homepage when there is one and the shipped file otherwise, so an edit
  applies on the next switch, never to a running session. A source that
  fetches prompts from Palmimo Portal can replace it later.

The model, voice, and language are fixed in `ai/controller.py` (`MODEL`,
`VOICE`, `LANGUAGE`); the pilot picks a mode, not a model.

### Editing prompts

The homepage (`/`) lists each mode in a prompt section below the status bar.
Open one, edit the text, and press the save button; the reset button brings
back the shipped prompt. Edits are saved outside the package, in
`$XDG_DATA_HOME/palmimo/controller/prompts/<mode>.md` (by default
`~/.local/share/palmimo/controller/prompts/`), so an app update keeps them.
The shipped files in `palmimo_controller/modes/` are never changed. Only the
two presets can be edited; the mode list itself is fixed.

The page uses `GET /api/prompts`, `PUT /api/prompts/<mode>` (body
`{"text": ...}`, at most 8000 characters) and `DELETE /api/prompts/<mode>`.

### Telemetry

`palmimo_controller.telemetry.Telemetry` logs one JSON line per event on the
`palmimo_controller.telemetry` logger: `session_start`, `session_end`,
`mode_switch` (from, to, ok, latency, error), and `ai_error`. No audio or
transcript text is recorded.

### Privacy

In an AI mode, microphone audio is streamed to OpenAI under the account that
owns `OPENAI_API_KEY`, and that account's OpenAI terms govern it. Visitors
near the robot are recorded while an AI mode runs; tell them, or keep the
robot in teleop where that is not acceptable.

## How it works

### Roles and the control slot

Both pages open the same `/api/control` WebSocket and announce a role as
their first message (`{"role": "pilot"}` or `{"role": "viewer"}`).
`palmimo_controller.session.PilotSession` holds a single pilot slot: the first
`"pilot"` request gets it; a second one gets back
`{"granted": false, "reason": "busy"}` and is treated as a viewer instead.
The slot is released only when that pilot's socket disconnects -- there is no
automatic hand-off, so the next person opens `/pilot` again to claim it fresh.

Every open connection, pilot or viewer, receives the same status broadcast
twice a second: `pilot_present`, the currently resolved `motion`, the
applied `speed` multiplier (see [Speed](#speed)), the measured `loop_fps`, `camera_ok`, `servo_attached` (whether a servo driver
was attached at startup, as opposed to running compute-only -- see
[Hardware notes](#hardware-notes)), and `robot_ok` (whether the most recent
robot control cycle completed without raising -- see
[Servo health](#servo-health)).

If a pilot's own `{"role": "pilot"}` request comes back
`{"granted": false}` (someone else already holds the slot), the pilot page
falls back to view-only: it hides the drive controls and shows a banner,
the same as opening `/` directly.

### The control loop

`palmimo_controller.robot_loop.RobotLoop` runs on its own thread at a fixed
control rate (`--fps`, default 60) independent of the web server's async
event loop -- it is the only thread that ever calls into `Palmimo` once
connected, so WebSocket handlers only ever write to `PilotSession`, never
touch the robot directly. Each cycle:

1. Read `PilotSession.effective_input()` -- the pilot's last frame, folded
   through the deadman timer and the currently playing gesture (see below).
2. `palmimo_controller.control.resolve_motion(move, rotate, gesture)` turns that
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

The pilot page has seven gesture buttons above the neck stick -- **Wave**,
**Dance**, **Nod**, **Clap**, **Push-up**, **Body tilt** and **Bow**
(keyboard `1`-`7`) -- in a four-column grid together with the AI mode
button. Tapping one sends an independent `{"play": <name>}` message over
`/api/control` -- separate from the per-100ms control frame -- which only the
current pilot may send; `palmimo_controller.session.PilotSession.play` ignores it
otherwise.

A play request starts (or replaces) a gesture that then runs for
`session.GESTURE_PLAY_SECONDS` (5 seconds for each, at the time of writing)
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

### Speed

A row of buttons under the d-pad selects a speed multiplier -- **1x, 1.5x,
2x, 2.5x** (`control.ALLOWED_SPEEDS`) -- applied to walking and rotating
only; gestures always play at their own speed. Tapping one sends an
independent `{"speed": <n>}` message, which only the current pilot may send
and which is dropped unless it is one of those values. The selection holds
until the pilot picks another one; the highlighted button is the multiplier
the server reports as applied in the status broadcast, not the one last
tapped.

The robot loop scales the gait speed the robot was started with
(`--gait-speed`), written to `robot.engine.gait_speed` since `Palmimo` takes
gait speed only as a constructor argument. The multiplied gait speed is
capped at `--gait-speed`'s own maximum (`control.MAX_GAIT_SPEED`, 0.05).

Faster commands alone do not make the robot walk faster. The driver's
time-based Profile_Velocity (300 ms per goal by default) low-passes the leg
servos, so a faster gait comes out with a much smaller stride. So while a
walking or rotating gait runs, the robot loop also divides the 18 leg
servos' Profile_Velocity by the same factor as the gait speed (120 ms at
2.5x), which keeps the 1x stride. It returns them to the driver default when
the robot stands still or switches to a gesture, and writes nothing while
the gesture plays (`Palmimo` retunes the servos for a wave itself). A
dropped bus reply on one of these writes is retried next cycle instead of
failing it.

**AI modes always walk at 1x.** Starting an AI mode resets the multiplier to
1x, a speed message sent while an AI mode is starting or running is
dropped, and the pilot comes back to teleop at 1x. The multiplier also falls
back to 1x whenever the robot is not being actively piloted -- the deadman
expiring, the pilot leaving (the next pilot starts at 1x) -- and on shutdown
the robot settles to neutral at 1x.

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

`palmimo_controller.video.VideoStream` owns the head camera's open/close
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
teardown: a running AI mode is stopped first, so the robot stops talking
before it parks; then the robot loop is signaled to stop, stops the robot, and steps it
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

This project is its own standalone uv project (own `pyproject.toml`/`uv.lock`),
not a member of the repository's root workspace: `cd` into this directory and
`uv sync` -- no separate `.env` or extra install step. It declares
`palmimo-sdk[hardware,vision,voice]` from PyPI, so the servo bus,
head-camera (`cv2`), and microphone dependencies install with it. AI mode
plays the model's voice through `aplay` on the ReSpeaker board, and records
from the same board.

To try it against local, unreleased SDK changes instead of the published
`palmimo-sdk`, run from this directory:

```bash
uv run --with-editable ../../packages/palmimo_sdk python -m palmimo_controller.main
```

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--port` | `8010` | HTTP/WebSocket port to serve on (8000 is left to the `teleop` app) |
| `--fps` | `60` | Robot control loop rate in cycles per second (minimum 1) |
| `--gait-speed` | `0.012` | `Palmimo` gait speed applied to every teleop-driven motion (0.001-0.05); the 1x that the pilot page's [speed](#speed) buttons scale |
| `--dry-run` | off | Compute-only: never attach a servo driver |
| `--no-camera` | off | Never attach the head camera |
| `--no-face` | off | Never attach the face display. Without this flag, a face display is attached only when one is plugged in and opens, so a robot without a face still starts |
| `--servo-port` | none (auto-detected) | Servo bus serial port, e.g. `/dev/ttyACM0`; ignored under `--dry-run` |
| `--audio-device` | `respeaker` | AI mode mic and speaker. `respeaker`: a development robot's ReSpeaker 4 Mic Array, with echo cancellation. `booth-usb`: an exhibition robot's push-to-talk USB mic ("USB PnP", 48 kHz, no echo cancellation; hold its button while talking) and USB speaker ("Composite") |

| Environment variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI key for AI modes. Optional: without it, teleop works and AI modes stay off |

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
`robot_loop.NECK_PITCH_SIGN` and `robot_loop.NECK_YAW_SIGN` (both `1.0`)
were confirmed against a physical robot: both axes track the stick as-is.
If a future neck revision changes either direction, flip the constant there
(the one place both axes are applied) rather than inverting the axis
anywhere else.

As with any live motion: keep the robot in the air on first run, or after
changing `--gait-speed` or picking a speed multiplier you have not run on
that robot before, the same as any other motion in this SDK.

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

**There is no authentication.** `/api/control`, `/api/prompts` and
`/video.mjpeg` are open to anyone who can reach the port, so anyone on the
LAN can also change what the AI modes say -- this example is built for a single trusted
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

- AI modes are Japanese-only, and the mode list is the two shipped presets.
  Their prompts are edited on the controller's own homepage; editing them
  (or adding modes) from Palmimo Portal needs a Portal-backed
  `PromptSource`, which does not exist yet.
- The pilot page's own labels are English except the mode bar.

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
