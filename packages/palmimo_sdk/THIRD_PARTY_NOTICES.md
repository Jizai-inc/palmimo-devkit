# Third-Party Notices

What these lists are, how they were built, and what they deliberately do not
cover is declared at the top of
[`../../THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md). This file travels on its own, so read that
first.

Except for the base dependency section, each section below applies **only**
when the named `palmimo-sdk` extra is installed and used. None of the
components listed are vendored or distributed with `palmimo-sdk` itself — they
are either installed as regular PyPI dependencies of the extra, or, for a model
weights entry, downloaded automatically (or via a documented manual step) into
a local cache directory.

The workspace root declares some of these as direct dependencies of its own, in
which case the root's copy is unconditional and the extra-scoped wording above
does not apply to it — see
[`../../THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md).

## Base dependency

Applies to every install of `palmimo-sdk`, with or without extras.

### pyserial

- License: BSD-3-Clause
- Source: https://github.com/pyserial/pyserial
- The published wheel does not bundle a license file, so installing the
  package does not reproduce the license text. The attribution above points
  at the upstream repository, which carries it.

## `voice` extra

Applies to mic capture + GTCRN noise removal + DTLN-AEC echo cancellation
(`palmimo_sdk.io` / `palmimo_sdk.audio`). The GTCRN and DTLN-AEC model
weights are both downloaded automatically at runtime into a local cache
directory (see `palmimo_sdk.audio.denoise.ensure_denoise_model` and
`palmimo_sdk.audio.dtln.ensure_dtln_models`).

### DTLN-AEC model weights (`dtln_aec_256_1.tflite` + `dtln_aec_256_2.tflite`)

- License: MIT
- Model source: https://github.com/breizhn/DTLN-aec
- Auto-download URL: https://raw.githubusercontent.com/breizhn/DTLN-aec/master/pretrained_models/
- SHA-256 (default `size=256` pair; `size=128` and `size=512` are also
  published upstream and downloadable the same way, see
  `palmimo_sdk.audio.dtln.MODEL_SHA256`):
  - `dtln_aec_256_1.tflite`: `4a3a588b69fd79d837bc068b579a26faa92cac39dddbb00001d2dc1c3d869d60`
  - `dtln_aec_256_2.tflite`: `fa2590243aad1bf893c5be45b20709e8c50feec65e3604d1d52bae6eeddc23d3`

### ai-edge-litert

- License: Apache-2.0
- Source: https://github.com/google-ai-edge/LiteRT

### GTCRN model weights (`gtcrn_simple.onnx`)

- License: MIT
- Model source: https://github.com/Xiaobin-Rong/gtcrn
- Distributed via: k2-fsa/sherpa-onnx releases
- Auto-download URL: https://github.com/k2-fsa/sherpa-onnx/releases/download/speech-enhancement-models/gtcrn_simple.onnx
- SHA-256: `e77603ac0c23dac3227dd2d7135b3a585cbee2679048aecfa886657d3ae1b534`

### sherpa-onnx

- License: Apache-2.0
- Source: https://github.com/k2-fsa/sherpa-onnx
- The wheel ships `dist-info/licenses/LICENSE` and no `NOTICE` file, so
  Apache-2.0 §4(d) propagates nothing further. That `LICENSE` is the
  unmodified Apache-2.0 text with no third-party appendix, which matters
  because the wheel is mostly native code and does bundle a library of its
  own:
- **Bundles ALSA (`libasound`, LGPL-2.1-or-later)**, vendored into
  `sherpa_onnx.libs/` by the wheel build (~5.5 MB), on the `x86_64` and the
  `aarch64` Linux wheels alike. What the wheel does *not* carry is any LGPL
  text: there is no license file for it anywhere in the archive, so this
  notice is the only place it appears at all. How it is linked, as read from
  the extension module's dynamic section: recorded as a `NEEDED` entry,
  resolved through an `$ORIGIN`-relative search path, and renamed by the wheel
  build to a hashed `SONAME`, so substituting a build of your own means
  matching that name rather than dropping in a stock `libasound.so.2`. What
  LGPL-2.1 §6 then requires of a pre-installed image — which of its options
  applies, and the copy of the license it asks to accompany the binary — is
  decided where that image is built, not here.
