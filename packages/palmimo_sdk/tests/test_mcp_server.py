"""palmimo_sdk.mcp — MCP server registration, dispatch, error mapping, and CLI parsing."""

import asyncio
import base64
import contextlib
import logging
import signal
import subprocess
import sys
import threading
import time
from types import FrameType
from typing import Any, ClassVar, NoReturn, cast

import anyio
import httpx2
import mcp_types as types
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.session import ClientSession
from mcp.client.stdio import FORCE_KILL_TIMEOUT, PROCESS_TERMINATION_TIMEOUT
from mcp.client.streamable_http import streamable_http_client

import palmimo_sdk
from palmimo_sdk import MotionCancelled, Palmimo
from palmimo_sdk.agent import PalmimoLike
from palmimo_sdk.agent.tools import Tool, ToolResult
from palmimo_sdk.agent.toolset import TOOL_MODELS, AgentToolSet
from palmimo_sdk.mcp import __main__ as mcp_main
from palmimo_sdk.mcp import build_mcp_server
from palmimo_sdk.mcp.__main__ import _build_http_app, _parse_args, _split_names


class FakeRobot:
    """Minimal duck-typed Palmimo double -- schema-only tests never call execute()."""

    def _arm_cancel_scope(self) -> None:
        """No-op: this fake never runs a paced call, so there is nothing to arm."""

    def _disarm_cancel_scope(self) -> None:
        """No-op: pairs with _arm_cancel_scope() above."""


# ----------------------------------------------------------------------
# list_tools()
# ----------------------------------------------------------------------


async def test_list_tools_mirrors_toolset_registration() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()))
    server = build_mcp_server(toolset)

    async with Client(server) as client:
        result = await client.list_tools()

    by_name = {tool.name: tool for tool in result.tools}
    assert set(by_name) == set(TOOL_MODELS)
    for name, cls in TOOL_MODELS.items():
        assert by_name[name].description == cls.description
        assert by_name[name].input_schema == cls.parameters_schema()


async def test_list_tools_exposes_optional_reason_on_every_tool() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()))
    server = build_mcp_server(toolset)

    async with Client(server) as client:
        result = await client.list_tools()

    for tool in result.tools:
        assert "reason" in tool.input_schema["properties"]
        assert "reason" not in tool.input_schema.get("required", [])


async def test_list_tools_respects_include_and_exclude() -> None:
    toolset = AgentToolSet(
        cast(PalmimoLike, FakeRobot()), include=["forward", "backward", "stop"], exclude=["backward"]
    )
    server = build_mcp_server(toolset)

    async with Client(server) as client:
        result = await client.list_tools()

    assert {tool.name for tool in result.tools} == {"forward", "stop"}


# ----------------------------------------------------------------------
# call_tool(): dispatch, text, images, unknown-tool
# ----------------------------------------------------------------------


class EchoTool(Tool):
    """Test-only tool recording the arguments it was called with."""

    name: ClassVar[str] = "echo"
    description: ClassVar[str] = "Echo back the given message, for testing MCP call dispatch."

    message: str

    def execute(self, robot: PalmimoLike) -> ToolResult:
        return ToolResult(text=f"echoed: {self.message}")


async def test_call_tool_dispatches_to_toolset_and_returns_text() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(EchoTool)
    server = build_mcp_server(toolset)

    async with Client(server) as client:
        result = await client.call_tool("echo", {"message": "hello"})

    assert result.is_error is not True
    [content] = result.content
    assert isinstance(content, types.TextContent)
    assert content.text == "echoed: hello"


class SnapshotTool(Tool):
    """Test-only tool returning a fixed 'image' (not real JPEG bytes -- content mapping doesn't decode it)."""

    name: ClassVar[str] = "snapshot"
    description: ClassVar[str] = "Return a canned image, for testing MCP image content mapping."

    def execute(self, robot: PalmimoLike) -> ToolResult:
        return ToolResult(text="captured", images=[b"\xff\xd8\xff\xfake-jpeg-bytes"])


async def test_call_tool_returns_image_content_when_result_has_images() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(SnapshotTool)
    server = build_mcp_server(toolset)

    async with Client(server) as client:
        result = await client.call_tool("snapshot", {})

    text_content, image_content = result.content
    assert isinstance(text_content, types.TextContent)
    assert text_content.text == "captured"
    assert isinstance(image_content, types.ImageContent)
    assert image_content.mime_type == "image/jpeg"
    assert base64.b64decode(image_content.data) == b"\xff\xd8\xff\xfake-jpeg-bytes"


async def test_call_tool_reports_unknown_tool_as_text_when_name_invalid() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["forward", "stop"])
    server = build_mcp_server(toolset)

    async with Client(server) as client:
        result = await client.call_tool("not_a_real_tool", {})

    # toolset.call() never raises for an unknown name -- it reports the
    # failure as ordinary TextContent, so this must not surface as an MCP
    # protocol/tool *error response* (raised/rejected) -- it still sets
    # CallToolResult.is_error=True so a client can tell the call failed
    # without parsing the text.
    assert result.is_error is True
    [content] = result.content
    assert isinstance(content, types.TextContent)
    assert "not_a_real_tool" in content.text
    assert "forward" in content.text


async def test_call_tool_reports_validation_error_as_is_error() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(EchoTool)
    server = build_mcp_server(toolset)

    async with Client(server) as client:
        # EchoTool requires `message`; omitting it is a pydantic validation error.
        result = await client.call_tool("echo", {})

    assert result.is_error is True
    [content] = result.content
    assert isinstance(content, types.TextContent)
    assert "Invalid arguments" in content.text


async def test_call_tool_normal_call_has_is_error_false() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(EchoTool)
    server = build_mcp_server(toolset)

    async with Client(server) as client:
        result = await client.call_tool("echo", {"message": "hi"})

    assert result.is_error is False


# ----------------------------------------------------------------------
# call_tool(): concurrent calls are serialized
# ----------------------------------------------------------------------


class RecordingSleepTool(Tool):
    """Test-only tool that sleeps briefly, recording its enter/exit onto a shared log.

    Used to prove build_mcp_server()'s call_tool handler serializes concurrent
    calls: two overlapping calls must never have their sleeps interleaved.
    """

    name: ClassVar[str] = "recording_sleep"
    description: ClassVar[str] = "Sleep briefly while recording enter/exit, for testing call serialization."

    log: ClassVar[list[tuple[str, str]]] = []
    call_id: str

    def execute(self, robot: PalmimoLike) -> ToolResult:
        self.log.append(("enter", self.call_id))
        time.sleep(0.05)
        self.log.append(("exit", self.call_id))
        return ToolResult(text=f"done: {self.call_id}")


