"""Helpers for calling MCP tools in tests. Import what you need explicitly."""
import json


def get_tool(tools: list, name: str):
    """The tool called ``name`` in ``tools`` (KeyError when absent)."""
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"Tool '{name}' not found in tool list")


def tool_result_json(result: dict) -> dict:
    """Decode the JSON text of an MCP tool result envelope."""
    return json.loads(result["content"][0]["text"])
