# Third-Party Notices

**What this list is.** The notices in this tree itemize the dependencies each
project here *declares*, classified by the license each distribution declares
for itself. They are not an inventory of everything an installed environment
ends up containing: a wheel can bundle third-party binaries that its own
metadata never mentions, and what follows records the ones that have been
found rather than asserting there are no others. A complete attribution for a
pre-installed image will be produced from the resolved lockfile as a separate
artifact rather than maintained here, and nothing here substitutes for it.

These notices cover the direct dependencies of the `palmimo-devkit-software`
workspace root itself, plus any weak-copyleft transitive dependency reached
from its own `uv.lock` that `packages/palmimo_sdk`'s own file does not already
claim (`packages/*` is this workspace's only member; each example under
`examples/` is a standalone project outside it, with its own `uv.lock`).
`tests/contracts/test_notice_contracts.py` runs a curated-list check against
that same lock for every such example — every distribution on a fixed
weak-copyleft list (`tqdm`, `certifi`) that the example's own `uv.lock`
resolves must have a matching section in that example's own notice file
below — as a CI contract rather than the manual sweep this file's own list
was built from:

- [`packages/palmimo_sdk/THIRD_PARTY_NOTICES.md`](packages/palmimo_sdk/THIRD_PARTY_NOTICES.md)
- [`examples/agents/companion/THIRD_PARTY_NOTICES.md`](examples/agents/companion/THIRD_PARTY_NOTICES.md)
- [`examples/agents/wakeword/THIRD_PARTY_NOTICES.md`](examples/agents/wakeword/THIRD_PARTY_NOTICES.md)
- [`examples/teleop/THIRD_PARTY_NOTICES.md`](examples/teleop/THIRD_PARTY_NOTICES.md)

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
license is copyleft, however weak. Every dist in this workspace's own
`uv.lock` was checked against that rule by its declared license; `tqdm` is the
only one that meets it (`certifi`, reached the same way before the examples
moved out to their own standalone projects, no longer resolves into this
workspace's `uv.lock` at all -- see
[`examples/agents/companion/THIRD_PARTY_NOTICES.md`](examples/agents/companion/THIRD_PARTY_NOTICES.md#certifi)
for where that attribution lives now). `pathspec` (MPL-2.0) is reached only
through `mypy` in the dev group, so it never ships. That count is about
declared licenses only — copyleft material bundled inside a wheel does not
appear in one, and cases of that are recorded in whichever of these four files
covers the dist that bundles it: FFmpeg under each of the two OpenCV dists,
ALSA under `sherpa-onnx`, and the dual-licensed FreeType under `matplotlib`.

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
