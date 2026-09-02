# palmimo_wakeword_agent

A minimal wake-word voice agent for Palmimo. Deliberately simple:
no autonomous/ReAct loop, no LLM-provider abstraction (the `openai` SDK is
used directly), no TUI.

## Running it

Say the wake word and a command together in one breath, and it is executed via
LLM tool-calling.

```bash
uv run palmimo-wakeword-agent
```

Requires `OPENAI_API_KEY` (STT) and, with the default Gemini command model,
`GEMINI_API_KEY` — see [Setup](#setup) for where the `.env` file has to live.
The Silero VAD model auto-downloads into `~/.cache/palmimo/models` on first
run; command transcription runs remotely via the OpenAI Whisper API.

## How it works

The agent listens continuously through a shared `palmimo_sdk.MicStream`
(16 kHz mono, echo-cancelled by default via the ReSpeaker's loopback channel;
`processors=[Denoiser()]` is the noise-only fallback without it), attached to
the `Palmimo` facade as
`Palmimo(mic=...)` so one `connect()`/`disconnect()` manages the mic together
with the servo bus and speaker; chunks are consumed via the mic's
with-managed `stream()` subscription. Each mic chunk is rebuffered into
512-sample frames and scored by a Silero VAD v5 model; an `UtteranceSegmenter`
turns the probability stream into complete utterances (pre-roll included, so
the start of speech is never clipped).

The agent is one-shot only, with no waiting state: every completed utterance
is transcribed with OpenAI's Whisper transcription API and checked for the
wake word.

The check is on **sounds, not spelling**. The name is not a word of the
transcription language, so the transcriber writes it in Latin script and picks
a different spelling nearly every time -- `Parmimo`, `Parumimo`, `Par mi mo`,
`Farmimo`, `Varumimo`. Both sides are therefore folded to a romaji skeleton and
compared by similarity (see `palmimo_sdk.NameMatcher`). Measured against 40
recorded calls, spelling comparison matched 3 and sound comparison matched 39,
with no false accept across 40 utterances of ordinary speech.

- No match: the utterance is ignored.
- A match with no command text after it (the wake word alone, nothing else in
  the same utterance) is also ignored -- there is no follow-up window, so say
  the wake word and the command together in one breath, e.g.
  `パルミーモ、前に進んで` ("Palmimo, move forward").
- A match with a command attached (wake word plus a follow-on phrase in the
  same utterance) executes: the command text is run through the LLM
  tool-calling agent, and the reply is spoken.

Command execution is one round of tool-calling, not a loop: the model may
return tool calls, each is executed against the SDK's `AgentToolSet`, and a
single follow-up completion turns the tool results into one short spoken
reply.

While the robot's own TTS is speaking, incoming mic chunks are dropped and
the segmenter is reset, so the agent never mishears (and reacts to) its own
voice.

## Setup

This project is a member of this repository's uv workspace, so the workspace's
regular dependency sync (see [Resolving Dependencies](../../../doc/guides/installation.md#resolving-dependencies))
also covers it. Settings are loaded through `WakewordAgentSettings`
(pydantic-settings) — copy the sample env file and fill in your keys:

```bash
cp .env.sample .env
# edit .env and set OPENAI_API_KEY=... and GEMINI_API_KEY=...
```

**That `.env` has to sit next to `.env.sample`, in this project directory**
(`examples/agents/wakeword/`): `WakewordAgentSettings` names it by absolute
path, so a `.env` at the repository root, or in whichever directory you
happen to launch from, is never read. The companion agent works the same way
with its own directory's file; there is no shared repository-wide `.env`.

The default config needs BOTH keys: `OPENAI_API_KEY` for STT (Whisper
transcription always runs on OpenAI) and `GEMINI_API_KEY` for the default
`gemini-3.5-flash-lite` command model, reached through Google's
OpenAI-compatible endpoint. Setting `WAKEWORD_AGENT_COMMAND_MODEL` back to a
plain OpenAI model (e.g. `gpt-5-nano`) reverts command execution to
`OPENAI_API_KEY` only — `GEMINI_API_KEY` becomes unnecessary in that case.

Alternatively, export the keys (and any `WAKEWORD_AGENT_*` var) in the
shell — process environment variables always take precedence over `.env`.
The agent fails fast with a clear message if a required key is missing.

On first run, the Silero VAD v5 ONNX model (~2 MB) auto-downloads into
`~/.cache/palmimo/models/` and is cached for later runs. Transcription itself
runs remotely (OpenAI Whisper API), so no speech model is downloaded locally.

The agent line-buffers stdout at startup and sends log records to stderr, so
redirecting output to a file or running it under a service manager (systemd,
nohup) shows each line as it is produced -- `PYTHONUNBUFFERED=1` is no longer
needed.

## Cost & privacy note

There is no local pre-filter: EVERY detected utterance -- including ambient
conversation near the mic that never says the wake word -- is sent to the
OpenAI transcription API. This prioritizes transcription accuracy over a
local, low-latency wake-word detector, at the cost of continuous API usage
and sending audio off-device even when nothing was meant for the robot.

A local recognizer ahead of the API was measured on a Raspberry Pi 5 and is a
real trade rather than an upgrade: it keeps ordinary conversation on the device
and works offline, but costs a 153 MB model download, ~385 MB of resident
memory, and catches fewer calls than the remote path does. The remote path is
the default because of that trade, not because the local one was untried.

## Options

Settings resolve as CLI flag > process env > `.env` file > default. Every
`WAKEWORD_AGENT_*` env var (plus plain `OPENAI_API_KEY` / `GEMINI_API_KEY`)
is also documented in [`.env.sample`](.env.sample).

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--device` | `WAKEWORD_AGENT_DEVICE` | none (system default) | Input device name substring hint (`MicStream(device_name_hint=...)`) |
| `--speaker-device` | `WAKEWORD_AGENT_SPEAKER_DEVICE` | `ReSpeaker` | Output device name substring hint (`SpeakerConfig(device_name_hint=...)`), matched against the ALSA card id or its long name rather than a card index a replug can change; empty or unmatched uses the ALSA default |
| `--command-model` | `WAKEWORD_AGENT_COMMAND_MODEL` | `gemini-3.5-flash-lite` | Chat model used for command tool-calling; a `gemini-` model routes to Google's OpenAI-compatible endpoint (needs `GEMINI_API_KEY`), any other model (e.g. `gpt-5-nano`) uses `OPENAI_API_KEY` |
| `--stt-model` | `WAKEWORD_AGENT_STT_MODEL` | `gpt-4o-mini-transcribe` | Whisper-family transcription model for command text |
| `--language` | `WAKEWORD_AGENT_LANGUAGE` | `en` | ISO 639-1 code driving the Whisper transcription hint, the piper TTS voice, and the reply-language instruction given to the LLM; the SDK speaker currently ships voices for `en` and `ja` (set to `ja` for Japanese) |
| `--tts` / `--no-tts` | `WAKEWORD_AGENT_TTS` (`false` to disable) | on | Speak replies aloud (`--no-tts`: no `Speaker` attached) |
| `--servo` / `--no-servo` | `WAKEWORD_AGENT_SERVO` (`false` to disable) | on | Attach the servo driver (`--no-servo`: compute-only; motions print/compute but don't move a real robot) |
| `--servo-port` | `WAKEWORD_AGENT_SERVO_PORT` | none (auto-detected) | Servo bus serial port, e.g. `/dev/ttyACM0` |

## Hardware notes

On a robot, the servo bus is auto-detected and motions are LIVE by default:
`build_runtime()` builds a `DynamixelDriver(port=settings.servo_port)` and
probes it (`connect()`) before building `Palmimo`, the same way it pre-probes
the `Speaker`. Pass `--no-servo` to force compute-only regardless of what's
attached, or `--servo-port` to pin a specific port instead of auto-detection.
Without hardware attached (or when the port can't be found), the probe fails
and the agent degrades gracefully: it prints a one-line warning ("servo bus
not available -- motions run compute-only (...)") and continues with no
driver attached, exactly like a missing `piper` degrades TTS.

Spoken replies additionally need the `piper` CLI on `PATH` (installed via the
`palmimo-sdk[speech]` extra, part of this project's dependencies) and a voice
model available to piper-plus for the configured language (see
[First-Time Setup for Voice Output](../../../doc/guides/installation.md#first-time-setup-for-voice-output)).
At startup, `build_runtime()` probes piper via
`Speaker.open()` before doing anything else; if piper is missing or fails the
probe, it prints a one-line warning ("piper not available -- spoken replies
disabled") and continues with no `Speaker` attached (`--no-tts` behavior)
instead of crashing. Replies default to English (`SpeakerConfig(lang="en")`);
set `WAKEWORD_AGENT_LANGUAGE=ja` (or `--language ja`) to switch STT, TTS, and
the LLM's reply language to Japanese.

## Limitations

The agent is single-threaded by design: while it is transcribing (Whisper),
running tool calls, or speaking a reply, it is not listening. Mic audio
queued during that stretch is drained and discarded once the agent is ready
to listen again (rather than spliced into whatever utterance comes next),
and a dropped-chunk count
is logged when the queue actually lost audio. In practice this means a
command spoken while the robot is still replying to the previous one will be
missed — wait for the reply to finish before speaking again.