- Where the ONNX Runtime it runs on comes from depends on the wheel. On the
  `aarch64` wheel this tree ships on, and on the `x86_64` one, it is **not** in
  this wheel: the extension module records `libonnxruntime.so` as a `NEEDED`
  entry and resolves it through the same `$ORIGIN`-relative path, expecting it
  to sit beside them, and `sherpa-onnx-core` is what puts it there (next
  section) — on macOS by the same marker. The `armv7l` wheel is the exception
  and carries its own `sherpa_onnx/lib/libonnxruntime.so` (~20 MB), which is
  what makes it roughly three times the size of the others; it is also the one
  Linux platform with no `sherpa-onnx-core` wheel to supply the runtime.
  Either way the attribution in the next section covers the runtime — that
  section opens by naming the platforms it installs on, but what it attributes
  is the same binary wherever it is linked from.

### sherpa-onnx-core

Only installed on `aarch64` (the Raspberry Pi the robot runs on) and on macOS
— the marker in `palmimo-sdk`'s `voice` extra is
`platform_machine == 'aarch64' or sys_platform == 'darwin'`, because neither
platform has a prebuilt `sherpa-onnx` wheel carrying the runtime. **It is
therefore on the shipping path and absent from an x86_64 development
install**, which is exactly the asymmetry that makes it easy to miss.

The dist carries no Python code at all: the `aarch64` wheel is 17 entries,
three of which are the shared libraries it exists to deliver. That, and the
absence of any license file below, were read from the 1.13.4 `aarch64` wheel's
own archive index rather than from its metadata — the two together are why this
section is the only place the runtime is attributed.

