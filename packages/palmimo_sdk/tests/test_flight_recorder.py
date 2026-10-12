import errno
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from palmimo_sdk import HardwareErrorRead
from palmimo_sdk.flight_recorder import AxisSample, ServoFlightRecorder, default_incident_dir


AXES = {"a": AxisSample(1200, 2000, 2100, 40, 7.4)}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def recorder(tmp_path: Path) -> ServoFlightRecorder:
    return ServoFlightRecorder(tmp_path / "incidents", clock=Clock(), wall_clock=lambda: datetime(2026, 10, 13))


def observe(recorder: ServoFlightRecorder, now: float, **errors: int) -> None:
    recorder.observe_hardware_errors(now, HardwareErrorRead(errors), "forward")


def trigger(recorder: ServoFlightRecorder) -> None:
    observe(recorder, 0, a=0, b=0, c=0)
    observe(recorder, 1, a=32)


def dump(recorder: ServoFlightRecorder) -> list[dict[str, Any]]:
    path = recorder.flush()
    assert path is not None
    assert recorder.last_incident_path == path
    return [json.loads(line) for line in path.read_text().splitlines()]


def errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.ERROR]


def test_recorder_writes_nothing_while_clean(recorder: ServoFlightRecorder) -> None:
    observe(recorder, 0, a=0)
    recorder.capture_fast(1, "idle", AXES)
    recorder.capture_context(1, "idle", AXES, {}, None)
    assert recorder.flush() is None
    assert recorder.last_incident_path is None
    assert not recorder.incident_dir.exists()


