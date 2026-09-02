"""Root-package export contract -- ``palmimo_sdk`` is the SDK's single
public window (see ``palmimo_sdk/__init__.py``'s docstring); every peripheral
resource users construct must be importable straight from the package root,
never by reaching into ``palmimo_sdk.io`` (blocked for external callers by
ruff's TID251 banned-api rule). This test guards the TTS boundary
(:class:`TtsEngine`, :class:`TtsVoice`, :class:`PiperEngine`) added alongside
``Speaker``'s pluggable-engine support.
"""

import palmimo_sdk


def test_tts_types_are_exported_from_package_root() -> None:
    """``TtsEngine``, ``TtsVoice``, and ``PiperEngine`` are importable from
    ``palmimo_sdk`` directly, matching how ``Speaker``/``SpeakerConfig`` are
    exported, so ``Speaker(engine=PiperEngine())`` never requires reaching
    into ``palmimo_sdk.io``. This import must succeed with no dependency on
    ``piper-plus`` being installed: ``PiperEngine`` only imports ``piper``
    lazily, inside the methods that actually synthesize speech (see
    ``palmimo_sdk/io/tts/piper.py``), so merely importing the class -- like
    importing ``palmimo_sdk`` itself -- stays dependency-free."""
    from palmimo_sdk import PiperEngine, TtsEngine, TtsVoice

    assert palmimo_sdk.PiperEngine is PiperEngine
    assert palmimo_sdk.TtsEngine is TtsEngine
    assert palmimo_sdk.TtsVoice is TtsVoice


def test_tts_types_are_listed_in_dunder_all() -> None:
    """``__all__`` documents the public surface; the TTS types must appear in
    it alongside the other peripheral resources."""
    assert "PiperEngine" in palmimo_sdk.__all__
    assert "TtsEngine" in palmimo_sdk.__all__
    assert "TtsVoice" in palmimo_sdk.__all__
