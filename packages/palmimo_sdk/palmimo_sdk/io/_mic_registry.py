# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Process-local registry of open :class:`~palmimo_sdk.io.mic_stream.MicStream` instances.

A microphone is identified by a ``device_key`` — an opaque string the caller
chooses to name a physical mic (e.g. ``"default"`` or ``"respeaker"``). This
registry is what lets :class:`~palmimo_sdk.io.microphone.Microphone` and
:class:`~palmimo_sdk.io.mic_stream.MicStream` discover, for the same
``device_key``, whether the other has already opened the device — so
``Microphone`` can skip its own probe / delegate ``record`` instead of racing
the shared :class:`MicStream` for the hardware.

ALSA device strings (``plughw:3,0``) and sounddevice device names/indices
cannot be machine-matched against each other — they come from different
enumeration systems for what may or may not be the same physical card. So
this registry deliberately does NOT attempt device-identity resolution: it is
a convention, not a guarantee. Callers that want ``Microphone`` and
``MicStream`` to cooperate on one physical mic must give both the *same*
``device_key`` string; two different keys are always treated as different
mics even if they happen to be the same hardware.

Scope: a single Python process's global dict, guarded by one lock. It does
NOT arbitrate across processes — two processes each opening ``device_key=
"default"`` will each succeed and independently fight over the real hardware
at the OS level.
"""

from __future__ import annotations

import threading


_lock = threading.Lock()
_streams: dict[str, object] = {}


def register(device_key: str, stream: object) -> None:
    """Register *stream* as the open owner of *device_key*.

    Raises
    ------
    RuntimeError
        If *device_key* is already registered to a (different or same)
        stream — fail-fast rather than silently letting two owners believe
        they hold the same device.
    """
    with _lock:
        if device_key in _streams:
            raise RuntimeError(
                f"device_key {device_key!r} is already registered — a MicStream for this "
                "key is already open. Close it first, or use a different device_key."
            )
        _streams[device_key] = stream


def unregister(device_key: str, stream: object) -> None:
    """Remove *stream* from the registry if it is the registered owner of *device_key*.

    Idempotent: a no-op if *device_key* isn't registered, or is registered to
    a *different* stream (never removes another owner's registration).
    """
    with _lock:
        if _streams.get(device_key) is stream:
            del _streams[device_key]


def get(device_key: str) -> object | None:
    """Return the currently registered stream for *device_key*, or ``None``."""
    with _lock:
        return _streams.get(device_key)