async def test_call_tool_serializes_concurrent_calls() -> None:
    RecordingSleepTool.log.clear()
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(RecordingSleepTool)
    server = build_mcp_server(toolset)

    async with (
        Client(server) as client,
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(client.call_tool, "recording_sleep", {"call_id": "a"})
        tg.start_soon(client.call_tool, "recording_sleep", {"call_id": "b"})

    log = RecordingSleepTool.log
    assert len(log) == 4
    # Whichever call ran first, its "exit" must precede the other call's "enter" --
    # i.e. the two sleeps never overlapped.
    first_id = log[0][1]
    assert log[0] == ("enter", first_id)
    assert log[1] == ("exit", first_id)
    second_id = "b" if first_id == "a" else "a"
    assert log[2] == ("enter", second_id)
    assert log[3] == ("exit", second_id)


# ----------------------------------------------------------------------
# call_tool(): non-preemptive even when the HANDLER's own task is cancelled
# (restores the earlier synchronous implementation's abandon_on_cancel=False
# guarantee at this layer, via asyncio.ensure_future + asyncio.shield).
# ----------------------------------------------------------------------


class SlowTool(Tool):
    """Test-only tool that blocks on an Event, for testing handler-level cancellation."""

    name: ClassVar[str] = "slow"
    description: ClassVar[str] = "Blocks until released, for testing call_tool()'s non-preemptive guarantee."

    def execute(self, robot: PalmimoLike) -> ToolResult:
        _SLOW_TOOL_ENTERED.set()
        _SLOW_TOOL_RELEASE.wait(timeout=2.0)
        _SLOW_TOOL_FINISHED.set()
        return ToolResult(text="ran")


_SLOW_TOOL_ENTERED = threading.Event()
_SLOW_TOOL_RELEASE = threading.Event()
_SLOW_TOOL_FINISHED = threading.Event()


async def test_call_tool_completes_the_tool_call_even_when_its_own_task_is_cancelled() -> None:
    """build_mcp_server()'s call_tool handler shields toolset.call() from the
    handler's OWN task being cancelled -- a client disconnect or session
    shutdown abandoning the request must not leave the tool execution
    abandoned mid-motion. Reaches the handler directly via
    server.get_request_handler("tools/call") so cancelling its task
    deterministically lands inside call_tool's own await, rather than hoping
    a client-level disconnect propagates the same way."""
    _SLOW_TOOL_ENTERED.clear()
    _SLOW_TOOL_RELEASE.clear()
    _SLOW_TOOL_FINISHED.clear()

    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(SlowTool)
    server = build_mcp_server(toolset)

    call_tool_entry = server.get_request_handler("tools/call")
    assert call_tool_entry is not None

    async def invoke() -> types.CallToolResult:
        # None: this test reaches the raw handler directly (see the docstring
        # above) rather than through a real MCP session, so there is no real
        # ServerRequestContext to pass -- the handler under test never reads it.
        result = await call_tool_entry.handler(None, types.CallToolRequestParams(name="slow", arguments={}))  # type: ignore[arg-type]
        return cast(types.CallToolResult, result)

    handler_task = asyncio.ensure_future(invoke())
    await asyncio.get_event_loop().run_in_executor(None, _SLOW_TOOL_ENTERED.wait, 2.0)

    handler_task.cancel()
    await asyncio.sleep(0)  # let the cancellation actually reach the handler's await

    assert not _SLOW_TOOL_FINISHED.is_set()  # the worker is still blocked -- cancelling didn't abandon it
    _SLOW_TOOL_RELEASE.set()

    with pytest.raises(asyncio.CancelledError):
        await handler_task

    assert _SLOW_TOOL_FINISHED.is_set()  # the tool ran to completion despite the handler task being cancelled


# ----------------------------------------------------------------------
# CLI: argument parsing (function-level, no subprocess/main())
# ----------------------------------------------------------------------


def test_parse_args_defaults_to_stdio_transport() -> None:
    args = _parse_args([])
    assert args.transport == "stdio"
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.servo_port is None
    assert args.no_servo is False
    assert args.include is None
    assert args.exclude is None


def test_parse_args_accepts_http_transport_and_host_port() -> None:
    args = _parse_args(["--transport", "http", "--host", "0.0.0.0", "--port", "9000"])
    assert args.transport == "http"
    assert args.host == "0.0.0.0"
    assert args.port == 9000


def test_parse_args_no_servo_flag() -> None:
    args = _parse_args(["--no-servo"])
    assert args.no_servo is True


def test_split_names_splits_include_and_exclude_on_comma() -> None:
    assert _split_names("forward,stop") == ["forward", "stop"]
    assert _split_names("forward, stop , backward") == ["forward", "stop", "backward"]


def test_split_names_returns_none_when_flag_not_given() -> None:
    assert _split_names(None) is None


def test_split_names_drops_blank_entries() -> None:
    assert _split_names("forward,,stop,") == ["forward", "stop"]


# ----------------------------------------------------------------------
# CLI: streamable-HTTP ASGI app construction (smoke only, no socket bind)
# ----------------------------------------------------------------------


def test_build_http_app_smoke() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)

    app = _build_http_app(server)

    assert any(getattr(route, "path", None) == "/mcp" for route in app.routes)


def test_parse_args_token_defaults_to_none() -> None:
    args = _parse_args([])
    assert args.token is None


def test_parse_args_accepts_token_with_http_transport() -> None:
    args = _parse_args(["--transport", "http", "--token", "s3cr3t"])
    assert args.token == "s3cr3t"


def test_parse_args_token_reads_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """PALMIMO_MCP_TOKEN is the recommended way to pass a token -- it never shows up in `ps`."""
    monkeypatch.setenv("PALMIMO_MCP_TOKEN", "env-s3cr3t")
    args = _parse_args(["--transport", "http"])
    assert args.token == "env-s3cr3t"


def test_parse_args_explicit_token_flag_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALMIMO_MCP_TOKEN", "env-s3cr3t")
    args = _parse_args(["--transport", "http", "--token", "flag-s3cr3t"])
    assert args.token == "flag-s3cr3t"


# ----------------------------------------------------------------------
# CLI: main() -- reaches serve() when compute-only; validates tool names
# before probing any hardware; and the streamable-HTTP app's real ASGI
# round trip (including the non-loopback Origin-rejection guard).
# ----------------------------------------------------------------------


