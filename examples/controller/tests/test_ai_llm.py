from __future__ import annotations

from typing import Any

from openai.types.responses import Response

from palmimo_controller.ai.llm import OpenAiLlm
from palmimo_controller.ai.tools import TOOL_DEFINITIONS


def _response(output: list[dict[str, Any]]) -> Response:
    return Response.model_validate(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 0,
            "model": "gpt-5.4-mini",
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 15,
            },
        }
    )


class _FakeResponses:
    def __init__(self, response: Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Response:
        self.calls.append(kwargs)
        return self.response


class _FakeClient:
    def __init__(self, response: Response) -> None:
        self.responses = _FakeResponses(response)


def _llm(response: Response) -> tuple[OpenAiLlm, _FakeResponses]:
    llm = OpenAiLlm("sk-test")
    client = _FakeClient(response)
    llm._client = client  # type: ignore[assignment]
    return llm, client.responses


async def test_chat_sends_tools_with_reasoning_and_keeps_conversation_off_the_server() -> None:
    llm, responses = _llm(_response([]))
    items = [{"role": "system", "content": "prompt"}, {"role": "user", "content": "もしもし"}]

    await llm.chat(items, tools=TOOL_DEFINITIONS)

    sent = responses.calls[0]
    assert sent["input"] == items
    assert sent["tools"] == TOOL_DEFINITIONS
    assert sent["reasoning"] == {"effort": "low"}
    assert sent["store"] is False
    assert sent["include"] == ["reasoning.encrypted_content"]


async def test_chat_reads_speech_and_tool_calls_and_keeps_every_output_item() -> None:
    output: list[dict[str, Any]] = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"},
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": " 歩きます ", "annotations": []}],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "walk",
            "arguments": '{"direction": "forward", "seconds": 2}',
        },
    ]
    llm, _ = _llm(_response(output))

    reply = await llm.chat([{"role": "user", "content": "前に歩いて"}], tools=TOOL_DEFINITIONS)

    assert reply.text == "歩きます"
    assert [(call.id, call.name) for call in reply.tool_calls] == [("call_1", "walk")]
    assert [item["type"] for item in reply.items] == ["reasoning", "message", "function_call"]
    assert reply.items[0]["encrypted_content"] == "opaque"


async def test_chat_without_tools_sends_none() -> None:
    llm, responses = _llm(_response([]))

    await llm.chat([{"role": "user", "content": "はい"}], tools=None)

    assert "tools" not in responses.calls[0]
