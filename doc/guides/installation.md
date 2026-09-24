# Installation

Run every command here from the repository root — the directory containing the
README. Prerequisites: **Python 3.12+** and [**uv**](https://docs.astral.sh/uv/).

## Expected Environment

- Execution host: **Raspberry Pi** (the default assumption) — the control loop and servo writes run here end to end.
- Development machine: PC (macOS / Linux) — editing, launching, and log viewing; it is not involved in control.
- Interface board: the servo bus's USB-to-servo bridge

For the full execution model, see [architecture.md](../explanation/architecture.md) (the authoritative design doc).
For the Raspberry Pi build steps, see [raspberry-pi-setup.md](raspberry-pi-setup.md).

## Resolving Dependencies

```bash
uv sync
```

This installs the SDK with every optional extra, because `uv sync` includes
the `dev` dependency group and that group asks for all of them: the shared mic
stream (`MicStream`), GTCRN noise removal (`palmimo_sdk.audio`), piper-plus
TTS, the head camera, and the MCP server. With `--no-dev` only the core
(motor control) installs; `palmimo_sdk`'s own extras are forwarded under the
same names at the workspace root (e.g. `uv sync --no-dev --extra voice`), the
same way they work for anyone using `palmimo_sdk` as a standalone library.

The examples under `examples/` (the wake-word agent, the companion agent,
`palmimo_teleop`) are each their own standalone uv project, not a member of
this workspace — `uv sync` here does not install their dependencies. See
[examples/README.md](../../examples/README.md) for how to install and run one.

### Using `palmimo_sdk` as a library, outside this clone

Everything above sets up this repository's own workspace. To use
`palmimo_sdk` in another project instead, add the published package
directly:

```bash
uv add palmimo-sdk
```

See [`packages/palmimo_sdk/README.md`](../../packages/palmimo_sdk/README.md)
for the extras (`hardware`, `voice`, `agent`, ...) and a minimal example.

## Voice Output (piper-plus TTS)

Installing the workspace brings in piper-plus, the text-to-speech engine
`palmimo_sdk.audio` uses for spoken output. The voice models themselves are
not bundled with it, and you do not need to fetch them by hand:
`Speaker.open()` (which `robot.connect()` calls for you when a speaker is
attached) fetches whichever voice it needs the first time it needs it.
Expect the first run to take a minute or so per voice on a slow link — about
38 MB each — and to need network access; every run after that finds the
voice on disk.

### First-Time Setup for Voice Output

Voice models and the NLTK data needed for English phonemization auto-download
on first use, so no manual step is needed on a networked machine. They are
cached together under `$XDG_CACHE_HOME/palmimo/models` (or
`~/.cache/palmimo/models` when `XDG_CACHE_HOME` is unset). This works with the
restricted home directory used by the Palmimo Portal service.

For an offline machine, run the NLTK downloader on a networked machine and
copy the resulting `nltk_data` directory to the same cache location:

```bash
uv run python -m nltk.downloader \
  --download-dir ~/.cache/palmimo/models/nltk_data \
  averaged_perceptron_tagger averaged_perceptron_tagger_eng cmudict
```

For an offline machine, fetch a voice on a networked one and copy its whole
directory across, or pre-fetch it explicitly:

```bash
uv run piper --download-model ja_JP-tsukuyomi-chan-medium \
  --download-dir ~/.cache/palmimo/models/piper/ja_JP-tsukuyomi-chan-medium
```

### Where Voices Are Cached

Voices are cached outside the repository, one directory per voice:

```
~/.cache/palmimo/models/piper/ja_JP-tsukuyomi-chan-medium/   # $XDG_CACHE_HOME is honored
~/.cache/palmimo/models/piper/ja_JP-css10-6lang-medium/      # (English uses the multilingual model)
~/.cache/palmimo/models/nltk_data/                            # English phonemization data
```

The directory per voice is load-bearing: piper-plus's catalogue voices all
name their config `config.json`, so voices sharing one directory overwrite
each other's config. Only the per-voice directory is searched — piper's own
flat layout is not, precisely because it mispairs. `PiperEngine(data_dir=...)`
moves the root elsewhere; the layout inside it is the same.

### Per-Language Download Behavior

`Speaker` downloads only the voice it opens with (`SpeakerConfig(lang=...)`),
and only at `open()`. The other language is reported as missing with a
warning; nothing fetches it later, because downloading mid-utterance would
stall speech and hold up `disconnect()`. To speak both — `say(lang=...)` or
`say_bilingual` — pre-fetch the second voice with the offline command above.

### Upgrading From an Older Checkout

Models sitting in the repository root, or flat in a `data_dir`, are no
longer found. Move each voice's `.onnx` and its config into a directory
named after the voice, or just let the first run download it again.

### Disk Usage

Budget ~115 MB of disk per voice, not 38: the first time a voice is
*loaded*, piper-plus writes an ORT-optimized copy of the model
(`*.cpu.opt.onnx`, ~75 MB) beside it and reuses that on later runs.
`PIPER_DISABLE_CACHE=1` turns it off at the cost of a slower load.

### License Notice for `ja_JP-tsukuyomi-chan-medium`

This voice carries its own license, independent of piper-plus /
pyopenjtalk-plus — commercial and non-commercial use are both free, but
publishing this voice's output in a video, stream, or other public release
requires a credit notice. See the official terms and required wording at
https://tyc.rei-yumesaki.net/material/corpus/ (details:
[`packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)).

## Troubleshooting

| Error | Fix |
|--------|------|
| `ModuleNotFoundError: dynamixel_sdk` | `uv sync` |
| `ModuleNotFoundError: sounddevice` / `sherpa_onnx` | `uv sync` (inside the example's own directory if the error came from running one); `uv sync --extra voice` when using `palmimo_sdk` standalone |
| `ModuleNotFoundError: cv2` / `mediapipe` | `uv sync` (inside the example's own directory if the error came from running one); `uv sync --extra vision` when using `palmimo_sdk` standalone |
| `RuntimeError: piper-plus is not installed` | `uv sync`; installs piper-plus via `palmimo-sdk[speech]` |
| `RuntimeError: failed to download piper voice model ...` | The first-run voice download could not reach the network. Connect, or copy the voice directory in as described above. |
| `RuntimeError: no audio player available` | `Speaker` plays synthesized speech via `aplay`/`ffplay` (Linux) or `afplay` (macOS) — install one (e.g. `sudo apt install alsa-utils`) |

## Related Documents

- [Raspberry Pi setup](raspberry-pi-setup.md) — preparing the control host
- [Controlling motions](controlling-motions.md) — the first thing to run once installed
- [System architecture](../explanation/architecture.md) — what runs where, and why