def _patch_all_peripheral_builders(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Patch every ``_build_*`` peripheral probe to a no-hardware recorder.

    Returns a dict of call logs keyed by peripheral name, so a test can
    assert whether a given probe ran at all.
    """
    calls: dict[str, list[Any]] = {"servo": [], "display": [], "speaker": [], "camera": []}

    def build_servo_driver(servo_port: str | None) -> None:
        calls["servo"].append(servo_port)
        return None

    def build_display() -> None:
        calls["display"].append(True)
        return None

    def build_speaker(device_name_hint: str | None = None) -> None:
        calls["speaker"].append(device_name_hint)
        return None

    def build_camera() -> None:
        calls["camera"].append(True)
        return None

    monkeypatch.setattr(mcp_main, "_build_servo_driver", build_servo_driver)
    monkeypatch.setattr(mcp_main, "_build_display", build_display)
    monkeypatch.setattr(mcp_main, "_build_speaker", build_speaker)
    monkeypatch.setattr(mcp_main, "_build_camera", build_camera)
    return calls


def test_main_reaches_serve_when_every_peripheral_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compute-only run (every peripheral probe returns None) must still reach serve().

    Regression test: `Palmimo.connect()` used to be called
    unconditionally, which raises `RuntimeError` when nothing is attached --
    crashing main() before it ever served anything.
    """
    _patch_all_peripheral_builders(monkeypatch)
    served_with: list[Any] = []

    def fake_anyio_run(func: Any, *args: Any) -> None:
        served_with.append((func, args))

    monkeypatch.setattr(mcp_main.anyio, "run", fake_anyio_run)

    mcp_main.main([])

    assert len(served_with) == 1
    func, _args = served_with[0]
    assert func is mcp_main._serve_stdio


def test_main_names_the_speaker_device_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server must not inherit ALSA's default output: the card index that
    default points at belongs to one boot's enumeration order, so a replug
    leaves say() speaking into a device with no loudspeaker on it."""
    calls = _patch_all_peripheral_builders(monkeypatch)
    monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: None)

    mcp_main.main([])

    assert calls["speaker"] == ["ReSpeaker"]


def test_main_speaker_device_flag_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_all_peripheral_builders(monkeypatch)
    monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: None)

    mcp_main.main(["--speaker-device", "UACDemoV10"])

    assert calls["speaker"] == ["UACDemoV10"]


def test_main_empty_speaker_device_asks_for_the_alsa_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty value is the documented way back to ALSA's own default, so it
    has to reach the builder as a falsy hint rather than be treated as unset."""
    calls = _patch_all_peripheral_builders(monkeypatch)
    monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: None)

    mcp_main.main(["--speaker-device", ""])

    assert calls["speaker"] == [""]


def test_main_disconnects_only_when_it_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """No peripheral attached -> `Palmimo.disconnect()` must not run either (nothing was connected)."""
    _patch_all_peripheral_builders(monkeypatch)
    monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: None)
    disconnect_calls: list[Any] = []
    original_disconnect = Palmimo.disconnect

    def recording_disconnect(self: Palmimo, *, park: bool = True) -> None:
        disconnect_calls.append(park)
        original_disconnect(self, park=park)

    monkeypatch.setattr(Palmimo, "disconnect", recording_disconnect)

    mcp_main.main([])

    assert disconnect_calls == []


def test_main_include_typo_exits_before_probing_any_peripheral(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_all_peripheral_builders(monkeypatch)

    with pytest.raises(SystemExit):
        mcp_main.main(["--include", "not_a_real_tool"])

    assert calls == {"servo": [], "display": [], "speaker": [], "camera": []}


def test_main_exclude_typo_exits_before_probing_any_peripheral(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_all_peripheral_builders(monkeypatch)

    with pytest.raises(SystemExit):
        mcp_main.main(["--exclude", "not_a_real_tool"])

    assert calls == {"servo": [], "display": [], "speaker": [], "camera": []}


def test_main_no_display_no_speaker_no_camera_flags_skip_their_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_all_peripheral_builders(monkeypatch)
    monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: None)

    mcp_main.main(["--no-servo", "--no-display", "--no-speaker", "--no-camera"])

    assert calls == {"servo": [], "display": [], "speaker": [], "camera": []}


def test_main_token_with_stdio_transport_exits_before_probing_any_peripheral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--token only makes sense for --transport http; stdio has no request to attach a header to."""
    calls = _patch_all_peripheral_builders(monkeypatch)

    with pytest.raises(SystemExit):
        mcp_main.main(["--token", "s3cr3t"])

    assert calls == {"servo": [], "display": [], "speaker": [], "camera": []}


# ----------------------------------------------------------------------
# CLI: streamable-HTTP app -- real ASGI round trip + Origin rejection
# ----------------------------------------------------------------------


async def test_http_app_initialize_and_list_tools_round_trip() -> None:
    """A real (in-process, no socket) MCP session over the streamable-HTTP app."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server)

    async with app.router.lifespan_context(app):
        http_client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
        async with (
            http_client,
            streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http_client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.list_tools()

    assert {tool.name for tool in result.tools} == {"stop"}


async def test_http_app_rejects_request_carrying_origin_header_on_non_loopback_bind() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server, host="0.0.0.0")

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                    "origin": "http://evil.example",
                },
            )

    assert resp.status_code == 403


async def test_http_app_accepts_request_without_origin_header_on_non_loopback_bind() -> None:
    """Same non-loopback bind as the rejection test, but no Origin header -- a non-browser client."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server, host="0.0.0.0")

    async with app.router.lifespan_context(app):
        http_client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
        async with (
            http_client,
            streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http_client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.list_tools()

    assert {tool.name for tool in result.tools} == {"stop"}


# ----------------------------------------------------------------------
# CLI: streamable-HTTP app -- optional --token bearer auth
# ----------------------------------------------------------------------

_INIT_REQUEST_BODY: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
_INIT_REQUEST_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


async def test_http_app_without_token_configured_serves_request_with_no_authorization_header() -> None:
    """The default (no --token) path must not change at all -- no Authorization header is required."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server)

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            resp = await client.post("/mcp", json=_INIT_REQUEST_BODY, headers=_INIT_REQUEST_HEADERS)

    assert resp.status_code == 200


async def test_http_app_with_token_configured_rejects_request_missing_authorization_header() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server, token="s3cr3t")

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/mcp", json=_INIT_REQUEST_BODY, headers=_INIT_REQUEST_HEADERS)

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_http_app_with_token_configured_rejects_wrong_token() -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server, token="s3cr3t")

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/mcp",
                json=_INIT_REQUEST_BODY,
                headers={**_INIT_REQUEST_HEADERS, "authorization": "Bearer wrong-token"},
            )

    assert resp.status_code == 401


async def test_http_app_with_token_configured_accepts_correct_token() -> None:
    """A full MCP session (initialize + list_tools) succeeds when the correct bearer token is sent."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server, token="s3cr3t")

    async with app.router.lifespan_context(app):
        http_client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers={"authorization": "Bearer s3cr3t"}
        )
        async with (
            http_client,
            streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http_client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.list_tools()

    assert {tool.name for tool in result.tools} == {"stop"}


async def test_http_app_with_token_configured_accepts_lowercase_bearer_scheme() -> None:
    """RFC 6750: the auth-scheme token is case-insensitive -- 'bearer' must work, not just 'Bearer'."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server, token="s3cr3t")

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            resp = await client.post(
                "/mcp",
                json=_INIT_REQUEST_BODY,
                headers={**_INIT_REQUEST_HEADERS, "authorization": "bearer s3cr3t"},
            )

    assert resp.status_code == 200


