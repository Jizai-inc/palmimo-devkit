"""Wake-word matching against a free-form transcript.

:class:`WakeWordDetector` decides whether a transcript called the robot, and
returns whatever was said after the name. The transcript comes from
:mod:`palmimo_wakeword_agent.stt`; the comparison itself lives in
:class:`palmimo_sdk.NameMatcher`, which matches on sounds rather than spelling.

Why not spelling: the name is not a word of the transcription language, so a
transcriber asked for Japanese falls back to a Latin-script approximation and
picks a different one nearly every time -- ``Parmimo``, ``Parumimo``,
``Par mi mo``, ``Farmimo``, ``Varumimo``. Listing more spellings does not
converge; on 40 recorded calls the seven spellings this example used to ship
with matched 3. Matching on sounds matches 39 of the same 40, with no false
accept across 40 utterances of ordinary speech.
"""

from __future__ import annotations

from dataclasses import dataclass

from palmimo_sdk import NameMatcher


@dataclass
class WakeMatch:
    """A wake-word detection result: the text that followed it."""

    command: str


class WakeWordDetector:
    """Finds the robot's name in a transcript and returns the command after it.

    The wake word is fixed by the SDK: :class:`palmimo_sdk.NameMatcher` listens
    for :data:`palmimo_sdk.PALMIMO_NAMES` at its measured threshold, and takes
    no name or threshold to override -- see :mod:`palmimo_sdk.name_match` for
    why.
    """

    def __init__(self) -> None:
        self._matcher = NameMatcher()

    def match(self, text: str) -> WakeMatch | None:
        """Return the command following the wake word, or ``None`` if it was not called."""
        found = self._matcher.match(text)
        return None if found is None else WakeMatch(command=found.command)
