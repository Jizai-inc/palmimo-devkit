"""Tests for :mod:`palmimo_companion_agent.pipeline.history`."""

from __future__ import annotations

from palmimo_companion_agent.pipeline.history import (
    AgentThoughtEvent,
    CameraEvent,
    History,
    KeyboardEvent,
    MicInputEvent,
    SystemNoteEvent,
    ToolExecEvent,
)


def test_mic_input_event_becomes_a_user_message() -> None:
    assert MicInputEvent("dance for me").to_messages() == [{"role": "user", "content": "[speech] dance for me"}]


def test_mic_input_event_defaults_directed_true() -> None:
    assert MicInputEvent("dance for me").directed is True


def test_mic_input_event_directed_false_renders_overheard() -> None:
    event = MicInputEvent("I'm hungry", directed=False)
    assert event.to_messages() == [{"role": "user", "content": "[overheard] I'm hungry"}]


def test_keyboard_event_becomes_a_user_message() -> None:
    assert KeyboardEvent("say hi").to_messages() == [{"role": "user", "content": "[instruction] say hi"}]


def test_camera_event_becomes_a_user_message() -> None:
    assert CameraEvent("a face appeared").to_messages() == [{"role": "user", "content": "[vision] a face appeared"}]


def test_system_note_event_becomes_a_user_message() -> None:
    assert SystemNoteEvent("wake up").to_messages() == [{"role": "user", "content": "wake up"}]


def test_agent_thought_event_produces_no_messages() -> None:
    assert AgentThoughtEvent("thinking out loud").to_messages() == []


def test_tool_exec_event_becomes_assistant_and_tool_messages() -> None:
    event = ToolExecEvent(
        tool_call_id="call-1",
        name="dance",
        arguments='{"seconds": 4.0}',
        result="danced for 4s",
        thought="feeling good",
    )
    assert event.to_messages() == [
        {
            "role": "assistant",
            "content": "feeling good",
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "dance", "arguments": '{"seconds": 4.0}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "danced for 4s"},
    ]


def test_history_add_appends_and_notifies_subscribers() -> None:
    history = History()
    seen: list[object] = []
    history.subscribe(seen.append)
    event = KeyboardEvent("hello")
    history.add(event)
    assert list(history.events) == [event]
    assert seen == [event]


def test_history_evicts_oldest_beyond_maxlen() -> None:
    history = History(maxlen=2)
    history.add(KeyboardEvent("one"))
    history.add(KeyboardEvent("two"))
    history.add(KeyboardEvent("three"))
    assert [e.text for e in history.events] == ["two", "three"]


def test_to_messages_concatenates_events_in_order() -> None:
    history = History()
    history.add(SystemNoteEvent("start"))
    history.add(KeyboardEvent("dance"))
    assert history.to_messages() == [
        {"role": "user", "content": "start"},
        {"role": "user", "content": "[instruction] dance"},
    ]


def test_to_messages_returns_empty_list_when_history_is_empty() -> None:
    assert History().to_messages() == []


def test_to_messages_inserts_seed_when_window_starts_on_assistant_turn() -> None:
    history = History(maxlen=1)
    # AgentThoughtEvent contributes no message, so the deque holding only a
    # ToolExecEvent (assistant + tool) is exactly the eviction scenario the
    # seed guards against.
    history.add(ToolExecEvent(tool_call_id="c1", name="rest", arguments="{}", result="rested"))
    messages = history.to_messages()
    assert messages[0] == {"role": "user", "content": "[continued] Carry on naturally from what you were doing."}
    assert messages[1]["role"] == "assistant"
    assert messages[2]["role"] == "tool"


def test_add_survives_a_failing_subscriber_and_still_notifies_the_rest() -> None:
    history = History()
    seen: list[object] = []

    def broken(_: object) -> None:
        raise OSError("stream closed")

    history.subscribe(broken)
    history.subscribe(seen.append)
    event = SystemNoteEvent("note")
    history.add(event)
    assert history.events[-1] is event
    assert seen == [event]
