"""Tests for :mod:`palmimo_wakeword_agent.agent` (fake OpenAI client, no network).

A real ``AgentToolSet`` over a compute-only ``Palmimo()`` is not used here:
every movement/gesture tool blocks in real seconds via ``Palmimo.run()``
(minimum ~0.5s each, see ``palmimo_sdk.agent.tools._run_motion``), which would
make this suite slow for no benefit — none of it is about tool *behavior*,
only about ``CommandAgent``'s single-round tool-calling protocol. A minimal
fake toolset duck-typing ``to_openai_tools()`` / ``call()`` stands in
instead.
"""

from __future__ import annotations

from typing import Any

from palmimo_wakeword_agent.agent import CommandAgent


class FakeToolResult:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeToolSet:
    """Duck-types ``AgentToolSet``'s public surface without a real Palmimo."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.raise_on: set[str] = set()

    def to_openai_tools(self) -> list[dict[str, object]]:
        return [{"type": "function", "function": {"name": "forward", "description": "walk", "parameters": {}}}]

    async def call(self, name: str, args: object) -> FakeToolResult:
        self.calls.append((name, args))
        if name in self.raise_on:
            raise RuntimeError(f"boom in {name}")
        return FakeToolResult(text=f"{name} done")


class _ToolCallFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = _ToolCallFunction(name, arguments)


class _Message:
    def __init__(self, content: str | None, tool_calls: list[_ToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self) -> dict[str, object]:
        dumped: dict[str, object] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            dumped["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ]
        return dumped


class _Choice:
    def __init__(self, message: _Message) -> None:
        self.message = message


class _Response:
    def __init__(self, message: _Message) -> None:
        self.choices = [_Choice(message)]


class FakeCompletions:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.requests.append(kwargs)
        return self._responses.pop(0)


class FakeChat:
    def __init__(self, responses: list[_Response]) -> None:
        self.completions = FakeCompletions(responses)


class FakeClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.chat = FakeChat(responses)


async def test_run_executes_tool_calls_in_order_then_returns_follow_up_reply() -> None:
    toolset = FakeToolSet()
    tool_call_1 = _ToolCall("call_1", "forward", '{"seconds": 1.0}')
    tool_call_2 = _ToolCall("call_2", "wave", "{}")
    first = _Response(_Message(None, tool_calls=[tool_call_1, tool_call_2]))
    second = _Response(_Message("進んで手を振りました。"))
    client = FakeClient([first, second])
    agent = CommandAgent(client, toolset)

    reply = await agent.run("前に進んで手を振って")

    assert reply == "進んで手を振りました。"
    assert toolset.calls == [("forward", '{"seconds": 1.0}'), ("wave", "{}")]

    # First call included tools=; the follow-up call must NOT (one tool round only).
    first_request = client.chat.completions.requests[0]
    second_request = client.chat.completions.requests[1]
    assert "tools" in first_request
    assert "tools" not in second_request

    # The follow-up conversation carries a role="tool" message per call.
    follow_up_messages = second_request["messages"]
    tool_messages = [m for m in follow_up_messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_1", "call_2"]
    assert [m["content"] for m in tool_messages] == ["forward done", "wave done"]


async def test_run_with_no_tool_calls_returns_direct_reply() -> None:
    toolset = FakeToolSet()
    response = _Response(_Message("はい、パルミーモです。"))
    client = FakeClient([response])
    agent = CommandAgent(client, toolset)

    reply = await agent.run("あなたは誰ですか")

    assert reply == "はい、パルミーモです。"
    assert toolset.calls == []
    # No tool round happened, so there should be exactly one completion call.
    assert len(client.chat.completions.requests) == 1


async def test_run_feeds_toolset_call_error_back_without_crashing() -> None:
    toolset = FakeToolSet()
    toolset.raise_on.add("forward")
    tool_call = _ToolCall("call_1", "forward", "{}")
    first = _Response(_Message(None, tool_calls=[tool_call]))
    second = _Response(_Message("失敗しました。"))
    client = FakeClient([first, second])
    agent = CommandAgent(client, toolset)

    reply = await agent.run("前に進んで")

    assert reply == "失敗しました。"
    follow_up_messages = client.chat.completions.requests[1]["messages"]
    tool_messages = [m for m in follow_up_messages if m.get("role") == "tool"]
    assert "boom in forward" in tool_messages[0]["content"]


async def test_run_returns_fallback_text_when_final_reply_is_empty() -> None:
    toolset = FakeToolSet()
    tool_call = _ToolCall("call_1", "forward", "{}")
    first = _Response(_Message(None, tool_calls=[tool_call]))
    second = _Response(_Message(""))
    client = FakeClient([first, second])
    agent = CommandAgent(client, toolset)

    reply = await agent.run("前に進んで")

    assert reply == "Done."


async def test_run_returns_fallback_text_when_direct_reply_is_none() -> None:
    toolset = FakeToolSet()
    response = _Response(_Message(None))
    client = FakeClient([response])
    agent = CommandAgent(client, toolset)

    reply = await agent.run("何か言って")

    assert reply == "Done."


async def test_system_message_includes_reply_language_suffix_for_non_default_language() -> None:
    toolset = FakeToolSet()
    response = _Response(_Message("done"))
    client = FakeClient([response])
    agent = CommandAgent(client, toolset, language="ja")

    await agent.run("前に進んで")

    system_message = client.chat.completions.requests[0]["messages"][0]
    assert system_message["role"] == "system"
    assert system_message["content"].endswith("Reply in language: ja.")


async def test_system_message_uses_english_language_suffix_by_default() -> None:
    toolset = FakeToolSet()
    response = _Response(_Message("done"))
    client = FakeClient([response])
    agent = CommandAgent(client, toolset)

    await agent.run("go forward")

    system_message = client.chat.completions.requests[0]["messages"][0]
    assert system_message["content"].endswith("Reply in language: en.")
