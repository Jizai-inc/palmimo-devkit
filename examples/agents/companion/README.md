# palmimo_companion_agent

An always-on companion agent for Palmimo: a small, curious hexapod that
listens, talks, and reacts on its own terms. Unlike the wake-word example
(one-shot "wake word + command"), the companion agent runs continuously with
two distinct behavior channels -- an autonomous idle loop and an event-driven
response to speech/keyboard input (see "How it works" below) -- guarded
speech routing (so it only reacts to speech actually directed at it), a
27-tool motion/expression vocabulary built on `palmimo_sdk.agent`'s
`AgentToolSet`, and two lightweight non-LLM reflexes (wave-back, face
tracking) for immediate, low-latency reactions vision alone can drive.

## Running it

```bash
uv run palmimo-companion-agent
```

Defaults to a Textual TUI; pass `--ui cli` for a headless front end (stdin
instructions in, JSONL `History` events out on stdout — for scripting or an
external driver) and `--no-hardware` to run fully compute-only. Requires
`GEMINI_API_KEY` and/or `OPENAI_API_KEY` depending on which models are
configured — see [Setup](#setup) for where the `.env` file has to live, and
[Options](#options) for the rest of the flags.

## Layout: one character, two runtimes

The package splits into a shared character layer and a runtime layer, so a
second runtime can sit beside the first without duplicating the character:

- **`core/`** -- the character, independent of how it is driven: `prompts/`
  (persona/identity/behavior text), `tools.py` + `toolview.py` (the LLM tool
  vocabulary and its per-turn-kind views), `vision.py` (camera-based
  detectors), and `reflexes.py` (`ReflexEngine`, the non-LLM wave-back /
  face-tracking reactions). `core/` never imports from either runtime -- not
  `pipeline/` and not `realtime/` -- `ReflexEngine` reports what it did
  through a plain `notify` callback instead of depending on `History`
  directly (see `reflexes.py`'s docstring).
- **`pipeline/`** -- the cascaded STT -> LLM -> TTS runtime this README
  describes below: `bus.py`, `conductor.py`, `dispatch.py`, `idle.py`,
  `respond.py`, `llm.py`, `prompt.py`, `history.py`, `event_log.py`,
  `speech.py`, `vad.py`, `sleeping.py`, `settings.py`, `ui/`, `wiring.py`.
  `History` and `Conductor` are owned here, not in `core/` -- a second
  runtime keeps its own event log and its own turn-scheduling loop rather
  than sharing this one.
- **`realtime/`** (a sibling of `pipeline/`, see `palmimo-realtime` below) --
  a lower-latency runtime handing the microphone straight to the OpenAI
  Realtime API instead of relaying through separate STT/LLM/TTS stages:
  `protocol.py` (the typed wire layer -- every event-name string lives only
  here), `client.py` (the websocket owner), `bridge.py` (`ToolBridge`, turning
  a finished response into tool calls), `state.py` (its own `Sleeping`
  mirror -- `History`-free, unlike `pipeline/sleeping.py`), `settings.py`
  (`RealtimeSettings`), `log.py` (a minimal JSONL log, a different shape from
  `pipeline/event_log.py`'s `History`-event one), `prompts.py`, `app.py`
  (`RealtimeSession` + the `palmimo-realtime` entry point), and `services/`
  (`audio.py`, `router.py`, `idle.py`, `frames.py`, `reflexes.py` -- one
  background loop per concern, all implementing the `Service` protocol in
  `services/base.py`). It reuses `core/` (the same character, tools, and
  reflexes) and `core/prompts/idle.md` + `core/toolview.py`'s
  `IDLE_TOOL_NAMES` allowlist for its own idle behavior, but NOT
  `pipeline/idle.py`'s `IdleTurn` class itself -- that class's
  one-ReAct-tick-per-pass shape is specific to the cascaded runtime's
  turn-taking, not something a realtime session (which has no discrete
  "turns" to tick) can reuse as-is. `realtime/` never imports from
  `pipeline/`.
- **`settings.py`** (package root) -- `CompanionSettings`, the base every
  runtime's settings subclass extends (see `pipeline/settings.py`'s
  `PipelineSettings` and the Options section below).
- **`main.py`** (package root) -- the console-script entry point, dispatching
  to whichever runtime's front end `--ui` (or a future runtime-selecting
  flag) names.

## How it works

This section describes the `pipeline/` runtime (`--ui tui` / `--ui cli`
today). Three independent pipelines feed one shared `Conductor` and its
`History` event log. The conductor alternates between two turn types, never
running both at once:

- **Idle turn** (autonomous, `pipeline.idle.IdleTurn`): runs whenever nothing
  is queued and the robot isn't asleep (`pipeline.sleeping.Sleeping` gates
  this -- a successful `sleep` tool call stops idle ticks until a successful
  `wake_up`, so a limp sleeping robot is never stretched or turned by an
  idle-turn motion). One ReAct-style tick per pass -- a single LLM call,
  restricted to a small, speech-free tool vocabulary
  (`core.toolview.IDLE_TOOL_NAMES`: gaze, neck, body, and display tools only;
  no `say`, no showy social gestures like `dance` / `wave_both` / `bow`), and
  at most the first tool call it picks. Paced by a random 2-6s pause between
  ticks, and motions here run slower (4-8s) than a response's -- see
  `core/prompts/idle.md`.
- **Respond turn** (event-driven, `pipeline.respond.RespondTurn`): runs
  whenever speech or a keyboard instruction is queued -- including while
  asleep, since `wake_up` is a respond-only tool and this is the only turn
  type that can call it. A SINGLE LLM call sees the FULL tool vocabulary and
  may return up to 4 tool calls in one plan (`parallel_tool_calls=True`, sent
  only to providers that accept that parameter; the 4-call cap is enforced in
  code either way), executed **sequentially** in the order given -- e.g. `forward` then `say`
  to speak after arriving, or `forward(say=...)` for "moving while talking".
  There is no follow-up chat call within one respond turn (single-shot; an
  observation-dependent question the model can't answer from the plan alone
  is an accepted limitation). See `core/prompts/respond.md`.
- **Speech** (`SpeechPipeline`, only when a mic is attached): mic chunks
  (`palmimo_sdk.MicStream`, 16 kHz mono, echo-cancelled by default) -> `UtteranceSegmenter`
  (Silero VAD v5) -> STT (`LlmProvider.transcribe`) -> a guard model
  (`LlmProvider.classify_speech`) that classifies each utterance as
  noise / command / question / ambient -> `Conductor.submit_speech`
  (noise is dropped outright; command/question cancel whatever the robot is
  doing, ambient does not).
- **Vision reflexes** (`VisionWatch` + `core.reflexes.ReflexEngine`, only when
  a camera is attached and the `vision` extra's wave detector loads): camera
  frames -> `WaveDetector` (MediaPipe HandLandmarker) -> a detected wave
  makes the robot wave back and switch to a happy face, dispatched through
  the same `AgentToolSet` both turn types' tool calls go through (never
  straight at the facade, and never through the LLM), and skipped outright
  whenever the robot is already busy with something else. `wiring.py` wires
  `ReflexEngine`'s `notify` callback to append a `SystemNoteEvent` to
  `History` -- `core/reflexes.py` itself has no `History` dependency (see the
  Layout section above).

Both turn types dispatch tool calls through the same cancellation-aware
`pipeline.dispatch.run_tool` over a `core.toolview.ToolView` -- a read-only,
per-turn-kind filter (allowlist + optional `say`-squashing) over one shared
`palmimo_sdk.agent.toolset.AgentToolSet` (the SDK's stock tool vocabulary --
including its own optional `reason` field, which every prompt still requires
filled in every call -- is used as-is; `palmimo_companion_agent.core.tools`
registers only a same-name override for each SDK tool whose behavior this
agent extends, plus the composites, on top of it; see that module's
docstring). A tool's own `say` argument lets the robot speak concurrently
with its action rather than only as a separate turn; say no longer blocks the
tool call until the utterance finishes (`Speaker.say` is fire-and-forget,
briefly joined just long enough to catch an immediate TTS failure -- see
`palmimo_sdk.agent.tools.SayTool`). A tool call whose result carries an image
(`capture` and the composites that end in one) is described through the VLM
by `pipeline.dispatch.describe_images`, recorded as a `CameraEvent` alongside
the tool's own result.

### Prompts

`core/prompts/` holds the system prompt in four static files, composed by
`pipeline.prompt.load_prompt()` as persona + identity + the active turn kind:

- `persona.md` -- **the character-swap customization point.** Name, personality,
  likes/dislikes, and how self-referential questions ("what can you do?",
  "what's your favorite food?") get answered from the character's own voice
  rather than as a capability listing. Edit this file to reskin the robot;
  nothing else needs to change.
- `identity.md` -- shared physical/hardware facts (neck travel, what `look`
  does vs. `capture`) and the tool contract (`face`/`say` arguments, the
  `set_face` vs. `show_emoji` split, `reason`) every turn kind needs,
  independent of which character `persona.md` names.
- `idle.md` / `respond.md` -- the two turn kinds' own behavior rules (see
  "How it works" above).

