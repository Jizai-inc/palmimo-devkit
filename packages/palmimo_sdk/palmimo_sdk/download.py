# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""download_atomic — shared helper for fetching model files onto disk.

Model downloads (voice-extra ASR/VAD/denoiser ONNX models, and similar
"download to a local cache path if missing" call sites) tend to get
re-implemented ad hoc with two recurring bugs:

  1. Non-atomic writes: writing straight to the destination path means a
     crash / kill *during* the download leaves a truncated file that a later
     ``Path.exists()`` check mistakes for a complete download — the next run
     "sees" the model as present and hands the corrupt file to whatever
     library loads it.
  2. No timeout: ``urllib.request.urlretrieve`` has no timeout, so a stalled
     connection hangs the caller forever.

:func:`download_atomic` fixes both: it streams the response to a temporary
file in the *same directory* as the destination (so the final ``os.replace``
is an atomic same-filesystem rename, not a cross-device copy that could itself
be interrupted) and applies a connect/read timeout. It also optionally
verifies a sha256 checksum before the file is ever placed at *dest*, so a
truncated or tampered download can never masquerade as a complete one.

:func:`default_model_dir` is the other half every such call site needs: the
one cache root they all download into, so a model lands outside whatever
directory the process happens to be started from.

Shared home: this lives in ``palmimo_sdk`` because it is the natural common
package for any model-auto-download call site (voice extras and beyond) to
depend on, avoiding a new cross-package dependency just for this helper.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path


def default_model_dir() -> Path:
    """Return the shared cache directory for auto-downloaded models.

    ``$XDG_CACHE_HOME/palmimo/models``, falling back to ``~/.cache/palmimo/models``
    when unset. Evaluated at call time (not import time) so tests can
    monkeypatch the environment / home directory.
    """
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache_home) if xdg_cache_home else Path.home() / ".cache"
    return base / "palmimo" / "models"


def download_atomic(url: str, dest: str | Path, *, timeout: float = 30.0, sha256: str | None = None) -> None:
    """Download *url* to *dest*, atomically and with a bounded timeout.

    Streams the response body into a temp file created in ``dest``'s parent
    directory, then ``os.replace``s it into place. A crash, kill, or exception
    at any point before the replace leaves *dest* untouched (the partial file
    is removed) instead of a truncated file masquerading as a complete
    download.

    Parameters
    ----------
    url : str
        Source URL (``urllib.request.urlopen`` — http(s) or file).
    dest : str | Path
        Final destination path. Parent directory is created if missing.
    timeout : float
        Seconds allowed for connecting *and* for each individual socket read;
        a stalled server raises instead of hanging the caller forever.
    sha256 : str | None
        Expected sha256 hex digest of the downloaded bytes. When given, the
        temp file's digest is checked *after* the full body is written and
        *before* ``os.replace`` moves it into place; a mismatch removes the
        temp file and raises ``OSError`` instead of ever exposing an
        unverified or tampered file at *dest*.
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest_path.parent), prefix=f".{dest_path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as tmp_f, urllib.request.urlopen(url, timeout=timeout) as resp:
            shutil.copyfileobj(resp, tmp_f)
        if sha256 is not None:
            actual = _sha256_of(tmp_name)
            if actual != sha256:
                raise OSError(f"sha256 mismatch for {url}: expected {sha256}, got {actual}")
        os.replace(tmp_name, dest_path)
    except http.client.HTTPException as exc:
        # A truncated / aborted response body raises http.client.IncompleteRead
        # (an HTTPException — NOT an OSError or URLError) straight out of the read.
        # Normalize it to OSError so every call site's existing os/url error guard
        # catches it (with its offline-venue hint) instead of leaking a raw
        # HTTPException that slips past those excepts.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise OSError(f"incomplete download from {url}: {exc}") from exc
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _sha256_of(path: str) -> str:
    """Compute the sha256 hex digest of the file at *path*.

    Parameters
    ----------
    path : str
        Path to the file to hash.

    Returns
    -------
    str
        Hex-encoded sha256 digest of the file's contents.
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
