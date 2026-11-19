"""Record the homepage's voice samples: one short line per Realtime voice, saved as WAV.

Each sample comes from the same Realtime API the AI modes speak through, so
the preview sounds exactly like an AI mode. Run it once after changing
`SAMPLE_LINE` or `voices.VOICES`, and commit the results:

    OPENAI_API_KEY=... uv run python scripts/record_voice_samples.py [voice ...]
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import wave

from palmimo_controller.ai.audio import API_RATE
from palmimo_controller.ai.client import RealtimeClient
from palmimo_controller.ai.controller import DEFAULT_MODEL
from palmimo_controller.ai.protocol import (
    AudioDelta,
    ErrorEvent,
    ItemCreate,
    ResponseCreate,
    ResponseDone,
    SessionUpdate,
)
from palmimo_controller.voices import SAMPLE_DIR, VOICES


#: What every voice says. Japanese, because the AI modes speak Japanese.
SAMPLE_LINE = "こんにちは、パルミモです。よろしくね！"

_INSTRUCTIONS = (
    "You are a voice sample recorder. Read the user's text aloud exactly as written, "
    "in a friendly, cheerful tone, and say nothing else."
)


async def record(voice: str, api_key: str, model: str) -> bytes:
    """Return *voice* reading `SAMPLE_LINE`, as 16-bit mono PCM at `API_RATE`."""
    pcm = bytearray()
    async with RealtimeClient.connect(model=model, api_key=api_key) as client:
        await client.send(SessionUpdate(instructions=_INSTRUCTIONS, voice=voice, rate=API_RATE))
        await client.send(ItemCreate.text(SAMPLE_LINE))
        await client.send(ResponseCreate(output_modalities=["audio"]))
        async for event in client.events():
            if isinstance(event, AudioDelta):
                pcm.extend(base64.b64decode(event.delta))
            elif isinstance(event, ErrorEvent):
                raise RuntimeError(f"{voice}: {event.error}")
            elif isinstance(event, ResponseDone):
                break
    if not pcm:
        raise RuntimeError(f"{voice}: the response carried no audio")
    return bytes(pcm)


def save(voice: str, pcm: bytes) -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    with wave.open(str(SAMPLE_DIR / f"{voice}.wav"), "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(API_RATE)
        clip.writeframes(pcm)


async def main(voices: list[str]) -> int:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is not set.", file=sys.stderr)
        return 1
    unknown = [voice for voice in voices if voice not in VOICES]
    if unknown:
        print(f"Unknown voice(s): {', '.join(unknown)}. Choose from: {', '.join(VOICES)}", file=sys.stderr)
        return 1
    model = os.environ.get("CONTROLLER_REALTIME_MODEL") or DEFAULT_MODEL
    for voice in voices:
        pcm = await record(voice, api_key, model)
        save(voice, pcm)
        print(f"{voice}: {len(pcm) / (API_RATE * 2):.1f} s -> {SAMPLE_DIR / (voice + '.wav')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:] or list(VOICES))))
