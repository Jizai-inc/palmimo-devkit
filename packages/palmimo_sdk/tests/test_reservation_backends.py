"""Backend lifetimes reserve logical resources, even during deferred teardown."""

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from palmimo_sdk import (
    DynamixelDriver,
    FaceDisplay,
    HeadCamera,
    Microphone,
    MicrophoneConfig,
    MicStream,
    Palmimo,
    ResourceBusyError,
    Speaker,
)
from palmimo_sdk.io import camera as camera_module
from palmimo_sdk.io import mic_stream as mic_module
from palmimo_sdk.io import microphone as microphone_module
from palmimo_sdk.io._dynamixel_bus import DynamixelBus
from palmimo_sdk.reservation import ReservationSetupError

from .test_reservation import contender, holder


@pytest.fixture
def directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("PALMIMO_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


@pytest.fixture
def backend_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Any]:
    import cv2
    import dynamixel_sdk

    def create(resource: str) -> Any:
        if resource == "servo_bus":
            port = MagicMock(is_open=False)

            def open_port() -> bool:
                port.is_open = True
                return True

            def close_port() -> None:
                port.is_open = False

            port.openPort.side_effect = open_port
            port.closePort.side_effect = close_port
            monkeypatch.setattr(dynamixel_sdk, "PortHandler", lambda _: port)
            return DynamixelBus("fake", {}, "xc330-m288")
        if resource == "camera":
            cap = MagicMock()
            cap.isOpened.return_value = True
            cap.read.return_value = (False, None)
            monkeypatch.setattr(cv2, "VideoCapture", lambda _: cap)
            return HeadCamera()
        if resource == "display":
            return FaceDisplay(port="fake", serial_factory=lambda *args, **kwargs: MagicMock())
        if resource == "speaker":
            engine = MagicMock(name="fake")

            def synthesize(text: str, wav: Any) -> None:
                wav.setframerate(16000)
                wav.setsampwidth(2)
                wav.setnchannels(1)
                wav.writeframes(b"\x00\x00")

            engine.load_voice.return_value.synthesize.side_effect = synthesize
            return Speaker(engine=engine, player=MagicMock())
        if resource == "microphone":
            return Microphone(runner=lambda *args, **kwargs: MagicMock(returncode=0, stdout=b"wav"), system="Darwin")
        if resource == "mic_stream":
            stream = MagicMock()

            def read(n: int) -> tuple[Any, bool]:
                time.sleep(0.005)
                return np.zeros((n, 1), dtype=np.int16), False

            stream.read.side_effect = read
            return MicStream(device_key=str(id(stream)), processors=[], input_stream_factory=lambda *args: stream)
        raise ValueError(resource)

    return create


def open_backend(backend: Any) -> None:
    (backend.connect if isinstance(backend, (DynamixelBus, FaceDisplay)) else backend.open)()


def close_backend(backend: Any) -> None:
    if isinstance(backend, DynamixelBus):
        backend.disconnect(disable_torque=False)
    elif isinstance(backend, FaceDisplay):
        backend.disconnect()
    else:
        backend.close()


@pytest.mark.parametrize("backend_name", ["servo_bus", "camera", "display", "speaker", "microphone", "mic_stream"])
def test_backend_reserves_until_last_instance_closes(
    directory: Path, backend_factory: Callable[[str], Any], backend_name: str
) -> None:
    resource = "microphone" if backend_name == "mic_stream" else backend_name
    first, second = backend_factory(backend_name), backend_factory(backend_name)
    try:
        open_backend(first)
        open_backend(first)
        open_backend(second)
        assert contender(resource, directory).returncode == 23
        close_backend(first)
        assert contender(resource, directory).returncode == 23
    finally:
        close_backend(first)
        close_backend(second)
    assert contender(resource, directory).returncode == 0


@pytest.mark.parametrize("backend_name", ["servo_bus", "camera", "display", "speaker", "microphone", "mic_stream"])
def test_backend_propagates_busy_before_hardware_open(
    directory: Path, backend_factory: Callable[[str], Any], backend_name: str
) -> None:
    resource = "microphone" if backend_name == "mic_stream" else backend_name
    backend = backend_factory(backend_name)
    try:
        with holder(resource, directory), pytest.raises(ResourceBusyError):
            open_backend(backend)
    finally:
        close_backend(backend)


@pytest.mark.parametrize("backend_name", ["camera", "microphone"])
@pytest.mark.parametrize("error", [ResourceBusyError("camera"), ReservationSetupError("Invalid permissions")])
def test_capture_auto_open_propagates_reservation_error(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    backend_factory: Callable[[str], Any],
    backend_name: str,
    error: Exception,
) -> None:
    backend = backend_factory(backend_name)
    module = camera_module if backend_name == "camera" else microphone_module
    monkeypatch.setattr(module, "acquire_resource", MagicMock(side_effect=error), raising=False)
    with pytest.raises(type(error)):
        if backend_name == "camera":
            backend.read()
        else:
            backend.record(1)


