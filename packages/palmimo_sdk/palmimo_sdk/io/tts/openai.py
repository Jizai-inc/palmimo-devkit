# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""OpenAiEngine — OpenAI's speech API as a :class:`~palmimo_sdk.io.tts.base.TtsEngine`.

An alternative voice, not a faster one. What this buys over the local engine
is how it sounds, plus :data:`DEFAULT_INSTRUCTIONS` -- a style prompt the
model accepts alongside the text, which piper has no equivalent for.

It costs more than it saves, so :class:`~palmimo_sdk.io.speaker.Speaker` keeps
constructing :class:`~palmimo_sdk.io.tts.piper.PiperEngine` by default and a
caller opts in with ``Speaker(engine=OpenAiEngine())``. Measured on a Pi 5
(three Japanese sentences, three passes each): piper 0.19 s per utterance
median, this 1.57 s -- the round trip dominates, and piper's synthesis is not
the CPU hog it would have to be for offloading to pay. On top of the latency
this adds an API key, per-utterance billing, and a hard network dependency: a
robot that cannot reach the internet cannot speak with this engine, where the
local one only sounds worse.

Unlike the local engines there is no model to download and nothing to load, so
:meth:`OpenAiEngine.load_voice` is cheap; what :meth:`OpenAiEngine.preflight`
checks is the API key, the only thing that can be missing.

Uses ``urllib`` rather than an SDK client: one POST returning WAV bytes does
not justify a dependency, and :class:`~palmimo_sdk.io.speaker.Speaker` already
calls this on its own worker thread, so blocking here is correct.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
import wave
from collections.abc import Callable
from typing import Any

from .base import TtsEngine, TtsVoice


SPEECH_URL = "https://api.openai.com/v1/audio/speech"

#: Environment variable holding the API key. Set it once in the environment
#: and any agent that uses this engine picks it up with nothing new to wire.
API_KEY_ENV = "OPENAI_API_KEY"

DEFAULT_MODEL = "gpt-4o-mini-tts"
DEFAULT_VOICE = "marin"

#: One utterance's budget. Below Speaker's own say_timeout_s so a slow network
#: surfaces as a synthesis failure -- which evicts and reloads the voice -- and
#: not as the whole utterance timing out.
DEFAULT_TIMEOUT_S = 15.0

#: The style prompt this model accepts alongside the text. It has no equivalent
#: in piper, and it is the main handle on how the voice sounds.
DEFAULT_INSTRUCTIONS = "小さなロボットが子どもに話しかけるように、明るく、はっきりと、少し高めの声で。"


