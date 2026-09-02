"""Tests for :mod:`palmimo_wakeword_agent.wiring`'s servo-driver build/degrade seam."""

from __future__ import annotations

import pytest
from openai import OpenAI

from palmimo_wakeword_agent.settings import WakewordAgentSettings
from palmimo_wakeword_agent.wiring import _GEMINI_OPENAI_BASE_URL, _build_servo_driver, _chat_client_for


class FakeServoDriver:
    """Fake standing in for a `DynamixelDriver`-shaped object."""

    def __init__(self, port: str | None = None) -> None:
        self.port = port
        self.connect_called = False

    def connect(self) -> None:
        self.connect_called = True


def _settings(**overrides: object) -> WakewordAgentSettings:
    return WakewordAgentSettings.with_env_file(None).model_copy(update=overrides)


def test_build_servo_driver_returns_connected_driver_on_success() -> None:
    built: list[FakeServoDriver] = []

    def factory(*, port: str | None) -> FakeServoDriver:
        driver = FakeServoDriver(port=port)
        built.append(driver)
        return driver

    settings = _settings(servo=True, servo_port="/dev/ttyACM0")
    driver = _build_servo_driver(settings, driver_factory=factory)

    assert driver is built[0]
    assert built[0].port == "/dev/ttyACM0"
    assert built[0].connect_called is True


def test_build_servo_driver_returns_none_when_servo_disabled() -> None:
    def factory(*, port: str | None) -> FakeServoDriver:
        raise AssertionError("driver_factory should not be called when settings.servo is False")

    settings = _settings(servo=False)
    driver = _build_servo_driver(settings, driver_factory=factory)

    assert driver is None


def test_build_servo_driver_returns_none_on_port_detection_error(capsys: pytest.CaptureFixture[str]) -> None:
    from palmimo_sdk import PortDetectionError

    class RaisingDriver:
        def __init__(self, *, port: str | None) -> None:
            pass

        def connect(self) -> None:
            raise PortDetectionError("Servo bus not found. Check connection or specify with --port.")

    settings = _settings(servo=True)
    driver = _build_servo_driver(settings, driver_factory=RaisingDriver)

    assert driver is None
    assert "servo bus not available" in capsys.readouterr().out


# --- _chat_client_for, tested directly ---


def test_chat_client_for_routes_gemini_model_to_google_endpoint() -> None:
    settings = _settings(command_model="gemini-3.5-flash-lite", gemini_api_key="gk-test-123")
    openai_client = OpenAI(api_key="sk-test-123")

    chat_client = _chat_client_for(settings, openai_client)

    assert chat_client is not openai_client
    assert str(chat_client.base_url) == _GEMINI_OPENAI_BASE_URL
    assert chat_client.api_key == "gk-test-123"


@pytest.mark.parametrize("model", ["gpt-5-nano", "gemini/gemini-3.5-flash-lite"])
def test_chat_client_for_reuses_openai_client_for_non_gemini_dash_model(model: str) -> None:
    # Only the bare `gemini-` API model-ID prefix routes to Google; a
    # LiteLLM-style `gemini/` string is not this example's format and must
    # not silently pick the Gemini key.
    settings = _settings(command_model=model)
    openai_client = OpenAI(api_key="sk-test-123")

    chat_client = _chat_client_for(settings, openai_client)

    assert chat_client is openai_client


def test_build_servo_driver_returns_none_on_generic_exception(capsys: pytest.CaptureFixture[str]) -> None:
    class RaisingDriver:
        def __init__(self, *, port: str | None) -> None:
            pass

        def connect(self) -> None:
            raise RuntimeError("bus handshake failed")

    settings = _settings(servo=True)
    driver = _build_servo_driver(settings, driver_factory=RaisingDriver)

    assert driver is None
    assert "servo bus not available" in capsys.readouterr().out
