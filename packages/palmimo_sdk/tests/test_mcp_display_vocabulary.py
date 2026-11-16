"""MCP initialization supplies display meanings without repeating tool schemas."""

import json
from collections.abc import Iterable
from typing import Any, cast

import pytest
from mcp.client import Client

from palmimo_sdk import FaceDisplay, Palmimo
from palmimo_sdk.agent import AgentToolSet
from palmimo_sdk.mcp import build_mcp_server


class CatalogDisplay:
    is_connected = True

    def catalog(self, *, tags: Iterable[str] = (), locale: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "id": "com.example.app/asset_42",
                "description": "Signal that the next task is ready.",
                "params": {"tint": {"dim": 4, "description": "Accent tint for the ready signal."}},
            }
        ]


@pytest.mark.parametrize("include", [["play_asset", "wave"], ["wave"]])
async def test_mcp_initialization_exposes_catalog_meanings_once(
    include: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PALMIMO_APP_ID", "com.example.app")
    display = CatalogDisplay()
    tools = AgentToolSet(Palmimo(display=cast(FaceDisplay, display)), include=include)

    async with Client(build_mcp_server(tools)) as client:
        instructions = client.instructions
        assert instructions is not None
        for value in (
            "asset_42",
            "Signal that the next task is ready.",
            "Accent tint for the ready signal.",
        ):
            assert instructions.count(value) == 1
        assert "com.example.app/asset_42" not in instructions
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == set(include)
        schemas = json.dumps([tool.model_dump() for tool in listed.tools])
        assert "Signal that the next task is ready." not in schemas
