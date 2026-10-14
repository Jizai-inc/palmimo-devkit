"""The teleop package must not hide import failures other than a missing lerobot."""

from __future__ import annotations

import importlib
import sys

import pytest


pytest.importorskip("lerobot")


def test_package_import_raises_when_a_non_lerobot_dependency_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # config_palmimo stays cached: re-executing it would register "palmimo" twice.
    for name in ("lerobot_teleoperator_palmimo", "lerobot_teleoperator_palmimo.palmimo"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "palmimo_sdk", None)

    with pytest.raises(ModuleNotFoundError, match="palmimo_sdk"):
        importlib.import_module("lerobot_teleoperator_palmimo")
