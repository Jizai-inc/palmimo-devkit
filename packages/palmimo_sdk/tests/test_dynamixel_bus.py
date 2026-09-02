"""Span reads — the address maths and the partial-sweep split, without hardware.

The bus itself needs a serial port, but the two decisions a span read makes
(which bytes to ask for, and what a half-answered sweep means) are pure
functions here, so they are pinned without one.
"""

import pytest

from palmimo_sdk.io._dynamixel_bus import (
    CONTROL_TABLE,
    DynamixelBus,
    SpanRead,
    collect_span,
    slice_field,
    span_of,
)


# Comm results, matching the vendored SDK's robotis_def constants.
COMM_SUCCESS = 0
COMM_TX_FAIL = -1001
COMM_RX_FAIL = -1002

TELEMETRY = ("Present_Current", "Present_Input_Voltage", "Present_Temperature")
# Present_Current(126,2) .. Present_Temperature(146,1) — the range the guards read.
SPAN_START, SPAN_LENGTH = 126, 21


def build_buffer(current: int, decivolts: int, celsius: int) -> list[int]:
    """One motor's answer to the telemetry span, as the servo would send it."""
    buffer = [0] * SPAN_LENGTH
    raw_current = current if current >= 0 else (1 << 16) + current
    buffer[0] = raw_current & 0xFF
    buffer[1] = (raw_current >> 8) & 0xFF
    buffer[144 - SPAN_START] = decivolts & 0xFF
    buffer[145 - SPAN_START] = (decivolts >> 8) & 0xFF
    buffer[146 - SPAN_START] = celsius
    return buffer


def test_span_of_covers_every_requested_register() -> None:
    assert span_of(TELEMETRY) == (SPAN_START, SPAN_LENGTH)


def test_span_of_spans_the_registers_in_between() -> None:
    """The gap between two registers rides along; one request beats two."""
    start, length = span_of(("Present_Current", "Present_Temperature"))
    position_addr, position_size = CONTROL_TABLE["Present_Position"]
    assert start <= position_addr and position_addr + position_size <= start + length


def test_span_of_rejects_an_empty_field_list() -> None:
    with pytest.raises(ValueError):
        span_of(())


def test_span_of_rejects_an_unlisted_register() -> None:
    with pytest.raises(KeyError):
        span_of(("Present_Nonsense",))


def test_slice_field_decodes_each_register_at_its_own_offset() -> None:
    buffer = build_buffer(current=812, decivolts=47, celsius=41)
    assert slice_field(buffer, SPAN_START, "Present_Current") == 812
    assert slice_field(buffer, SPAN_START, "Present_Input_Voltage") == 47
    assert slice_field(buffer, SPAN_START, "Present_Temperature") == 41


def test_slice_field_decodes_current_as_signed() -> None:
    """Present_Current is two's complement — a reverse-torque draw reads negative."""
    buffer = build_buffer(current=-1300, decivolts=47, celsius=41)
    assert slice_field(buffer, SPAN_START, "Present_Current") == -1300


def test_slice_field_leaves_unsigned_registers_alone() -> None:
    """A voltage with the high bit set is a large positive, not a negative."""
    buffer = build_buffer(current=0, decivolts=0xFF01, celsius=0)
    assert slice_field(buffer, SPAN_START, "Present_Input_Voltage") == 0xFF01


def test_collect_span_reads_every_motor_when_all_answer() -> None:
    names = ["leg_1_yaw", "leg_2_yaw"]
    buffers = {n: build_buffer(current=100, decivolts=47, celsius=30) for n in names}
    sweep = collect_span(names, buffers, TELEMETRY, SPAN_START, SPAN_LENGTH)
    assert set(sweep.values) == set(names)
    assert sweep.silent == ()
    assert sweep.unreached == ()