def _default_transport(body: dict[str, Any], api_key: str, timeout: float) -> bytes:
    """POST *body* to the speech endpoint and return the WAV bytes.

    Raises:
        RuntimeError: The API rejected the request, with its own message
            attached -- an opaque HTTP code costs a round trip to diagnose --
            or could not be reached at all.
        TimeoutError: The connection attempt ran past *timeout*.
    """
    request = urllib.request.Request(
        SPEECH_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"OpenAI speech API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        # HTTPError subclasses URLError, so what is left here is the request
        # never getting an answer: DNS, no route, a captive portal. On a robot
        # that moves between venues this is the likeliest failure of a
        # network-backed voice, and left unwrapped it arrives as
        # "<urlopen error [Errno -3] ...>", which failure_hint cannot match.
        # A connect timeout lands here too, wrapped -- surfaced as TimeoutError
        # so it reads the same as one raised during the read.
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError(f"the speech API timed out after {timeout:g}s") from exc
        raise RuntimeError(f"OpenAI speech API is unreachable: {exc.reason}") from exc


class _OpenAiTtsVoice:
    """:class:`~palmimo_sdk.io.tts.base.TtsVoice` over one speech-API configuration.

    Holds no session and no model -- every utterance is a fresh request -- so
    this exists to close over the settings and keep callers on the
    ``synthesize(text, wav_file)`` contract.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        speed: float,
        volume: float,
        instructions: str | None,
        timeout: float,
        transport: Callable[[dict[str, Any], str, float], bytes],
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._speed = speed
        self._volume = volume
        self._instructions = instructions
        self._timeout = timeout
        self._transport = transport

    def synthesize(self, text: str, wav_file: wave.Wave_write) -> None:
        body: dict[str, Any] = {
            "model": self._model,
            "input": text,
            "voice": self._voice,
            "response_format": "wav",
            "speed": self._speed,
        }
        if self._instructions:
            body["instructions"] = self._instructions
        try:
            returned = self._transport(body, self._api_key, self._timeout)
            # The API returns a complete WAV; the caller supplied an open writer,
            # so the frames are copied across rather than the file replaced.
            with wave.open(io.BytesIO(returned)) as source:
                channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
                frames = source.readframes(source.getnframes())
        except Exception:
            # The caller opened this writer and will close it while unwinding.
            # Closing one that never had its format set raises from __exit__
            # and replaces the real error -- a network failure, or a 200 whose
            # body is not the WAV this expects, would surface as "# channels
            # not specified". Give it a valid empty header so the original
            # exception is what reaches Speaker, and failure_hint can read it.
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            raise

        # Past the point where an exception could be masked: the writer now has
        # its format, so anything raised below arrives at Speaker intact.
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(width)
        wav_file.setframerate(rate)
        if self._volume != 1.0:
            if width != 2:
                # _scaled reads the buffer as int16. Reinterpreting a different
                # width would garble the audio rather than fail, so say so.
                raise RuntimeError(
                    f"The speech API returned {width * 8}-bit audio; volume scaling only handles 16-bit. "
                    "Leave volume at 1.0 for this response format."
                )
            frames = _scaled(frames, self._volume)
        wav_file.writeframes(frames)


def _scaled(frames: bytes, volume: float) -> bytes:
    """Apply *volume* to 16-bit frames, since the API has no gain parameter.

    Only reached when *volume* is not 1.0, which is why numpy is not a hard
    dependency of this module -- the default path never touches it.
    """
    try:
        import numpy as np  # lazy: keeps `import palmimo_sdk` dependency-free
    except ModuleNotFoundError as exc:
        # Names numpy itself rather than an extra: it is only declared under
        # [voice], which is the mic/denoise stack -- a several-hundred-megabyte
        # install to scale playback volume, and nothing this module needs.
        raise RuntimeError(
            "numpy is not installed, and OpenAiEngine(volume=...) needs it to scale the samples; "
            "uv add numpy, or leave volume at 1.0"
        ) from exc

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) * volume
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


class OpenAiEngine(TtsEngine):
    """:class:`~palmimo_sdk.io.tts.base.TtsEngine` over OpenAI's speech API.

    ``voice`` is one of the API's voice names; ``speed`` is its own rate
    multiplier (``>1`` faster, the inverse of piper's ``length_scale``);
    ``volume`` is applied to the returned samples
    because the API has no gain parameter. ``instructions`` steers delivery in
    plain language and has no counterpart in the local engines. ``transport``
    is a test seam (defaults to a real POST; fakes return WAV bytes).

    Every language uses the same voice: the model is multilingual and takes
    the language from the text, so unlike piper there is no per-language model
    to select.
    """

    name = "openai"

    def __init__(
        self,
        *,
        voice: str = DEFAULT_VOICE,
        model: str = DEFAULT_MODEL,
        speed: float = 1.0,
        volume: float = 1.0,
        instructions: str | None = DEFAULT_INSTRUCTIONS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        api_key_env: str = API_KEY_ENV,
        transport: Callable[[dict[str, Any], str, float], bytes] | None = None,
    ) -> None:
        self._voice = voice
        self._model = model
        self._speed = speed
        self._volume = volume
        self._instructions = instructions
        self._timeout = timeout_s
        self._api_key_env = api_key_env
        self._transport = transport or _default_transport

    def preflight(self, lang: str, *, fetch: bool = True) -> None:
        """Check the API key is present.

        There is nothing else to check without paying for a request: no model
        on disk, nothing to download, and this must not spend a request
        validating the key. ``fetch`` is therefore ignored — there is no
        cheaper question to ask.

        Raises:
            RuntimeError: The API key environment variable is unset.
        """
        if not os.environ.get(self._api_key_env):
            raise RuntimeError(f"{self._api_key_env} is not set; the OpenAI voice needs it.")

    def load_voice(self, lang: str) -> TtsVoice:
        """Return a voice bound to this engine's settings.

        Cheap by nature -- there is no model to load -- so unlike the local
        engines this costs nothing at startup.
        """
        self.preflight(lang)
        return _OpenAiTtsVoice(
            api_key=os.environ[self._api_key_env],
            model=self._model,
            voice=self._voice,
            speed=self._speed,
            volume=self._volume,
            instructions=self._instructions,
            timeout=self._timeout,
            transport=self._transport,
        )

    def failure_hint(self, error_text: str, lang: str) -> str:
        """Name the failures that have a fix: a bad key, an unknown voice, no network."""
        if "401" in error_text or "invalid_api_key" in error_text:
            return f" {self._api_key_env} was rejected; check the key in .env"
        if "voice" in error_text and "400" in error_text:
            return f" Voice {self._voice!r} was rejected; check it against the API's voice list"
        # Before the timeout branch: an unreachable host's reason text can itself
        # mention a timeout, and "there is no network" is the more useful hint.
        if "unreachable" in error_text:
            return " The speech API could not be reached; this voice needs a network, unlike PiperEngine"
        if "timed out" in error_text or "timeout" in error_text:
            return " The speech API did not answer in time; this voice needs a working network"
        return ""


__all__ = ["API_KEY_ENV", "DEFAULT_MODEL", "DEFAULT_VOICE", "SPEECH_URL", "OpenAiEngine"]
