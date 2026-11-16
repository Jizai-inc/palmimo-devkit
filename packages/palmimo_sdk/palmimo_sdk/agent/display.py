# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Bind agent schemas and prompt text to one connected display catalog."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from pydantic import Field, ValidationInfo, create_model, field_validator

from palmimo_sdk._display_names import named_catalog, resolve_asset_name

from .tools import Expressive, PlayAssetTool, Tool


def render_display_prompt(template: str, catalog: Iterable[dict[str, Any]]) -> str:
    """Replace ``{{display_assets}}`` with the supplied catalog's names and descriptions.

    Use the same AgentToolSet.display_catalog snapshot as the tool schemas.
    Empty catalogs explicitly disable display actions; no fallback names are added.
    """
    lines = []
    for asset in named_catalog(catalog, os.environ.get("PALMIMO_APP_ID")):
        lines.append(f"- {asset['name']}: {asset['description']}")
        for sid, param in asset.get("params", {}).items():
            lines.append(f"  {sid} ({param['dim']} values): {param['description']}")
    vocabulary = (
        "\n".join(lines) or "No display assets are available. Do not request display actions or face arguments."
    )
    return template.replace("{{display_assets}}", vocabulary)


def bind_display_tool(
    cls: type[Tool], catalog: list[dict[str, Any]], resolution_ids: tuple[str, ...] | None = None
) -> type[Tool]:
    """Derive a per-toolset model without changing shared or custom tool classes."""
    if not issubclass(cls, (Expressive, PlayAssetTool)):
        return cls
    ids = tuple(asset["id"] for asset in catalog)
    names = [asset["name"] for asset in catalog]
    available = resolution_ids if resolution_ids is not None else ids
    fields: dict[str, Any] = {}
    if issubclass(cls, Expressive):
        fields["face"] = (
            str | None,
            Field(
                default=None,
                description="Catalog display name; see the display vocabulary.",
                json_schema_extra={"anyOf": [{"type": "string", "enum": names}, {"type": "null"}]},
            ),
        )
    if issubclass(cls, PlayAssetTool):
        fields["id"] = (
            str,
            Field(description="Name from the connected display catalog.", json_schema_extra={"enum": names}),
        )

    def validate_asset(value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        try:
            value = resolve_asset_name(
                value, os.environ.get("PALMIMO_APP_ID"), lambda: available, allow_aliases=info.field_name == "face"
            )
        except LookupError as error:
            raise ValueError(str(error)) from error
        if value not in ids:
            raise ValueError("Asset is outside the connected display catalog")
        return value

    validators = {"validate_asset": field_validator(*fields)(validate_asset)}
    bound = create_model(f"Bound{cls.__name__}", __base__=cls, __validators__=validators, **fields)
    bound._display_enabled = bool(catalog)
    bound._display_asset_ids = ids
    bound._display_asset_names = {asset["id"]: asset["name"] for asset in catalog}
    if issubclass(bound, PlayAssetTool):
        bound._display_parameters = {asset["id"]: asset.get("params", {}) for asset in catalog}
    return bound
