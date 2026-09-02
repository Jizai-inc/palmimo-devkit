"""OpenAiEngine — the hosted TTS backend's contract and its failure paths."""

from __future__ import annotations

import email.message
import io
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any

import pytest

from palmimo_sdk.io.tts.openai import API_KEY_ENV, OpenAiEngine


def _wav_bytes(*, channels: int = 1, width: int = 2, rate: int = 24000, frames: bytes = b"\x01\x00" * 16) -> bytes:
    """A complete little WAV, the shape the API is documented to return."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(frames)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "sk-test-key")


def _synthesize(engine: OpenAiEngine, path: Path, text: str = "こんにちは") -> None:
    """Drive the engine exactly as Speaker does — including the writer's close."""
    voice = engine.load_voice("ja")
    with wave.open(str(path), "wb") as wav_file:
        voice.synthesize(text, wav_file)


def test_preflight_is_network_free_and_only_needs_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("preflight must not touch the network")

    OpenAiEngine(transport=_explode).preflight("ja")

    monkeypatch.delenv(API_KEY_ENV)
    with pytest.raises(RuntimeError, match=API_KEY_ENV):
        OpenAiEngine(transport=_explode).preflight("ja")


def test_synthesize_copies_the_api_format_rather_than_assuming_one(tmp_path: Path) -> None:
    """The written file has to carry the API's own header: a hardcoded rate would
    play back at the wrong speed the day the API changes it."""
    engine = OpenAiEngine(transport=lambda body, key, timeout: _wav_bytes(rate=16000))

    _synthesize(engine, tmp_path / "out.wav")

    with wave.open(str(tmp_path / "out.wav")) as written:
        assert written.getframerate() == 16000
        assert written.getnchannels() == 1
        assert written.getnframes() == 16


def test_a_malformed_body_surfaces_its_own_error_not_the_writers(tmp_path: Path) -> None:
    """HTTP 200 with a body that is not a WAV. Closing a writer that never had its
    format set raises "# channels not specified" from __exit__ and replaces the
    real error -- which also blinds failure_hint."""
    engine = OpenAiEngine(transport=lambda body, key, timeout: b"<html>gateway error</html>")

    with pytest.raises(wave.Error) as caught:
        _synthesize(engine, tmp_path / "out.wav")

    assert "channels not specified" not in str(caught.value)


def test_a_transport_failure_surfaces_its_own_error(tmp_path: Path) -> None:
    def _fails(body: dict[str, Any], key: str, timeout: float) -> bytes:
        raise RuntimeError("OpenAI speech API returned HTTP 401: invalid_api_key")

    engine = OpenAiEngine(transport=_fails)

    with pytest.raises(RuntimeError, match="401"):
        _synthesize(engine, tmp_path / "out.wav")


def test_a_timeout_surfaces_its_own_error(tmp_path: Path) -> None:
    def _times_out(body: dict[str, Any], key: str, timeout: float) -> bytes:
        raise TimeoutError("the read operation timed out")

    engine = OpenAiEngine(transport=_times_out)

    with pytest.raises(TimeoutError, match="timed out"):
        _synthesize(engine, tmp_path / "out.wav")


def test_an_unreachable_api_says_so_rather_than_leaking_urllib(monkeypatch: pytest.MonkeyPatch) -> None:
    """No route / no DNS is the likeliest failure of a network-backed voice on a
    robot that moves between venues. Unwrapped it reads "<urlopen error [Errno
    -3] ...>", which failure_hint has nothing to match on."""

    def _urlopen(request: Any, timeout: float = 0.0) -> Any:
        raise urllib.error.URLError(OSError(-3, "Temporary failure in name resolution"))

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    engine = OpenAiEngine()

    with pytest.raises(RuntimeError) as caught, wave.open(io.BytesIO(), "wb") as wav_file:
        engine.load_voice("ja").synthesize("こんにちは", wav_file)

    assert "unreachable" in str(caught.value)
    assert "name resolution" in str(caught.value)
    assert engine.failure_hint(str(caught.value), "ja")


def test_a_connect_timeout_reads_like_a_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """urlopen wraps a connect timeout in URLError while a read timeout raises
    TimeoutError directly. The caller should not have to tell them apart."""

    def _urlopen(request: Any, timeout: float = 0.0) -> Any:
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    engine = OpenAiEngine(timeout_s=15.0)

    with pytest.raises(TimeoutError) as caught, wave.open(io.BytesIO(), "wb") as wav_file:
        engine.load_voice("ja").synthesize("こんにちは", wav_file)

    assert "timed out after 15s" in str(caught.value)
    assert engine.failure_hint(str(caught.value), "ja")


