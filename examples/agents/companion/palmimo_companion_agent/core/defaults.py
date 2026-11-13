"""Optional standard assets used by deterministic companion gestures."""

import logging
from collections.abc import Iterable

from palmimo_sdk.agent import PalmimoLike


DEFAULT_ASSETS = {
    "greeting": "palmimo/happy",
    "dance": "palmimo/groove",
    "yes": "palmimo/maru",
    "no": "palmimo/batsu",
    "sleep": "palmimo/sleeping",
    "drowsy": "palmimo/sleepy",
}


def play_default(robot: PalmimoLike, key: str, available: Iterable[str], hold_ms: int | None = None) -> None:
    """Play an optional default only when the bound vocabulary contains it."""
    id = DEFAULT_ASSETS[key]
    if robot.display is None or id not in available:
        return
    try:
        robot.display.play(id, hold_ms)
    except Exception:
        logging.getLogger(__name__).warning("Could not play default display asset %s", id, exc_info=True)
