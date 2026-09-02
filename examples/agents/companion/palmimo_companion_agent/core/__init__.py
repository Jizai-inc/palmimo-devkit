"""The shared character layer: prompts, tools, toolview, vision, reflexes.

Independent of how the character is driven -- imported by both the
``pipeline/`` (cascaded STT -> LLM -> TTS) and ``realtime/`` (OpenAI
Realtime API) runtimes, and imports neither in return. See the package
README's "Layout: one character, two runtimes" section.
"""
