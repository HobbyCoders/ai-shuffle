"""Every tool schema the Antigravity (ACP) bridge serves must be one Gemini accepts.

Antigravity turns each served tool into a Gemini function declaration, and
Gemini rejects the WHOLE request when any one of them is malformed: an array
without ``items`` failed every Antigravity turn, even "hello", with
``INVALID_ARGUMENT ... properties[mcp_servers].items: missing field`` (the
app-config createSubagent/updateSubagent tools). The agent only reported
"Agent execution terminated due to error."

The eager listing (no deferral: proactive runs, a profile without ToolSearch,
``AI_SHUFFLE_PERF_BRIDGE_DEFER=0``) sends every schema, so it is the one that
must hold; the deferred listing is checked too.
"""

from collections.abc import Iterator
from typing import Any

import pytest

import app.core.provider.harness_adapter as ha
from tests.core.test_acp_bridge_deferral import _listing, _options
from tests.core.test_delegated_bridge_tiers import _isolated  # noqa: F401  (autouse)

_SUBSCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SUBSCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")


def _arrays_without_items(schema: Any, path: str = "parameters") -> Iterator[str]:
    if not isinstance(schema, dict):
        return
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if "array" in kinds and not isinstance(schema.get("items"), dict):
        yield path
    for key in _SUBSCHEMA_MAPS:
        for name, sub in (schema.get(key) or {}).items():
            yield from _arrays_without_items(sub, f"{path}.{key}[{name}]")
    for key in _SUBSCHEMA_LISTS:
        for i, sub in enumerate(schema.get(key) or []):
            yield from _arrays_without_items(sub, f"{path}.{key}[{i}]")
    for key in ("items", "additionalProperties", "not"):
        yield from _arrays_without_items(schema.get(key), f"{path}.{key}")


def test_the_walker_finds_an_array_without_items():
    bad = {"type": "object", "properties": {"x": {"type": ["array", "null"]},
                                            "y": {"type": "array", "items": {"type": "string"}}}}
    assert list(_arrays_without_items(bad)) == ["parameters.properties[x]"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["code", "chat"])
@pytest.mark.parametrize("deferred", [False, True])
async def test_every_bridged_tool_schema_gives_its_arrays_an_item_type(tmp_path, lane, deferred):
    options = _options(tmp_path, lane)
    tools = await _listing(tmp_path, options, ha._acp_bridge_deferral(options) if deferred else None)
    assert tools
    offenders = [f"{name}: {where}"
                 for name, tool in tools.items()
                 for where in _arrays_without_items(tool.get("inputSchema") or tool.get("input_schema"))]
    assert offenders == []
