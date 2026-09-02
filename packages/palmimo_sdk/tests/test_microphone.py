"""Microphone resource tests.

Only the hardware bit (the ``arecord`` / ``rec`` subprocess) is faked, via the
``runner`` seam — the real per-OS command builder and the open/record control
flow run, so the capture behaviour is genuinely exercised without a mic.
"""

from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from palmimo_sdk.io import Microphone, MicrophoneConfig, _mic_registry
from palmimo_sdk.io.microphone import _record_command


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: bytes = b"RIFFfake-wav-bytes") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


class _FakeRunner:
    """Records each call and returns a queued (or default) result."""

    def __init__(self, results: list[_FakeResult] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._results = list(results) if results else None

    def __call__(self, args: list[str], **kwargs: Any) -> _FakeResult:
        self.calls.append((args, kwargs))
        if self._results:
            return self._results.pop(0)
        return _FakeResult()


def _mic(
    runner: _FakeRunner,
    system: str = "Linux",
    config: MicrophoneConfig | None = None,
    processors: Sequence[Any] = (),
) -> Microphone:
    return Microphone(config=config, runner=runner, system=system, processors=processors)


def test_record_command_linux_uses_arecord_with_device() -> None:
    cfg = MicrophoneConfig(device="plughw:2,0", sample_rate=16000, channels=1)
    args = _record_command("Linux", cfg, 5)
    assert args[0] == "arecord"
    assert "-D" in args and args[args.index("-D") + 1] == "plughw:2,0"
    assert "-f" in args and args[args.index("-f") + 1] == "S16_LE"
    assert args[args.index("-r") + 1] == "16000"
    assert args[args.index("-d") + 1] == "5"  # duration, integer seconds
    # WAV stream to stdout: `-t wav` immediately precedes the `-` output sink.
    assert args[-3:] == ["-t", "wav", "-"]


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0.5, "1"),  # sub-second rounds up to 1, never `-d 0` (= record forever)
        (3.0, "3"),  # whole seconds pass through
        (3.5, "4"),  # fractional seconds round up so the tail isn't cut off
    ],
)
def test_record_command_linux_rounds_duration_up(seconds: float, expected: str) -> None:
    cfg = MicrophoneConfig(device="plughw:2,0")
    args = _record_command("Linux", cfg, seconds)
    assert args[args.index("-d") + 1] == expected


def test_record_command_macos_uses_rec_and_ignores_device() -> None:
    cfg = MicrophoneConfig(device="plughw:9,9", sample_rate=44100, channels=2)
    args = _record_command("Darwin", cfg, 3)
    assert args[0] == "rec"
    assert "plughw:9,9" not in args  # macOS rec records from the default input
    assert args[args.index("-r") + 1] == "44100"
    assert args[args.index("-c") + 1] == "2"
    assert args[-3:] == ["trim", "0", "3"]
    # sox grammar is `rec [format-opts] outfile [effects]`: every format flag
    # must precede the `-` output, else sox reads them as effects and errors.
    out = args.index("-")
    for flag in ("-c", "-r", "-b", "-t"):
        assert args.index(flag) < out
    assert out < args.index("trim")  # effects come after the output sink


def test_record_returns_wav_bytes_on_success() -> None:
    runner = _FakeRunner([_FakeResult(0, b"RIFF....data"), _FakeResult(0, b"RIFF-audio")])
    mic = _mic(runner)
    audio = mic.record(5)
    assert audio == b"RIFF-audio"  # second call is the real record (first is the probe)


def test_record_returns_none_on_nonzero_returncode() -> None:
    # probe OK, record fails
    runner = _FakeRunner([_FakeResult(0), _FakeResult(returncode=1, stdout=b"")])
    assert _mic(runner).record(5) is None


def test_record_returns_none_on_empty_stream() -> None:
    runner = _FakeRunner([_FakeResult(0), _FakeResult(0, stdout=b"")])
    assert _mic(runner).record(5) is None


def test_record_returns_none_when_device_unavailable() -> None:
    # probe itself fails -> open() raises -> record() swallows and returns None
    runner = _FakeRunner([_FakeResult(returncode=1)])
    mic = _mic(runner)
    assert mic.record(5) is None
    assert not mic.is_open


def test_open_raises_when_device_cannot_be_accessed() -> None:
    mic = _mic(_FakeRunner([_FakeResult(returncode=1)]))
    with pytest.raises(RuntimeError, match="Cannot access microphone"):
        mic.open()


def test_open_is_idempotent_probes_once() -> None:
    runner = _FakeRunner()
    mic = _mic(runner)
    mic.open()
    mic.open()
    assert mic.is_open
    assert len(runner.calls) == 1  # second open() is a no-op


def test_record_auto_opens_then_reuses_probe() -> None:
    runner = _FakeRunner()
    mic = _mic(runner)
    mic.record(2)
    mic.record(2)
    # 1 probe + 2 records (probe runs once because record auto-opens idempotently)
    assert len(runner.calls) == 3