async def test_http_app_with_token_configured_accepts_uppercase_bearer_scheme() -> None:
    """RFC 6750: 'BEARER' (all caps) must also work."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server, token="s3cr3t")

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            resp = await client.post(
                "/mcp",
                json=_INIT_REQUEST_BODY,
                headers={**_INIT_REQUEST_HEADERS, "authorization": "BEARER s3cr3t"},
            )

    assert resp.status_code == 200


async def test_http_app_with_token_configured_rejects_non_ascii_presented_token_with_401_not_500() -> None:
    """A non-UTF-8 presented token must fail closed (401), not crash `hmac.compare_digest` (500)."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server, token="s3cr3t")

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/mcp",
                json=_INIT_REQUEST_BODY,
                headers=httpx2.Headers(
                    [
                        (b"content-type", b"application/json"),
                        (b"accept", b"application/json, text/event-stream"),
                        (b"authorization", b"Bearer \xff\xfe"),
                    ]
                ),
            )

    assert resp.status_code == 401


async def test_http_app_with_token_configured_rejects_duplicate_authorization_headers() -> None:
    """Two Authorization headers (even if one is correct) are rejected -- which one a proxy in
    front of this server meant is ambiguous, so treat the request as unauthenticated rather than
    guessing."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server, token="s3cr3t")

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/mcp",
                json=_INIT_REQUEST_BODY,
                headers=httpx2.Headers(
                    [
                        (b"content-type", b"application/json"),
                        (b"accept", b"application/json, text/event-stream"),
                        (b"authorization", b"Bearer s3cr3t"),
                        (b"authorization", b"Bearer wrong-token"),
                    ]
                ),
            )

    assert resp.status_code == 401


async def test_http_app_with_token_and_non_loopback_bind_still_rejects_origin_header() -> None:
    """Presenting a valid token must not bypass the separate browser-Origin rejection guard."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    server = build_mcp_server(toolset)
    app = _build_http_app(server, host="0.0.0.0", token="s3cr3t")

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/mcp",
                json=_INIT_REQUEST_BODY,
                headers={
                    **_INIT_REQUEST_HEADERS,
                    "authorization": "Bearer s3cr3t",
                    "origin": "http://evil.example",
                },
            )

    assert resp.status_code == 403


# ----------------------------------------------------------------------
# CLI: shutdown signals must park the robot
#
# A termination signal whose disposition is still the platform default kills
# the interpreter without unwinding, so main()'s `finally` never parks and the
# servos stay torque-enabled (measured: 21/21 servos torque=1 after SIGTERM,
# neck servos heating 26 C -> 45 C in ~4 minutes). The MCP SDK's stdio client
# sends exactly that signal when it shuts a server down, so this is the
# ordinary client-disconnect path.
#
# The signal is delivered for real, in-process, via signal.raise_signal --
# portable to Windows, which has SIGTERM (though not SIGHUP) and runs Python
# signal handlers for a raised signal the same way POSIX does. Each test first
# installs _sentinel_handler as a safety net: without it, a regression would
# make raise_signal() find the default disposition and terminate the *pytest*
# process rather than fail a test. The sentinel doubles as the "previous
# handler" these tests assert main() restores.
# ----------------------------------------------------------------------


class _SentinelSignalError(BaseException):
    """Raised by the handler main() is expected to have replaced while serving.

    Derives from ``BaseException``, not ``Exception``, to model the real
    condition faithfully: an unhandled SIGTERM is not catchable at all, so a
    stand-in that ``suppress(Exception)`` swallows would let broken code look
    correct -- exactly what happens in ``Palmimo.disconnect()``, whose neck
    release is guarded against ``Exception`` only.
    """


def _sentinel_handler(signum: int, frame: FrameType | None) -> NoReturn:
    raise _SentinelSignalError(signum)


@pytest.fixture
def sigterm_sentinel() -> Any:
    """Install :func:`_sentinel_handler` for SIGTERM, restoring the real one afterwards."""
    previous = signal.signal(signal.SIGTERM, _sentinel_handler)
    try:
        yield _sentinel_handler
    finally:
        signal.signal(signal.SIGTERM, previous)


def _patch_connected_robot(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Give main() one attachable peripheral and record every ``disconnect(park=)`` call.

    A peripheral must be present for main() to run the connect/disconnect
    ceremony at all -- with none attached the robot is compute-only and there
    is nothing to park. ``Palmimo.connect``/``disconnect`` are replaced
    wholesale so no real backend is opened.
    """
    _patch_all_peripheral_builders(monkeypatch)
    monkeypatch.setattr(mcp_main, "_build_display", lambda: cast(Any, object()))
    park_calls: list[bool] = []
    monkeypatch.setattr(Palmimo, "connect", lambda self: None)
    monkeypatch.setattr(Palmimo, "disconnect", lambda self, *, park=True: park_calls.append(park))
    return park_calls


def test_main_parks_the_robot_when_sigterm_arrives_during_stdio_serving(
    monkeypatch: pytest.MonkeyPatch,
    sigterm_sentinel: Any,
) -> None:
    """SIGTERM while serving over stdio must unwind into the park, not kill the process."""
    park_calls = _patch_connected_robot(monkeypatch)

    def serve_then_get_sigterm(func: Any, *args: Any) -> None:
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(mcp_main.anyio, "run", serve_then_get_sigterm)

    mcp_main.main([])

    assert park_calls == [True]


def test_main_parks_the_robot_when_sigterm_arrives_during_http_serving(
    monkeypatch: pytest.MonkeyPatch,
    sigterm_sentinel: Any,
) -> None:
    """SIGTERM while serving over HTTP must park too, despite uvicorn re-raising it.

    uvicorn is not the safety net it appears to be. Its real
    ``Server.capture_signals`` -- used here rather than a hand-written stand-in
    -- handles SIGTERM gracefully, then restores the previous handler and
    re-raises the signal at it. With the default disposition underneath, that
    kills the process from inside ``uvicorn.run()`` (measured exit code 143)
    before it can return to main()'s ``finally``.
    """
    park_calls = _patch_connected_robot(monkeypatch)

    def fake_uvicorn_run(app: Any, **kwargs: Any) -> None:
        server = uvicorn.Server(uvicorn.Config(app))
        with server.capture_signals():
            signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(mcp_main.uvicorn, "run", fake_uvicorn_run)

    mcp_main.main(["--transport", "http"])

    assert park_calls == [True]


def test_main_installs_a_raising_sigterm_handler_while_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """While serving, SIGTERM must reach a handler that raises, not the terminate-outright default.

    Asserts the disposition only. It is deliberately not asserted that this
    equals what SIGINT does inside the event loop: asyncio gives SIGINT a
    cooperative cancellation that can drain an in-flight tool call, which
    raising from a handler does not reproduce.
    """
    _patch_all_peripheral_builders(monkeypatch)
    observed: list[Any] = []

    def record_handler_while_serving(func: Any, *args: Any) -> None:
        observed.append(signal.getsignal(signal.SIGTERM))

    monkeypatch.setattr(mcp_main.anyio, "run", record_handler_while_serving)

    mcp_main.main([])

    assert len(observed) == 1
    handler = observed[0]
    assert callable(handler), f"SIGTERM was left at disposition {handler!r}, which terminates without unwinding"
    with pytest.raises(KeyboardInterrupt):
        handler(int(signal.SIGTERM), None)


def test_main_restores_the_previous_sigterm_handler_after_serving(
    monkeypatch: pytest.MonkeyPatch,
    sigterm_sentinel: Any,
) -> None:
    """main() is called in-process (here, and by embedders) -- it must not leak its handler."""
    _patch_all_peripheral_builders(monkeypatch)
    monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: None)

    mcp_main.main([])

    assert signal.getsignal(signal.SIGTERM) is sigterm_sentinel


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP is POSIX-only")
def test_main_parks_the_robot_when_sighup_arrives_during_stdio_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing the controlling terminal (ssh drop, closed window) must park too, not kill silently."""
    # Looked up through the enum rather than as signal.SIGHUP, which does not
    # type-check on Windows (where the attribute genuinely does not exist).
    sighup = signal.Signals["SIGHUP"]
    previous = signal.signal(sighup, _sentinel_handler)
    try:
        park_calls = _patch_connected_robot(monkeypatch)
        monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: signal.raise_signal(sighup))

        mcp_main.main([])

        assert park_calls == [True]
        assert signal.getsignal(sighup) is _sentinel_handler
    finally:
        signal.signal(sighup, previous)


