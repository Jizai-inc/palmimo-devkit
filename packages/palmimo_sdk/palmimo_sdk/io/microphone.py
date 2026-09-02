# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Microphone I/O resource — the single owner of the USB mic capture.

Mirrors the servo / camera I/O layer (:mod:`palmimo_sdk.io.dynamixel`,
:mod:`palmimo_sdk.io.camera`): the SDK owns the hardware resource, and higher
layers consume raw audio from it instead of shelling out to ``arecord``
themselves. Speech recognition and command matching stay with the consumer —
this class only owns the *capture*, returning a WAV byte stream, the same way
:class:`HeadCamera` owns the frame grab but not face detection.

Capture shells out to ``arecord`` (Linux / ALSA) or ``rec`` (macOS / sox),
written to stdout as a WAV stream so there is no temp file to clean up. The
recording tool is invoked lazily (per :meth:`record`), so importing this module
stays dependency- and hardware-free for compute-only use, matching
:class:`~palmimo_sdk.io.dynamixel.DynamixelDriver`.
"""

from __future__ import annotations

import logging
import math
import platform
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from . import _mic_registry
from .alsa_devices import resolve_alsa_device


if TYPE_CHECKING:
    from ..audio.processor import AudioProcessor


@dataclass(frozen=True)
class MicrophoneConfig:
    """Capture settings for the USB microphone.

    ``device`` is the ALSA device passed to ``arecord -D`` on Linux. It
    defaults to ``None``, meaning "resolve it from ``device_name_hint``, and
    fall back to ALSA's own default" -- an index is a property of one
    machine's boot order rather than of the mic, since a USB array attached
    after the kernel enumerated the built-in devices lands behind them. Set
    ``device`` to pin a specific string and skip resolution entirely. macOS
    ``rec`` records from the default input and ignores both.

    ``device_name_hint`` is a case-insensitive substring matched against the
    card id and name in ``arecord -l`` (e.g. ``"ReSpeaker"``); it resolves to
    a ``plughw:CARD=<id>,DEV=<n>`` string, which survives a renumber.

    ``device_key`` names the physical mic for coordination with a
    :class:`~palmimo_sdk.io.mic_stream.MicStream` that may already own it (see
    :mod:`palmimo_sdk.io._mic_registry`) — set it to match the ``MicStream``'s
    ``device_key`` when both are meant to share one physical mic. Unrelated
    ``Microphone`` / ``MicStream`` pairs should use distinct keys (or leave
    this at the default, which only collides with another default-keyed one).
    """

    device: str | None = None
    device_name_hint: str | None = None
    sample_rate: int = 16000
    channels: int = 1
    sample_width_bits: int = 16  # S16_LE
    device_key: str = "default"


def _record_command(system: str, config: MicrophoneConfig, seconds: float) -> list[str]:
    """Build the argv that captures ``seconds`` of WAV audio to stdout.

    Pure (no I/O) so the command can be unit-tested per OS without a mic.
    ``system`` is a :func:`platform.system` value (``"Darwin"`` → sox ``rec``,
    anything else → ALSA ``arecord``).
    """
    if system == "Darwin":  # macOS: sox `rec` records from the default input.
        # sox grammar is `rec [format-opts] outfile [effects]`: the format flags
        # must precede the `-` output, otherwise sox reads them as effects and errors.
        return [
            "rec",
            "-q",
            "-c",
            str(config.channels),
            "-r",
            str(config.sample_rate),
            "-b",
            str(config.sample_width_bits),
            "-t",
            "wav",
            "-",
            "trim",
            "0",
            str(seconds),
        ]
    # Linux and others: ALSA `arecord` from the configured device.
    return [
        "arecord",
        "-D",
        # `None` reaches here only when a caller builds the command straight
        # from an unresolved config; Microphone itself passes a resolved one.
        config.device or "default",
        "-f",
        f"S{config.sample_width_bits}_LE",
        "-r",
        str(config.sample_rate),
        "-c",
        str(config.channels),
        # arecord -d takes whole seconds; round up (min 1) so fractional
        # durations don't truncate or, at <1s, become `-d 0` (= record forever).
        "-d",
        str(max(1, math.ceil(seconds))),
        "-t",
        "wav",
        "-",
    ]


class Microphone:
    """USB microphone that owns the recording subprocess.

    Lifecycle: :meth:`open` (idempotent availability probe) / :meth:`record`
    (auto-opens) / :meth:`close`. Usable as a context manager. The capture is
    stateless between calls — each :meth:`record` spawns one recorder — so a
    single instance just centralizes the device config and probe.

    ``runner`` is a seam for tests: it defaults to :func:`subprocess.run` and
    can be replaced with a fake that returns a ``CompletedProcess``-like object
    (``returncode`` / ``stdout``) without touching hardware.

    ``processors`` defaults to empty — unlike
    :class:`~palmimo_sdk.io.mic_stream.MicStream`, ``Microphone`` does NOT
    denoise by default. ``Microphone`` (no ``voice`` extra needed) is meant to
    stay dependency-free for existing callers; defaulting to a processor here
    would silently pull in that requirement and change behaviour for everyone
    already using it. Pass ``processors=[ClipDenoiser()]`` explicitly to opt
    in — NOT :class:`~palmimo_sdk.audio.processor.Denoiser`, whose streaming
    engine is never flushed here: ``Microphone`` calls :meth:`process` once
    per complete, self-contained recording, so a stateful streaming denoiser
    would cut off its tail and bleed residue into the next recording.
    :class:`~palmimo_sdk.audio.processor.ClipDenoiser` denoises the whole clip
    in one stateless call, which is what a complete recording needs.
    """

    def __init__(
        self,
        config: MicrophoneConfig | None = None,
        *,
        runner: Callable[..., Any] | None = None,
        system: str | None = None,
        processors: Sequence[AudioProcessor] = (),
    ) -> None:
        self.config = config or MicrophoneConfig()
        self._runner = runner
        self._system = system or platform.system()
        self._opened = False
        self._processors = processors
        self._log = logging.getLogger(__name__)
        # Config with `device` filled in, resolved on first use rather than
        # here: a Microphone is routinely built before its USB device is
        # attached. None until then.
        self._resolved: MicrophoneConfig | None = None

    @property
    def is_open(self) -> bool:
        """Whether this ``Microphone`` is ready to record.

        ``True`` in two distinct situations that both satisfy the public
        contract but differ in who owns the hardware: (1) this instance ran
        its own ``arecord``/``rec`` probe successfully — it owns the device;
        or (2) a same-``device_key`` :class:`~palmimo_sdk.io.mic_stream.MicStream`
        is already open and :meth:`open` deferred to it instead of probing
        (see :meth:`open`) — in this case this ``Microphone`` does NOT own
        the hardware, :meth:`record` delegates every call to that stream, and
        :meth:`close` merely flips this flag back without touching anything
        shared.
        """
        return self._opened

    def _capture_config(self) -> MicrophoneConfig:
        """This mic's config with ``device`` resolved to a concrete string.

        An explicit ``device`` is honoured as given. Otherwise the hint is
        looked up and a *hit* is remembered; ``"default"`` stands in for a miss
        without being cached, so a mic built before its USB device was attached
        resolves properly on a later call rather than keeping the answer from
        before it existed. ``"default"`` is ALSA's own default device, which is
        also what ``arecord -D`` falls back to. macOS records from the default
        input and ignores the flag, so nothing is looked up there.
        """
        if self._resolved is not None:
            return self._resolved
        if self.config.device is not None or self._system == "Darwin":
            self._resolved = self.config
            return self._resolved
        device = resolve_alsa_device(self.config.device_name_hint, kind="capture", log=self._log)
        if device is None:
            # Not remembered: open() is a retryable probe, and a miss is what
            # attaching the device fixes.
            return replace(self.config, device="default")
        self._resolved = replace(self.config, device=device)
        return self._resolved

    def _run(self, args: list[str], timeout: float) -> Any:
        runner = self._runner
        if runner is None:
            import subprocess  # lazy: keeps `import palmimo_sdk` dependency-free

            runner = subprocess.run
        return runner(args, capture_output=True, timeout=timeout)

    def open(self) -> None:
        """Probe that the recorder can access the mic; idempotent.

        Records a 1-second throwaway clip and raises :class:`RuntimeError` if
        the tool is missing or the device cannot be opened. Skipped entirely
        when a :class:`~palmimo_sdk.io.mic_stream.MicStream` sharing this
        config's ``device_key`` is already open: it already owns the
        hardware, so an ``arecord``/``rec`` probe here would just fail with
        the device busy. :meth:`record` delegates to that stream too.
        """
        if self._opened:
            return
        if _mic_registry.get(self.config.device_key) is not None:
            self._opened = True
            return
        # Resolved once and held: a miss is deliberately not remembered (see
        # _capture_config), so asking again in the failure path below would
        # fork a second `arecord -l`, could name a device this probe never
        # touched, and would memoize that late hit as a side effect of
        # formatting an error string.
        config = self._capture_config()
        try:
            result = self._run(_record_command(self._system, config, 1), timeout=5)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Recorder not found: {exc}") from exc
        except Exception as exc:  # timeouts etc. — surface as our error type
            raise RuntimeError(f"Microphone probe failed: {exc}") from exc
        if result.returncode != 0:
            raise RuntimeError(f"Cannot access microphone {config.device!r}")
        self._opened = True

    def record(self, seconds: float) -> bytes | None:
        """Capture ``seconds`` of audio; return WAV bytes, or ``None`` on failure.

        Delegates to a same-``device_key`` :class:`~palmimo_sdk.io.mic_stream.MicStream`
        when one is open (see :meth:`open`) instead of shelling out — the two
        can't both hold the device. Otherwise auto-opens and shells out as
        usual. Mirrors :meth:`HeadCamera.read`: a capture failure (missing
        tool, device busy, empty stream) is reported as ``None`` rather than
        raised, so callers can skip a bad take.

        ``processors`` (see the constructor) is applied only on this direct
        ``arecord``/``rec`` path, in order — NOT on the ``MicStream``
        delegation path above, whose own ``processors`` chain (denoising by
        default) already ran on that audio; applying this instance's
        processors again there would double-process it. A processor
        exception is logged and the whole recording is discarded (``None``),
        the same failure semantics as any other capture failure here.
        """
        stream = _mic_registry.get(self.config.device_key)
        if stream is not None:
            return stream.record(seconds)  # type: ignore[attr-defined]
        try:
            self.open()
        except RuntimeError:
            return None
        try:
            result = self._run(
                _record_command(self._system, self._capture_config(), seconds),
                timeout=seconds + 5,
            )
        except Exception:
            return None
        if result.returncode != 0 or not result.stdout:
            return None
        wav = result.stdout
        try:
            for processor in self._processors:
                wav = processor.process(wav)
        except Exception as exc:
            self._log.warning("Microphone processor raised, discarding recording: %s", exc)
            return None
        return wav

    def close(self) -> None:
        self._opened = False

    def __enter__(self) -> Microphone:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
