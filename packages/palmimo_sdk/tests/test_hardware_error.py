"""Latched hardware error records and bit descriptions."""

import pytest

from palmimo_sdk import HardwareErrorRead, describe_hardware_error


@pytest.mark.parametrize(
    ("bits", "description"),
    [
        (0, "-"),
        (0x20, "Overload"),
        (0x21, "InputVoltage,Overload"),
        (0x02, "bit1"),
        (0x04, "Overheating"),
        (0x08, "bit3"),
        (0x10, "ElectricalShock"),
        (0x80, "bit7"),
        (0xA2, "bit1,Overload,bit7"),
    ],
)
def test_describe_hardware_error_names_set_bits(bits: int, description: str) -> None:
    assert describe_hardware_error(bits) == description


def test_hardware_error_read_latched_excludes_healthy_responses() -> None:
    result = HardwareErrorRead(
        errors={"leg_1_yaw": 0, "leg_1_pitch1": 0x20},
        silent=("leg_1_pitch2",),
        unreached=("leg_2_yaw",),
    )
    assert result.latched == {"leg_1_pitch1": 0x20}