def test_camera_auto_open_reports_external_holder(directory: Path, backend_factory: Callable[[str], Any]) -> None:
    with holder("camera", directory), pytest.raises(ResourceBusyError):
        backend_factory("camera").read()


@pytest.mark.parametrize("failure", ["open", "handshake", "torque"])
def test_servo_failure_closes_port_and_releases_reservation(
    directory: Path, backend_factory: Callable[[str], Any], failure: str
) -> None:
    bus = backend_factory("servo_bus")
    try:
        if failure == "open":
            bus.port_handler.openPort.side_effect = OSError("Cannot open")
            with pytest.raises(ConnectionError):
                bus.connect()
        elif failure == "handshake":
            bus.motors = {"missing": 1}
            bus.packet_handler = MagicMock()
            bus.packet_handler.ping.return_value = (0, -1, 0)
            with pytest.raises(RuntimeError):
                bus.connect()
        else:
            bus.connect()
            bus.disable_torque = MagicMock(side_effect=RuntimeError("Torque write failed"))
            with pytest.raises(RuntimeError):
                bus.disconnect()
        assert not bus.port_handler.is_open
        assert contender("servo_bus", directory).returncode == 0
    finally:
        bus.disconnect(disable_torque=False)


def test_servo_baudrate_configuration_does_not_open_unreserved_port(
    directory: Path, backend_factory: Callable[[str], Any]
) -> None:
    bus = backend_factory("servo_bus")
    bus.port_handler.getBaudRate.return_value = 1000000

    def set_baudrate(rate: int) -> bool:
        bus.port_handler.is_open = True
        bus.port_handler.getBaudRate.return_value = rate
        return True

    bus.port_handler.setBaudRate.side_effect = set_baudrate
    with holder("servo_bus", directory):
        bus.set_baudrate(57600)
        assert not bus.is_connected
        with pytest.raises(ResourceBusyError):
            bus.connect()


