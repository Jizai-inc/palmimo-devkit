"""Local SDK checks respect the compatibility range declared by each example."""

import pytest

from tools.check_sdk_overlay import allows_sdk_overlay


@pytest.mark.parametrize(
    ("dependencies", "sdk_version", "expected"),
    [
        (["palmimo-sdk[agent]>=0.2,<0.3"], "0.2.0", True),
        (["palmimo-sdk[agent]>=0.1.3,<0.2"], "0.2.0", False),
        (["palmimo-sdk"], "0.2.0", True),
        (["palmimo_sdk[face]~=0.2.0"], "0.2.0", True),
        (["packaging>=24"], "0.2.0", False),
    ],
)
def test_sdk_overlay_respects_declared_compatibility(dependencies: list[str], sdk_version: str, expected: bool) -> None:
    assert allows_sdk_overlay(dependencies, sdk_version) is expected
