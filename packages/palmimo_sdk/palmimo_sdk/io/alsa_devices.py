# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Resolve an ALSA device string from a device-name hint.

The two shell-out audio paths in this package -- :class:`~palmimo_sdk.io.speaker.Speaker`
(``aplay``) and :class:`~palmimo_sdk.io.microphone.Microphone` (``arecord``) -- address a
device by an ALSA string, not by the PortAudio index
:mod:`~palmimo_sdk.io.mic_stream` resolves. Both need the same thing from a
hint, so it lives here rather than twice.

:func:`resolve_alsa_device` is exported from :mod:`palmimo_sdk` because the
same need arises outside this package: an application that spawns its own
``aplay`` -- the realtime voice runtime plays the model's own audio that way
rather than synthesizing through :class:`~palmimo_sdk.io.speaker.Speaker` --
faces the identical renumbering problem and must not have to reimplement the
lookup or reach into a private module for it.

**Why a hint rather than a fixed device.** ALSA numbers cards in registration
order, so a USB audio device's index depends on whether it was attached before
the kernel enumerated it -- attach the same array after boot and it lands
behind the built-in outputs instead of ahead of them. An index baked into a
default is therefore a property of one machine's boot, not of the hardware.
The card *id* (``ArrayUAC10``) does not move, so this resolves to
``plughw:CARD=<id>,DEV=<n>`` and the caller keeps working across a renumber.

``None`` means "say nothing and let ALSA pick", which is the behaviour of a
command with no ``-D``.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from typing import Literal


#: One entry of ``aplay -l`` / ``arecord -l``, e.g.
#: ``card 0: ArrayUAC10 [ReSpeaker 4 Mic Array (UAC1.0)], device 0: USB Audio [USB Audio]``
_CARD_LINE = re.compile(r"^card (?P<index>\d+): (?P<id>\S+) \[(?P<name>[^]]*)\], device (?P<device>\d+):")

_LIST_TIMEOUT_S = 5.0

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=_LIST_TIMEOUT_S, check=False)


def resolve_alsa_device(
    hint: str | None,
    *,
    kind: Literal["playback", "capture"],
    run: Runner = _run,
    log: logging.Logger | None = None,
) -> str | None:
    """Return the ALSA device string for the card matching *hint*.

    Args:
        hint: Case-insensitive substring, matched against both the card id and
            its long name (``ReSpeaker`` matches ``ArrayUAC10 [ReSpeaker 4 Mic
            Array (UAC1.0)]`` on either). Falsy means "no preference".
        kind: Which device list to read -- ``aplay -l`` for playback,
            ``arecord -l`` for capture. A card can offer one and not the other,
            so the lists are not interchangeable.
        run: Seam for the subprocess call, so the parsing is testable without
            an audio device.
        log: Logger for the warnings this emits; the module logger by default.

    Returns:
        ``plughw:CARD=<id>,DEV=<n>`` for the first matching card, or ``None``
        when there is no hint, no match, or the listing could not be read.
        ``None`` is not a failure: it means the caller should issue its command
        without a device and let ALSA's own default apply.
    """
    logger = log if log is not None else logging.getLogger(__name__)
    if not hint:
        return None
    argv = ["aplay", "-l"] if kind == "playback" else ["arecord", "-l"]
    try:
        result = run(argv)
    except FileNotFoundError:
        logger.warning("%s not found; using the ALSA default %s device", argv[0], kind)
        return None
    except Exception as exc:  # timeout, OSError -- a listing we cannot read is not fatal
        logger.warning("%s failed (%s); using the ALSA default %s device", argv[0], exc, kind)
        return None
    if result.returncode != 0:
        logger.warning("%s exited %d; using the ALSA default %s device", argv[0], result.returncode, kind)
        return None
    hint_low = hint.lower()
    for line in (result.stdout or "").splitlines():
        match = _CARD_LINE.match(line.strip())
        if match is None:
            continue
        card_id = match.group("id")
        if hint_low not in card_id.lower() and hint_low not in match.group("name").lower():
            continue
        device = f"plughw:CARD={card_id},DEV={match.group('device')}"
        logger.info("Selected %s device %s for hint %r", kind, device, hint)
        return device
    logger.warning("No %s device matches hint %r; using the ALSA default", kind, hint)
    return None
