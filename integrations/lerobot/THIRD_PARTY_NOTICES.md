# Third-Party Notices

These notices apply to the two LeRobot plugins in this workspace. None of the
components below are vendored or distributed with them — every one is resolved
at install time by uv from `uv.lock`, so a user who installs this workspace
receives it from its own upstream.

Listed here are the framework this workspace extends and the dependencies whose
terms a downstream integrator has to weigh: copyleft, proprietary, and anything
whose effective license differs from the Python package's own. The remainder of
the resolved tree is permissive (Apache-2.0, MIT, BSD, PSF) and retains its own
license and copyright notice within its distribution.

This inventory was taken from `uv.lock` by reading each pinned package's license
metadata from PyPI. Retake it when the lock changes — new transitive
dependencies arrive without any edit to this workspace's own `pyproject.toml`.

## LeRobot

This workspace exists to extend LeRobot, and both plugins import it. It is
pinned by git tag rather than taken from PyPI, so its own Apache-2.0 NOTICE
travels with the checkout uv resolves.

### lerobot

- License: Apache-2.0
- Copyright: The HuggingFace Inc. team
- Source: https://github.com/huggingface/lerobot
- Pinned as: `{ git = "https://github.com/huggingface/lerobot", tag = "v0.5.1" }`

## Copyleft dependencies reached through LeRobot

None of these are imported by Palmimo code, vendored here, or statically linked.
Each is an ordinary Python package that uv installs and that LeRobot imports at
runtime. They stay replaceable, which is the condition the licenses below are
being relied on under.

### pynput

- License: LGPLv3
- Source: https://github.com/moses-palmer/pynput
- Reached through: LeRobot's `KeyboardTeleop`, which the Palmimo teleoperator
  uses for keyboard input. Palmimo code never imports pynput directly.
- Decision: kept rather than replaced. The LGPL component is not bundled and
  stays replaceable, which is what the license is being relied on under. The
  alternative weighed was sshkeyboard (MIT), which would require a LeRobot fork
  or an upstream pull request to swap in — overhead out of proportion to the
  benefit.

### python-xlib

- License: LGPLv2+
- Source: https://github.com/python-xlib/python-xlib
- Reached through: pynput's X11 backend. Installed on Linux only
  (marker: `'linux' in sys_platform`).

### pyyaml-include

- License: GPLv3+
- Source: https://github.com/tanbro/pyyaml-include
- Reached through: LeRobot's configuration parser draccus, which requires it.
- This is the strongest copyleft term in the resolved tree. It is recorded here
  so that whoever ships or redistributes an environment built from this lock
  file evaluates it rather than discovering it. The package is pure Python, so
  an install places its source on disk alongside the GPLv3 text.

### certifi, pathspec, tqdm

- License: MPL-2.0 (tqdm is dual-licensed MPL-2.0 AND MIT)
- Weak copyleft, file-scoped. All three arrive transitively and are unmodified.

## NVIDIA CUDA redistributables

PyTorch pulls the CUDA runtime components (`nvidia-*-cu12`, `cuda-bindings`) on
Linux x86_64 only (marker:
`platform_machine == 'x86_64' and sys_platform == 'linux'`). A Raspberry Pi
(aarch64) install resolves none of them.

- License: NVIDIA proprietary terms — **not** open source. The declared license
  varies per component.
- Terms: https://docs.nvidia.com/cuda/eula/

That enumeration covers the CUDA redistributables themselves. One further
NVIDIA-authored package rides along with them — `cuda-bindings` requires
`cuda-pathfinder`, which has no other parent and so inherits the same x86_64 and
Linux condition, leaving a Raspberry Pi install without it as well. It is named
here only so the `nvidia-*-cu12` pattern is not read as exhaustive: it ships no
CUDA redistributable and declares Apache-2.0, which makes it permissive on the
terms it states, so it is not a dependency this inventory has to weigh.

## Media decoding

`av`, `torchcodec`, and `imageio-ffmpeg` are BSD-licensed Python packages that
wrap or ship FFmpeg builds. FFmpeg itself is LGPL or GPL depending on how the
particular build was configured, so the effective terms follow the binary that
gets installed rather than the Python wrapper's own license.

`opencv-python-headless` falls in the same category. The wrapper's own code is
Apache-2.0, but the published wheels bundle third-party binaries alongside it,
most notably FFmpeg under LGPL-2.1. Read `LICENSE-3RD-PARTY.txt` in the
installed `opencv_python_headless` dist-info for the full list and license
texts, since the bundled set varies by platform.

- FFmpeg licensing: https://www.ffmpeg.org/legal.html
