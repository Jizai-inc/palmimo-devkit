"""Tests for :mod:`palmimo_companion_agent.realtime.state` -- this runtime's own Sleeping mirror."""

from __future__ import annotations

from palmimo_companion_agent.realtime.state import Sleeping


def test_sleeping_starts_awake() -> None:
    assert Sleeping().asleep is False


def test_observing_sleep_flips_the_mirror_asleep() -> None:
    sleeping = Sleeping()
    sleeping.observe("sleep")
    assert sleeping.asleep is True


def test_observing_wake_up_flips_the_mirror_awake() -> None:
    sleeping = Sleeping()
    sleeping.asleep = True
    sleeping.observe("wake_up")
    assert sleeping.asleep is False


def test_observing_an_unrelated_tool_does_not_change_the_mirror() -> None:
    sleeping = Sleeping()
    sleeping.observe("nod")
    assert sleeping.asleep is False
