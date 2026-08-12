"""A tiny in-repo stdio MCP server (official ``mcp`` SDK) for offline tests.

Run as ``python fake_mcp_server.py``. Exposes three tools and, when
``LOOM_FAKE_TTL`` is set, advertises a ``tools/list`` freshness hint so the
client's TTL cache can be exercised. The ``need_input`` tool returns an MRTR
``input_required`` result (an elicitation round-trip) to test that path.
"""

import os

import anyio
import mcp.types as t
from mcp.server import Server
from mcp.server.caching import CacheHint
from mcp.server.stdio import stdio_server


async def on_list_tools(ctx, params):
    return t.ListToolsResult(
        tools=[
            t.Tool(
                name="get_weather",
                description="Look up the current weather for a city.",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
                annotations=t.ToolAnnotations(read_only_hint=True),
            ),
            t.Tool(
                name="write_note",
                description="Persist a note to the server's scratch store.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            ),
            t.Tool(
                name="need_input",
                description="A tool that requires elicited input before finishing.",
                input_schema={"type": "object", "properties": {}},
            ),
        ],
    )


async def on_call_tool(ctx, params):
    name = params.name
    args = params.arguments or {}
    if name == "get_weather":
        city = args.get("city", "?")
        return t.CallToolResult(content=[t.TextContent(type="text", text=f"{city}: sunny, 21C")])
    if name == "write_note":
        return t.CallToolResult(content=[t.TextContent(type="text", text="note saved")])
    if name == "need_input":
        elicit = t.ElicitRequest(
            method="elicitation/create",
            params=t.ElicitRequestFormParams(
                mode="form",
                message="Confirm before continuing?",
                requested_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
            ),
        )
        return t.InputRequiredResult(input_requests={"confirm": elicit}, request_state="rs-1")
    return t.CallToolResult(
        content=[t.TextContent(type="text", text=f"unknown tool {name}")], is_error=True
    )


async def main():
    cache_hints = None
    if os.environ.get("LOOM_FAKE_TTL"):
        cache_hints = {"tools/list": CacheHint(ttl_ms=int(os.environ["LOOM_FAKE_TTL"]), scope="private")}
    server = Server(
        "fakesrv", version="0.1.0",
        on_list_tools=on_list_tools, on_call_tool=on_call_tool,
        cache_hints=cache_hints,
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