# ----------------------------------------------------------------------
# CLI: the park must survive the signal that started the shutdown, and the
# window before serving must be covered too.
#
# An MCP stdio client stops a server by closing stdin, waiting 2.0s
# (PROCESS_TERMINATION_TIMEOUT), sending SIGTERM, then SIGKILL 2.0s later. The
# park is a ~2.5s neck ramp plus the leg return, so that SIGTERM lands *during*
# the park on every ordinary disconnect -- and Palmimo.disconnect() guards its
# neck release with suppress(Exception), which does not catch KeyboardInterrupt,
# so an interrupt there escapes before the unsuppressed driver.disconnect()
# that actually cuts torque.
# ----------------------------------------------------------------------


class _MarkerDriver:
    """Minimal ServoDriver stand-in recording only whether torque was cut."""

    def __init__(self, *, is_connected: bool = False) -> None:
        self.is_connected = is_connected
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


def test_main_cuts_servo_torque_when_sigterm_arrives_during_the_park(
    monkeypatch: pytest.MonkeyPatch,
    sigterm_sentinel: Any,
) -> None:
    """The SIGTERM an MCP client sends mid-park must not skip the torque-off.

    Runs the real ``Palmimo.disconnect()`` -- only the two motion steps are
    stubbed -- so the suppress(Exception) gap in front of ``driver.disconnect()``
    is the code under test, not a mock of it.
    """
    _patch_all_peripheral_builders(monkeypatch)
    driver = _MarkerDriver(is_connected=True)
    monkeypatch.setattr(mcp_main, "_build_servo_driver", lambda servo_port: cast(Any, driver))
    monkeypatch.setattr(Palmimo, "connect", lambda self: None)
    monkeypatch.setattr(Palmimo, "return_to_neutral", lambda self, *a, **k: None)

    def park_neck_then_get_sigterm(self: Palmimo) -> None:
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(Palmimo, "_park_neck", park_neck_then_get_sigterm)
    monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: None)

    mcp_main.main([])

    assert driver.disconnected, "SIGTERM during the park skipped the driver disconnect that cuts torque"


def test_main_cuts_servo_torque_when_sigterm_arrives_during_peripheral_probing(
    monkeypatch: pytest.MonkeyPatch,
    sigterm_sentinel: Any,
) -> None:
    """The servo bus is energised by its probe, before serving -- a signal there must still park.

    ``_build_servo_driver()`` calls ``DynamixelDriver.connect()``, which enables
    torque on every servo; the display/speaker/camera probes that follow take
    seconds on a Pi. That window is part of the guarantee, not a prologue to it.
    """
    driver = _MarkerDriver()
    monkeypatch.setattr(mcp_main, "_build_servo_driver", lambda servo_port: cast(Any, driver))
    monkeypatch.setattr(mcp_main, "_build_display", lambda: None)
    monkeypatch.setattr(mcp_main, "_build_speaker", lambda *_: None)

    def camera_probe_then_get_sigterm() -> None:
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(mcp_main, "_build_camera", camera_probe_then_get_sigterm)
    served: list[Any] = []
    monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: served.append(func))

    mcp_main.main([])

    assert served == [], "the signal should have ended the run before serving"
    assert driver.disconnected, "a signal during peripheral probing left the servo bus energised"


def test_shutdown_signals_ignored_discards_the_signal_and_restores_the_handler(
    sigterm_sentinel: Any,
) -> None:
    """SIG_IGN must drop the signal outright, not defer it to the moment the block exits.

    A blocked signal (pthread_sigmask) would stay pending and fire on unblock,
    which would only move the kill to just after the park.
    """
    with mcp_main._shutdown_signals_ignored():
        signal.raise_signal(signal.SIGTERM)

    assert signal.getsignal(signal.SIGTERM) is sigterm_sentinel


def test_shutdown_signals_ignored_covers_sigint_so_a_mashed_ctrl_c_cannot_skip_the_torque_off() -> None:
    """Ctrl-C during the park is the other way to escape the unsuppressed driver disconnect.

    Wider than the raise-set on purpose: SIGINT does not need converting into a
    KeyboardInterrupt (Python already does that), but it does need silencing
    once the park has started.
    """
    previous = signal.getsignal(signal.SIGINT)
    try:
        with mcp_main._shutdown_signals_ignored():
            signal.raise_signal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)

    assert signal.getsignal(signal.SIGINT) is previous


# ----------------------------------------------------------------------
# CLI: real process, real signal disposition
#
# The in-process tests above all install a raising sentinel before main() runs,
# so the SIG_DFL condition the bug actually depends on never exists in them --
# they prove main() replaced the handler, not that a process which would
# otherwise have been killed still parks. These two spawn a real server and let
# it own its own signal dispositions. The stdin-EOF case runs everywhere and
# proves the harness (stub peripherals, marker file, shutdown path); the
# SIGTERM case is POSIX-only because Windows has no way to deliver a real
# SIGTERM to another process.
# ----------------------------------------------------------------------


_MARKER_SERVER_SCRIPT = '''
import signal
import sys

from palmimo_sdk.mcp import __main__ as mcp_main

marker, mode = sys.argv[1], sys.argv[2]


class MarkerDriver:
    """Stands in for a servo bus; records the torque-off to a file.

    is_connected stays False so Palmimo.wake() and the neck-release ramp
    no-op -- this process is about the shutdown path reaching disconnect(),
    not about reproducing motion.
    """

    is_connected = False

    def connect(self):
        pass

    def disconnect(self):
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("torque-off")


mcp_main._build_servo_driver = lambda servo_port: MarkerDriver()
mcp_main._build_display = lambda: None
mcp_main._build_speaker = lambda *_: None


def camera_probe():
    # Announced from inside the last probe, so the parent only acts once the
    # shutdown-signal handlers are already installed.
    print("probes-done", file=sys.stderr, flush=True)
    return None


mcp_main._build_camera = camera_probe

if mode == "self-sigterm":
    # Deliver SIGTERM to this process at the point serving would block. Nothing
    # here installs a handler, so the disposition is whatever main() set up --
    # on unfixed code the platform default, which terminates the process.
    mcp_main.anyio.run = lambda func, *args: signal.raise_signal(signal.SIGTERM)

mcp_main.main([])
'''