`palmimo_companion_agent.pipeline.wiring.build_runtime` assembles all of this from
`PipelineSettings`: with `--no-hardware` it wires a bare, compute-only
`palmimo_sdk.Palmimo` (no driver/display/speaker/camera/mic attached, so no
speech, no vision, and `Runtime.connect()` skips calling the facade's own
`connect()` -- with nothing attached it would just raise); with hardware (the
default), it unconditionally builds the full peripheral set (servo bus, face
display, speaker, camera, mic, MediaPipe wave detector / face locator) and
hands them all to the same `Palmimo` -- wiring never probes anything or
decides what degrades. Whether the robot actually comes up is entirely the
SDK's `Palmimo.connect()` call, made from `Runtime.start()`: a real connect is
all-or-nothing, so one missing or broken peripheral rolls back everything
already opened and the whole startup fails -- see Hardware notes below.

## Setup

This project is a member of this repository's uv workspace, so the workspace's
regular dependency sync (see [Resolving Dependencies](../../../doc/guides/installation.md#resolving-dependencies))
also covers it. Settings (including the LLM API keys) are loaded through
`PipelineSettings` (pydantic-settings; the shared `CompanionSettings` base plus
this runtime's own chat/guard/VLM/STT/voice knobs) -- copy the sample env file
and fill in your key(s):

```bash
cp .env.sample .env
# edit .env and set GEMINI_API_KEY and/or OPENAI_API_KEY
```

**That `.env` has to sit next to `.env.sample`, in this project directory**
(`examples/agents/companion/`) -- same placement rule as the
[wakeword agent's `.env`](../wakeword/README.md#setup). One exception here:
the `tui` / `cli` pipeline can appear to work off a repository-root `.env`
too, because importing LiteLLM loads one into the process environment on its
own -- `palmimo-realtime` (which uses no LiteLLM) and the wake-word agent get
nothing from it, so do not rely on it. One file in this directory serves
every runtime here, `palmimo-realtime` included.

Alternatively, export the keys (and any `COMPANION_AGENT_*` var) in the
shell -- process environment variables always take precedence over `.env`.
At startup the agent checks every configured model's provider (`gemini/...`
needs `GEMINI_API_KEY`, `openai/...` needs `OPENAI_API_KEY`) and fails fast
with a clear message if a required key is missing -- this is the only
startup preflight this example performs.

On first run (hardware speech enabled), the Silero VAD v5 ONNX model
(~2 MB) auto-downloads into `~/.cache/palmimo/models/` and is cached for
later runs; the MediaPipe HandLandmarker / FaceDetector models used by the
wave-back and face-tracking reflexes auto-download similarly (see
`THIRD_PARTY_NOTICES.md`) -- `opencv-python` / `mediapipe` are regular
dependencies of this project (not an extra), since `--hardware` (the
default) always needs them.

Both entry points (`palmimo-companion-agent` and `palmimo-realtime`)
line-buffer stdout at startup and send log records to stderr, so redirecting
output to a file or running under a service manager (systemd, nohup) shows
each line as it is produced rather than in one burst at exit --
`PYTHONUNBUFFERED=1` is no longer needed. Nothing either front end prints was
losing output before this — the `cli` front end flushes its JSONL events one by
one — so what it buys is that the guarantee now belongs to the stream rather
than to each individual call site, and it extends to output produced inside the
SDK.

## Options

Settings resolve as CLI flag > process env > `.env` file > default. Every
`COMPANION_AGENT_*` env var (plus `GEMINI_API_KEY` / `OPENAI_API_KEY`) is
also documented in [`.env.sample`](.env.sample).

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--ui` | (none) | `tui` | Front end: `tui` (Textual) or `cli` (headless, JSONL stdout over stdin/stdout) |
| `--hardware` / `--no-hardware` | `COMPANION_AGENT_HARDWARE` (`false` to disable) | on | Attach real hardware peripherals; `--no-hardware` runs fully compute-only (a bare `Palmimo`, no speech, no vision) |
| `--port` | `COMPANION_AGENT_PORT` | none (auto-detected) | Servo bus serial port, e.g. `/dev/ttyACM0` |
| `--log-path` | `COMPANION_AGENT_LOG_PATH` | none (disabled) | JSONL event log file path (every `History` event, same shape as the `cli` front end's stdout) |
| (none) | `COMPANION_AGENT_CHAT_MODEL` | `gemini/gemini-3.5-flash-lite` | LiteLLM model for both the idle and respond turns' tool-calling chat |
| (none) | `COMPANION_AGENT_GUARD_MODEL` | `gemini/gemini-3.5-flash-lite` | LiteLLM model for the speech-classification guard |
| (none) | `COMPANION_AGENT_VLM_MODEL` | `gemini/gemini-3.5-flash-lite` | LiteLLM model for image-to-text description (the `capture` tool) |
| (none) | `COMPANION_AGENT_STT_MODEL` | `openai/gpt-4o-mini-transcribe` | LiteLLM model for speech-to-text transcription |
| (none) | `COMPANION_AGENT_LANGUAGE` | `ja` | ISO 639-1 code driving the STT hint and the reply language |
| (none) | `COMPANION_AGENT_SILENCE_SECONDS` | none (segmenter default) | How long the talker must stop before the utterance is treated as finished |
| (none) | `COMPANION_AGENT_VOICE_BACKEND` | `piper` | TTS backend: `piper` (local once its voice has been downloaded) or `openai` (hosted; needs `OPENAI_API_KEY` and a network) |
| (none) | `COMPANION_AGENT_VOICE_NAME` | none (backend default) | Voice name (openai) or Japanese catalogue voice key (piper) |
| (none) | `COMPANION_AGENT_VOICE_SPEED` | `1.0` | Speaking rate; higher is faster (inverted for piper's own `length_scale`) |
| (none) | `COMPANION_AGENT_VOICE_VOLUME` | `1.0` | Output gain; `1.0` is the voice's own level |
| (none) | `COMPANION_AGENT_VOICE_DIR` | none (the SDK's model cache) | Root holding one directory per piper voice model |
| (none) | `COMPANION_AGENT_SPEAKER_DEVICE` | `ReSpeaker` | Substring naming the ALSA playback card (id or long name, never an index); empty or unmatched uses ALSA's default |
| (none) | `COMPANION_AGENT_ECHO_CANCEL` | `true` | Cancel the robot's own speech out of the capture |
| (none) | `COMPANION_AGENT_NEAR_CHANNEL` / `..._REFERENCE_CHANNEL` | see `.env.sample` | Which mic-array channels the echo canceller treats as near/reference |
| (none) | `GEMINI_API_KEY` / `OPENAI_API_KEY` | none | API key(s) for whichever provider prefix the configured models above use (checked at startup) |

### Running against a locally hosted model

The model settings above take any LiteLLM model string, so the chat, guard,
and VLM models can be served from a machine you run yourself instead of a
hosted API. For Ollama, set `COMPANION_AGENT_CHAT_MODEL` to
`ollama_chat/<model>` (e.g. `ollama_chat/qwen3`), and point `OLLAMA_API_BASE`
at the server if it is not at its default `http://localhost:11434`. No API key
is involved -- the startup check above requires one only for the `gemini/` and
`openai/` prefixes, and asks for nothing from any other provider.

Use the `ollama_chat/` prefix rather than `ollama/`: only `ollama_chat/`
carries tool definitions to the model, and this agent acts entirely through
tool calls, so a model reached through `ollama/` would answer in words and
never move. Transcription is the one stage that stays remote --
`COMPANION_AGENT_STT_MODEL` still needs a hosted model whenever a mic is
attached.

### `palmimo-realtime` -- the Realtime voice front end

A second runtime, alongside the `tui` / `cli` pipeline ones:

```bash
uv run palmimo-realtime --seconds 360
```

Requires `OPENAI_API_KEY`, from the environment or from the same `.env` the
chat front ends read — this front end ships inside the companion project and
has no `.env` of its own. Billed per turn on the whole conversation context, so
cost grows with session length; the run prints a token and dollar summary on
exit. A 118 s session measured $0.91 (15 responses) — roughly $28/hour —
worth knowing before leaving it running. Its code lives under `palmimo_companion_agent/realtime/`
(see [Layout](#layout-one-character-two-runtimes) above) and never imports
from `pipeline/`.

The chat front ends run a relay -- voice detection, transcription, a chat
model, then synthesis -- and each stage waits for the one before it. This one
hands the microphone straight to the OpenAI Realtime API: the model hears the
audio, decides when the talker has stopped, answers in its own voice, and
calls the same tools the chat agent does. There is no `Speaker`, because the
model's audio *is* the voice, so no TTS model is needed (`voice_backend` does
not apply here).

Needs `OPENAI_API_KEY`, read from this project's `.env` (see [Setup](#setup))
or the environment -- it has no `.env` of its own. It is billed per turn on
the whole conversation context, so cost grows with session length -- the run
prints a token and dollar summary when it exits.

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--model` | `COMPANION_AGENT_MODEL` | `gpt-realtime-2.1` | OpenAI Realtime model |
| `--voice` | `COMPANION_AGENT_VOICE` | `coral` | Session voice (cannot change once the session has spoken) |
| `--pitch` | `COMPANION_AGENT_PITCH` | `1.15` | Playback pitch multiplier (resamples the reply, raising pitch and tempo together at no latency cost); `1.0` is off |
| `--reply-chars` | `COMPANION_AGENT_REPLY_CHARS` | `60` | Reply-length hint folded into the prompt; `0` leaves it to the model's judgement |
| `--frame-seconds` | `COMPANION_AGENT_FRAME_SECONDS` | `10.0` | How often a camera frame is offered to the model; each one replaces the last |
| `--seconds` | `COMPANION_AGENT_SESSION_SECONDS` | `120.0` | Session length |
| `--port` | `COMPANION_AGENT_PORT` | none (auto-detected) | Servo bus serial port |
| `--log-path` | `COMPANION_AGENT_LOG_PATH` | none (disabled) | JSONL event log path (this runtime's own log -- see `realtime/log.py`, a different shape from the pipeline's `History` events) |

It also reads the shared `COMPANION_AGENT_*` knobs (`echo_cancel`,
`near_channel`/`reference_channel`, `speaker_device`, `language`) documented
in the table above; `voice_backend` and the other TTS-only settings do not
apply. `speaker_device` reaches this runtime by a different route than the
pipeline's: the model's own audio is played by `Playback`'s `aplay`, not
synthesized through the SDK's `Speaker`, so the hint is resolved in
`realtime/app.py` and handed to `Playback` as a device string.

Ctrl+C and `SIGTERM` both end the session and park the robot on the way out.
In-flight tool work is cancelled and given a few seconds to settle first, so
a motion is never still writing to the servo bus while the robot disconnects.

**There is no directed/ambient guard on this runtime.** The pipeline's guard
model classifies each utterance (noise / command / question / ambient)
before it reaches the dialogue loop, so background chatter mostly does not
interrupt an in-progress reply. This runtime has nothing upstream of the
Realtime API's own voice-activity detector: any speech onset -- someone
addressing the robot, a bystander's aside, or a VAD false positive -- barges
in and silences whatever the robot was doing (see `services/router.py`'s
`BargeIn`). That is a deliberate latency trade-off, not an oversight: adding
a guard here would mean transcribing and classifying before the model is
even allowed to react, which is exactly the per-stage latency this runtime
exists to avoid.

### `/hear` -- testing the speech path headlessly

In the `cli` front end, a stdin line starting with `/hear ` (e.g.
`/hear hello`) submits the rest of the line through
`Conductor.hear()` instead of `submit_user_text()`: it runs the same
guard-classification path a real mic utterance goes through (noise / command
/ question / ambient), so the respond turn's speech-specific behavior (must
answer with voice, barge-in on a command/question) can be exercised without
an actual microphone or STT call. An empty argument (`/hear` alone) prints a
warning to stderr and is ignored. Plain lines (no `/hear` prefix) still go
through `submit_user_text()` as a keyboard instruction, and `/exit` still
ends the session.

## Hardware notes

With `--hardware` (the default), `build_runtime` assumes the full robot: the
servo bus, face display, speaker, camera, mic, and the MediaPipe wave
detector / face locator are all constructed unconditionally and handed to
`palmimo_sdk.Palmimo` -- wiring never probes a peripheral and never decides
to run with less than the full set. Whether the robot actually comes up is
entirely `Palmimo.connect()`'s call (invoked from `Runtime.start()`): a real
SDK connect is all-or-nothing, so a single missing or broken peripheral (no
servo bus attached, the port not found, no camera, ...) rolls back every
peripheral already opened and the whole startup fails with the SDK's own
error. There is no partial/degraded hardware mode -- run `--no-hardware` for
a fully compute-only, no-peripheral `Palmimo` instead.

## Cost & privacy note

Every guarded utterance (mic audio that clears the VAD) is sent to a remote
STT API, and every non-noise utterance is also sent to a remote guard model
and (once accepted) the chat model -- there is no local pre-filter beyond
VAD segmentation and a few Python-level noise heuristics (short fragments,
known filler words, known hallucination phrases). `capture` sends a JPEG
frame to a remote VLM. This prioritizes accuracy and conversational range
over minimizing API usage or keeping audio/images fully on-device.

## Limitations

This is a single companion instance with no persistence across runs:
`History` is a fixed-length in-memory window (oldest events are silently
evicted), so nothing is remembered once the process exits (aside from
whatever a JSONL event log captured). The wave-back and face-tracking
reflexes are skipped outright whenever the robot is already busy with an
in-flight tool call or another reflex's cooldown -- a wave held out during a
long motion, or a face that appears while another reflex just fired, may go
unanswered. There is no extra behavior layer (no lifecycle/energy,
mood seeding, or priority keywords) -- this example does not implement one.
