"""Bundled stdio MCP server — expose a omni ToolRegistry to the delegated CLI.

Under the delegated (``claude`` CLI) backend the CLI, not the harness, runs the
agent loop and dispatches tools. To let the CLI reach the harness's *in-process*
tools (the ~10 AI-Shuffle modules, builtins), we front the same
``ToolRegistry`` behind an MCP server the CLI connects to like any other.

Two halves, because tool handlers are live Python closures that cannot be
serialized across a process boundary:

  * **Parent side (in-process):** ``serve_registry_socket(registry, path, ctx)``
    runs a unix-socket server speaking newline-delimited JSON. It answers
    ``{"op":"list"}`` and ``{"op":"call","name":...,"arguments":...}`` by
    consulting / dispatching the *live* registry — the single source of truth.

  * **Child side (subprocess):** ``python -m omni.tools.mcp.bundled_server
    --socket <path>`` runs an ``mcp`` stdio ``Server`` whose ``on_list_tools`` /
    ``on_call_tool`` proxy over that socket to the parent. This is the process
    the CLI actually launches (declared by ``write_bundled_config`` in an
    ``.mcp.json``-style file passed as ``--mcp-config``).

``write_bundled_config`` also passes through any user-provided MCP servers so a
single ``--mcp-config`` carries both the bundled server and the user's servers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from omni.tools.registry import ToolCtx, ToolRegistry

_BUNDLED_SERVER_NAME = "omni_bundled"


# ─── Parent side: serve the live registry over a unix socket ─────────────────


async def serve_registry_socket(
    registry: ToolRegistry, socket_path: str, ctx: ToolCtx | None = None,
) -> asyncio.AbstractServer:
    """Serve ``list``/``call`` requests against ``registry`` over a unix socket.

    Returns the started ``asyncio`` server; the caller owns its lifetime
    (``server.close()`` + ``await server.wait_closed()``).
    """
    ctx = ctx or ToolCtx()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                resp = await _answer(registry, ctx, req)
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_unix_server(handle, path=socket_path)


async def _answer(registry: ToolRegistry, ctx: ToolCtx, req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    if op == "list":
        specs = [
            s for s in registry.list_tools(loaded_only=True)
            if s.source in ("inproc", "builtin")
        ]
        return {"tools": [
            {"name": s.name, "description": s.description, "input_schema": s.input_schema}
            for s in specs
        ]}
    if op == "call":
        outcome = await registry.dispatch(req.get("name", ""), req.get("arguments") or {}, ctx)
        return {"text": outcome.text, "is_error": outcome.is_error}
    return {"error": f"unknown op: {op!r}"}


# ─── Config generation ───────────────────────────────────────────────────────


def bundled_server_config(socket_path: str) -> dict[str, Any]:
    """The ``.mcp.json`` entry that launches the child bridge for this socket."""
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "omni.tools.mcp.bundled_server", "--socket", socket_path],
    }


def write_bundled_config(
    registry: ToolRegistry,
    path: str,
    *,
    socket_path: str,
    server_name: str = _BUNDLED_SERVER_NAME,
    user_servers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write an ``.mcp.json``-style config and return it.

    Includes the bundled server (only when the registry actually exposes
    inproc/builtin tools) plus any ``user_servers`` passed straight through.
    """
    servers: dict[str, Any] = {}
    specs = [
        s for s in registry.list_tools(loaded_only=True)
        if s.source in ("inproc", "builtin")
    ]
    if specs:
        servers[server_name] = bundled_server_config(socket_path)
    if user_servers:
        servers.update(user_servers)
    config = {"mcpServers": servers}
    Path(path).write_text(json.dumps(config, indent=2))
    return config


# ─── Child side: MCP stdio server proxying to the parent socket ──────────────


async def _socket_rpc(socket_path: str, req: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        return json.loads(line) if line else {}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _run_child(socket_path: str) -> None:
    import mcp.types as t
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    async def on_list_tools(ctx: Any, params: Any) -> Any:
        data = await _socket_rpc(socket_path, {"op": "list"})
        tools = [
            t.Tool(
                name=td["name"],
                description=td.get("description", ""),
                input_schema=td.get("input_schema") or {"type": "object"},
            )
            for td in data.get("tools", [])
        ]
        return t.ListToolsResult(tools=tools)

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        data = await _socket_rpc(
            socket_path, {"op": "call", "name": params.name, "arguments": params.arguments or {}},
        )
        return t.CallToolResult(
            content=[t.TextContent(type="text", text=data.get("text", ""))],
            is_error=bool(data.get("is_error")),
        )

    server = Server(
        _BUNDLED_SERVER_NAME, version="0.1.0",
        on_list_tools=on_list_tools, on_call_tool=on_call_tool,
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="omni.tools.mcp.bundled_server")
    parser.add_argument("--socket", required=True, help="unix socket path served by the parent")
    args = parser.parse_args(argv)
    import anyio

    anyio.run(_run_child, args.socket)


if __name__ == "__main__":
    main()
