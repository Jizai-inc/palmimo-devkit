# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Bind agent schemas and prompt text to one connected display catalog."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import Field, create_model, field_validator

from .tools import Expressive, PlayAssetTool, Tool


def render_display_prompt(template: str, catalog: Iterable[dict[str, Any]]) -> str:
    """Replace ``{{display_assets}}`` with the supplied catalog's ids and descriptions.

    Use the same AgentToolSet.display_catalog snapshot as the tool schemas.
    Empty catalogs explicitly disable display actions; no fallback names are added.
    """
    lines = []
    for asset in catalog:
        lines.append(f"- {asset['id']}: {asset['description']}")
        for sid, param in asset.get("params", {}).items():
            lines.append(f"  {sid} ({param['dim']} values): {param['description']}")
    vocabulary = (
        "\n".join(lines) or "No display assets are available. Do not request display actions or face arguments."
    )
    return template.replace("{{display_assets}}", vocabulary)


def bind_display_tool(cls: type[Tool], catalog: list[dict[str, Any]]) -> type[Tool]:
    """Derive a per-toolset model without changing shared or custom tool classes."""
    if not issubclass(cls, (Expressive, PlayAssetTool)):
        return cls
    ids = tuple(asset["id"] for asset in catalog)
    choices: Any = Literal.__getitem__(ids) if ids else type(None)
    fields: dict[str, Any] = {}
    if issubclass(cls, Expressive):
        fields["face"] = (str | None, Field(default=None, description="Catalog asset id; see the display vocabulary."))
    if issubclass(cls, PlayAssetTool):
        fields["id"] = (choices, Field(description="Asset id from the connected display catalog."))

    def validate_asset(value: str | None) -> str | None:
        if value is not None and value not in ids:
            raise ValueError("Asset is outside the connected display catalog")
        return value

    validators = {"validate_asset": field_validator(*fields)(validate_asset)}
    bound = create_model(f"Bound{cls.__name__}", __base__=cls, __validators__=validators, **fields)
    bound._display_enabled = bool(catalog)
    bound._display_asset_ids = ids
    if issubclass(bound, PlayAssetTool):
        bound._display_parameters = {asset["id"]: asset.get("params", {}) for asset in catalog}
    return bound