@pytest.mark.parametrize(
    ("error_text", "expected"),
    [
        ("OpenAI speech API returned HTTP 401: invalid_api_key", API_KEY_ENV),
        ("OpenAI speech API returned HTTP 400: unknown voice 'nope'", "voice"),
        ("OpenAI speech API is unreachable: [Errno -3] Temporary failure", "network"),
        ("the read operation timed out", "network"),
        ("something else entirely", ""),
    ],
)
def test_failure_hint_names_the_failures_that_have_a_fix(error_text: str, expected: str) -> None:
    hint = OpenAiEngine().failure_hint(error_text, "ja")
    if expected:
        assert expected in hint
    else:
        assert hint == ""


def test_the_request_carries_the_configured_settings() -> None:
    sent: dict[str, Any] = {}

    def _capture(body: dict[str, Any], key: str, timeout: float) -> bytes:
        sent.update(body)
        sent["_key"] = key
        sent["_timeout"] = timeout
        return _wav_bytes()

    engine = OpenAiEngine(voice="coral", speed=1.2, instructions="brightly", timeout_s=3.0, transport=_capture)
    with wave.open(io.BytesIO(), "wb") as wav_file:
        engine.load_voice("ja").synthesize("こんにちは", wav_file)

    assert sent["voice"] == "coral"
    assert sent["speed"] == 1.2
    assert sent["instructions"] == "brightly"
    assert sent["response_format"] == "wav"
    assert sent["_key"] == "sk-test-key"
    assert sent["_timeout"] == 3.0


def test_the_real_transport_sends_the_key_as_a_header_and_never_in_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives _default_transport itself. A fake transport can only prove the fake
    does not leak; the header construction and the HTTPError body handling are
    where a real leak would be."""
    seen: dict[str, Any] = {}

    def _urlopen(request: Any, timeout: float = 0.0) -> Any:
        seen["headers"] = dict(request.headers)
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            email.message.Message(),
            io.BytesIO(b'{"error":{"code":"invalid_api_key"}}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    engine = OpenAiEngine(timeout_s=7.0)

    with pytest.raises(RuntimeError) as caught, wave.open(io.BytesIO(), "wb") as wav_file:
        engine.load_voice("ja").synthesize("こんにちは", wav_file)

    # The key travels in the header and nowhere else.
    assert seen["headers"]["Authorization"] == "Bearer sk-test-key"
    assert seen["timeout"] == 7.0
    assert "sk-test-key" not in str(caught.value)
    # The server's own message survives, so failure_hint has something to match.
    assert "401" in str(caught.value)
    assert "invalid_api_key" in str(caught.value)
    assert engine.failure_hint(str(caught.value), "ja")


def test_volume_scaling_says_so_when_the_api_changes_bit_depth(tmp_path: Path) -> None:
    """_scaled reads the buffer as int16. A different width would be garbled
    audio with no exception, which is worse than failing."""
    engine = OpenAiEngine(volume=0.5, transport=lambda body, key, timeout: _wav_bytes(width=1, frames=b"@" * 8))

    with pytest.raises(RuntimeError, match="16-bit"):
        _synthesize(engine, tmp_path / "out.wav")


def test_volume_scaling_without_numpy_names_what_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The lazy import is what keeps this module dependency-free; a bare
    ModuleNotFoundError from inside synthesis says nothing about the fix."""
    monkeypatch.setitem(sys.modules, "numpy", None)
    engine = OpenAiEngine(volume=0.5, transport=lambda body, key, timeout: _wav_bytes())

    with pytest.raises(RuntimeError, match="numpy is not installed"):
        _synthesize(engine, tmp_path / "out.wav")


def test_volume_scales_the_returned_samples(tmp_path: Path) -> None:
    loud = b"\x00\x40" * 8  # 16384, halves cleanly
    engine = OpenAiEngine(volume=0.5, transport=lambda body, key, timeout: _wav_bytes(frames=loud))

    _synthesize(engine, tmp_path / "out.wav")

    with wave.open(str(tmp_path / "out.wav")) as written:
        assert written.readframes(8) == b"\x00\x20" * 8  # 8192