def test_recorder_opens_window_on_new_latch_and_writes_after_post_window(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    assert not recorder.window_due(2.99)
    assert recorder.window_due(3)
    recorder.capture_fast(3, "idle", AXES)
    assert not recorder.incident_dir.exists()
    header, row = dump(recorder)
    assert header["schema"] == "palmimo-incident/1"
    assert header["trigger_bits"] == {"a": 32}
    assert header["motion"] == "forward"
    assert header["monotonic_s"] == 1
    assert header["ts"] == "2026-10-13T00:00:00"
    assert header["post_s"] == header["post_s_captured"] == 2
    assert header["truncated"] is False
    assert row["t"] == 2
    assert len(list(recorder.incident_dir.glob("*.jsonl"))) == 1


def test_recorder_fast_rows_keep_pre_latch_samples_before_t_zero(recorder: ServoFlightRecorder) -> None:
    observe(recorder, 0, a=0)
    for i in range(301):
        recorder.capture_fast(i / 30, "forward", AXES)
    observe(recorder, 10, a=32)
    for i in range(301, 361):
        recorder.capture_fast(i / 30, "idle", AXES)
    rows = dump(recorder)[1:]
    assert len(rows) == 240
    assert min(row["t"] for row in rows) <= -4.9
    assert max(row["t"] for row in rows) == 2


def test_recorder_dumps_each_axis_once_per_incident(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    dump(recorder)
    for now in range(2, 10):
        observe(recorder, now, a=32)
        assert recorder.flush() is None
    assert len(list(recorder.incident_dir.glob("*.jsonl"))) == 1


def test_recorder_folds_second_axis_into_open_window_and_caps_tail_at_post_max(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    observe(recorder, 2.5, b=32)
    assert not recorder.window_due(3)
    observe(recorder, 4, c=4)
    assert not recorder.window_due(4.99)
    assert recorder.window_due(5)
    recorder.capture_fast(5, "idle", AXES)
    header = dump(recorder)[0]
    assert header["trigger_bits"] == {"a": 32, "b": 32, "c": 4}
    assert header["post_s"] == 4
    assert header["truncated"] is False


def test_recorder_opens_new_window_for_another_axis_after_dump(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    dump(recorder)
    observe(recorder, 4, b=32)
    assert dump(recorder)[0]["trigger_bits"] == {"b": 32}
    assert len(list(recorder.incident_dir.glob("*.jsonl"))) == 2


def test_recorder_logs_but_does_not_reopen_on_additional_bits_of_latched_axis(
    recorder: ServoFlightRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    trigger(recorder)
    dump(recorder)
    caplog.clear()
    observe(recorder, 4, a=33)
    assert len(errors(caplog)) == 1
    assert recorder.flush() is None
    caplog.clear()
    observe(recorder, 5, a=33)
    assert not errors(caplog)


@pytest.mark.parametrize("missing", ["silent", "unreached"])
def test_recorder_does_not_rearm_on_partial_read_of_incident_servo(recorder: ServoFlightRecorder, missing: str) -> None:
    trigger(recorder)
    dump(recorder)
    read = HardwareErrorRead(silent=("a",)) if missing == "silent" else HardwareErrorRead(unreached=("a", "b", "c"))
    recorder.observe_hardware_errors(3, read, "idle")
    observe(recorder, 4, a=32, b=0, c=0)
    assert recorder.flush() is None
    assert len(list(recorder.incident_dir.glob("*.jsonl"))) == 1


def test_recorder_rearms_when_each_incident_servo_reads_clean(
    recorder: ServoFlightRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    trigger(recorder)
    observe(recorder, 2, b=32)
    dump(recorder)
    caplog.clear()
    observe(recorder, 4, a=0)
    assert not caplog.records
    observe(recorder, 5, b=0)
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1
    observe(recorder, 6, a=32)
    assert dump(recorder)[0]["trigger_bits"] == {"a": 32}


def test_recorder_baseline_latch_logs_without_writing(
    recorder: ServoFlightRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    observe(recorder, 0, a=32)
    assert len(errors(caplog)) == 1
    assert recorder.flush() is None
    assert not recorder.incident_dir.exists()


def test_recorder_baselines_each_axis_on_its_first_answer(recorder: ServoFlightRecorder) -> None:
    recorder.observe_hardware_errors(0, HardwareErrorRead({"a": 0}, silent=("b",)), "idle")
    observe(recorder, 1, a=32)
    assert dump(recorder)[0]["trigger_bits"] == {"a": 32}
    observe(recorder, 4, b=32)
    assert recorder.flush() is None


def test_recorder_does_not_relog_known_latch_after_reconnect(
    recorder: ServoFlightRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    trigger(recorder)
    dump(recorder)
    recorder.reset_for_connect()
    caplog.clear()
    observe(recorder, 4, a=32)
    assert not errors(caplog)
    assert recorder.flush() is None


def test_recorder_keeps_incident_servos_across_reconnect_and_drops_rings(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    recorder.capture_fast(1, "old", AXES)
    recorder.capture_context(1, "old", AXES, {"a": 32}, None)
    recorder.reset_for_connect()
    old_path = recorder.last_incident_path
    assert old_path is not None
    observe(recorder, 4, a=32, b=0)
    observe(recorder, 5, b=32)
    recorder.capture_fast(6, "new", AXES)
    rows = dump(recorder)
    assert rows[0]["trigger_bits"] == {"b": 32}
    assert [row["motion"] for row in rows[1:]] == ["new"]


def test_recorder_marks_window_truncated_from_captured_tail_not_clock(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    assert recorder.window_due(100)
    header = dump(recorder)[0]
    assert header["post_s_captured"] == 0
    assert header["truncated"] is True


def test_recorder_flush_writes_truncated_window(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    recorder.capture_fast(1.5, "idle", AXES)
    header = dump(recorder)[0]
    assert header["post_s_captured"] == 0.5
    assert header["truncated"] is True


def test_recorder_flush_is_noop_without_open_window(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    dump(recorder)
    old_path = recorder.last_incident_path
    assert recorder.flush() is None
    assert recorder.last_incident_path == old_path


def test_recorder_header_carries_last_clean_t(recorder: ServoFlightRecorder) -> None:
    observe(recorder, 10, a=0)
    observe(recorder, 12.5, a=32)
    assert dump(recorder)[0]["last_clean_t"] == -2.5


def test_recorder_context_row_is_written_without_a_leg_sweep(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    recorder.capture_context(2, "idle", {"neck_yaw": AxisSample(None, 2000, None, 50, None)}, {"a": 32}, 1.8)
    row = dump(recorder)[1]
    assert row["tier"] == "context"
    assert row["t_neck"] == pytest.approx(0.8)
    assert row["hardware_errors"] == {"a": 32}
    assert row["axes"]["neck_yaw"]["gap"] is None


@pytest.mark.parametrize("failure", [OSError(errno.ENOSPC, "full"), KeyboardInterrupt()])
def test_recorder_published_file_is_complete_or_absent_when_write_fails(
    recorder: ServoFlightRecorder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: BaseException,
) -> None:
    trigger(recorder)
    original = json.dumps
    calls = 0

    def fail_during_write(*args: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        return original(*args, **kwargs)

    recorder.capture_fast(2, "idle", AXES)
    with monkeypatch.context() as patch:
        patch.setattr(json, "dumps", fail_during_write)
        if isinstance(failure, OSError):
            assert recorder.flush() is None
            assert any(r.levelno == logging.WARNING for r in caplog.records)
        else:
            with pytest.raises(KeyboardInterrupt):
                recorder.flush()
    assert list(recorder.incident_dir.iterdir()) == []
    assert recorder.last_incident_path is None
    observe(recorder, 4, a=32)
    assert recorder.flush() is None


def test_recorder_keeps_complete_tmp_when_publish_fails(
    recorder: ServoFlightRecorder, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    trigger(recorder)
    recorder.capture_fast(3, "idle", AXES)

    def fail_replace(*args: Any) -> None:
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr("os.replace", fail_replace)
    rows = dump(recorder)
    assert rows[0]["trigger_bits"] == {"a": 32}
    assert rows[1]["axes"]["a"]["current"] == 1200
    assert recorder.last_incident_path is not None
    assert recorder.last_incident_path.suffix == ".tmp"
    assert not list(recorder.incident_dir.glob("*.jsonl"))
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_recorder_creates_directory_at_first_flush_not_at_prepare(recorder: ServoFlightRecorder) -> None:
    recorder.prepare()
    assert not recorder.incident_dir.exists()
    trigger(recorder)
    dump(recorder)
    assert recorder.incident_dir.is_dir()


def test_recorder_warns_at_prepare_when_directory_is_not_writable(
    recorder: ServoFlightRecorder, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorder.incident_dir.mkdir()
    monkeypatch.setattr("os.access", lambda *args: False)
    recorder.prepare()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_recorder_reports_leftover_tmp_and_empty_dumps_at_prepare(
    recorder: ServoFlightRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    recorder.incident_dir.mkdir()
    (recorder.incident_dir / "incident-old.jsonl").touch()
    (recorder.incident_dir / "incident-old.jsonl.tmp").write_text("partial")
    recorder.prepare()
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert len(list(recorder.incident_dir.iterdir())) == 2


@pytest.mark.parametrize("large", [False, True])
def test_recorder_warns_when_directory_holds_too_many_dumps(
    recorder: ServoFlightRecorder, caplog: pytest.LogCaptureFixture, large: bool
) -> None:
    recorder.incident_dir.mkdir()
    for i in range(1 if large else 101):
        with (recorder.incident_dir / f"incident-{i}.jsonl").open("wb") as file:
            file.write(b"x")
            if large:
                file.truncate(100_000_001)
    recorder.prepare()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_recorder_file_names_do_not_collide_within_one_second(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    dump(recorder)
    first = recorder.last_incident_path
    observe(recorder, 4, b=32)
    dump(recorder)
    assert first != recorder.last_incident_path
    assert first is not None and first.exists()
    assert len(list(recorder.incident_dir.glob("*.jsonl"))) == 2


def test_recorder_rows_use_monotonic_t_not_wall_clock(tmp_path: Path) -> None:
    wall = datetime(2026, 10, 13)
    recorder = ServoFlightRecorder(tmp_path / "incidents", clock=Clock(), wall_clock=lambda: wall)
    recorder.capture_fast(0.5, "forward", AXES)
    trigger(recorder)
    wall = datetime(1970, 1, 1)
    recorder.capture_context(2, "idle", AXES, {}, None)
    recorder.capture_fast(3, "idle", AXES)
    assert [row["t"] for row in dump(recorder)[1:]] == [-0.5, 1, 2]


def test_recorder_voltage_is_written_in_volts(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    recorder.capture_fast(2, "idle", AXES)
    header, row = dump(recorder)
    assert header["units"]["voltage"] == "V"
    assert row["axes"]["a"]["voltage"] == 7.4
    assert row["axes"]["a"]["gap"] == 100


def test_recorder_flush_and_capture_from_two_threads_do_not_corrupt(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)

    def capture() -> None:
        for i in range(1000):
            recorder.capture_fast(i / 30, "idle", AXES)
            recorder.capture_context(i, "idle", AXES, {}, None)

    with ThreadPoolExecutor(2) as pool:
        capturing = pool.submit(capture)
        flushing = pool.submit(recorder.flush)
        capturing.result(timeout=5)
        path = flushing.result(timeout=5)
    assert path is not None
    assert json.loads(path.read_text().splitlines()[0])["trigger_bits"] == {"a": 32}
    for line in path.read_text().splitlines():
        assert isinstance(json.loads(line), dict)


@pytest.mark.parametrize("state_home", ["/tmp/custom-state", "relative", ""])
def test_default_incident_dir_resolves_at_call_time(monkeypatch: pytest.MonkeyPatch, state_home: str) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", state_home)
    root = Path(state_home) if state_home.startswith("/") else Path.home() / ".local/state"
    assert default_incident_dir() == root / "palmimo/incidents"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert default_incident_dir() == Path.home() / ".local/state/palmimo/incidents"


@pytest.mark.parametrize("stage", ["mkdir", "open", "fsync"])
def test_recorder_filesystem_failure_closes_window_without_publishing(
    recorder: ServoFlightRecorder, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    trigger(recorder)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.ENOSPC, "full")

    with monkeypatch.context() as patch:
        if stage == "fsync":
            patch.setattr("os.fsync", fail)
        else:
            patch.setattr(Path, stage, fail)
        assert recorder.flush() is None
    assert recorder.last_incident_path is None
    assert not list(recorder.incident_dir.glob("*"))
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    observe(recorder, 5, a=32)
    assert recorder.flush() is None


def test_recorder_recovery_within_window_keeps_tail(recorder: ServoFlightRecorder) -> None:
    trigger(recorder)
    observe(recorder, 2, a=0)
    recorder.capture_fast(3, "idle", AXES)
    header = dump(recorder)[0]
    assert header["last_clean_t"] == -1
    assert header["trigger_bits"] == {"a": 32}
    assert header["truncated"] is False


def test_recorder_context_ring_bounds_samples_and_copies_inputs(recorder: ServoFlightRecorder) -> None:
    axes = dict(AXES)
    hardware_errors = {"a": 32}
    for now in range(50):
        recorder.capture_context(now, "idle", axes, hardware_errors, None)
    axes.clear()
    hardware_errors.clear()
    observe(recorder, 0, a=0)
    observe(recorder, 50, a=32)
    rows = dump(recorder)[1:]
    assert len(rows) == 36
    assert rows[0]["t"] == -36
    assert all(row["axes"]["a"]["current"] == 1200 and row["hardware_errors"] == {"a": 32} for row in rows)


def test_recorder_prepare_scan_failure_warns_without_disabling(
    recorder: ServoFlightRecorder, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorder.incident_dir.mkdir()

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.EACCES, "denied")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "glob", fail)
        recorder.prepare()
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    trigger(recorder)
    assert dump(recorder)[0]["trigger_bits"] == {"a": 32}


def test_recorder_can_capture_and_observe_while_dump_io_is_blocked(
    recorder: ServoFlightRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    trigger(recorder)
    entered = Event()
    release = Event()
    replace = os.replace

    def blocked_replace(*args: Any) -> None:
        entered.set()
        assert release.wait(timeout=5)
        replace(*args)

    monkeypatch.setattr(os, "replace", blocked_replace)
    with ThreadPoolExecutor(2) as pool:
        flushing = pool.submit(recorder.flush)
        try:
            assert entered.wait(timeout=5)

            def capture_and_latch() -> None:
                recorder.capture_fast(4, "idle", AXES)
                observe(recorder, 4, b=32)

            pool.submit(capture_and_latch).result(timeout=2)
        finally:
            release.set()
        first = flushing.result(timeout=5)
    assert first is not None
    assert json.loads(first.read_text().splitlines()[0])["trigger_bits"] == {"a": 32}
    assert dump(recorder)[0]["trigger_bits"] == {"b": 32}


def test_recorder_directory_sync_failure_keeps_published_dump(
    recorder: ServoFlightRecorder, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import os

    trigger(recorder)
    sync = os.fsync

    def fail_after_publish(descriptor: int) -> None:
        if list(recorder.incident_dir.glob("*.jsonl")):
            raise OSError(errno.EIO, "sync failed")
        sync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_after_publish)
    assert dump(recorder)[0]["trigger_bits"] == {"a": 32}
    assert recorder.last_incident_path is not None
    assert recorder.last_incident_path.suffix == ".jsonl"
    assert any(r.levelno == logging.WARNING for r in caplog.records)