def test_close_resets_open_state() -> None:
    mic = _mic(_FakeRunner())
    mic.open()
    assert mic.is_open
    mic.close()
    assert not mic.is_open


def test_context_manager_opens_and_closes() -> None:
    mic = _mic(_FakeRunner())
    with mic as m:
        assert m.is_open
    assert not mic.is_open


def test_record_duration_drives_timeout() -> None:
    runner = _FakeRunner()
    mic = _mic(runner)
    mic.record(8)
    record_call = runner.calls[-1]  # last call is the record
    assert record_call[1]["timeout"] == 8 + 5
    assert record_call[1]["capture_output"] is True


# ----------------------------------------------------------------------
# MicStream delegation — a Microphone shares device_key with a running
# MicStream, which already owns the hardware.
# ----------------------------------------------------------------------
class _FakeMicStream:
    """Duck-types the bit of MicStream that Microphone delegates to."""

    def __init__(self, record_result: bytes | None = b"RIFF-stream-audio") -> None:
        self.record_calls: list[float] = []
        self._record_result = record_result

    def record(self, seconds: float) -> bytes | None:
        self.record_calls.append(seconds)
        return self._record_result


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Every test that registers a fake stream must unregister it; safety net."""
    yield
    _mic_registry._streams.clear()


def test_open_skips_probe_when_mic_stream_owns_the_device_key() -> None:
    stream = _FakeMicStream()
    _mic_registry.register("shared-mic", stream)
    runner = _FakeRunner()
    mic = _mic(runner, config=MicrophoneConfig(device_key="shared-mic"))
    mic.open()
    assert mic.is_open
    assert runner.calls == []  # no arecord/rec probe was shelled out
    _mic_registry.unregister("shared-mic", stream)


def test_record_delegates_to_mic_stream_when_one_owns_the_device_key() -> None:
    stream = _FakeMicStream(record_result=b"RIFF-from-stream")
    _mic_registry.register("shared-mic", stream)
    runner = _FakeRunner()
    mic = _mic(runner, config=MicrophoneConfig(device_key="shared-mic"))
    audio = mic.record(3)
    assert audio == b"RIFF-from-stream"
    assert stream.record_calls == [3]
    assert runner.calls == []  # the arecord/rec runner was never invoked
    _mic_registry.unregister("shared-mic", stream)


def test_record_falls_back_to_runner_when_no_mic_stream_registered() -> None:
    runner = _FakeRunner()
    mic = _mic(runner, config=MicrophoneConfig(device_key="unshared-mic"))
    audio = mic.record(2)
    assert audio == b"RIFFfake-wav-bytes"  # from the default _FakeResult
    assert len(runner.calls) == 2  # probe + record, unchanged from before


# ----------------------------------------------------------------------
# processors — Microphone defaults to () (no denoising); opt-in only. Fake
# AudioProcessor implementations here just append a marker so the exact
# ordering / call count is easy to assert; they don't need to be valid WAV
# to exercise the pass-through wiring under test.
# ----------------------------------------------------------------------
class _MarkerProcessor:
    def __init__(self, marker: bytes) -> None:
        self.marker = marker
        self.calls: list[bytes] = []

    def process(self, wav: bytes) -> bytes:
        self.calls.append(wav)
        return wav + self.marker


class _RaisingProcessor:
    def process(self, wav: bytes) -> bytes:
        raise RuntimeError("boom")


def test_record_applies_processors_in_order_on_the_direct_capture_path() -> None:
    runner = _FakeRunner([_FakeResult(0), _FakeResult(0, b"RIFF-raw")])
    proc_a = _MarkerProcessor(b"-A")
    proc_b = _MarkerProcessor(b"-B")
    mic = _mic(runner, processors=[proc_a, proc_b])

    audio = mic.record(5)

    assert audio == b"RIFF-raw-A-B"
    assert proc_a.calls == [b"RIFF-raw"]
    assert proc_b.calls == [b"RIFF-raw-A"]  # fed proc_a's output, not the raw capture


def test_record_does_not_apply_processors_on_the_mic_stream_delegation_path() -> None:
    stream = _FakeMicStream(record_result=b"RIFF-from-stream")
    _mic_registry.register("shared-mic-proc", stream)
    proc = _MarkerProcessor(b"-X")
    runner = _FakeRunner()
    mic = _mic(runner, config=MicrophoneConfig(device_key="shared-mic-proc"), processors=[proc])

    audio = mic.record(3)

    assert audio == b"RIFF-from-stream"  # unmodified — the MicStream side already processed it
    assert proc.calls == []
    _mic_registry.unregister("shared-mic-proc", stream)


def test_record_returns_none_when_a_processor_raises(caplog: pytest.LogCaptureFixture) -> None:
    runner = _FakeRunner([_FakeResult(0), _FakeResult(0, b"RIFF-raw")])
    mic = _mic(runner, processors=[_RaisingProcessor()])

    with caplog.at_level("WARNING"):
        audio = mic.record(5)

    assert audio is None
    assert any("processor" in r.message.lower() for r in caplog.records)
