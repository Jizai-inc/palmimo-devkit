"""NameCallWatch turns transcripts into detections, and drops the rest.

The transcripts here are real: they came back from the transcription API while
someone called the robot into a ReSpeaker, and from the same microphone in the
same session while deliberately saying words that share sounds with the name.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from palmimo_companion_agent.core.hearing import NameCallWatch
from palmimo_companion_agent.core.perception import Detection, DetectionKind, merge


async def test_a_call_becomes_a_name_call_detection() -> None:
    watch = NameCallWatch()
    iterator = watch.watch()
    task = asyncio.ensure_future(anext(iterator))
    await asyncio.sleep(0)  # let watch() capture the running loop

    watch.heard("Parumimo.")

    detection = await asyncio.wait_for(task, 1.0)
    assert detection.kind is DetectionKind.NAME_CALL


async def test_the_summary_carries_what_was_said_after_the_name() -> None:
    watch = NameCallWatch()
    iterator = watch.watch()
    task = asyncio.ensure_future(anext(iterator))
    await asyncio.sleep(0)

    watch.heard("パルミーモ、踊って")

    detection = await asyncio.wait_for(task, 1.0)
    assert "踊って" in detection.summary


@pytest.mark.parametrize("transcript", ["ハーモニカを吹いた。", "パルマ産のハムを買った。", "丸見えになっている。", ""])
async def test_ordinary_speech_produces_nothing(transcript: str) -> None:
    watch = NameCallWatch()
    iterator = watch.watch()
    task = asyncio.ensure_future(anext(iterator))
    await asyncio.sleep(0)

    watch.heard(transcript)

    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()


def test_a_call_before_the_loop_is_running_is_dropped_not_raised() -> None:
    """The speech pipeline must never lose an utterance to a reflex that is not up yet."""
    NameCallWatch().heard("Parumimo.")  # no watch() yet; must not raise


async def test_a_backlog_is_dropped_rather_than_answered_late() -> None:
    """Answering a call from ten seconds ago is a bug, not a courtesy."""
    watch = NameCallWatch()
    iterator = watch.watch()
    pending = asyncio.ensure_future(anext(iterator))
    await asyncio.sleep(0)  # let watch() capture the running loop
    watch.heard("パルミーモ")
    await asyncio.wait_for(pending, 1.0)  # the consumer takes the first one

    # Nobody is consuming now -- a real reflex is busy waving for several
    # seconds while calls keep arriving.
    for _ in range(50):
        watch.heard("パルミーモ")
    await asyncio.sleep(0.05)

    drained = 0
    while True:
        try:
            await asyncio.wait_for(anext(iterator), 0.05)
        except TimeoutError:
            break
        drained += 1
    assert drained <= 5, "the queue is bounded, so a burst cannot pile up"


async def test_merge_interleaves_two_senses() -> None:
    async def source(kind: DetectionKind, count: int) -> AsyncIterator[Detection]:
        for i in range(count):
            yield Detection(summary=f"{kind}-{i}", kind=kind)
            await asyncio.sleep(0)

    seen = [d.kind async for d in merge(source(DetectionKind.WAVE, 2), source(DetectionKind.NAME_CALL, 2))]

    assert sorted(seen) == [DetectionKind.NAME_CALL, DetectionKind.NAME_CALL, DetectionKind.WAVE, DetectionKind.WAVE]


async def test_merge_of_one_source_is_a_pass_through() -> None:
    async def source() -> AsyncIterator[Detection]:
        yield Detection(summary="only", kind=DetectionKind.FACE)

    assert [d.summary async for d in merge(source())] == ["only"]
