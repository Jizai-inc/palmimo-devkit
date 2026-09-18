# Third-Party Notices

What these lists are, how they were built, and what they deliberately do not
cover is declared at the top of
[`THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/THIRD_PARTY_NOTICES.md). This file travels on its own
(a copied example has no parent tree), so those links point at the published
repository; read that first.

These notices apply to `palmimo-wakeword-agent` itself, in addition to
whatever the `palmimo_sdk` extras it depends on already require (see
[`packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)
for the `voice`/`speech`/`hardware` extras it selects). None of the components
below are vendored or distributed with this package — the dependencies are
installed from PyPI, and the Silero VAD model is downloaded automatically at
runtime into a local cache directory (see
`palmimo_wakeword_agent.vad.SileroVad.load`), the same way
`palmimo_sdk.audio.denoise` resolves the GTCRN model.

This package is its own standalone uv project, so everything here lands in an
ordinary install of this project's own directory, with no extra selected.
`numpy` is declared here too, but it is a `palmimo-sdk` dependency as well and
is attributed in its file above.

## Dependencies

### openai

- License: Apache-2.0
- Source: https://github.com/openai/openai-python
- The wheel bundles the unmodified Apache-2.0 text and no `NOTICE` file, so
  §4(d) propagates nothing further.

### certifi

Not declared directly; reached transitively via `openai` -> `httpx` ->
`certifi`. Listed here because it is weak copyleft.

- License: **MPL-2.0**
- Source: https://github.com/certifi/python-certifi
- Also bundles the Mozilla CA root store as data, so the notice covers that
  bundle as well as the code.
- MPL-2.0 applies per file and does not reach the code that imports it.
  `certifi` is used unmodified, so the only obligation is keeping
  `site-packages/certifi/` (including `cacert.pem`, the data the notice
  covers) and `certifi-*.dist-info/LICENSE` on any image that ships it,
  mirroring `tqdm`'s section below.

### tqdm

Not declared directly; reached transitively via `openai` -> `tqdm`.

- License: **MPL-2.0 AND MIT**
- Source: https://github.com/tqdm/tqdm

The package is licensed per file, as stated in the `LICENCE` bundled with the
wheel and reproduced verbatim below. The paths are upstream's own and reflect
its historical layout — `tqdm/_tqdm.py` was renamed to `tqdm/std.py` and no
longer exists under that name:

- files `*` — MPL-2.0, 2015-2024 (c) Casper da Costa-Luis
- file `tqdm/_tqdm.py` — MIT, 2016 (c) [PR #96] on behalf of Google Inc.
- files `tqdm/_tqdm.py`, `README.rst`, `.gitignore` — MIT, 2013 (c) Noam
  Yorav-Raphael

MPL-2.0 is a file-level (weak) copyleft: it reaches only the MPL-covered files
themselves, not the code that imports them. tqdm is used unmodified, so §3.2's
obligation to publish modifications never arises, and this package does not
distribute tqdm at all — it is installed from PyPI. A pre-installed image
must still keep `site-packages/tqdm/*.py` (Source Code Form) and
`tqdm-*.dist-info/LICENCE` in place, the same two conditions the root
[`THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/THIRD_PARTY_NOTICES.md#tqdm)
spells out in full.

### onnxruntime

- License: MIT
- Author: Microsoft Corporation
- Source: https://onnxruntime.ai — the project home page, which is what the
  dist declares; it publishes no repository URL of its own.
- The published wheel bundles no license file, so installing the package does
  not reproduce the license text — the same situation as `pyserial`.

### pydantic-settings

- License: MIT
- Copyright (c) 2022 Samuel Colvin and other contributors
- Source: https://github.com/pydantic/pydantic-settings

### typer

- License: MIT
- Copyright (c) 2019 Sebastián Ramírez
- Source: https://github.com/fastapi/typer

`onnxruntime`, `pydantic-settings`, and `typer` are declared by the companion
example as well, so they also appear in
[`examples/agents/companion/THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/examples/agents/companion/THIRD_PARTY_NOTICES.md).
Each file lists what its own package declares, so either one stands alone.

## Model weights

### Silero VAD model weights (`silero_vad.onnx`)

- License: MIT
- Model source: https://github.com/snakers4/silero-vad (v5.1.2 tag)
- Auto-download URL: https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx
- SHA-256: `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`
- Both the code and the model weights in the upstream repo are MIT-licensed.
