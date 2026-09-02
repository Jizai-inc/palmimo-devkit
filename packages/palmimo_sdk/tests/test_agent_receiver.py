"""palmimo_sdk.agent.receiver.PalmimoLike -- the real Palmimo satisfies it.

Two checks: the annotated assignment below is a STATIC assertion (mypy/pyright
fail the module if ``Palmimo`` drifts out of structural conformance); the test
function is the RUNTIME counterpart, via ``PalmimoLike``'s
``@runtime_checkable`` decoration.
"""

from __future__ import annotations

from palmimo_sdk import Palmimo
from palmimo_sdk.agent import PalmimoLike


_palmimo_is_palmimo_like: PalmimoLike = Palmimo()


def test_palmimo_satisfies_palmimo_like() -> None:
    """A real (compute-only) Palmimo structurally satisfies PalmimoLike at runtime too."""
    assert isinstance(Palmimo(), PalmimoLike)