def _run_marker_server(tmp_path: Any, stop: Any, mode: str = "serve") -> Any:
    """Spawn the stub MCP server, wait until it is past its probes, then *stop* it.

    Returns the marker path, which exists only if the driver's disconnect (the
    call that cuts torque on a real robot) ran.
    """
    marker = tmp_path / "park.marker"
    script = tmp_path / "run_marker_server.py"
    script.write_text(_MARKER_SERVER_SCRIPT, encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script), str(marker), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stderr is not None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = process.stderr.readline()
            if not line or "probes-done" in line:
                break
        stop(process)
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
    return marker


def test_server_process_cuts_servo_torque_on_stdin_eof(tmp_path: Any) -> None:
    """Closing stdin (what an MCP client does first) must run the torque-off."""

    def close_stdin(process: Any) -> None:
        assert process.stdin is not None
        process.stdin.close()

    marker = _run_marker_server(tmp_path, close_stdin)

    assert marker.exists(), "a clean stdin-EOF shutdown never reached the driver disconnect"


def test_server_process_cuts_servo_torque_on_sigterm_at_the_default_disposition(tmp_path: Any) -> None:
    """A real SIGTERM in a real process whose signal dispositions are entirely main()'s own.

    The child raises SIGTERM at itself rather than receiving it from the
    parent, purely so this runs on Windows too (which cannot signal another
    process); the disposition under test is identical either way, and nothing
    in the child pre-installs a handler. This is the test that actually
    reproduces the reported condition -- on unfixed code the child dies at
    SIG_DFL and the marker is never written -- rather than assuming, as the
    in-process tests must, that a handler was already there.
    """
    marker = _run_marker_server(tmp_path, lambda process: None, mode="self-sigterm")

    assert marker.exists(), "SIGTERM killed the server without releasing servo torque"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot deliver a real SIGTERM to another process")
def test_server_process_cuts_servo_torque_on_sigterm_from_another_process(tmp_path: Any) -> None:
    """The same, delivered the way an MCP client / systemd / kill actually delivers it."""

    def send_sigterm(process: Any) -> None:
        process.send_signal(signal.SIGTERM)

    marker = _run_marker_server(tmp_path, send_sigterm)

    assert marker.exists(), "SIGTERM killed the server without releasing servo torque"


# ----------------------------------------------------------------------
# CLI: a signal landing INSIDE a probe, not between two probes
#
# _build_servo_driver() calls DynamixelDriver.connect(), which enables torque
# on all 21 servos. An interrupt raised in there is not an Exception, so
# neither `except` clause catches it, and the assignment in main() never
# happens -- main's local `driver` stays None, the finally rebuilds the facade
# without it, and nothing is left holding the energised bus. Each probe must
# therefore close what it already opened before letting the interrupt through.
# ----------------------------------------------------------------------


def test_build_servo_driver_closes_the_bus_when_a_shutdown_signal_lands_inside_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe must not let an interrupt escape while leaving the bus armed."""
    built: list[Any] = []

    class InterruptedDriver:
        def __init__(self, port: str | None = None) -> None:
            self.disconnected = False
            built.append(self)

        def connect(self) -> None:
            # Stands in for the interrupt arriving after bus.enable_torque().
            raise _SentinelSignalError(int(signal.SIGTERM))

        def disconnect(self) -> None:
            self.disconnected = True

    monkeypatch.setattr(palmimo_sdk, "DynamixelDriver", InterruptedDriver)

    with pytest.raises(_SentinelSignalError):
        mcp_main._build_servo_driver(None)

    assert built[0].disconnected, "an interrupt inside connect() left the servo bus energised"


def test_build_servo_driver_still_degrades_to_none_on_an_ordinary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new BaseException guard must not disturb the degrade-to-compute-only path."""

    class UnavailableDriver:
        def __init__(self, port: str | None = None) -> None:
            pass

        def connect(self) -> None:
            raise RuntimeError("no servo bus attached")

    monkeypatch.setattr(palmimo_sdk, "DynamixelDriver", UnavailableDriver)

    assert mcp_main._build_servo_driver(None) is None


def test_main_cuts_servo_torque_when_sigterm_arrives_inside_the_servo_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: the real handler main() installs, firing inside the servo probe.

    Distinct from the between-probes test above -- here the signal lands before
    _build_servo_driver() has returned, so main() never gets a reference to the
    energised bus and only the probe itself can close it.
    """
    built: list[Any] = []

    class InterruptedDriver:
        def __init__(self, port: str | None = None) -> None:
            self.disconnected = False
            built.append(self)

        def connect(self) -> None:
            # Torque is on at this point on real hardware.
            signal.raise_signal(signal.SIGTERM)

        def disconnect(self) -> None:
            self.disconnected = True

    monkeypatch.setattr(palmimo_sdk, "DynamixelDriver", InterruptedDriver)
    monkeypatch.setattr(mcp_main, "_build_display", lambda: None)
    monkeypatch.setattr(mcp_main, "_build_speaker", lambda *_: None)
    monkeypatch.setattr(mcp_main, "_build_camera", lambda: None)
    served: list[Any] = []
    monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: served.append(func))

    mcp_main.main([])

    assert served == [], "the signal should have ended the run before serving"
    assert built[0].disconnected, "SIGTERM inside the servo probe left all 21 servos energised"


# ----------------------------------------------------------------------
# Shutdown budget
#
# An MCP stdio client stops a server on a fixed schedule and escalates to
# SIGKILL at _SHUTDOWN_BUDGET_SECONDS. Nothing here shortens the shutdown to
# fit -- the park is uninterruptible on purpose -- so the only thing under
# test is the report: a stop that overran the budget is the case where a
# counting client would have killed the process mid-park, torque still on.
# ----------------------------------------------------------------------


def test_shutdown_budget_matches_the_mcp_client_escalation_schedule() -> None:
    """The budget is the client's schedule, not a number of ours -- read it off the client.

    ``mcp.client.stdio`` waits PROCESS_TERMINATION_TIMEOUT after closing stdin
    before SIGTERM, then FORCE_KILL_TIMEOUT more before SIGKILL. If a future
    mcp release moves either, a warning calibrated to the old sum reports the
    wrong shutdowns, so the drift is caught here rather than on a hot servo.
    """
    assert mcp_main._SHUTDOWN_BUDGET_SECONDS == PROCESS_TERMINATION_TIMEOUT + FORCE_KILL_TIMEOUT


def test_main_measures_the_shutdown_from_the_signal_that_started_it(
    monkeypatch: pytest.MonkeyPatch,
    sigterm_sentinel: Any,
) -> None:
    """The wiring the report depends on: the handler's clock is the one the park reads.

    Asserted end to end because either half is silently useless on its own --
    a handler timestamping a request nobody passed on, or a park handed a
    request nothing ever started -- and both fail the same way, by reporting
    no overrun however long the shutdown took.
    """
    _patch_connected_robot(monkeypatch)
    measured: list[float | None] = []

    def record_elapsed(robot: Any, shutdown: Any) -> None:
        measured.append(shutdown.seconds_since())

    monkeypatch.setattr(mcp_main, "_park_and_close", record_elapsed)
    monkeypatch.setattr(mcp_main.anyio, "run", lambda func, *args: signal.raise_signal(signal.SIGTERM))

    mcp_main.main([])

    assert len(measured) == 1
    assert measured[0] is not None, "the park could not tell that a signal had started this shutdown, or when"


def test_shutdown_request_clock_starts_at_the_first_signal_not_the_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeat signal must not restart the clock and hide an overrun.

    A second SIGTERM can land while the unwind is still on its way to the park
    (only the park itself runs under _shutdown_signals_ignored). Measuring from
    that one would subtract exactly the time that made the shutdown late.

    The clock is driven rather than slept out, so the two readings differ by
    more than the platform's timer resolution.
    """
    now = [100.0]
    monkeypatch.setattr(mcp_main.time, "monotonic", lambda: now[0])
    shutdown = mcp_main._ShutdownRequest()

    shutdown.record()
    now[0] = 105.0
    shutdown.record()
    now[0] = 110.0

    assert shutdown.seconds_since() == 10.0, "the repeat signal restarted the shutdown clock"