def test_palmimo_busy_servo_does_not_open_peripherals(
    directory: Path, backend_factory: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = backend_factory("servo_bus")
    driver = DynamixelDriver(port="fake")

    def connect_bus(*args: object) -> Any:
        bus.connect()
        return bus

    driver._bus_factory = connect_bus
    display = backend_factory("display")
    robot = Palmimo(driver=driver, display=display, auto_wake=False)
    with holder("servo_bus", directory), pytest.raises(ResourceBusyError):
        robot.connect()
    assert not display.is_connected
    assert contender("display", directory).returncode == 0


def test_palmimo_busy_peripheral_rolls_back_prior_resources(
    directory: Path, backend_factory: Callable[[str], Any]
) -> None:
    display, speaker, camera = (backend_factory(name) for name in ("display", "speaker", "camera"))
    robot = Palmimo(display=display, speaker=speaker, camera=camera, auto_wake=False)
    with holder("camera", directory), pytest.raises(ResourceBusyError):
        robot.connect()
    assert contender("display", directory).returncode == 0
    assert contender("speaker", directory).returncode == 0


def test_compute_only_does_not_reserve_resources(directory: Path) -> None:
    robot = Palmimo()
    robot.forward()
    robot.step()
    robot.disconnect()
    assert not directory.exists()


def test_camera_close_timeout_holds_reservation_until_read_finishes(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cv2

    entered, release = threading.Event(), threading.Event()
    cap = MagicMock()
    cap.isOpened.return_value = True

    def read() -> tuple[bool, None]:
        entered.set()
        assert release.wait(5)
        return False, None

    cap.read.side_effect = read
    monkeypatch.setattr(cv2, "VideoCapture", lambda _: cap)
    monkeypatch.setattr(camera_module, "_CLOSE_LOCK_TIMEOUT_S", 0.01)
    camera = HeadCamera()
    camera.open()
    thread = threading.Thread(target=camera.read)
    thread.start()
    try:
        assert entered.wait(2)
        camera.close()
        assert contender("camera", directory).returncode == 23
        assert not cap.release.called
    finally:
        release.set()
        thread.join(timeout=5)
    assert cap.release.called
    camera.close()
    assert contender("camera", directory).returncode == 0


def test_mic_close_timeout_holds_reservation_until_worker_closes(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    stream = MagicMock()

    def read(n: int) -> tuple[Any, bool]:
        entered.set()
        assert release.wait(5)
        return np.zeros((n, 1), dtype=np.int16), False

    stream.read.side_effect = read
    stream.close.side_effect = closed.set
    monkeypatch.setattr(mic_module, "_JOIN_TIMEOUT_S", 0.01)
    mic = MicStream(processors=[], input_stream_factory=lambda *args: stream)
    mic.open()
    try:
        assert entered.wait(2)
        mic.close()
        assert not closed.is_set()
        assert contender("microphone", directory).returncode == 23
    finally:
        release.set()
        assert closed.wait(5)
    assert contender("microphone", directory).returncode == 0


@pytest.mark.parametrize("backend_name", ["camera", "display", "speaker", "microphone", "mic_stream"])
def test_backend_open_failure_releases_reservation(
    directory: Path, monkeypatch: pytest.MonkeyPatch, backend_factory: Callable[[str], Any], backend_name: str
) -> None:
    backend = backend_factory(backend_name)
    error = RuntimeError("Hardware open failed")
    if backend_name == "camera":
        import cv2

        monkeypatch.setattr(cv2, "VideoCapture", MagicMock(side_effect=error))
    elif backend_name == "display":
        backend._serial_factory = MagicMock(side_effect=error)
    elif backend_name == "speaker":
        backend._engine.preflight.side_effect = error
    elif backend_name == "microphone":
        backend._runner = MagicMock(side_effect=error)
    else:
        backend._input_stream_factory = MagicMock(side_effect=error)
    with pytest.raises(RuntimeError):
        open_backend(backend)
    resource = "microphone" if backend_name == "mic_stream" else backend_name
    assert contender(resource, directory).returncode == 0


def test_speaker_say_without_open_reserves_before_enqueue(
    directory: Path, backend_factory: Callable[[str], Any]
) -> None:
    speaker = backend_factory("speaker")
    with holder("speaker", directory), pytest.raises(ResourceBusyError):
        speaker.say("hello")
    speaker.close()


def test_speaker_failed_worker_start_releases_reservation(
    directory: Path, monkeypatch: pytest.MonkeyPatch, backend_factory: Callable[[str], Any]
) -> None:
    speaker = backend_factory("speaker")
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", MagicMock(side_effect=RuntimeError("Cannot start worker")))
        with pytest.raises(RuntimeError):
            speaker.say("hello")
    assert contender("speaker", directory).returncode == 0
    speaker.close()


def test_display_late_open_holds_reservation_until_orphan_closes(directory: Path) -> None:
    from palmimo_sdk import FaceDisplayConnectTimeoutError

    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    serial = MagicMock()
    serial.close.side_effect = closed.set

    def factory(*args: object, **kwargs: object) -> Any:
        entered.set()
        assert release.wait(5)
        return serial

    display = FaceDisplay(port="fake", serial_factory=factory, connect_timeout=0.05)
    try:
        with pytest.raises(FaceDisplayConnectTimeoutError):
            display.connect()
        assert entered.is_set()
        assert contender("display", directory).returncode == 23
        display.disconnect()
        assert contender("display", directory).returncode == 23
    finally:
        release.set()
        assert closed.wait(5)
    assert contender("display", directory).returncode == 0


def test_microphone_delegation_keeps_reservation_after_stream_closes(
    directory: Path, backend_factory: Callable[[str], Any]
) -> None:
    stream = backend_factory("mic_stream")
    mic = Microphone(runner=MagicMock(), system="Darwin")
    mic.config = MicrophoneConfig(device_key=stream.device_key)
    try:
        stream.open()
        mic.open()
        stream.close()
        assert contender("microphone", directory).returncode == 23
    finally:
        stream.close()
        mic.close()
    assert contender("microphone", directory).returncode == 0


@pytest.mark.parametrize("backend_name", ["camera", "display", "servo_bus"])
def test_backend_close_failure_keeps_reservation_for_retry(
    directory: Path, backend_factory: Callable[[str], Any], backend_name: str
) -> None:
    backend = backend_factory(backend_name)
    open_backend(backend)
    handle = (
        backend._cap
        if backend_name == "camera"
        else backend._ser
        if backend_name == "display"
        else backend.port_handler
    )
    closer = (
        handle.release if backend_name == "camera" else handle.close if backend_name == "display" else handle.closePort
    )
    closer.side_effect = RuntimeError("Cannot close device")
    try:
        with pytest.raises(RuntimeError):
            close_backend(backend)
        assert contender(backend_name, directory).returncode == 23
    finally:
        if backend_name == "servo_bus":
            closer.side_effect = lambda: setattr(handle, "is_open", False)
        else:
            closer.side_effect = None
        close_backend(backend)
    assert contender(backend_name, directory).returncode == 0


def test_microphone_close_keeps_reservation_during_active_recording(directory: Path) -> None:
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def runner(*args: object, **kwargs: object) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            entered.set()
            assert release.wait(5)
        return MagicMock(returncode=0, stdout=b"wav")

    mic = Microphone(runner=runner, system="Darwin")
    mic.open()
    thread = threading.Thread(target=mic.record, args=(1,))
    thread.start()
    try:
        assert entered.wait(2)
        mic.close()
        assert contender("microphone", directory).returncode == 23
    finally:
        release.set()
        thread.join(timeout=5)
        mic.close()
    assert contender("microphone", directory).returncode == 0


@pytest.mark.parametrize("stage", ["configure", "profile", "enable"])
def test_driver_arming_failure_closes_port_and_releases_reservation(
    directory: Path, monkeypatch: pytest.MonkeyPatch, backend_factory: Callable[[str], Any], stage: str
) -> None:
    from palmimo_sdk.io import _dynamixel_bus as bus_module

    bus = backend_factory("servo_bus")
    driver = DynamixelDriver(port="fake")
    bus.port_handler.getBaudRate.return_value = driver._baudrate
    bus.configure_motors = MagicMock()
    bus.sync_read = MagicMock(return_value=None)
    bus.sync_write = MagicMock()
    bus.enable_torque = MagicMock()
    operation = {"configure": bus.configure_motors, "profile": bus.sync_write, "enable": bus.enable_torque}[stage]
    operation.side_effect = RuntimeError("Arming failed")
    monkeypatch.setattr(bus_module, "DynamixelBus", lambda **kwargs: bus)
    try:
        with pytest.raises(RuntimeError):
            driver.connect()
        assert not bus.is_connected
        assert contender("servo_bus", directory).returncode == 0
    finally:
        bus.disconnect(disable_torque=False)


@pytest.mark.parametrize("backend_name", ["speaker", "microphone"])
def test_backend_concurrent_open_does_not_leak_reservation(
    directory: Path, monkeypatch: pytest.MonkeyPatch, backend_factory: Callable[[str], Any], backend_name: str
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from palmimo_sdk.io import speaker as speaker_module

    backend = backend_factory(backend_name)
    module = speaker_module if backend_name == "speaker" else microphone_module
    original = module.acquire_resource
    entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    def paused_acquire(resource: str) -> Any:
        nonlocal calls
        reservation = original(resource)
        with calls_lock:
            calls += 1
            first = calls == 1
        if first:
            entered.set()
            assert release.wait(5)
        else:
            second_entered.set()
        return reservation

    monkeypatch.setattr(module, "acquire_resource", paused_acquire)
    if backend_name == "speaker":
        backend._player.start.return_value.communicate.return_value = (b"", b"")
        backend._player.start.return_value.returncode = 0
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(backend.open)
            assert entered.wait(2)
            second = pool.submit(backend.say, "hello") if backend_name == "speaker" else pool.submit(backend.open)
            try:
                second_entered.wait(0.3)
            finally:
                release.set()
            first.result(timeout=5)
            second.result(timeout=5)
    finally:
        release.set()
        backend.close()
    assert contender(backend_name, directory).returncode == 0


@pytest.mark.parametrize("resource", ["servo_bus", "display", "speaker", "camera"])
def test_mcp_probe_propagates_external_resource_holder(
    directory: Path, monkeypatch: pytest.MonkeyPatch, backend_factory: Callable[[str], Any], resource: str
) -> None:
    import palmimo_sdk
    from palmimo_sdk.mcp import __main__ as mcp_main

    backend = backend_factory(resource)
    if resource == "servo_bus":
        driver = DynamixelDriver(port="fake")

        def connect_bus(*args: object) -> Any:
            backend.connect()
            return backend

        driver._bus_factory = connect_bus
        monkeypatch.setattr(palmimo_sdk, "DynamixelDriver", lambda **kwargs: driver)

        def probe_servo() -> Any:
            return mcp_main._build_servo_driver("fake")

        probe = probe_servo
    elif resource == "display":
        monkeypatch.setattr(mcp_main, "FaceDisplay", lambda: backend)
        probe = mcp_main._build_display
    elif resource == "speaker":
        monkeypatch.setattr(mcp_main, "Speaker", lambda *args: backend)
        probe = mcp_main._build_speaker
    else:
        monkeypatch.setattr(mcp_main, "HeadCamera", lambda: backend)
        probe = mcp_main._build_camera
    try:
        with holder(resource, directory) as process, pytest.raises(ResourceBusyError) as caught:
            probe()
        assert caught.value.holder_pid == process.pid
        assert caught.value.holder_app == "companion"
    finally:
        close_backend(backend)
