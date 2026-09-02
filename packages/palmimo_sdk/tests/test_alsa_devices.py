"""Device resolution for the two shell-out audio paths."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from palmimo_sdk.io import resolve_alsa_device
from palmimo_sdk.io.microphone import Microphone, MicrophoneConfig, _record_command
from palmimo_sdk.io.speaker import _playback_argv_candidates, _with_alsa_device


# Verbatim `aplay -l` from a Raspberry Pi 5 running Raspberry Pi OS Lite with
# the microphone array attached after boot -- the ordering that motivated
# resolving by name: the array is card 2, so ALSA's default (card 0) is HDMI.
APLAY_L = """**** List of PLAYBACK Hardware Devices ****
card 0: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 1: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: ArrayUAC10 [ReSpeaker 4 Mic Array (UAC1.0)], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


def _runner(stdout: str = APLAY_L, returncode: int = 0) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    return run


def test_resolve_matches_the_card_id() -> None:
    assert resolve_alsa_device("ArrayUAC10", kind="playback", run=_runner()) == "plughw:CARD=ArrayUAC10,DEV=0"


def test_resolve_matches_the_long_name_too() -> None:
    """`ReSpeaker` appears only in the bracketed name, not in the card id."""
    assert resolve_alsa_device("ReSpeaker", kind="playback", run=_runner()) == "plughw:CARD=ArrayUAC10,DEV=0"


def test_resolve_is_case_insensitive() -> None:
    assert resolve_alsa_device("respeaker", kind="playback", run=_runner()) == "plughw:CARD=ArrayUAC10,DEV=0"


def test_resolve_names_the_card_by_id_not_by_index() -> None:
    """The point of the exercise: the answer must not contain the card number,
    which moves when the device is attached at a different time."""
    resolved = resolve_alsa_device("ReSpeaker", kind="playback", run=_runner())
    assert resolved is not None
    assert "CARD=ArrayUAC10" in resolved
    assert "plughw:2" not in resolved


@pytest.mark.parametrize("hint", [None, ""])
def test_no_hint_means_no_preference(hint: str | None) -> None:
    """No hint must not even read the device list.

    The runner records instead of raising: `resolve_alsa_device` catches
    `Exception` around the call, so a raising probe would be swallowed and the
    test would pass with the guard deleted.
    """
    seen: list[list[str]] = []

    def record(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=APLAY_L, stderr="")

    assert resolve_alsa_device(hint, kind="playback", run=record) is None
    assert seen == []


def test_unmatched_hint_falls_back_to_the_alsa_default() -> None:
    assert resolve_alsa_device("Nonexistent", kind="playback", run=_runner()) is None


def test_missing_listing_tool_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    with caplog.at_level(logging.WARNING):
        assert resolve_alsa_device("ReSpeaker", kind="capture", run=run) is None
    assert "arecord" in caplog.text


def test_failed_listing_falls_back() -> None:
    assert resolve_alsa_device("ReSpeaker", kind="playback", run=_runner(returncode=1)) is None


def test_capture_reads_the_capture_list() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=APLAY_L, stderr="")

    resolve_alsa_device("ReSpeaker", kind="capture", run=run)
    assert seen == [["arecord", "-l"]]


def test_playback_reads_the_playback_list() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=APLAY_L, stderr="")

    resolve_alsa_device("ReSpeaker", kind="playback", run=run)
    assert seen == [["aplay", "-l"]]


def test_with_alsa_device_addresses_aplay() -> None:
    assert _with_alsa_device(["aplay", "/tmp/x.wav"], "plughw:CARD=ArrayUAC10,DEV=0") == [
        "aplay",
        "-D",
        "plughw:CARD=ArrayUAC10,DEV=0",
        "/tmp/x.wav",
    ]


@pytest.mark.parametrize("argv", [["play", "/tmp/x.wav"], ["afplay", "/tmp/x.wav"], []])
def test_with_alsa_device_leaves_other_players_alone(argv: list[str]) -> None:
    """Only aplay takes -D; guessing at the others' flags would break playback."""
    assert _with_alsa_device(list(argv), "plughw:CARD=ArrayUAC10,DEV=0") == argv


def test_with_alsa_device_is_a_no_op_without_a_device() -> None:
    assert _with_alsa_device(["aplay", "/tmp/x.wav"], None) == ["aplay", "/tmp/x.wav"]


def test_playback_candidates_are_unchanged_by_this_change(tmp_path: Path) -> None:
    """The candidate list itself still carries no device -- resolution is applied
    to it afterwards, so a test that stubs this function keeps working."""
    for argv in _playback_argv_candidates(tmp_path / "x.wav"):
        assert "-D" not in argv


def test_microphone_resolves_its_device_from_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    import palmimo_sdk.io.microphone as microphone_module

    calls: list[str | None] = []

    def fake_resolve(hint: str | None, **kwargs: object) -> str:
        calls.append(hint)
        return "plughw:CARD=ArrayUAC10,DEV=0"

    monkeypatch.setattr(microphone_module, "resolve_alsa_device", fake_resolve)
    mic = Microphone(MicrophoneConfig(device_name_hint="ReSpeaker"), system="Linux")
    assert mic._capture_config().device == "plughw:CARD=ArrayUAC10,DEV=0"
    assert calls == ["ReSpeaker"]
    # Resolved once and remembered: a listing per utterance would be waste.
    mic._capture_config()
    assert calls == ["ReSpeaker"]