def test_park_and_close_reports_a_shutdown_that_overran_the_client_kill_deadline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Overrunning the budget is invisible on a healthy PC and fatal on the robot -- say it.

    The elapsed time is stubbed rather than slept out: the point is the
    comparison against the real budget, and a park this test can afford to
    wait for would never exceed a real one.
    """
    over_budget = mcp_main._SHUTDOWN_BUDGET_SECONDS + 1.0
    monkeypatch.setattr(mcp_main._ShutdownRequest, "seconds_since", lambda self: over_budget)

    mcp_main._park_and_close(Palmimo(), mcp_main._ShutdownRequest())

    err = capsys.readouterr().err
    assert f"shutdown took {over_budget:.2f}s" in err
    assert f"over the {mcp_main._SHUTDOWN_BUDGET_SECONDS:.1f}s an MCP client allows before SIGKILL" in err


def test_park_and_close_stays_quiet_for_a_shutdown_that_fitted_the_budget(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stop the client would have waited out is not worth a warning -- torque was released in time."""
    within_budget = mcp_main._SHUTDOWN_BUDGET_SECONDS - 1.0
    monkeypatch.setattr(mcp_main._ShutdownRequest, "seconds_since", lambda self: within_budget)

    mcp_main._park_and_close(Palmimo(), mcp_main._ShutdownRequest())

    assert "SIGKILL" not in capsys.readouterr().err


