"""Bundled stdio MCP server — expose a fluid ToolRegistry to the delegated CLI.

Under the delegated (``claude`` CLI) backend the CLI, not the harness, runs the
agent loop and dispatches tools. To let the CLI reach the harness's *in-process*
tools (a host app's own modules, builtins), we front the same
``ToolRegistry`` behind an MCP server the CLI connects to like any other.

Two halves, because tool handlers are live Python closures that cannot be
serialized across a process boundary:

  * **Parent side (in-process):** ``serve_registry_socket(registry, addr, ctx)``
    runs a socket server speaking newline-delimited JSON. It answers
    ``{"op":"list"}`` and ``{"op":"call","name":...,"arguments":...}`` by
    consulting / dispatching the *live* registry — the single source of truth.

  * **Child side (subprocess):** ``python -m fluid.tools.mcp.bundled_server
    --socket <addr>`` runs an ``mcp`` stdio ``Server`` whose list/call handlers
    proxy over that socket to the parent. This is the process the CLI actually
    launches (declared by ``write_bundled_config`` in an ``.mcp.json``-style
    file passed as ``--mcp-config``).

Both halves accept two address forms so the bridge works off-unix (Windows has
no ``AF_UNIX``):

  * a filesystem path — a unix domain socket (the default where available);
  * ``"tcp:<host>:<port>"`` — a TCP socket, which the caller is expected to bind
    to loopback only (see ``_parse_address``).

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

from fluid.tools.registry import ToolCtx, ToolRegistry

_BUNDLED_SERVER_NAME = "fluid_bundled"
_TCP_PREFIX = "tcp:"


# ─── Address scheme ──────────────────────────────────────────────────────────


def _parse_address(address: str) -> tuple[str, int] | None:
    """Split a bridge address: ``(host, port)`` for TCP, ``None`` for a path.

    ``"tcp:127.0.0.1:8123"`` → ``("127.0.0.1", 8123)``; anything not prefixed
    with ``tcp:`` is treated as a unix domain socket path.

    Security: callers must only ever bind/dial **loopback** here. The TCP form
    exists solely because Windows has no ``AF_UNIX``; unlike a unix socket
    (which filesystem permissions scope to the current user) a loopback port is
    reachable by any process on the host, so the trust boundary is "same
    machine" rather than "same user". That is the same trust domain the CLI
    child already runs in, so no auth token is used here — if the bridge ever
    binds anything other than 127.0.0.1, a shared secret in the handshake
    becomes mandatory.
    """
    if not address.startswith(_TCP_PREFIX):
        return None
    host, _, port = address[len(_TCP_PREFIX):].rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"malformed tcp bridge address: {address!r}")
    return host, int(port)


# ─── Parent side: serve the live registry over a socket ──────────────────────


async def serve_registry_socket(
    registry: ToolRegistry, socket_path: str, ctx: ToolCtx | None = None,
) -> asyncio.AbstractServer:
    """Serve ``list``/``call`` requests against ``registry`` over a socket.

    ``socket_path`` is either a unix socket path or ``"tcp:<host>:<port>"``
    (port ``0`` binds an ephemeral port — read it back off
    ``server.sockets[0].getsockname()[1]``).

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

    tcp = _parse_address(socket_path)
    if tcp is not None:
        host, port = tcp
        return await asyncio.start_server(handle, host=host, port=port)
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
    """The ``.mcp.json`` entry that launches the child bridge for this address.

    ``socket_path`` must be the *resolved* address — for the TCP form that means
    the real bound port, not ``tcp:127.0.0.1:0``.
    """
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "fluid.tools.mcp.bundled_server", "--socket", socket_path],
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
    """One request/response round-trip against the parent's bridge socket."""
    tcp = _parse_address(socket_path)
    if tcp is not None:
        reader, writer = await asyncio.open_connection(*tcp)
    else:
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


