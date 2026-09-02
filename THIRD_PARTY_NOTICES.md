# Third-Party Notices

**What this list is.** The notices in this tree itemize the dependencies each
project here *declares*, classified by the license each distribution declares
for itself. They are not an inventory of everything an installed environment
ends up containing: a wheel can bundle third-party binaries that its own
metadata never mentions, and what follows records the ones that have been
found rather than asserting there are no others. A complete attribution for a
pre-installed image will be produced from the resolved lockfile as a separate
artifact rather than maintained here, and nothing here substitutes for it.

These notices cover two sets: the direct dependencies of the
`palmimo-devkit-software` workspace root itself, and any weak-copyleft
transitive dependency that no workspace member's own file claims — wherever in
the graph that one is reached from. Everything else reached through a member is
covered by that member's own file:

- [`packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)
- [`examples/agents/companion/THIRD_PARTY_NOTICES.md`](examples/agents/companion/THIRD_PARTY_NOTICES.md)
- [`examples/agents/wakeword/THIRD_PARTY_NOTICES.md`](examples/agents/wakeword/THIRD_PARTY_NOTICES.md)

The root also declares `dynamixel-sdk` and `pyserial` directly, and both are
`palmimo-sdk` dependencies as well, so the attribution text that applies to
them is the one in its file above. The scope wording there differs, though:
`pyserial` is a `palmimo-sdk` base dependency, so its section is unconditional
and matches the root exactly, whereas `dynamixel-sdk` sits under the `hardware`
extra, and that file scopes each extra's section to "only when the named extra
is installed and used". From the root, `dynamixel-sdk` is an unconditional
direct dependency: it is installed with or without that extra selected.

None of the components below are vendored or distributed with this repository
— they are installed as regular PyPI dependencies.

The list covers direct dependencies plus any transitive dependency whose own
license is copyleft, however weak. Every dist in `uv.lock` was checked against
that rule by its declared license; `tqdm` and `certifi` are the only two that
meet it. `pathspec` (MPL-2.0) is reached only through `mypy` in the dev group,
so it never ships. That count is about declared licenses only — copyleft
material bundled inside a wheel does not appear in one, and three such cases
are recorded below: FFmpeg under each of the two OpenCV dists, ALSA under
`sherpa-onnx`, and the dual-licensed FreeType under `matplotlib`.

That check reads what each dist declares for itself, so it cannot see
third-party material bundled inside a wheel, which carries its own terms. That
was searched for separately: for every dist with a section in one of these four
files, the wheel resolved by `uv.lock` was inspected for license texts other
than the dist's own, and each hit is recorded at that dist's section. The hits
recorded so far are FFmpeg (LGPL-2.1) under both `opencv-python` and
`opencv-contrib-python`, the OpenJTalk dictionary (Modified BSD, NAIST) under
`pyopenjtalk-plus`, a Plan 9 / Lucent Technologies notice appended to
`mediapipe`'s own license, the fonts and native libraries — including
dual-licensed FreeType — reproduced in `matplotlib`'s, PortAudio
(BSD-3-Clause) under `sounddevice`, ALSA (LGPL-2.1-or-later) under
`sherpa-onnx`, and ONNX Runtime (MIT, Microsoft) under `sherpa-onnx-core`.

Two things about that list. It is open rather than closed — the wording above
says what has been found, not what exists, and the two scoping limits below say
why that distinction is not pedantic. And a wheel is a per-platform artifact:
the inspection was done against the wheels `uv.lock` resolves for **linux
`x86_64` and `aarch64`**, `aarch64` being what the product runs on. Where a
section below names macOS wheels, that comes from the upstream license file
those dists carry, not from opening a macOS wheel. A dist whose marker excludes
the machine you resolve on is invisible to a scan run there, which is how `sherpa-onnx-core` — `aarch64` and macOS only, and the
carrier for a 34 MB ONNX Runtime — went unrecorded until it was looked for
deliberately.

Note what that scoping means: a wheel is inspected only once its dist already
has a section, so the rule above decides what gets looked at, and a dist that
declares a permissive license for itself can still bundle copyleft material.
`opencv-contrib-python` is the case that exposed this — it arrives
transitively through `mediapipe`, declares Apache-2.0, and was therefore never
selected, yet it bundles the same LGPL-2.1 FFmpeg as `opencv-python`. It and
`matplotlib` were added to
[`examples/agents/companion/THIRD_PARTY_NOTICES.md`](examples/agents/companion/THIRD_PARTY_NOTICES.md)
for that reason; a future dependency added on the same footing needs the same
manual look.

## tqdm

Used by `scripts/diagnose_servos.py` for progress bars.

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
obligation to publish modifications never arises, and this repository does not
distribute tqdm at all — it is installed from PyPI.

That last point stops holding for a pre-installed image (an SD card shipped
with the environment already populated is a distribution in Executable Form
under §3.2). Two conditions keep such an image compliant without any extra
work, and both are easy to break silently:

- **Keep the Python sources on the image.** `site-packages/tqdm/*.py` *is*
  Source Code Form, so an ordinary install satisfies §3.2(a) by construction.
  Shipping only `.pyc`, or a frozen bundle (PyInstaller, Nuitka) with sources
  stripped, removes that and re-arms the obligation.
- **Keep the wheel's license file on the image.** §3.1 requires the license
  notice to travel with the code, which here means leaving
  `tqdm-*.dist-info/LICENCE` in place rather than pruning `dist-info`
  directories to save space.

## certifi

Reached only through workspace members, by two routes that both start at the
example agents: `openai` / `litellm` → `httpx` → `certifi` (`httpx` is
also reached a third way, `litellm` → `tokenizers` → `huggingface-hub` →
`httpx`), and `litellm` → `tiktoken` → `requests` → `certifi`. `palmimo-sdk`'s
own closure does not contain it on any path — the `speech` extra reaches
`nltk`, whose dependencies are `click`, `defusedxml`, `joblib`, `regex` and
`tqdm`, and no `requests`. It is listed here rather than in a member file
because it is weak copyleft that no member file claims — the second of the two
sets this file covers. It is part of the default resolution, with no extra
selected.

- License: **MPL-2.0**
- Source: https://github.com/certifi/python-certifi

`certifi` also bundles the Mozilla CA root store as data, so the notice covers
that bundle as well as the code.

MPL-2.0 applies per file and does not reach the code that imports it. certifi
is used unmodified, so the obligation is the same as for tqdm, and so are the
two image conditions listed under it: `site-packages/certifi/` is Source Code
Form (including `cacert.pem`, the data the notice covers), and
`certifi-*.dist-info/LICENSE` must stay on the image.
