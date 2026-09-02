# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""LLM tool-calling layer for Palmimo.

Wraps the synchronous, blocking :class:`~palmimo_sdk.robot.Palmimo` facade as
a set of pydantic-validated, LLM-callable tools (:class:`Tool` /
:class:`ToolResult`, defined in :mod:`palmimo_sdk.agent.tools`) plus a
registry/dispatcher (:class:`AgentToolSet`, in
:mod:`palmimo_sdk.agent.toolset`) that renders LLM-ready tool schemas and
turns a provider's tool-call back into a facade call::

    from palmimo_sdk import Palmimo
    from palmimo_sdk.agent import AgentToolSet

    robot = Palmimo()
    tools = AgentToolSet(robot)
    llm_tools = tools.to_openai_tools()
    result = await tools.call("forward", {"seconds": 1.5})
    print(result.text)

This subpackage is the only part of palmimo_sdk that imports pydantic --
``import palmimo_sdk`` itself stays dependency-free (see the top-level
package docstring). Install the ``palmimo-sdk[agent]`` extra to use it.
"""

from __future__ import annotations

from .receiver import PalmimoLike
from .tools import Tool, ToolResult
from .toolset import AgentToolSet


__all__ = ["AgentToolSet", "PalmimoLike", "Tool", "ToolResult"]
