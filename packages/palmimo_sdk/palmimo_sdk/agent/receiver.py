# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""PalmimoLike — the structural surface of :class:`~palmimo_sdk.robot.Palmimo`
that this agent layer (:mod:`palmimo_sdk.agent.tools` /
:mod:`palmimo_sdk.agent.toolset`) actually depends on.

A :class:`~typing.Protocol`, not the concrete facade, so a duck-typed test
double (a ``FakeRobot`` recording calls, never touching hardware) satisfies
the type checker exactly to the extent it implements what is used -- no
subclassing the real facade's connection lifecycle, no ``cast(Palmimo, ...)``
at every call site. Enumerated by walking every ``robot.<member>`` access in
``tools.py`` and ``toolset.py``; the real ``Palmimo`` satisfies it too (see
``test_agent_receiver.py`` for the static assertion). :class:`Tool.execute`
and :class:`~palmimo_sdk.agent.toolset.AgentToolSet`'s constructor are typed
against this Protocol instead of the concrete facade.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from palmimo_sdk.robot import NeckPitchDegrees, NeckPitchNormalized, NeckYawDegrees, NeckYawNormalized


@runtime_checkable
class _SpeechHandleLike(Protocol):
    """Structural surface of :class:`~palmimo_sdk.io.speaker.SpeechHandle` that :meth:`PalmimoLike.say`'s
    caller depends on: :meth:`~palmimo_sdk.agent.tools.Tool.execute` joins it (bounded) and polls
    ``is_alive()``; a failure is read back via ``getattr(handle, "error", None)`` rather than a typed
    attribute, so it is deliberately not part of this Protocol.
    """

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


@runtime_checkable
class _CameraLike(Protocol):
    """Structural surface of :class:`~palmimo_sdk.io.camera.HeadCamera` that :class:`~palmimo_sdk.agent.tools.CaptureTool`
    (``read``) and a custom face-tracking ``Tool`` such as ``examples/agents/companion``'s (``latest``,
    the background-drain reader) depend on.
    """

    def read(self) -> tuple[bool, Any]: ...

    def latest(self, *, timeout: float = 2.0) -> Any | None: ...


@runtime_checkable
class PalmimoLike(Protocol):
    """Structural surface of :class:`~palmimo_sdk.robot.Palmimo` the agent layer depends on (see module docstring).

    Movement/gesture setters, ``run``/``stop``/``cancel``, neck
    ``look``/``look_center``, ``set_expression``/``say``/``stop_speech``, the
    ``display``/``camera`` peripherals, ``cancel_checkpoint``/
    ``raise_if_cancelled`` (a custom ``Tool.execute()``'s own cooperative
    loop -- see e.g. ``examples/agents/companion``'s face-tracking tool), and
    the cancel-scope arm/disarm pair :meth:`~palmimo_sdk.agent.toolset.AgentToolSet.call`
    wraps every dispatch in. Precedent: :class:`palmimo_wakeword_agent.wiring._ServoDriverLike`.
    """

    # ----------------------------------------------------------------
    # Peripheral properties -- only ever compared to ``None`` or (camera)
    # read from, never assigned, so a read-only property is enough.
    # ----------------------------------------------------------------
    @property
    def display(self) -> object | None: ...

    @property
    def camera(self) -> _CameraLike | None: ...

    # ----------------------------------------------------------------
    # Movement / gesture setters -- each just switches the engine's target
    # motion; the real signatures take no arguments and return nothing.
    # ----------------------------------------------------------------
    def forward(self) -> None: ...

    def backward(self) -> None: ...

    def strafe_left(self) -> None: ...

    def strafe_right(self) -> None: ...

    def rotate_left(self) -> None: ...

    def rotate_right(self) -> None: ...

    def creep(self) -> None: ...

    def dance(self) -> None: ...

    def body_tilt(self) -> None: ...

    def pushup(self) -> None: ...

    def wave(self) -> None: ...

    def wave_both(self) -> None: ...

    def clap(self) -> None: ...

    def bow(self) -> None: ...

    def stretch(self) -> None: ...

    def nod(self) -> None: ...

    def head_shake(self) -> None: ...

    # ----------------------------------------------------------------
    # Run / stop / cancel
    # ----------------------------------------------------------------
    def run(self, *, seconds: float | None = None, steps: int | None = None) -> object:
        """Paced-run the current motion. Return value is never read by the agent layer -- ``object``
        covers both the real facade's ``dict[str, int]`` and a test double's ``None``.
        """
        ...

    def stop(self) -> None: ...

    def cancel(self) -> None: ...

    # ----------------------------------------------------------------
    # Sleep / wake -- a pose-and-stiffness pair, not a motion. Peripherals
    # stay open through both, so a sleeping robot can still be spoken to.
    # ----------------------------------------------------------------
    def sleep(self, duration: float = 1.5, end_gain: int = 300) -> None: ...

    def wake(self, duration: float = 1.5, start_gain: int = 300) -> None: ...

    def cancel_checkpoint(self) -> int: ...

    def raise_if_cancelled(self, checkpoint: int) -> None: ...

    # ----------------------------------------------------------------
    # Neck
    # ----------------------------------------------------------------
    def look(
        self,
        pitch: float | NeckPitchDegrees | NeckPitchNormalized = 0.0,
        yaw: float | NeckYawDegrees | NeckYawNormalized = 0.0,
    ) -> None: ...

    def look_center(self) -> None: ...

    # ----------------------------------------------------------------
    # Face / voice
    # ----------------------------------------------------------------
    def set_expression(self, name: str, hold_ms: int = 0) -> str | None: ...

    def say(self, text: str, lang: str | None = None) -> _SpeechHandleLike | None: ...

    def stop_speech(self) -> None: ...

    # ----------------------------------------------------------------
    # Cancellation-scope bracketing -- AgentToolSet.call() arms this around
    # every dispatch so a cancel() delivered between validation and the
    # worker thread's paced entry is not silently absorbed. Private on the
    # real facade (leading underscore) but part of this Protocol because the
    # toolset genuinely calls it -- see toolset.py's docstring.
    # ----------------------------------------------------------------
    def _arm_cancel_scope(self) -> None: ...

    def _disarm_cancel_scope(self) -> None: ...
