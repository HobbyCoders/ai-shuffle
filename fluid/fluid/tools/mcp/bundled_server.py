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

Trust boundary: the parent side is the harness's own tool dispatch, so it is
gated the way ``NativeLoop`` gates a call — the session tool filter on both
``list`` and ``call``, then the PreToolUse hook, the permission engine and the
PostToolUse hook when the loop has them. Every request must carry the
per-session bridge token; the child receives it through the
``FLUID_BUNDLED_TOKEN`` environment entry of its MCP config, never on the
command line. A request without the right token gets one error line and a
closed connection.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fluid.tools.mrtr import unresolved_notice
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry

if TYPE_CHECKING:
    from fluid.hooks.bus import HookBus
    from fluid.permissions.engine import PermissionEngine

_BUNDLED_SERVER_NAME = "fluid_bundled"
_TCP_PREFIX = "tcp:"
# Environment entry the child reads its bridge token from.
TOKEN_ENV = "FLUID_BUNDLED_TOKEN"
_UNAUTHORIZED = {"error": "unauthorized: bridge token missing or wrong"}


# ─── Address scheme ──────────────────────────────────────────────────────────


def _parse_address(address: str) -> tuple[str, int] | None:
    """Split a bridge address: ``(host, port)`` for TCP, ``None`` for a path.

    ``"tcp:127.0.0.1:8123"`` → ``("127.0.0.1", 8123)``; anything not prefixed
    with ``tcp:`` is treated as a unix domain socket path.

    Security: callers must only ever bind/dial **loopback** here. The TCP form
    exists solely because Windows has no ``AF_UNIX``; unlike a unix socket
    (which filesystem permissions scope to the current user) a loopback port is
    reachable by any process on the host. That is why every request carries
    the per-session token (``serve_registry_socket(token=...)``): the port
    alone identifies nothing.
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
    *,
    token: str | None = None,
    tool_filter: Callable[[str], bool] | None = None,
    permissions: PermissionEngine | None = None,
    hooks: HookBus | None = None,
) -> asyncio.AbstractServer:
    """Serve ``list``/``call`` requests against ``registry`` over a socket.

    ``socket_path`` is either a unix socket path or ``"tcp:<host>:<port>"``
    (port ``0`` binds an ephemeral port — read it back off
    ``server.sockets[0].getsockname()[1]``).

    ``token``: when set, every request must carry it as ``"token"`` or the
    connection is answered with an error and closed. ``DelegatedLoop`` always
    sets one; ``None`` exists for in-process tests of the wire format.
    ``tool_filter``, ``permissions`` and ``hooks`` gate the registry the way
    the native loop's dispatch path does (see ``_answer``).

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
                if not isinstance(req, dict):
                    continue
                if token is not None and not _token_ok(token, req.get("token")):
                    writer.write((json.dumps(_UNAUTHORIZED) + "\n").encode())
                    await writer.drain()
                    return
                resp = await _answer(
                    registry, ctx, req,
                    tool_filter=tool_filter, permissions=permissions, hooks=hooks,
                )
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()

    tcp = _parse_address(socket_path)
    if tcp is not None:
        host, port = tcp
        return await asyncio.start_server(handle, host=host, port=port)
    return await asyncio.start_unix_server(handle, path=socket_path)


def _token_ok(expected: str, presented: Any) -> bool:
    return isinstance(presented, str) and hmac.compare_digest(
        expected.encode(), presented.encode()
    )


def _permitted(filt: Callable[[str], bool] | None, name: str) -> bool:
    return filt(name) if filt is not None else True


async def _gate_call(
    registry: ToolRegistry, ctx: ToolCtx, name: str, args: dict[str, Any],
    *,
    permissions: PermissionEngine | None,
    hooks: HookBus | None,
) -> ToolOutcome:
    """PreToolUse hook → PermissionEngine → dispatch → PostToolUse, the order
    ``NativeLoop._gated_call`` uses, for a caller with no user on the far
    end: an ``ask`` verdict has nobody to ask and becomes the non-interactive
    denial.
    """
    from fluid.hooks.bus import POST_TOOL_USE, PRE_TOOL_USE
    from fluid.permissions.engine import non_interactive_denial

    if hooks is not None:
        decision = await hooks.emit(
            PRE_TOOL_USE, {"name": name, "input": args, "tool_use_id": None},
        )
        if decision.action == "deny":
            return ToolOutcome(
                text=f"blocked by hook: {decision.note or 'denied'}", is_error=True,
            )
        if decision.action == "modify" and decision.replacement_input is not None:
            args = decision.replacement_input
    spec = registry.get(name)
    if permissions is not None and spec is not None:
        decided = permissions.decide(spec, args)
        verdict = decided.decision
        deny_message: str | None = decided.deny_message()
        if verdict == "allow" and permissions.note_execution(name, args):
            verdict = "deny"
            deny_message = non_interactive_denial(
                name, "doom-loop guard: this exact tool call was repeated "
                "several times in a row",
            )
        elif verdict == "ask":
            verdict = "deny"
            deny_message = non_interactive_denial(name)
        if verdict == "deny":
            return ToolOutcome(text=deny_message or "permission denied", is_error=True)
    outcome = await registry.dispatch(name, args, ctx)
    if hooks is not None:
        await hooks.emit(
            POST_TOOL_USE,
            {"name": name, "tool_use_id": None, "input": args,
             "output": outcome.text, "is_error": outcome.is_error},
        )
    return outcome


