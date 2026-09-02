# Third-Party Notices

What these lists are, how they were built, and what they deliberately do not
cover is declared at the top of
[`../../../THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md). This file travels on its own, so read that
first.

These notices apply to `palmimo-companion-agent` itself, in addition to
whatever the `palmimo_sdk` extras it depends on already require (see
[`../../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](../../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)
for the `voice`/`vision`/`agent` extras it selects). Nothing below is vendored
or distributed with this package — the dependencies are installed from PyPI,
and each model weights entry is downloaded automatically at runtime into a
local cache directory.

This package is an unconditional dependency of the workspace root, so
everything here lands in a default install of the tree, with no extra selected.
`numpy` and `opencv-python` are declared here too, but they are `palmimo-sdk`
dependencies as well and are attributed in its file above.

## Dependencies

### litellm

- License: MIT
- Copyright (c) 2023 Berri AI
- Source: https://github.com/BerriAI/litellm
- The bundled `LICENSE` opens by carving out an `enterprise/` directory under a
  separate license "if that directory exists". It does not exist in the
  published wheel (no `litellm/enterprise/*` entries in its `RECORD`), so what
  is installed here is MIT in full.

### pydantic-settings

- License: MIT
- Copyright (c) 2022 Samuel Colvin and other contributors
- Source: https://github.com/pydantic/pydantic-settings

### onnxruntime

- License: MIT
- Author: Microsoft Corporation
- Source: https://onnxruntime.ai — the project home page, which is what the
  dist declares; it publishes no repository URL of its own.
- The published wheel bundles no license file, so installing the package does
  not reproduce the license text — the same situation as `pyserial`.

### typer

- License: MIT
- Copyright (c) 2019 Sebastián Ramírez
- Source: https://github.com/fastapi/typer

### textual

- License: MIT
- Copyright (c) 2021 Will McGugan
- Source: https://github.com/Textualize/textual

### websockets

- License: BSD-3-Clause
- Copyright (c) Aymeric Augustin and contributors
- Source: https://github.com/python-websockets/websockets
- Pure Python and dependency-free; the wheel bundles no third-party material
  beyond its own `LICENSE`.

### scipy

- License: BSD-3-Clause
- Copyright (c) 2001-2002 Enthought, Inc.; 2003- SciPy Developers
- Source: https://github.com/scipy/scipy
- The wheel's `LICENSE.txt` is not just SciPy's own terms: it is a combined
  document that also reproduces the licenses of the native libraries bundled
  into the binary distribution, each under a `Name:` / `License:` heading.
  Those are **OpenBLAS** (BSD-3-Clause, (c) 2011-2014 The OpenBLAS Project),
  **LAPACK** (BSD-3-Clause-Open-MPI, (c) 1992-2013 The University of Tennessee
  and others), bundled inside it, and the **GCC runtime library / libgfortran,
  `GPL-3.0-or-later WITH GCC-exception-3.1`** ((c) 2002-2017 Free Software
  Foundation), statically linked into the same OpenBLAS binary.
- The GCC Runtime Library Exception is what permits shipping that binary as
  part of a non-GPL image, and its text is reproduced in the same
  `LICENSE.txt`. That file must therefore stay reachable in any image that
  ships `scipy` and must not be pruned — the same requirement `matplotlib`
  carries below.

### mediapipe

- License: Apache-2.0
- Source: https://github.com/google/mediapipe
- The wheel's `LICENSE` is the Apache-2.0 text plus one appended third-party
  notice, for files under `tasks/cc/text/language_detector/custom_ops/utils/utf/`:
  authored by Rob Pike and Ken Thompson, Copyright (c) 2002 by Lucent
  Technologies, under permissive terms that require *this entire notice* to be
  included in all copies. Keeping the wheel's `LICENSE` reachable satisfies
  both it and Apache-2.0 §4(d); the wheel ships no separate `NOTICE` file.

### opencv-contrib-python

