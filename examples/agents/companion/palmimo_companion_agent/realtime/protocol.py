"""Typed wire layer for the OpenAI Realtime API dialect.

Every event-name string literal this runtime sends or matches on (e.g.
``"response.output_audio.delta"``, ``"conversation.item.create"``) lives ONLY
in this module -- everything else (``client.py``, ``services/``, ``bridge.py``)
speaks typed Python models and never touches a raw ``type`` string. That keeps
a future protocol change (or a second provider dialect) to one file.

Server events are a discriminated union on their ``type`` field
(:data:`ServerEvent`), parsed by :func:`parse_server_event`. An event kind
this runtime does not otherwise care about (there are many -- rate limits,
buffer commits, item lifecycle chatter, ...) comes back as :class:`Unknown`
rather than raising, so the router can log it and move on -- and so does a
KNOWN kind whose payload doesn't validate (an API change, a field renamed)
and a syntactically valid JSON value that isn't an object at all (e.g. a
bare array). Only malformed JSON itself (a frame ``json.loads`` cannot even
parse) is treated as fatal -- see :func:`parse_server_event`'s docstring.

Client events are plain models with a ``to_dict()`` (mirroring the wire
shape); :func:`dump` renders one to the JSON string :meth:`~.client.RealtimeClient.send`
puts on the socket.
"""

from __future__ import annotations

import base64
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError


# ----------------------------------------------------------------------
# Server events -- what the API sends us
# ----------------------------------------------------------------------


class FunctionCallItem(BaseModel):
    """One ``function_call`` item inside a :class:`Response`'s ``output``."""

    type: Literal["function_call"] = "function_call"
    name: str = ""
    arguments: str = "{}"
    call_id: str = ""


class Response(BaseModel):
    """The ``response`` object carried by ``response.created`` / ``response.done``.

    ``output`` is kept as raw dicts rather than a strict discriminated union:
    a response's output list can carry item kinds this runtime has no use for
    (reasoning items, audio-only items, ...), and a schema change to one of
    those must not break parsing of the two kinds this runtime actually reads
    (see :attr:`function_calls` / :attr:`has_message`).
    """

    id: str = ""
    status: str = "completed"
    metadata: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    output: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def function_calls(self) -> list[FunctionCallItem]:
        """Every ``function_call`` item in ``output``, in order."""
        return [FunctionCallItem.model_validate(item) for item in self.output if item.get("type") == "function_call"]

    @property
    def has_message(self) -> bool:
        """Whether ``output`` includes a ``message`` item (i.e. the model spoke)."""
        return any(item.get("type") == "message" for item in self.output)


class SpeechStarted(BaseModel):
    """``input_audio_buffer.speech_started`` -- the talker just started speaking."""

    type: Literal["input_audio_buffer.speech_started"] = "input_audio_buffer.speech_started"


class SpeechStopped(BaseModel):
    """``input_audio_buffer.speech_stopped`` -- the talker just stopped speaking."""

    type: Literal["input_audio_buffer.speech_stopped"] = "input_audio_buffer.speech_stopped"


class AudioDelta(BaseModel):
    """``response.output_audio.delta`` -- one chunk of the model's spoken reply."""

    type: Literal["response.output_audio.delta"] = "response.output_audio.delta"
    delta: str = ""


class ResponseCreated(BaseModel):
    """``response.created`` -- a response has started generating."""

    type: Literal["response.created"] = "response.created"
    response: Response = Field(default_factory=Response)


class ResponseDone(BaseModel):
    """``response.done`` -- a response finished (completed, cancelled, or failed)."""

    type: Literal["response.done"] = "response.done"
    response: Response = Field(default_factory=Response)


class TranscriptCompleted(BaseModel):
    """``conversation.item.input_audio_transcription.completed`` -- what the human said."""

    type: Literal["conversation.item.input_audio_transcription.completed"] = (
        "conversation.item.input_audio_transcription.completed"
    )
    transcript: str = ""


class TranscriptDone(BaseModel):
    """``response.output_audio_transcript.done`` -- what the model said."""

    type: Literal["response.output_audio_transcript.done"] = "response.output_audio_transcript.done"
    transcript: str = ""


class ErrorEvent(BaseModel):
    """``error`` -- something the API rejected or failed on."""

    type: Literal["error"] = "error"
    error: dict[str, Any] = Field(default_factory=dict)


ServerEvent = Annotated[
    SpeechStarted
    | SpeechStopped
    | AudioDelta
    | ResponseCreated
    | ResponseDone
    | TranscriptCompleted
    | TranscriptDone
    | ErrorEvent,
    Field(discriminator="type"),
]

_server_event_adapter: TypeAdapter[ServerEvent] = TypeAdapter(ServerEvent)

#: Every ``type`` value :data:`ServerEvent` knows how to parse. Anything else
#: is reported as :class:`Unknown` by :func:`parse_server_event`.
_KNOWN_SERVER_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "response.output_audio.delta",
        "response.created",
        "response.done",
        "conversation.item.input_audio_transcription.completed",
        "response.output_audio_transcript.done",
        "error",
    }
)


class Unknown(BaseModel):
    """A server event kind this runtime does not model. Deliberately outside :data:`ServerEvent`.

    The router logs these rather than silently dropping them, so an
    unhandled-but-real event is visible instead of vanishing.
    """

    type: str