async def _answer(
    registry: ToolRegistry, ctx: ToolCtx, req: dict[str, Any],
    *,
    tool_filter: Callable[[str], bool] | None = None,
    permissions: PermissionEngine | None = None,
    hooks: HookBus | None = None,
) -> dict[str, Any]:
    op = req.get("op")
    if op == "list":
        specs = [
            s for s in registry.list_tools(loaded_only=True)
            if s.source in ("inproc", "builtin")
            # Same rule as the native request schemas: a tool whose host
            # capability is not wired on this ctx is invisible, not
            # advertised-and-erroring.
            and all(key in ctx.extra for key in (s.requires_extra or ()))
            # The session tool restriction: a filtered tool is not listed,
            # not merely refused when called.
            and _permitted(tool_filter, s.name)
        ]
        return {"tools": [
            {"name": s.name, "description": s.description, "input_schema": s.input_schema}
            for s in specs
        ]}
    if op == "call":
        name = req.get("name", "")
        if not _permitted(tool_filter, name):
            return {"text": f"tool not available in this session: {name}", "is_error": True}
        outcome = await _gate_call(
            registry, ctx, name, req.get("arguments") or {},
            permissions=permissions, hooks=hooks,
        )
        # MRTR: the tool paused to ask the user something (AskUser,
        # StopPlanning). This bridge is a request/response socket with no event
        # bus and no user on the far end — the delegated CLI owns the loop — so
        # the round-trip cannot happen here. Its first leg is an EMPTY result
        # carrying the marker; forwarding that text would tell the CLI's model
        # the question was answered with silence. Say what happened instead.
        if outcome.input_required is not None:
            return {
                "text": unresolved_notice(
                    name,
                    "Ask the user directly in your reply and wait for them, or "
                    "proceed without the answer and say what you assumed.",
                ),
                "is_error": True,
            }
        return {"text": outcome.text, "is_error": outcome.is_error}
    return {"error": f"unknown op: {op!r}"}


# ─── Config generation ───────────────────────────────────────────────────────


def bundled_server_config(socket_path: str, token: str | None = None) -> dict[str, Any]:
    """The ``.mcp.json`` entry that launches the child bridge for this address.

    ``socket_path`` must be the *resolved* address — for the TCP form that means
    the real bound port, not ``tcp:127.0.0.1:0``. ``token`` rides in the
    entry's ``env`` (not ``args``: a process list is world-readable).
    """
    config: dict[str, Any] = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "fluid.tools.mcp.bundled_server", "--socket", socket_path],
    }
    if token is not None:
        config["env"] = {TOKEN_ENV: token}
    return config


def write_bundled_config(
    registry: ToolRegistry,
    path: str,
    *,
    socket_path: str,
    server_name: str = _BUNDLED_SERVER_NAME,
    user_servers: dict[str, Any] | None = None,
    token: str | None = None,
    tool_filter: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Write an ``.mcp.json``-style config and return it.

    Includes the bundled server (only when the registry actually exposes
    inproc/builtin tools that pass ``tool_filter``) plus any ``user_servers``
    passed straight through.
    """
    servers: dict[str, Any] = {}
    specs = [
        s for s in registry.list_tools(loaded_only=True)
        if s.source in ("inproc", "builtin") and _permitted(tool_filter, s.name)
    ]
    if specs:
        servers[server_name] = bundled_server_config(socket_path, token)
    if user_servers:
        servers.update(user_servers)
    config = {"mcpServers": servers}
    Path(path).write_text(json.dumps(config, indent=2))
    return config


# ─── Child side: MCP stdio server proxying to the parent socket ──────────────


async def _socket_rpc(
    socket_path: str, req: dict[str, Any], token: str | None = None,
) -> dict[str, Any]:
    """One request/response round-trip against the parent's bridge socket."""
    tcp = _parse_address(socket_path)
    if tcp is not None:
        reader, writer = await asyncio.open_connection(*tcp)
    else:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    if token is not None:
        req = {**req, "token": token}
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


def build_child_server(socket_path: str, token: str | None = None) -> Any:
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
            data = await _socket_rpc(socket_path, {"op": "list"}, token)
            return t.ListToolsResult(
                tools=[_tool_from_spec(t, td) for td in data.get("tools", [])],
            )

        async def on_call_tool(ctx: Any, params: Any) -> Any:
            data = await _socket_rpc(
                socket_path,
                {"op": "call", "name": params.name, "arguments": params.arguments or {}},
                token,
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
        data = await _socket_rpc(socket_path, {"op": "list"}, token)
        return [_tool_from_spec(t, td) for td in data.get("tools", [])]

    @server.call_tool()
    async def legacy_call_tool(name: str, arguments: dict[str, Any] | None) -> Any:
        data = await _socket_rpc(
            socket_path, {"op": "call", "name": name, "arguments": arguments or {}}, token,
        )
        # Returning a CallToolResult (rather than bare content) is what lets
        # the parent's is_error survive: the decorator's own normalization path
        # always reports success.
        return _call_result(t, data.get("text", ""), bool(data.get("is_error")))

    return server


async def _run_child(socket_path: str, token: str | None) -> None:
    from mcp.server.stdio import stdio_server

    server = build_child_server(socket_path, token)
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

    anyio.run(_run_child, args.socket, os.environ.get(TOKEN_ENV) or None)


if __name__ == "__main__":
    main()