def test_collect_span_keeps_the_answers_collected_before_a_silent_motor() -> None:
    """The point of reading the buffers directly: a failure does not void the rest."""
    names = ["leg_1_yaw", "leg_2_yaw", "leg_3_yaw"]
    buffers: dict[str, list[int]] = {
        "leg_1_yaw": build_buffer(current=100, decivolts=47, celsius=30),
        "leg_2_yaw": [],
        "leg_3_yaw": [],
    }
    sweep = collect_span(names, buffers, TELEMETRY, SPAN_START, SPAN_LENGTH)
    assert set(sweep.values) == {"leg_1_yaw"}
    assert sweep.silent == ("leg_2_yaw",)
    assert sweep.unreached == ("leg_3_yaw",)


def test_collect_span_blames_only_the_motor_that_stopped_the_sweep() -> None:
    """Motors after the silent one were never asked, so they are not silent."""
    names = ["leg_1_yaw", "leg_2_yaw", "leg_3_yaw"]
    buffers: dict[str, list[int]] = {"leg_1_yaw": [], "leg_2_yaw": [], "leg_3_yaw": []}
    sweep = collect_span(names, buffers, TELEMETRY, SPAN_START, SPAN_LENGTH)
    assert sweep.silent == ("leg_1_yaw",)
    assert sweep.unreached == ("leg_2_yaw", "leg_3_yaw")


def test_collect_span_treats_a_short_buffer_as_no_answer() -> None:
    """A truncated reply is not a partial reading; it is not a reading."""
    names = ["leg_1_yaw"]
    buffers = {"leg_1_yaw": build_buffer(current=100, decivolts=47, celsius=30)[:-1]}
    sweep = collect_span(names, buffers, TELEMETRY, SPAN_START, SPAN_LENGTH)
    assert sweep.values == {}
    assert sweep.silent == ("leg_1_yaw",)


def test_collect_span_reports_nothing_for_an_empty_sweep() -> None:
    sweep = collect_span([], {}, TELEMETRY, SPAN_START, SPAN_LENGTH)
    assert sweep.values == {}
    assert sweep.silent == ()
    assert sweep.unreached == ()


class FakeSyncReader:
    """Stands in for ``dynamixel_sdk.GroupSyncRead``, down to how it fails.

    The real reader overwrites its per-motor buffers in place and stops at the
    first motor that does not answer, which is what the span read's failure
    handling is built around — so the fake reproduces exactly that.
    """

    def __init__(self, answers: dict[int, list[int]], tx_results: list[int] | None = None) -> None:
        self.answers = answers
        self.tx_results = tx_results if tx_results is not None else [COMM_SUCCESS]
        self.data_dict: dict[int, list[int]] = {}
        self.start_address = 0
        self.data_length = 0
        self.tx_calls = 0
        self.rx_calls = 0

    def clearParam(self) -> None:  # noqa: N802 - mirrors the vendored SDK's name
        self.data_dict.clear()

    def addParam(self, motor_id: int) -> None:  # noqa: N802 - mirrors the vendored SDK's name
        self.data_dict[motor_id] = []

    def txPacket(self) -> int:  # noqa: N802 - mirrors the vendored SDK's name
        result = self.tx_results[min(self.tx_calls, len(self.tx_results) - 1)]
        self.tx_calls += 1
        return result

    def rxPacket(self) -> int:  # noqa: N802 - mirrors the vendored SDK's name
        self.rx_calls += 1
        for motor_id in self.data_dict:
            answer = self.answers.get(motor_id)
            if answer is None:
                return COMM_RX_FAIL
            self.data_dict[motor_id] = list(answer)
        return COMM_SUCCESS


def make_bus(reader: FakeSyncReader, motors: dict[str, int]) -> DynamixelBus:
    """A bus wired to *reader*, skipping the constructor's dynamixel_sdk import."""
    bus = object.__new__(DynamixelBus)
    bus.motors = motors
    bus.sync_reader = reader
    bus._comm_success = COMM_SUCCESS
    return bus


MOTORS = {"leg_1_yaw": 1, "leg_1_pitch1": 2, "leg_1_pitch2": 3}


