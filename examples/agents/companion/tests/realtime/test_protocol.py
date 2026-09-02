"""Tests for :mod:`palmimo_companion_agent.realtime.protocol` -- the typed wire layer.

No websocket, no API key: this only exercises parsing/dumping in isolation.
Event-name spellings are pinned once here so a future refactor that changes
one cannot silently drift.
"""

from __future__ import annotations

import json

import pytest

from palmimo_companion_agent.realtime.protocol import (
    AudioAppend,
    AudioDelta,
    ErrorEvent,
    FunctionCallItem,
    ItemCreate,
    ItemDelete,
    Response,
    ResponseCreate,
    ResponseCreated,
    ResponseDone,
    SessionUpdate,
    SpeechStarted,
    SpeechStopped,
    TranscriptCompleted,
    TranscriptDone,
    Unknown,
    dump,
    parse_server_event,
)


# ----------------------------------------------------------------------
# Server events -- parse round-trips
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_type", "extra", "expected_cls"),
    [
        ("input_audio_buffer.speech_started", {}, SpeechStarted),
        ("input_audio_buffer.speech_stopped", {}, SpeechStopped),
        ("response.output_audio.delta", {"delta": "abc"}, AudioDelta),
        ("response.created", {"response": {"id": "r1"}}, ResponseCreated),
        ("response.done", {"response": {"id": "r1"}}, ResponseDone),
        (
            "conversation.item.input_audio_transcription.completed",
            {"transcript": "hello"},
            TranscriptCompleted,
        ),
        ("response.output_audio_transcript.done", {"transcript": "hi"}, TranscriptDone),
        ("error", {"error": {"message": "oops"}}, ErrorEvent),
    ],
)
def test_parse_server_event_round_trips_every_known_kind(raw_type: str, extra: dict, expected_cls: type) -> None:
    raw = json.dumps({"type": raw_type, **extra})
    event = parse_server_event(raw)
    assert isinstance(event, expected_cls)
    assert event.type == raw_type  # type: ignore[attr-defined]


def test_parse_server_event_falls_back_to_unknown_for_an_unrecognized_kind() -> None:
    event = parse_server_event(json.dumps({"type": "rate_limits.updated", "some": "payload"}))
    assert isinstance(event, Unknown)
    assert event.type == "rate_limits.updated"


def test_parse_server_event_raises_on_malformed_json() -> None:
    """Malformed JSON is fatal -- the router treats it as ending the session, unlike an unknown kind."""
    with pytest.raises(json.JSONDecodeError):
        parse_server_event("{not json")


def test_parse_server_event_falls_back_to_unknown_for_a_known_kind_with_a_surprising_payload() -> None:
    """A known kind whose payload doesn't validate (an API change, a renamed field) must not
    kill the session over one event -- only malformed JSON itself is fatal."""
    event = parse_server_event(json.dumps({"type": "response.created", "response": "not-an-object"}))
    assert isinstance(event, Unknown)
    assert event.type == "response.created"


def test_parse_server_event_falls_back_to_unknown_for_a_bare_json_array() -> None:
    """Valid JSON that isn't an object carries no `type` to dispatch on -- must not AttributeError."""
    event = parse_server_event(json.dumps([1, 2, 3]))
    assert isinstance(event, Unknown)
    assert event.type == ""


def test_parse_server_event_falls_back_to_unknown_for_a_bare_json_scalar() -> None:
    event = parse_server_event(json.dumps(None))
    assert isinstance(event, Unknown)
    assert event.type == ""


def test_response_function_calls_and_has_message() -> None:
    response = Response(
        id="r1",
        output=[
            {"type": "function_call", "name": "nod", "arguments": "{}", "call_id": "c1"},
            {"type": "message", "role": "assistant"},
            {"type": "function_call", "name": "wave_both", "arguments": "{}", "call_id": "c2"},
        ],
    )
    assert response.function_calls == [
        FunctionCallItem(name="nod", arguments="{}", call_id="c1"),
        FunctionCallItem(name="wave_both", arguments="{}", call_id="c2"),
    ]
    assert response.has_message is True


def test_response_with_no_message_item_reports_has_message_false() -> None:
    response = Response(id="r1", output=[{"type": "function_call", "name": "nod", "arguments": "{}", "call_id": "c1"}])
    assert response.has_message is False
    assert response.function_calls == [FunctionCallItem(name="nod", arguments="{}", call_id="c1")]


# ----------------------------------------------------------------------
# Client events -- wire shape, pinned once
# ----------------------------------------------------------------------


def test_session_update_wire_shape() -> None:
    event = SessionUpdate(instructions="be nice", tools=[{"type": "function"}], voice="coral", rate=24000)
    assert json.loads(dump(event)) == {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": "be nice",
            "tools": [{"type": "function"}],
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "gpt-4o-mini-transcribe", "language": "ja"},
                },
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "coral"},
            },
        },
    }


def test_response_create_with_no_fields_omits_the_response_object() -> None:
    """A bare continuation is `{"type": "response.create"}` -- no nested `response` key at all."""
    assert json.loads(dump(ResponseCreate())) == {"type": "response.create"}


def test_response_create_nests_only_the_given_fields() -> None:
    event = ResponseCreate(tool_choice="required", metadata={"palmimo_turn": "idle"})
    assert json.loads(dump(event)) == {
        "type": "response.create",
        "response": {"tool_choice": "required", "metadata": {"palmimo_turn": "idle"}},
    }


def test_item_create_text() -> None:
    event = ItemCreate.text("hello", item_id="i1")
    assert json.loads(dump(event)) == {
        "type": "conversation.item.create",
        "item": {"id": "i1", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
    }


def test_item_create_image() -> None:
    event = ItemCreate.image(b"\xff\xd8jpeg", item_id="frame_000001")
    payload = json.loads(dump(event))
    assert payload["type"] == "conversation.item.create"
    assert payload["item"]["id"] == "frame_000001"
    assert payload["item"]["content"][0]["type"] == "input_image"
    assert payload["item"]["content"][0]["image_url"].startswith("data:image/jpeg;base64,")


def test_item_create_function_call_output() -> None:
    event = ItemCreate.function_call_output("c1", "did the thing")
    assert json.loads(dump(event)) == {
        "type": "conversation.item.create",
        "item": {"type": "function_call_output", "call_id": "c1", "output": "did the thing"},
    }


def test_item_delete() -> None:
    assert json.loads(dump(ItemDelete(item_id="frame_000001"))) == {
        "type": "conversation.item.delete",
        "item_id": "frame_000001",
    }


def test_audio_append() -> None:
    assert json.loads(dump(AudioAppend(audio_b64="YWJj"))) == {
        "type": "input_audio_buffer.append",
        "audio": "YWJj",
    }