def test_microphone_retries_after_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """A miss is not remembered: it is exactly what attaching the device fixes."""
    import palmimo_sdk.io.microphone as microphone_module

    answers = [None, "plughw:CARD=ArrayUAC10,DEV=0"]

    def fake_resolve(hint: str | None, **kwargs: object) -> str | None:
        return answers.pop(0)

    monkeypatch.setattr(microphone_module, "resolve_alsa_device", fake_resolve)
    mic = Microphone(MicrophoneConfig(device_name_hint="ReSpeaker"), system="Linux")
    assert mic._capture_config().device == "default"
    assert mic._capture_config().device == "plughw:CARD=ArrayUAC10,DEV=0"
    assert answers == []


def test_microphone_open_failure_does_not_resolve_a_second_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe's failure message must report the device that was probed.

    A miss is not remembered, so re-asking while formatting the error would
    fork a second `arecord -l`, could name a device this probe never touched,
    and would memoize that late hit as a side effect of building a string.
    """
    import palmimo_sdk.io.microphone as microphone_module

    answers: list[str | None] = [None, "plughw:CARD=ArrayUAC10,DEV=0"]

    def fake_resolve(hint: str | None, **kwargs: object) -> str | None:
        return answers.pop(0)

    class _Failed:
        returncode = 1

    monkeypatch.setattr(microphone_module, "resolve_alsa_device", fake_resolve)
    mic = Microphone(MicrophoneConfig(device_name_hint="ReSpeaker"), system="Linux")
    monkeypatch.setattr(mic, "_run", lambda args, timeout: _Failed())

    with pytest.raises(RuntimeError, match="'default'"):
        mic.open()

    # The second answer is still queued: the failure path asked nobody.
    assert answers == ["plughw:CARD=ArrayUAC10,DEV=0"]
    assert mic._resolved is None


def test_speaker_charges_the_device_lookup_to_the_utterance_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """say_timeout_s is documented as the whole utterance. The lookup shells
    out with its own multi-second ceiling, so time spent there has to come out
    of the playback timeout rather than extend the total past what was asked."""
    import palmimo_sdk.io.speaker as speaker_module

    clock = iter([100.0, 100.75])  # lookup takes 0.75s

    def slow_lookup(hint: str | None, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(speaker_module, "resolve_alsa_device", slow_lookup)
    monkeypatch.setattr(speaker_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(speaker_module, "_playback_argv_candidates", lambda path: [["aplay", str(path)]])

    timeouts: list[float] = []

    class _Result:
        returncode = 0

    def _fake_run_playback(args: list[str], timeout: float, handle: object | None = None) -> _Result:
        timeouts.append(timeout)
        return _Result()

    spk = speaker_module.Speaker(speaker_module.SpeakerConfig(device_name_hint="ReSpeaker"))
    monkeypatch.setattr(spk, "_run_playback", _fake_run_playback)

    spk._play(Path("/tmp/x.wav"), timeout=5.0)

    assert timeouts == [pytest.approx(4.25)]


def test_speaker_retries_after_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same rule on the playback side."""
    import palmimo_sdk.io.speaker as speaker_module

    answers: list[str | None] = [None, "plughw:CARD=ArrayUAC10,DEV=0"]
    monkeypatch.setattr(speaker_module, "resolve_alsa_device", lambda hint, **kwargs: answers.pop(0))
    spk = speaker_module.Speaker(speaker_module.SpeakerConfig(device_name_hint="ReSpeaker"))
    assert spk._playback_device() is None
    assert spk._playback_device() == "plughw:CARD=ArrayUAC10,DEV=0"


def test_speaker_does_not_resolve_when_no_candidate_player_takes_a_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS plays through ``afplay``, which takes no ``-D``, so the argv rewrite
    discards whatever resolution returns. Looking it up anyway would fork a
    missing ``aplay -l`` and warn about it once per utterance -- the capture
    side already skips it (see test_microphone_on_macos_does_not_resolve)."""
    import palmimo_sdk.io.speaker as speaker_module

    hints: list[str | None] = []

    def _record_hint(hint: str | None, **_kwargs: object) -> None:
        hints.append(hint)
        return None

    monkeypatch.setattr(speaker_module, "resolve_alsa_device", _record_hint)
    monkeypatch.setattr(speaker_module, "_playback_argv_candidates", lambda path: [["afplay", str(path)]])

    played: list[list[str]] = []

    class _Result:
        returncode = 0

    def _fake_run_playback(args: list[str], timeout: float, handle: object | None = None) -> _Result:
        played.append(args)
        return _Result()

    spk = speaker_module.Speaker(speaker_module.SpeakerConfig(device_name_hint="ReSpeaker"))
    monkeypatch.setattr(spk, "_run_playback", _fake_run_playback)

    wav = Path("/tmp/x.wav")
    spk._play(wav, timeout=1.0)

    assert hints == []
    assert played == [["afplay", str(wav)]]


def test_microphone_without_a_hint_uses_the_alsa_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import palmimo_sdk.io.microphone as microphone_module

    monkeypatch.setattr(microphone_module, "resolve_alsa_device", lambda hint, **kwargs: None)
    mic = Microphone(MicrophoneConfig(), system="Linux")
    assert mic._capture_config().device == "default"


def test_microphone_honours_an_explicit_device() -> None:
    mic = Microphone(MicrophoneConfig(device="plughw:9,9"), system="Linux")
    assert mic._capture_config().device == "plughw:9,9"
    assert "plughw:9,9" in _record_command("Linux", mic._capture_config(), 1)


def test_microphone_on_macos_does_not_resolve() -> None:
    """`rec` records from the default input; there is no ALSA list to read."""
    mic = Microphone(MicrophoneConfig(device_name_hint="ReSpeaker"), system="Darwin")
    assert mic._capture_config().device is None


def test_record_command_falls_back_when_the_device_is_unresolved() -> None:
    argv = _record_command("Linux", MicrophoneConfig(), 1)
    assert argv[argv.index("-D") + 1] == "default"