- License: Apache-2.0 (as declared in the wheel's `METADATA`)
- Source: https://github.com/k2-fsa/sherpa-onnx
- **The wheel bundles no license file of any kind** — no `LICENSE`, no
  `NOTICE`, nothing under `dist-info/licenses/`. Installing it does not put
  the Apache-2.0 text on disk, so the attribution has to come from here.
- **Bundles ONNX Runtime** as `sherpa_onnx/lib/libonnxruntime.so` (~34 MB) —
  MIT, Copyright (c) Microsoft Corporation, https://github.com/microsoft/onnxruntime.
  MIT requires its copyright and permission notice to accompany the binary,
  and since the wheel ships neither, this entry is the only place that
  obligation is met. The same runtime is a declared dependency of both example
  agents in its own right (`onnxruntime` on PyPI), attributed there as well.
- Also bundles `libsherpa-onnx-c-api.so` and `libsherpa-onnx-cxx-api.so`,
  which are sherpa-onnx's own code under the Apache-2.0 above.

### sounddevice (PortAudio bindings)

- License: MIT
- Bundles/binds: PortAudio, BSD-3-Clause
- Source: https://github.com/spatialaudio/python-sounddevice

### numpy

- License: BSD-3-Clause
- Source: https://github.com/numpy/numpy

## `speech` extra

Applies to piper-plus text-to-speech (`palmimo_sdk.io.speaker`).

### piper-plus

- License: MIT
- Source: https://github.com/ayutaz/piper-plus

### pyopenjtalk-plus

- License: MIT (the Python bindings/code)
- Source: https://github.com/ayutaz/pyopenjtalk-plus
- Bundles the OpenJTalk Japanese dictionary (`open_jtalk_dic`, used for
  Japanese g2p/pronunciation), which is **not** MIT: it carries a
  Modified BSD (3-clause) notice from the Nara Institute of Science and
  Technology (NAIST) — see `pyopenjtalk/dictionary/COPYING` in the installed
  package — plus BSD-licensed contributions from the UniDic Consortium and
  the Open JTalk project. Redistributing the dictionary in binary form
  requires reproducing that copyright notice.

### SudachiDict-core

Pulled in by `pyopenjtalk-plus` as a required dependency, so it lands with the
`speech` extra. Listed despite being a permissive transitive dependency
because, like `open_jtalk_dic` above, it is a dictionary-data dist rather than
code (the wheel is ~72 MB of compiled dictionary), and data of that kind is
where separately licensed material tends to hide.

- License: Apache-2.0
- Source: https://github.com/WorksApplications/SudachiDict
- The wheel bundles the unmodified Apache-2.0 text as
  `sudachidict_core-*.dist-info/licenses/LICENSE-2.0.txt` and no `NOTICE`
  file, so nothing propagates under §4(d) and the dictionary data carries no
  separate notice of its own inside the dist.

### Voice model weights (downloaded on first use, see [the installation guide](../../doc/guides/installation.md#first-time-setup-for-voice-output))

These ship with neither this package nor piper-plus. The SDK fetches the voice
it is configured to speak with the first time a `Speaker` opens — so a plain
`robot.connect()` with a speaker attached downloads it — into
`~/.cache/palmimo/models/piper/<voice>/`, outside any repository. They carry
their own licenses, separate from the piper-plus/pyopenjtalk-plus code above,
and `ja_JP-tsukuyomi-chan-medium` is the default Japanese voice, so the credit
requirement below applies to a stock install that has never run a download
command by hand.

- **`ja_JP-tsukuyomi-chan-medium`** — licensed under the independent
  `Tsukuyomi-chan Corpus` terms, not a standard OSS license:
  https://tyc.rei-yumesaki.net/material/corpus/. Commercial and
  non-commercial use are both permitted, and the corpus is free to use, but
  publishing synthesized speech in video/audio content or other public
  releases **requires a credit notice** naming the `Tsukuyomi-chan`
  character and linking to the corpus page — see
  [License Notice for `ja_JP-tsukuyomi-chan-medium`](../../doc/guides/installation.md#license-notice-for-ja_jp-tsukuyomi-chan-medium)
  for the required wording.
- **`ja_JP-css10-6lang-medium`** — trained from the CSS10 Japanese corpus by
  the piper-plus project; the model repository publishes it under a
  `css10-public-domain` license tag (source:
  https://huggingface.co/ayousanz/piper-plus-css10-ja-6lang), i.e.
  public-domain-equivalent with no attribution requirement.

## `vision` extra

Applies to head camera capture (`palmimo_sdk.io.camera`).

### opencv-python

- License: Apache-2.0 (the `opencv-python` wheel's own code)
- Source: https://github.com/opencv/opencv-python
- The published wheel bundles third-party binaries alongside the Apache-2.0
  code, most notably **FFmpeg under LGPL-2.1** (plus several other
  LGPL-licensed libraries on some platforms — libbluray, libgnutls,
  libnettle, libhogweed, libintl, libmp3lame, libp11, librtmp, libsoxr,
  libtasn1 on macOS wheels). See `LICENSE-3RD-PARTY.txt` in the installed
  `opencv_python` dist-info for the full list and license texts.
- Beyond that file the wheel ships no `NOTICE`, so Apache-2.0 §4(d) propagates
  nothing further. The same FFmpeg binaries also arrive through
  `opencv-contrib-python` — see
  [`../../examples/agents/companion/THIRD_PARTY_NOTICES.md`](../../examples/agents/companion/THIRD_PARTY_NOTICES.md).

## `hardware` extra

Applies to the Dynamixel servo backend (`palmimo_sdk.io.dynamixel`).

### dynamixel-sdk

- License: Apache-2.0
- Copyright 2017 ROBOTIS CO., LTD.
- Source: https://github.com/ROBOTIS-GIT/DynamixelSDK
- The bundled `LICENSE.txt` is the unmodified Apache-2.0 text and carries no
  copyright line of its own; the line above is the one the package's source
  headers carry (e.g. `dynamixel_sdk/port_handler.py`). The wheel ships no
  `NOTICE` file, so Apache-2.0 §4(d) propagates nothing further.

## `face` extra

Applies to the face-display client (`palmimo_sdk.io.face`). Its only
dependency is `pyserial` — see the base dependency section above.

## `agent` extra

Applies to the LLM tool-calling layer (`palmimo_sdk.agent`).

### pydantic

- License: MIT
- Copyright (c) 2017 to present Pydantic Services Inc. and individual
  contributors.
- Source: https://github.com/pydantic/pydantic

## `mcp` extra

Applies to the MCP server (`palmimo_sdk.mcp`). It builds on the `agent`
extra, so that section's notices apply as well.

### mcp (Model Context Protocol Python SDK)

- License: MIT
- Copyright (c) 2024 Anthropic, PBC
- Source: https://github.com/modelcontextprotocol/python-sdk

### mcp-types

- License: MIT
- Copyright (c) 2024 Anthropic, PBC
- Source: https://github.com/modelcontextprotocol/python-sdk — the same
  repository as `mcp` above; `mcp-types` is published from it as a separate
  dist.

### starlette

- License: BSD-3-Clause
- Source: https://github.com/Kludex/starlette
- Copyright (c) 2018, [Encode OSS Ltd](https://www.encode.io/).
- The repository sits under a maintainer's account rather than the copyright
  holder's; both are as the dist declares them (`Project-URL: Source` and the
  bundled `LICENSE.md`). The same split applies to `uvicorn` below.

### uvicorn

- License: BSD-3-Clause
- Source: https://github.com/Kludex/uvicorn
- Copyright (c) 2017-present, [Encode OSS Ltd](https://www.encode.io/).
