# Third-Party Notices

What these lists are, how they were built, and what they deliberately do not
cover is declared at the top of
[`../../THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md). This file travels on its own, so read that
first.

These notices apply to `palmimo-teleop` itself, in addition to whatever the
`palmimo_sdk` extras it depends on already require (see
[`../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)
for the `hardware`/`vision` extras it selects). None of the components below
are vendored or distributed with this package — they are installed as
regular PyPI dependencies.

This package is an unconditional dependency of the workspace root, so
everything here lands in a default install of the tree, with no extra
selected. The frontend (`palmimo_teleop/static/`) is hand-written vanilla
JS/CSS/HTML with no bundled or CDN-loaded library; the only third-party
content it carries is the two inline SVG icons listed under
[Vendored assets](#vendored-assets).

## Vendored assets

### Phosphor Icons (`hand-waving`, `disco-ball`; bold weight)

- License: MIT
- Copyright (c) 2023 Phosphor Icons
- Source: https://github.com/phosphor-icons/core
- Where: inlined as `<svg>` in `palmimo_teleop/static/pilot.html` (the Wave
  and Dance gesture buttons)

## Dependencies

### fastapi

- License: MIT
- Copyright (c) 2018 Sebastián Ramírez
- Source: https://github.com/fastapi/fastapi

`uvicorn` and `typer` are declared here too, but they are already attributed
in full elsewhere in this tree, and a distribution is attributed once, not
once per project that declares it:

- `uvicorn` — [`../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](../../packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)
  (pulled in there by the `mcp` extra).
- `typer` — [`../agents/wakeword/THIRD_PARTY_NOTICES.md`](../agents/wakeword/THIRD_PARTY_NOTICES.md)
  or [`../agents/companion/THIRD_PARTY_NOTICES.md`](../agents/companion/THIRD_PARTY_NOTICES.md).

`uvicorn[standard]` additionally pulls in `httptools`, `uvloop`, `watchfiles`,
`websockets`, and `python-dotenv` as its own transitive dependencies (not
declared directly by this package, so outside this file's scope per the
policy note at the top) — all four permissively licensed (MIT/Apache-2.0/BSD),
none copyleft.