def parse_server_event(raw: str | bytes) -> ServerEvent | Unknown:
    """Parse one server-sent websocket frame.

    Falls back to :class:`Unknown` for:

    - any ``type`` this module has no model for -- a Realtime session sends
      many event kinds this runtime never acts on, and treating an
      unrecognized kind as fatal would end the session over something as
      harmless as a buffer-commit acknowledgement;
    - a KNOWN kind whose payload does not validate against its model (an API
      change, a renamed/removed field) -- one event's surprising shape must
      not kill the whole session either;
    - a syntactically valid JSON value that parses to something other than
      an object (e.g. a bare array, string, number, or ``null``) and
      therefore carries no ``type`` to dispatch on at all.

    Only malformed JSON itself -- a raw frame ``json.loads`` cannot even
    parse -- is NOT caught here and propagates: the router treats that as
    fatal, since it means the wire protocol itself is no longer being spoken
    correctly.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        return Unknown(type="")
    kind = data.get("type", "")
    if kind in _KNOWN_SERVER_EVENT_KINDS:
        try:
            return _server_event_adapter.validate_python(data)
        except ValidationError:
            return Unknown(type=kind)
    return Unknown(type=kind)


# ----------------------------------------------------------------------
# Client events -- what we send
# ----------------------------------------------------------------------


class SessionUpdate(BaseModel):
    """``session.update`` -- configure the session's instructions, tools, and audio format."""

    instructions: str
    tools: list[dict[str, Any]] = Field(default_factory=list)
    voice: str
    rate: int
    transcription_model: str = "gpt-4o-mini-transcribe"
    transcription_language: str = "ja"
    tool_choice: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        audio_format = {"type": "audio/pcm", "rate": self.rate}
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": self.instructions,
                "tools": self.tools,
                "tool_choice": self.tool_choice,
                "audio": {
                    "input": {
                        "format": audio_format,
                        "transcription": {
                            "model": self.transcription_model,
                            "language": self.transcription_language,
                        },
                    },
                    "output": {"format": audio_format, "voice": self.voice},
                },
            },
        }


class ResponseCreate(BaseModel):
    """``response.create`` -- ask the model to produce a response.

    Every field is optional: a bare continuation (all fields ``None``) sends
    ``{"type": "response.create"}`` with no nested ``response`` object at
    all, matching what a plain "please continue" call looks like on the wire.
    """

    instructions: str | None = None
    tools: list[dict[str, Any]] | None = None
    output_modalities: list[str] | None = None
    tool_choice: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        body = {key: value for key, value in self.model_dump().items() if value is not None}
        event: dict[str, Any] = {"type": "response.create"}
        if body:
            event["response"] = body
        return event


class ItemCreate(BaseModel):
    """``conversation.item.create`` -- append an item to the conversation.

    Built through the named constructors below rather than directly, so a
    caller never hand-assembles the wire shape.
    """

    item: dict[str, Any]

    @classmethod
    def text(cls, text: str, *, role: str = "user", item_id: str | None = None) -> ItemCreate:
        """A ``message`` item carrying plain text (e.g. a reflex's note, a live transcript echo)."""
        item: dict[str, Any] = {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}
        if item_id is not None:
            item["id"] = item_id
        return cls(item=item)

    @classmethod
    def image(cls, jpeg: bytes, *, role: str = "user", item_id: str | None = None) -> ItemCreate:
        """A ``message`` item carrying one JPEG frame as a data URL."""
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        item: dict[str, Any] = {
            "type": "message",
            "role": role,
            "content": [{"type": "input_image", "image_url": data_url}],
        }
        if item_id is not None:
            item["id"] = item_id
        return cls(item=item)

    @classmethod
    def function_call_output(cls, call_id: str, output: str) -> ItemCreate:
        """The result of one function call, keyed by the ``call_id`` the model gave it."""
        return cls(item={"type": "function_call_output", "call_id": call_id, "output": output})

    def to_dict(self) -> dict[str, Any]:
        return {"type": "conversation.item.create", "item": self.item}


class ItemDelete(BaseModel):
    """``conversation.item.delete`` -- remove a previously created item."""

    item_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "conversation.item.delete", "item_id": self.item_id}


class AudioAppend(BaseModel):
    """``input_audio_buffer.append`` -- one chunk of microphone audio, base64-encoded PCM."""

    audio_b64: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "input_audio_buffer.append", "audio": self.audio_b64}


ClientEvent = SessionUpdate | ResponseCreate | ItemCreate | ItemDelete | AudioAppend


def dump(event: ClientEvent) -> str:
    """Render *event* as the JSON string sent over the websocket."""
    return json.dumps(event.to_dict())


__all__ = [
    "AudioAppend",
    "AudioDelta",
    "ClientEvent",
    "ErrorEvent",
    "FunctionCallItem",
    "ItemCreate",
    "ItemDelete",
    "Response",
    "ResponseCreate",
    "ResponseCreated",
    "ResponseDone",
    "ServerEvent",
    "SessionUpdate",
    "SpeechStarted",
    "SpeechStopped",
    "TranscriptCompleted",
    "TranscriptDone",
    "Unknown",
    "dump",
    "parse_server_event",
]
