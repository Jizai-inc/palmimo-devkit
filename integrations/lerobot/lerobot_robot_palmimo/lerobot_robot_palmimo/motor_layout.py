"""Palmimo's 21-motor layout expressed in lerobot's types.

The layout itself belongs to the SDK: :func:`palmimo_sdk.palmimo_motor_ids` and
:data:`palmimo_sdk.SUPPORTED_MOTOR_MODELS` are its single source of truth, and the
SDK's own ``DynamixelDriver`` drives the bus through ``dynamixel_sdk`` without
lerobot. Translating that layout into ``lerobot.motors`` objects is what this
plugin needs, so it lives here rather than in the published SDK.
"""

from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.dynamixel import DynamixelMotorsBus

from palmimo_sdk import SUPPORTED_MOTOR_MODELS, palmimo_motor_ids


__all__ = ["build_palmimo_motors", "register_xc330_m288"]


def register_xc330_m288() -> None:
    """Register XC330-M288-T in lerobot's Dynamixel model tables.

    XC330-M288-T (model number 1240) is register-compatible with XL330-M288-T
    (1200) but is not shipped in lerobot's model tables. This copies the
    xl330-m288 entries under the xc330-m288 key so handshake and address
    lookups succeed.
    """
    src, dst = "xl330-m288", "xc330-m288"
    for table_name in (
        "model_baudrate_table",
        "model_ctrl_table",
        "model_encoding_table",
        "model_resolution_table",
    ):
        table = getattr(DynamixelMotorsBus, table_name)
        if src in table:
            value = table[src]
            table.setdefault(dst, dict(value) if isinstance(value, dict) else value)
    DynamixelMotorsBus.model_number_table.setdefault(dst, 1240)


def build_palmimo_motors(motor_model: str = "xc330-m288") -> dict[str, Motor]:
    """Construct Palmimo's 21-motor dict of lerobot ``Motor`` objects."""
    if motor_model not in SUPPORTED_MOTOR_MODELS:
        raise ValueError(f"Unsupported motor_model {motor_model!r}; expected one of {SUPPORTED_MOTOR_MODELS}.")
    return {
        name: Motor(motor_id, motor_model, MotorNormMode.RANGE_M100_100)
        for name, motor_id in palmimo_motor_ids().items()
    }