def test_park_and_close_says_nothing_about_the_budget_when_no_signal_arrived(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean exit is not a shutdown overrun -- there is no deadline running."""
    mcp_main._park_and_close(Palmimo(), mcp_main._ShutdownRequest())

    assert "SIGKILL" not in capsys.readouterr().err


# ----------------------------------------------------------------------
# Tool-call logging: one INFO record per dispatched call, on stderr only
# ----------------------------------------------------------------------


_TOOL_CALL_LOGGER = "palmimo_sdk.mcp.server"
_MCP_PACKAGE_LOGGER = "palmimo_sdk.mcp"


class FailingTool(Tool):
    """Test-only tool whose execute() raises, so a call can fail without hardware."""

    name: ClassVar[str] = "boom"
    description: ClassVar[str] = "Raise an error, for testing tool-call log outcomes."

    def execute(self, robot: PalmimoLike) -> ToolResult:
        raise RuntimeError("tool exploded")


class CancelledMotionTool(Tool):
    """Test-only tool raising MotionCancelled, exactly as an interrupted motion does."""

    name: ClassVar[str] = "cancelled_motion"
    description: ClassVar[str] = "Raise MotionCancelled, for testing tool-call log outcomes."

    def execute(self, robot: PalmimoLike) -> ToolResult:
        raise MotionCancelled("motion cancelled by request")


class LabelledPaceTool(Tool):
    """Test-only tool carrying one numeric and one free-text argument.

    Both halves of the summary-detail elision rule (numbers kept, strings
    reduced to a length) need a single call to be visible at once.
    """

    name: ClassVar[str] = "labelled_pace"
    description: ClassVar[str] = "Record a paced action, for testing tool-call argument logging."

    seconds: float
    label: str

    def execute(self, robot: PalmimoLike) -> ToolResult:
        return ToolResult(text="paced")


def _tool_call_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The rendered messages of the tool-call records captured so far."""
    return [record.getMessage() for record in caplog.records if record.name == _TOOL_CALL_LOGGER]


def _field(message: str, key: str) -> str:
    """Read a space-delimited ``key=value`` field out of a tool-call record.

    Only for the fields whose values never contain a space (``name``,
    ``outcome``, ``duration_ms``, ``images``) -- ``args`` and ``result`` are
    asserted on as substrings instead.
    """
    return next(field for field in message.split() if field.startswith(f"{key}=")).removeprefix(f"{key}=")


async def test_call_tool_logs_one_info_record_with_the_name_outcome_and_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A remote call that physically moves a robot must leave exactly one trace of itself."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(EchoTool)
    server = build_mcp_server(toolset)

    with caplog.at_level(logging.INFO, logger=_TOOL_CALL_LOGGER):
        async with Client(server) as client:
            await client.call_tool("echo", {"message": "hello"})

    [record] = [record for record in caplog.records if record.name == _TOOL_CALL_LOGGER]
    assert record.levelno == logging.INFO
    message = record.getMessage()
    assert _field(message, "name") == "echo"
    assert _field(message, "outcome") == "ok"
    # Parsed rather than merely present: the latency measurement this field
    # exists for reads it back mechanically.
    assert float(_field(message, "duration_ms")) >= 0.0


async def test_call_tool_logs_a_failed_call_distinguishably_from_a_successful_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure has to be greppable as such -- otherwise the log cannot answer "what went wrong"."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(EchoTool)
    toolset.register(FailingTool)
    server = build_mcp_server(toolset)

    with caplog.at_level(logging.INFO, logger=_TOOL_CALL_LOGGER):
        async with Client(server) as client:
            await client.call_tool("echo", {"message": "hello"})
            await client.call_tool("boom", {})

    succeeded, failed = _tool_call_records(caplog)
    assert _field(succeeded, "name") == "echo"
    assert _field(succeeded, "outcome") == "ok"
    assert _field(failed, "name") == "boom"
    assert _field(failed, "outcome") == "error"


async def test_call_tool_logs_an_interrupted_motion_as_neither_success_nor_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pins the coupling to AgentToolSet's "interrupted: " result text through a real cancellation.

    A motion cut short comes back with ``is_error=False``, so without this
    third outcome an interrupted call would be logged as a clean success.
    """
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(CancelledMotionTool)
    server = build_mcp_server(toolset)

    with caplog.at_level(logging.INFO, logger=_TOOL_CALL_LOGGER):
        async with Client(server) as client:
            result = await client.call_tool("cancelled_motion", {})

    assert result.is_error is False
    [message] = _tool_call_records(caplog)
    assert _field(message, "outcome") == "interrupted"


async def test_call_tool_log_keeps_numeric_arguments_and_elides_strings_when_detail_is_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default detail keeps every argument NAME and the numbers, but no caller-authored text."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(LabelledPaceTool)
    server = build_mcp_server(toolset)

    with caplog.at_level(logging.INFO, logger=_TOOL_CALL_LOGGER):
        async with Client(server) as client:
            await client.call_tool("labelled_pace", {"seconds": 1.5, "label": "hello"})

    [message] = _tool_call_records(caplog)
    assert "seconds=1.5" in message
    assert "label=<str, 5 chars>" in message
    assert "hello" not in message


async def test_call_tool_log_prints_argument_values_verbatim_when_detail_is_full(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The verbose end of the flag is what makes a specific call debuggable."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(LabelledPaceTool)
    server = build_mcp_server(toolset, log_tool_calls="full")

    with caplog.at_level(logging.INFO, logger=_TOOL_CALL_LOGGER):
        async with Client(server) as client:
            await client.call_tool("labelled_pace", {"seconds": 1.5, "label": "hello"})

    [message] = _tool_call_records(caplog)
    assert "label='hello'" in message
    assert "result='paced'" in message


async def test_call_tool_logs_nothing_when_detail_is_off(caplog: pytest.LogCaptureFixture) -> None:
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(EchoTool)
    server = build_mcp_server(toolset, log_tool_calls="off")

    with caplog.at_level(logging.INFO, logger=_TOOL_CALL_LOGGER):
        async with Client(server) as client:
            await client.call_tool("echo", {"message": "hello"})

    assert _tool_call_records(caplog) == []


async def test_tool_call_logging_writes_the_record_to_stderr_and_nothing_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stdout carries the MCP protocol on the stdio transport -- one stray byte desynchronises it."""
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(EchoTool)
    server = build_mcp_server(toolset)

    with mcp_main._tool_call_logging("summary"):
        async with Client(server) as client:
            await client.call_tool("echo", {"message": "hello"})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tool call name=echo" in captured.err


def test_tool_call_logging_installs_a_stderr_handler_that_cannot_propagate_to_a_stdout_handler() -> None:
    """Choosing the stream is not enough: propagation to someone else's root handler is cut too."""
    mcp_logger = logging.getLogger(_MCP_PACKAGE_LOGGER)

    with mcp_main._tool_call_logging("summary"):
        [handler] = mcp_logger.handlers
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stderr
        assert mcp_logger.propagate is False
        assert mcp_logger.level == logging.INFO


def test_tool_call_logging_restores_the_logger_state_it_found() -> None:
    """main() is an ordinary callable the test suite runs in-process -- it must leave no global behind."""
    mcp_logger = logging.getLogger(_MCP_PACKAGE_LOGGER)
    before = (list(mcp_logger.handlers), mcp_logger.level, mcp_logger.propagate)

    with mcp_main._tool_call_logging("summary"):
        pass

    assert (list(mcp_logger.handlers), mcp_logger.level, mcp_logger.propagate) == before


def test_tool_call_logging_installs_no_handler_when_detail_is_off() -> None:
    """`off` leaves an application's own handler for this logger alone rather than overriding it."""
    mcp_logger = logging.getLogger(_MCP_PACKAGE_LOGGER)

    with mcp_main._tool_call_logging("off"):
        assert mcp_logger.handlers == []


def test_parse_args_log_tool_calls_defaults_to_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mcp_main._LOG_TOOL_CALLS_ENV_VAR, raising=False)

    assert _parse_args([]).log_tool_calls == "summary"


def test_parse_args_log_tool_calls_reads_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mcp_main._LOG_TOOL_CALLS_ENV_VAR, "full")

    assert _parse_args([]).log_tool_calls == "full"


def test_parse_args_explicit_log_tool_calls_flag_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mcp_main._LOG_TOOL_CALLS_ENV_VAR, "off")

    assert _parse_args(["--log-tool-calls", "full"]).log_tool_calls == "full"


def test_main_invalid_log_tool_calls_env_var_exits_before_probing_any_peripheral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """argparse's `choices=` never checks a default, so a typo in the env var needs its own guard."""
    calls = _patch_all_peripheral_builders(monkeypatch)
    monkeypatch.setenv(mcp_main._LOG_TOOL_CALLS_ENV_VAR, "verbose")

    with pytest.raises(SystemExit):
        mcp_main.main([])

    assert calls == {"servo": [], "display": [], "speaker": [], "camera": []}


async def test_call_tool_log_elides_a_secret_looking_argument_value_when_detail_is_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default detail must not copy a caller-supplied string into the log, whatever it holds.

    Nothing in today's tool set is declared as a secret, so this is not about
    a known-sensitive field: it is the reason the summary rule elides string
    values by type. A client (or the model driving one) is free to put
    anything at all in a free-text argument -- including the very token this
    server was started with -- and the log must not become the place that
    outlives it.
    """
    toolset = AgentToolSet(cast(PalmimoLike, FakeRobot()), include=["stop"])
    toolset.register(EchoTool)
    server = build_mcp_server(toolset)

    with caplog.at_level(logging.INFO, logger=_TOOL_CALL_LOGGER):
        async with Client(server) as client:
            await client.call_tool("echo", {"message": "s3cr3t"})

    [message] = _tool_call_records(caplog)
    assert "s3cr3t" not in caplog.text
    # The result text echoes the argument, so eliding only the argument would
    # not be enough -- the result has to be reduced to its size too.
    assert "message=<str, 6 chars>" in message
    assert "result=<str, 14 chars>" in message


def test_main_never_prints_the_bearer_token_while_serving_over_http(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The transport's token is not an input to any record, and must not reach the output either.

    The tool-call log is built from a call's name, arguments, and result --
    never from the request that carried it -- so an ``Authorization`` header
    has no route into it by construction. This pins the other end: the CLI
    run that knows the token (it was passed on the command line) must not
    print it while setting logging up or while starting to serve, which is
    how a secret usually ends up in a log that is later pasted into a support
    thread.
    """
    _patch_all_peripheral_builders(monkeypatch)
    monkeypatch.setattr(mcp_main.uvicorn, "run", lambda app, **kwargs: None)

    mcp_main.main(["--transport", "http", "--token", "s3cr3t", "--log-tool-calls", "full"])

    captured = capsys.readouterr()
    assert "s3cr3t" not in captured.out + captured.err