def test_sync_read_span_reads_every_motor_that_answers() -> None:
    answer = build_buffer(current=812, decivolts=47, celsius=41)
    bus = make_bus(FakeSyncReader({1: answer, 2: answer, 3: answer}), MOTORS)

    sweep = bus.sync_read_span(TELEMETRY)

    assert sweep.values["leg_1_yaw"]["Present_Current"] == 812
    assert set(sweep.values) == set(MOTORS)
    assert sweep.silent == ()


def test_sync_read_span_blames_only_the_motor_that_stopped_the_sweep() -> None:
    answer = build_buffer(current=100, decivolts=47, celsius=30)
    bus = make_bus(FakeSyncReader({1: answer, 3: answer}), MOTORS)  # motor 2 is quiet

    sweep = bus.sync_read_span(TELEMETRY)

    assert set(sweep.values) == {"leg_1_yaw"}
    assert sweep.silent == ("leg_1_pitch1",)
    assert sweep.unreached == ("leg_1_pitch2",)


def test_sync_read_span_blames_no_motor_when_the_request_never_goes_out() -> None:
    """A dead port is not one servo's fault — blaming one would blacklist a healthy servo."""
    answer = build_buffer(current=100, decivolts=47, celsius=30)
    reader = FakeSyncReader({1: answer, 2: answer, 3: answer}, tx_results=[COMM_TX_FAIL])
    bus = make_bus(reader, MOTORS)

    sweep = bus.sync_read_span(TELEMETRY)

    assert sweep.values == {}
    assert sweep.silent == ()
    assert sweep.unreached == tuple(MOTORS)
    assert reader.rx_calls == 0


def test_sync_read_span_retries_only_the_send() -> None:
    answer = build_buffer(current=100, decivolts=47, celsius=30)
    reader = FakeSyncReader({1: answer, 2: answer, 3: answer}, tx_results=[COMM_TX_FAIL, COMM_SUCCESS])
    bus = make_bus(reader, MOTORS)

    sweep = bus.sync_read_span(TELEMETRY, num_retry=1)

    assert set(sweep.values) == set(MOTORS)
    assert reader.tx_calls == 2
    assert reader.rx_calls == 1  # one receive, so a retry cannot overwrite collected answers


def test_sync_read_span_does_not_re_ask_a_motor_that_stayed_quiet() -> None:
    """Retrying the receive would cost the sweep the answers it already has."""
    answer = build_buffer(current=100, decivolts=47, celsius=30)
    reader = FakeSyncReader({1: answer, 3: answer})  # motor 2 is quiet
    bus = make_bus(reader, MOTORS)

    sweep = bus.sync_read_span(TELEMETRY, num_retry=3)

    assert reader.rx_calls == 1
    assert set(sweep.values) == {"leg_1_yaw"}


def test_sync_read_span_sweeps_only_the_named_motors_in_bus_order() -> None:
    answer = build_buffer(current=100, decivolts=47, celsius=30)
    reader = FakeSyncReader({1: answer, 3: answer})
    bus = make_bus(reader, MOTORS)

    sweep = bus.sync_read_span(TELEMETRY, motors=["leg_1_pitch2", "leg_1_yaw"])

    assert list(reader.data_dict) == [1, 3]
    assert set(sweep.values) == {"leg_1_yaw", "leg_1_pitch2"}


def test_sync_read_span_rejects_an_unknown_motor() -> None:
    bus = make_bus(FakeSyncReader({}), MOTORS)
    with pytest.raises(KeyError, match="leg_9_yaw"):
        bus.sync_read_span(TELEMETRY, motors=["leg_9_yaw"])


def test_sync_read_span_reports_nothing_for_an_empty_motor_set() -> None:
    reader = FakeSyncReader({})
    bus = make_bus(reader, MOTORS)

    sweep = bus.sync_read_span(TELEMETRY, motors=[])

    assert sweep == SpanRead()
    assert reader.tx_calls == 0
