"""Wake-word voice agent example for Palmimo.

Deliberately simple: no autonomous/ReAct loop, no LLM-provider abstraction
(the ``openai`` SDK is used directly), no TUI. One-shot only: the user says
the wake word -- fixed by the SDK, see :data:`palmimo_sdk.PALMIMO_NAMES` --
and the command in the same breath, and that single utterance is transcribed
and executed via LLM tool-calling against the Palmimo SDK. An utterance with
no wake word, or the wake word alone, is ignored.
"""
