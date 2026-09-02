# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Model Context Protocol server exposing Palmimo as MCP tools.

Wraps :class:`~palmimo_sdk.agent.toolset.AgentToolSet` (itself built on the
``[agent]`` extra's :class:`~palmimo_sdk.agent.tools.Tool` /
:class:`~palmimo_sdk.agent.tools.ToolResult` layer) as an MCP
``mcp.server.lowlevel.Server`` via :func:`build_mcp_server`, so any MCP
client -- Claude Code, Claude Desktop, or another MCP-speaking agent -- can
list and call Palmimo's motions, gestures, face, voice, and vision tools the
same way an OpenAI tool-calling loop does::

    from palmimo_sdk import Palmimo
    from palmimo_sdk.agent import AgentToolSet
    from palmimo_sdk.mcp import build_mcp_server

    robot = Palmimo()
    server = build_mcp_server(AgentToolSet(robot))

Run ``python -m palmimo_sdk.mcp`` for a ready-to-use CLI (stdio or
streamable-HTTP transport) wiring a real robot to this server.

This subpackage is the only part of palmimo_sdk that imports the ``mcp``
package (and, transitively, pydantic via the ``[agent]`` extra) --
``import palmimo_sdk`` itself stays dependency-free (see the top-level
package docstring). Install the ``palmimo-sdk[mcp]`` extra to use it.
"""

from __future__ import annotations

from .server import build_mcp_server


__all__ = ["build_mcp_server"]