def _field_name(model: Any, *candidates: str) -> str:
    """Pick whichever of ``candidates`` the installed pydantic model declares.

    ``mcp``'s models are ``extra="allow"``, so handing them the *wrong* spelling
    does not raise where it should: the value lands in an ignored extra field
    and the real one falls back to its default (or errors as "missing"). Probing
    ``model_fields`` keeps this file honest across SDK naming churn — mcp 2.0
    declares snake_case (``input_schema``, ``is_error``) with camelCase aliases;
    whichever spelling the installed models declare is the one used.
    """
    fields = getattr(model, "model_fields", None) or {}
    for name in candidates:
        if name in fields:
            return name
    return candidates[0]


def _tool_from_spec(types_mod: Any, spec: dict[str, Any]) -> Any:
    """A ``mcp.types.Tool`` for one ``{"op":"list"}`` entry from the parent."""
    key = _field_name(types_mod.Tool, "inputSchema", "input_schema")
    return types_mod.Tool(
        name=spec["name"],
        description=spec.get("description", ""),
        **{key: spec.get("input_schema") or {"type": "object"}},
    )


def _call_result(types_mod: Any, text: str, is_error: bool) -> Any:
    """A ``mcp.types.CallToolResult`` carrying one text block."""
    key = _field_name(types_mod.CallToolResult, "isError", "is_error")
    return types_mod.CallToolResult(
        content=[types_mod.TextContent(type="text", text=text)],
        **{key: is_error},
    )


def build_child_server(socket_path: str) -> Any:
    """The child's ``mcp`` ``Server``, with both tool handlers registered.

    Split out from ``_run_child`` so the wiring can be exercised without owning
    the process's stdio.
    """
    import mcp.types as t
    from mcp.server import Server

    # The installed mcp generation is whatever the host's own dependency tree
    # resolved — an embedder pinned to a CLI/SDK that requires mcp<2.0 gets 1.x
    # here — so the child supports both. mcp 2.0 registers handlers through
    # constructor kwargs (``on_list_tools`` / ``on_call_tool``), each taking the
    # per-request context plus the validated params model and returning the
    # full result model; mcp 1.x registers them through decorators and accepts
    # bare content. Detect which one is installed from the constructor.
    import inspect

    try:
        modern = "on_list_tools" in inspect.signature(Server.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic SDK builds
        modern = False

    if modern:
        async def on_list_tools(ctx: Any, params: Any) -> Any:
            data = await _socket_rpc(socket_path, {"op": "list"})
            return t.ListToolsResult(
                tools=[_tool_from_spec(t, td) for td in data.get("tools", [])],
            )

        async def on_call_tool(ctx: Any, params: Any) -> Any:
            data = await _socket_rpc(
                socket_path,
                {"op": "call", "name": params.name, "arguments": params.arguments or {}},
            )
            # Build the CallToolResult ourselves so the parent's is_error
            # survives instead of being flattened into a success.
            return _call_result(t, data.get("text", ""), bool(data.get("is_error")))

        return Server(
            _BUNDLED_SERVER_NAME,
            version="0.1.0",
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )

    server = Server(_BUNDLED_SERVER_NAME, version="0.1.0")

    @server.list_tools()
    async def legacy_list_tools() -> list[Any]:
        data = await _socket_rpc(socket_path, {"op": "list"})
        return [_tool_from_spec(t, td) for td in data.get("tools", [])]

    @server.call_tool()
    async def legacy_call_tool(name: str, arguments: dict[str, Any] | None) -> Any:
        data = await _socket_rpc(
            socket_path, {"op": "call", "name": name, "arguments": arguments or {}},
        )
        # Returning a CallToolResult (rather than bare content) is what lets
        # the parent's is_error survive: the decorator's own normalization path
        # always reports success.
        return _call_result(t, data.get("text", ""), bool(data.get("is_error")))

    return server


async def _run_child(socket_path: str) -> None:
    from mcp.server.stdio import stdio_server

    server = build_child_server(socket_path)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fluid.tools.mcp.bundled_server")
    parser.add_argument(
        "--socket", required=True,
        help="bridge address served by the parent: a unix socket path or 'tcp:<host>:<port>'",
    )
    args = parser.parse_args(argv)
    import anyio

    anyio.run(_run_child, args.socket)


if __name__ == "__main__":
    main()
