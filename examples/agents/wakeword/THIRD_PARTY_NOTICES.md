# Third-Party Notices

What these lists are, how they were built, and what they deliberately do not
cover is declared at the top of
[`../../../THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md). This file travels on its own, so read that
first.

These notices apply to `palmimo-wakeword-agent` itself, in addition to
whatever the `palmimo_sdk` extras it depends on already require (see
[`../../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](../../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)
for the `voice`/`speech`/`hardware` extras it selects). None of the components
below are vendored or distributed with this package — the dependencies are
installed from PyPI, and the Silero VAD model is downloaded automatically at
runtime into a local cache directory (see
`palmimo_wakeword_agent.vad.SileroVad.load`), the same way
`palmimo_sdk.audio.denoise` resolves the GTCRN model.

This package is an unconditional dependency of the workspace root, so
everything here lands in a default install of the tree, with no extra selected.
`numpy` is declared here too, but it is a `palmimo-sdk` dependency as well and
is attributed in its file above.

## Dependencies

### openai

- License: Apache-2.0
- Source: https://github.com/openai/openai-python
- The wheel bundles the unmodified Apache-2.0 text and no `NOTICE` file, so
  §4(d) propagates nothing further.

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
[`../companion/THIRD_PARTY_NOTICES.md`](../companion/THIRD_PARTY_NOTICES.md).
Each file lists what its own package declares, so either one stands alone.

## Model weights

### Silero VAD model weights (`silero_vad.onnx`)

- License: MIT
- Model source: https://github.com/snakers4/silero-vad (v5.1.2 tag)
- Auto-download URL: https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx
- SHA-256: `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`
- Both the code and the model weights in the upstream repo are MIT-licensed.
