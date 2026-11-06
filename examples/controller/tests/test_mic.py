"""Behavior of `palmimo_controller.main._open_mic_stream`: echo cancellation first, noise removal as fallback."""

from typing import Any, cast

from palmimo_controller.main import _open_mic_stream
from palmimo_sdk import Denoiser


class FakeStream:
    def __init__(self, *, processors: list[Any] | None = None, fail: bool = False) -> None:
        self.processors = processors
        self.fail = fail
        self.opened = False

    def open(self) -> None:
        if self.fail:
            raise RuntimeError("no input device with >= 6 channels")
        self.opened = True


def test_open_mic_stream_prefers_the_default_echo_canceller() -> None:
    stream: Any = _open_mic_stream(open_stream=cast(Any, FakeStream))
    assert stream.opened and stream.processors is None


def test_open_mic_stream_falls_back_to_noise_removal_without_a_six_channel_input() -> None:
    # Without this, a robot whose mic lacks the 6-channel firmware could never use AI mode.
    def open_stream(**kwargs: Any) -> FakeStream:
        return FakeStream(**kwargs, fail="processors" not in kwargs)

    stream: Any = _open_mic_stream(open_stream=cast(Any, open_stream))
    assert stream.opened
    assert stream.processors is not None and isinstance(stream.processors[0], Denoiser)