Not declared here — it arrives as a required dependency of `mediapipe`, so it
is installed whenever this package is. It is listed because it bundles
third-party binaries that its own declared license does not describe.

- License: Apache-2.0 (the `opencv-contrib-python` wheel's own code)
- Source: https://github.com/opencv/opencv-python
- The wheel bundles **FFmpeg under LGPL-2.1**, plus the same set of
  LGPL-licensed libraries as `opencv-python` on some platforms. See
  `LICENSE-3RD-PARTY.txt` in the installed `opencv_contrib_python` dist-info
  for the full list and license texts. This is the same bundled FFmpeg noted
  under `opencv-python` in
  [`../../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](../../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md),
  reaching the install a second way.

### matplotlib

Also not declared here — another required dependency of `mediapipe`, listed for
the same reason as `opencv-contrib-python`: it ships third-party material its
own license does not cover.

- License: the matplotlib license (PSF-derived, BSD-compatible)
- Source: https://github.com/matplotlib/matplotlib
- The wheel's `LICENSE` is not just that license: it is a combined document
  (~1250 lines) that also reproduces the terms of the fonts and native
  libraries statically bundled into the wheel, each under a `Name:` /
  `License:` heading. Among them are the AMS, BaKoMa/Computer Modern, Last
  Resort and STIX fonts (OFL-1.1 and the BaKoMa licence), Courier 10
  (Bitstream-Charter), HarfBuzz, libraqm, QHull, SheenBidi and **FreeType,
  which is dual-licensed `FTL OR GPL-2.0-or-later`** — used here under the
  FTL option, which is permissive and requires only credit in the
  documentation.
- The fonts also carry their own copies of these terms next to the files, in
  `matplotlib/mpl-data/fonts/ttf/LICENSE_DEJAVU` (Bitstream Vera, (c) 2003
  Bitstream, Inc.) and `LICENSE_STIX` (SIL OFL-1.1, (c) 2001-2010 the STI Pub
  Companies). Both texts require the notice to travel with the fonts, so
  neither the wheel's `LICENSE` nor these files may be pruned from an image
  that ships the package.

`mediapipe`'s remaining dependencies — `absl-py`, `attrs`, `flatbuffers`,
`jax`, `jaxlib`, `protobuf`, `sentencepiece`, and `sounddevice` — are all
permissive, and inspecting the wheel resolved by `uv.lock` for each found no
third-party license text bundled outside its own `dist-info`. (`sounddevice`
bundles PortAudio, which is attributed in the `voice` extra's section of the
`palmimo_sdk` file linked above.)

## Model weights

### Silero VAD model weights (`silero_vad.onnx`)

- License: MIT
- Model source: https://github.com/snakers4/silero-vad (v5.1.2 tag)
- Auto-download URL: https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx
- SHA-256: `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`
- Both the code and the model weights in the upstream repo are MIT-licensed.
- Downloaded by `palmimo_companion_agent.pipeline.vad.SileroVad.load` into the shared
  Palmimo model cache — the same model the wake-word example uses, but
  downloaded independently (this project deliberately does not import the
  wake-word example's code).

### MediaPipe HandLandmarker model weights (`hand_landmarker.task`)

- License: Apache-2.0
- Model source: https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task
- Part of Google's MediaPipe Solutions model zoo (https://ai.google.dev/edge/mediapipe),
  published under the Apache License 2.0.
- Downloaded automatically by
  `palmimo_companion_agent.core.vision.WaveDetector` into the user's home
  directory (`~/hand_landmarker.task`) on first use; drives the wave-back
  reflex.

### MediaPipe FaceDetector model weights (`blaze_face_short_range.tflite`)

- License: Apache-2.0
- Model source: https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite
- Same MediaPipe Solutions model zoo and license as the HandLandmarker model
  above.
- Downloaded automatically by
  `palmimo_companion_agent.core.vision.FaceLocator` into the user's home
  directory (`~/blaze_face_short_range.tflite`) on first use; drives
  `palmimo_companion_agent.core.tools.LookAtFaceTool`'s face-tracking.
