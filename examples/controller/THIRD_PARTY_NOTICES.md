# Third-Party Notices

What these lists are, how they were built, and what they deliberately do not
cover is declared at the top of
[`THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/THIRD_PARTY_NOTICES.md). This file travels on its own
(a copied example has no parent tree), so those links point at the published
repository; read that first.

These notices apply to `palmimo-controller` itself, in addition to whatever the
`palmimo_sdk` extras it depends on already require (see
[`packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)
for the `hardware`/`vision`/`voice` extras it selects). None of the components below
are vendored or distributed with this package — they are installed as
regular PyPI dependencies.

This package is its own standalone uv project, so everything here lands in an
ordinary install of this project's own directory, with no extra selected.
The frontend (`palmimo_controller/static/`) is hand-written vanilla
JS/CSS/HTML with no bundled or CDN-loaded library; the only third-party
content it carries is the two inline SVG icons listed under
[Vendored assets](#vendored-assets).

## Vendored assets

### Phosphor Icons (`hand-waving`, `disco-ball`; bold weight)

- License: MIT
- Copyright (c) 2023 Phosphor Icons
- Source: https://github.com/phosphor-icons/core
- Where: inlined as `<svg>` in `palmimo_controller/static/pilot.html` (the Wave
  and Dance gesture buttons)

## Dependencies

### fastapi

- License: MIT
- Copyright (c) 2018 Sebastián Ramírez
- Source: https://github.com/fastapi/fastapi

`uvicorn` and `typer` are declared here too, but they are already attributed
in full elsewhere in this tree, and a distribution is attributed once, not
once per project that declares it:

- `uvicorn` — [`packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)
  (pulled in there by the `mcp` extra).
- `typer` — [`examples/agents/wakeword/THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/examples/agents/wakeword/THIRD_PARTY_NOTICES.md)
  or [`examples/agents/companion/THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/examples/agents/companion/THIRD_PARTY_NOTICES.md).

`uvicorn[standard]` additionally pulls in `httptools`, `uvloop`, `watchfiles`,
`websockets`, and `python-dotenv` as its own transitive dependencies (not
declared directly by this package, so outside this file's scope per the
policy note at the top) — all four permissively licensed (MIT/Apache-2.0/BSD),
none copyleft.

AI mode adds `websockets`, `pydantic`, `numpy`, and `scipy` as direct
dependencies. All are already attributed in full elsewhere in this tree:

- `websockets`, `scipy` — [`examples/agents/companion/THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/examples/agents/companion/THIRD_PARTY_NOTICES.md).
  Note its `scipy` entry: the wheel's `LICENSE.txt` carries the GCC Runtime
  Library Exception for the bundled OpenBLAS binary and must stay in any
  image that ships `scipy`.
- `pydantic`, `numpy` — [`packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)
  (the `agent` and `voice` extras).

### tqdm

Not declared directly; reached transitively via `palmimo-sdk[voice]` ->
`ai-edge-litert` -> `tqdm`. Listed here because it is weak copyleft.

- License: **MPL-2.0 AND MIT**
- Source: https://github.com/tqdm/tqdm

The package is licensed per file, as stated in the `LICENCE` bundled with the
wheel. tqdm is used unmodified and this package does not distribute it — it
is installed from PyPI. A pre-installed image must still keep
`site-packages/tqdm/*.py` (Source Code Form) and `tqdm-*.dist-info/LICENCE`
in place, the same two conditions the root
[`THIRD_PARTY_NOTICES.md`](https://github.com/Jizai-inc/palmimo-devkit/blob/main/THIRD_PARTY_NOTICES.md#tqdm)
spells out in full.
