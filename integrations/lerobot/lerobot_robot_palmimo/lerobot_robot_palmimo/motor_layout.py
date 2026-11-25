"""Palmimo's 21-motor layout expressed in lerobot's types.

The layout itself belongs to the SDK: :func:`palmimo_sdk.palmimo_motor_ids` and
:data:`palmimo_sdk.SUPPORTED_MOTOR_MODELS` are its single source of truth, and the
SDK's own ``DynamixelDriver`` drives the bus through ``dynamixel_sdk`` without
lerobot. Translating that layout into ``lerobot.motors`` objects is what this
plugin needs, so it lives here rather than in the published SDK.
"""

import rustypot
from lerobot.motors import Motor, MotorNormMode, SerialMotorsBus

from palmimo_sdk import SUPPORTED_MOTOR_MODELS, palmimo_motor_ids


__all__ = ["PalmimoMotorsBus", "build_palmimo_motors"]

# XC330-M288-T (model number 1240) is register-compatible with XL330-M288-T but
# is not among the models rustypot's XL330 definition lists.
_XC330_M288 = "xc330-m288"
_XC330_M288_MODEL_NUMBER = 1240
_XC330_M288_TWIN = "xl330-m288"


class PalmimoMotorsBus(SerialMotorsBus):
    """``SerialMotorsBus`` that also resolves XC330-M288-T, for the recovery scripts.

    The robot plugin never builds one: it reaches the servos through the SDK.
    """

    @staticmethod
    def _find_model(model: str) -> tuple[int, rustypot.ServoDefinition]:
        if model.lower() == _XC330_M288:
            _, definition = SerialMotorsBus._find_model(_XC330_M288_TWIN)
            return _XC330_M288_MODEL_NUMBER, definition
        return SerialMotorsBus._find_model(model)

    def broadcast_ping(self) -> dict[int, int]:
        """Every id answering on the open port, with its model number.

        Unlike ``connect()``'s handshake, this does not require every configured
        motor to answer, so a partly powered robot can still be inspected.
        """
        definitions = {self._models[model][1].name: self._models[model][1] for model in self._models}
        found: dict[int, int] = {}
        for definition in definitions.values():
            found.update(self._scan(definition))
        return found


def build_palmimo_motors(motor_model: str = _XC330_M288) -> dict[str, Motor]:
    """Construct Palmimo's 21-motor dict of lerobot ``Motor`` objects."""
    if motor_model not in SUPPORTED_MOTOR_MODELS:
        raise ValueError(f"Unsupported motor_model {motor_model!r}; expected one of {SUPPORTED_MOTOR_MODELS}.")
    return {
        name: Motor(motor_id, motor_model, MotorNormMode.RANGE_M100_100)
        for name, motor_id in palmimo_motor_ids().items()
    }
