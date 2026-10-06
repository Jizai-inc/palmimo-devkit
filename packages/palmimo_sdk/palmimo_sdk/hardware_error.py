# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Descriptions of the servos' latched hardware error bits."""

# Bit definitions: ROBOTIS e-Manual, XC330-M288 Hardware Error Status(70).
HARDWARE_ERROR_BITS: dict[int, str] = {
    0: "InputVoltage",
    2: "Overheating",
    4: "ElectricalShock",
    5: "Overload",
}


def describe_hardware_error(bits: int) -> str:
    """Describe raw unsigned error bits in ascending bit order.

    Args:
        bits (int): Raw unsigned Hardware Error Status value.

    Returns:
        str: Comma-separated names, ``bit<N>`` for unknown bits, or ``-`` for zero.
    """
    return (
        ",".join(HARDWARE_ERROR_BITS.get(bit, f"bit{bit}") for bit in range(bits.bit_length()) if bits & (1 << bit))
        or "-"
    )
