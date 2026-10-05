# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Small building blocks shared by the overload and rail guards and their facade wiring."""

from __future__ import annotations

from collections import deque


class Streak:
    """Consecutive-sample counts, one per key."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    @property
    def active(self) -> bool:
        """Whether any key has a streak in progress."""
        return bool(self._counts)

    def advance(self, key: str) -> int:
        """Add one to *key*'s count and return the new value."""
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        return count

    def drop(self, key: str) -> None:
        """Forget *key*."""
        self._counts.pop(key, None)

    def clear(self) -> None:
        """Forget every key."""
        self._counts.clear()


class TripLog[T]:
    """A bounded queue of trips plus the newest one, which a drain does not clear.

    Args:
        maxlen (int): Queued trips kept; older ones are dropped on overflow.
    """

    def __init__(self, maxlen: int) -> None:
        self._queue: deque[T] = deque(maxlen=maxlen)
        self._last: T | None = None

    @property
    def last(self) -> T | None:
        """The most recent trip recorded, or ``None`` if there has been none."""
        return self._last

    def record(self, trip: T) -> None:
        """Queue *trip* and keep it as the newest."""
        self._queue.append(trip)
        self._last = trip

    def drain(self) -> list[T]:
        """Return and clear the queued trips, oldest first."""
        trips: list[T] = []
        # Popped one at a time: a trip the stepping thread appends meanwhile is kept, not cleared.
        while True:
            try:
                trips.append(self._queue.popleft())
            except IndexError:
                return trips


class RestingReport:
    """Lets only the first trip of an unbroken breach through while no leg motion runs."""

    def __init__(self) -> None:
        self._reported = False

    def filter[T](self, trip: T | None, *, resting: bool, breached: bool) -> T | None:
        """Return *trip*, or ``None`` if it repeats one already reported for this breach.

        Args:
            trip (T | None): The verdict from this sweep.
            resting (bool): Whether no leg motion is running.
            breached (bool): Whether the guard still reads a breach after this sweep.
        """
        if not resting or (trip is None and not breached):
            self._reported = False
        elif trip is not None:
            if self._reported:
                trip = None
            self._reported = True
        return trip

    def reset(self) -> None:
        """Forget that a trip was reported."""
        self._reported = False
