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
    A call may carry the CLI's ``tool_use_id``; a reply carries ``text``,
    ``is_error`` and, when the tool returned any, ``images`` (base64).

  * **Child side (subprocess):** ``python -m fluid.tools.mcp.bundled_server
    --socket <addr>`` runs an ``mcp`` stdio ``Server`` whose list/call handlers
    proxy over that socket to the parent. This is the process the CLI actually
    launches (declared by ``write_bundled_config`` in an ``.mcp.json``-style
    file passed as ``--mcp-config``). It forwards the id Claude Code puts in
    a call's ``_meta`` and turns the parent's images into ``ImageContent``.

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

The same socket can also answer a few plain HTTP/1.1 ``POST`` routes
(``http_routes``), for callers that speak only HTTP: AI Shuffle's Claude Code
mod posts its usage reports this way through ``$.http.fetch``. A connection
whose first line is an HTTP request line is served as one request and closed.
HTTP routes take their own token (``http_token``, sent as ``Authorization:
Bearer``), never the bridge token, so a holder of one cannot call tools;
bodies are JSON, capped at ``HTTP_MAX_BODY_BYTES``, and a TCP peer must be
loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fluid.tools.mrtr import unresolved_notice
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry

if TYPE_CHECKING:
    from fluid.hooks.bus import HookBus
    from fluid.permissions.engine import PermissionEngine

logger = logging.getLogger(__name__)

_BUNDLED_SERVER_NAME = "fluid_bundled"
_TCP_PREFIX = "tcp:"
# Longest line accepted on the bridge sockets and, in the parent, on the
# CLI's stdout. The CLI echoes whole tool results inside ``user`` frames and
# a bridge call carries whole arguments and results, so asyncio's 64 KiB
# default is exceeded by ordinary turns; a line over this cap raises out of
# ``readline`` and ends the turn.
STREAM_LINE_LIMIT = 16 * 1024 * 1024
# A hook answered ``continue: false`` on a bridge-served call; the loop that
# owns the CLI decides what ending the turn means.
OnStop = Callable[[str], Awaitable[None]]
# Environment entry the child reads its bridge token from.
TOKEN_ENV = "FLUID_BUNDLED_TOKEN"
_UNAUTHORIZED = {"error": "unauthorized: bridge token missing or wrong"}
# Most base64 image data one call result carries across the bridge. Half of
# STREAM_LINE_LIMIT, so the JSON line with its text still fits; an image that
# would pass it is dropped and the result text says so.
IMAGE_BUDGET_CHARS = 8 * 1024 * 1024
# A listed tool's ``_meta`` key that overrides its server's ``alwaysLoad``.
# Claude Code (2.1.285+) keeps a tool that sets it to false deferred behind
# ToolSearch even though its ``--mcp-config`` server is ``alwaysLoad``; older
# CLIs ignore the key and load the tool, the behaviour before tiering.
ALWAYS_LOAD_META_KEY = "anthropic/alwaysLoad"
# Where Claude Code puts the calling tool_use id in a tools/call request's
# ``_meta`` (seen in 2.1.285). The child forwards it so the parent's hooks and
# host tools can key on the same id the CLI's transcript uses.
TOOL_USE_ID_META_KEY = "claudecode/toolUseId"
# An HTTP route's handler: the decoded JSON body in, nothing out. A
# ``ValueError`` means the body was refused (400).
HttpRoute = Callable[[Any], Awaitable[None]]
# Largest HTTP body and header block an HTTP route reads, and how long one
# request may take to arrive.
HTTP_MAX_BODY_BYTES = 16 * 1024
_HTTP_MAX_HEADER_BYTES = 8 * 1024
_HTTP_READ_TIMEOUT = 5.0
_HTTP_REQUEST_LINE = re.compile(rb"^([A-Z]{3,7}) (/[!-~]*) HTTP/1\.[01]\r?\n$")
_HTTP_REASONS = {
    204: "No Content", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 411: "Length Required",
    413: "Content Too Large", 415: "Unsupported Media Type",
    431: "Request Header Fields Too Large",
}


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
    on_stop: OnStop | None = None,
    http_token: str | None = None,
    http_routes: dict[str, HttpRoute] | None = None,
    deferred: Callable[[str], bool] | None = None,
) -> asyncio.AbstractServer:
    """Serve ``list``/``call`` requests against ``registry`` over a socket.

    ``socket_path`` is either a unix socket path or ``"tcp:<host>:<port>"``
    (port ``0`` binds an ephemeral port — read it back off
    ``server.sockets[0].getsockname()[1]``).

    ``token``: when set, every request must carry it as ``"token"`` or the
    connection is answered with an error and closed. ``DelegatedLoop`` always
    sets one; ``None`` exists for in-process tests of the wire format.
    ``tool_filter``, ``permissions`` and ``hooks`` gate the registry the way
    the native loop's dispatch path does (see ``_answer``); ``on_stop`` is
    told when a hook ends the turn.

    ``deferred`` names the tools the CLI should keep behind its ToolSearch:
    each is still listed and callable, with ``_meta`` ``anthropic/alwaysLoad``
    false so only its name reaches the prompt until the model loads it.

    ``http_routes`` (with ``http_token``, required for them) maps a path to
    the handler of a JSON ``POST`` there; see ``_serve_http``.

    Lines on this socket carry whole tool arguments and whole results, so
    both sides read with the same cap as the CLI's stdout.

    Returns the started ``asyncio`` server; the caller owns its lifetime
    (``server.close()`` + ``await server.wait_closed()``).
    """
    ctx = ctx or ToolCtx()
    routes = http_routes if http_token else None

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = True
            while True:
                line = await reader.readline()
                if not line:
                    return
                if first and routes and _HTTP_REQUEST_LINE.match(line):
                    assert http_token is not None
                    await _serve_http(line, reader, writer, http_token, routes)
                    return
                first = False
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
                    on_stop=on_stop, deferred=deferred,
                )
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()

    tcp = _parse_address(socket_path)
    if tcp is not None:
        host, port = tcp
        return await asyncio.start_server(
            handle, host=host, port=port, limit=STREAM_LINE_LIMIT,
        )
    return await asyncio.start_unix_server(
        handle, path=socket_path, limit=STREAM_LINE_LIMIT,
    )


def _peer_is_local(writer: asyncio.StreamWriter) -> bool:
    """True for a unix-socket peer or a loopback TCP peer."""
    peer = writer.get_extra_info("peername")
    if not isinstance(peer, tuple) or not peer:
        return True  # a unix domain socket: the filesystem scopes it
    try:
        return ipaddress.ip_address(str(peer[0]).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


async def _respond_http(writer: asyncio.StreamWriter, status: int) -> None:
    reason = _HTTP_REASONS.get(status, "")
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()


async def _serve_http(
    request_line: bytes,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    token: str,
    routes: dict[str, HttpRoute],
) -> None:
    """Answer one HTTP/1.1 request on a bridge connection, then return.

    Only ``POST`` of a JSON body to a path in ``routes``, from a local peer,
    with ``Authorization: Bearer <token>``. The handler gets the decoded body;
    its ``ValueError`` is a 400, any other failure is logged and still a 204,
    since the body was accepted and the caller has nothing to retry.
    """
    match = _HTTP_REQUEST_LINE.match(request_line)
    assert match is not None
    method, target = match.group(1).decode(), match.group(2).decode()
    headers: dict[str, str] = {}
    size = 0
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), _HTTP_READ_TIMEOUT)
            size += len(line)
            if size > _HTTP_MAX_HEADER_BYTES:
                await _respond_http(writer, 431)
                return
            if line in (b"\r\n", b"\n"):
                break
            if not line:
                return
            name, sep, value = line.decode("latin-1").partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        if not _peer_is_local(writer):
            await _respond_http(writer, 403)
            return
        auth = headers.get("authorization", "")
        presented = auth[7:] if auth[:7].lower() == "bearer " else None
        if not _token_ok(token, presented):
            await _respond_http(writer, 401)
            return
        handler = routes.get(target.split("?", 1)[0])
        if handler is None:
            await _respond_http(writer, 404)
            return
        if method != "POST":
            await _respond_http(writer, 405)
            return
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            await _respond_http(writer, 415)
            return
        length_text = headers.get("content-length", "")
        if not length_text.isdigit():
            await _respond_http(writer, 411)
            return
        length = int(length_text)
        if length > HTTP_MAX_BODY_BYTES:
            await _respond_http(writer, 413)
            return
        raw = await asyncio.wait_for(reader.readexactly(length), _HTTP_READ_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
        return
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        await _respond_http(writer, 400)
        return
    try:
        await handler(body)
    except ValueError as exc:
        logger.debug("bridge refused a %s body: %s", target, exc)
        await _respond_http(writer, 400)
        return
    except Exception:  # noqa: BLE001 — a host failure is the host's, not the caller's
        logger.warning("bridge route %s failed", target, exc_info=True)
    await _respond_http(writer, 204)


def _token_ok(expected: str, presented: Any) -> bool:
    return isinstance(presented, str) and hmac.compare_digest(
        expected.encode(), presented.encode()
    )


def _permitted(filt: Callable[[str], bool] | None, name: str) -> bool:
    return filt(name) if filt is not None else True


class _CallExtra(MutableMapping[str, Any]):
    """``ctx.extra`` as one bridge call sees it.

    Reads find the call's own keys (its ``tool_use_id``) first, then the
    session's. Writes of any other key go to the session's dict, so state a
    handler keeps there outlives the call as it does on the native loop. The
    bridge serves concurrent calls on one shared ctx, which is why the call's
    keys cannot be written into that dict.
    """

    def __init__(self, base: dict[str, Any], own: dict[str, Any]) -> None:
        self._base = base
        self._own = own

    def __getitem__(self, key: str) -> Any:
        if key in self._own:
            return self._own[key]
        return self._base[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._own:
            self._own[key] = value
        else:
            self._base[key] = value

    def __delitem__(self, key: str) -> None:
        if key in self._own:
            del self._own[key]
        else:
            del self._base[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._own
        yield from (k for k in self._base if k not in self._own)

    def __len__(self) -> int:
        return len(self._own) + sum(1 for k in self._base if k not in self._own)


def _call_ctx(ctx: ToolCtx, tool_use_id: str | None) -> ToolCtx:
    """A shallow copy of ``ctx`` whose ``extra`` carries this call's id.

    The copy shares every per-session registry (read_files, read_seen, ...)
    with ``ctx``. ``ToolRegistry.dispatch`` publishes the id as
    ``current_tool_use_id`` for handlers that take no ctx.
    """
    if not tool_use_id:
        return ctx
    extra = _CallExtra(ctx.extra, {"tool_use_id": tool_use_id})
    return replace(ctx, extra=extra)  # type: ignore[arg-type]


async def _gate_call(
    registry: ToolRegistry, ctx: ToolCtx, name: str, args: dict[str, Any],
    *,
    permissions: PermissionEngine | None,
    hooks: HookBus | None,
    on_stop: OnStop | None = None,
    tool_use_id: str | None = None,
) -> ToolOutcome:
    """PreToolUse hook → PermissionEngine → dispatch → PostToolUse, the order
    ``NativeLoop._gated_call`` uses, for a caller with no user on the far
    end: an ``ask`` verdict has nobody to ask and becomes the non-interactive
    denial.

    The hook contract is the native loop's: a PreToolUse ``deny`` or
    ``stop`` refuses the call (``stop`` also ends the turn through
    ``on_stop``), a ``permissionDecision`` of ``allow`` skips the permission
    gate and ``ask`` forces one, ``updatedInput`` replaces the arguments; a
    PostToolUse ``deny`` or ``additionalContext`` rides back to the model
    appended to the result, and a PostToolUse ``stop`` ends the turn after
    it.

    ``tool_use_id`` is the CLI's id for the call when its request carried
    one. It goes into every hook payload and into the handler's
    ``ctx.extra["tool_use_id"]``.
    """
    from fluid.hooks.bus import POST_TOOL_USE, PRE_TOOL_USE, hook_reminder
    from fluid.permissions.engine import non_interactive_denial

    hook_permission: str | None = None
    hook_reason: str | None = None
    if hooks is not None:
        decision = await hooks.emit(
            PRE_TOOL_USE, {"name": name, "input": args, "tool_use_id": tool_use_id},
        )
        if decision.action == "deny" or decision.stop:
            reason = decision.note or ("stopped by hook" if decision.stop else "denied")
            if decision.stop and on_stop is not None:
                await on_stop(reason)
            return ToolOutcome(text=f"blocked by hook: {reason}", is_error=True)
        if decision.action == "modify" and decision.replacement_input is not None:
            args = decision.replacement_input
        hook_permission = decision.permission_decision
        hook_reason = decision.note
    spec = registry.get(name)
    if permissions is not None and spec is not None and hook_permission != "allow":
        decided = permissions.decide(spec, args, cwd=ctx.cwd)
        verdict = decided.decision
        deny_message: str | None = decided.deny_message()
        if verdict == "allow" and permissions.note_execution(name, args):
            verdict = "deny"
            deny_message = non_interactive_denial(
                name, "doom-loop guard: this exact tool call was repeated "
                "several times in a row",
            )
        elif verdict == "allow" and hook_permission == "ask":
            verdict = "deny"
            deny_message = non_interactive_denial(
                name, hook_reason or "a PreToolUse hook asked for confirmation",
            )
        elif verdict == "ask":
            verdict = "deny"
            deny_message = non_interactive_denial(name)
        if verdict == "deny":
            return ToolOutcome(text=deny_message or "permission denied", is_error=True)
    if hooks is not None:
        # The CLI only calls a tool it allowed (its own rules, hooks and
        # prompts) and this gate let it through: observers hear of it
        # before it runs, as on the native loop.
        await hooks.tool_allowed({
            "session_id": ctx.session_id, "name": name, "input": args,
            "tool_use_id": tool_use_id,
        })
    outcome = await registry.dispatch(name, args, _call_ctx(ctx, tool_use_id))
    if hooks is not None:
        decision = await hooks.emit(
            POST_TOOL_USE,
            {"name": name, "tool_use_id": tool_use_id, "input": args,
             "output": outcome.text, "is_error": outcome.is_error},
        )
        additions: list[str] = []
        if decision.stop:
            reason = decision.note or "stopped by hook"
            if on_stop is not None:
                await on_stop(reason)
        elif decision.action == "deny":
            additions.append(hook_reminder(
                "PostToolUse hook feedback", decision.note or "blocked by hook",
            ))
        if decision.additional_context:
            additions.append(hook_reminder(
                "PostToolUse hook context", decision.additional_context,
            ))
        if additions:
            outcome = replace(
                outcome, text="\n\n".join([outcome.text, *additions]).strip(),
                content_id=None, window_of=None,
            )
    return outcome


async def _answer(
    registry: ToolRegistry, ctx: ToolCtx, req: dict[str, Any],
    *,
    tool_filter: Callable[[str], bool] | None = None,
    permissions: PermissionEngine | None = None,
    hooks: HookBus | None = None,
    on_stop: OnStop | None = None,
    deferred: Callable[[str], bool] | None = None,
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
        tools: list[dict[str, Any]] = []
        for s in specs:
            entry: dict[str, Any] = {
                "name": s.name, "description": s.description, "input_schema": s.input_schema,
            }
            if deferred is not None and deferred(s.name):
                entry["meta"] = {ALWAYS_LOAD_META_KEY: False}
            tools.append(entry)
        return {"tools": tools}
    if op == "call":
        name = req.get("name", "")
        if not _permitted(tool_filter, name):
            return {"text": f"tool not available in this session: {name}", "is_error": True}
        tool_use_id = req.get("tool_use_id")
        outcome = await _gate_call(
            registry, ctx, name, req.get("arguments") or {},
            permissions=permissions, hooks=hooks, on_stop=on_stop,
            tool_use_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None,
        )
        # MRTR: the tool paused on input_required (an MCP server or a
        # builtin/in-proc tool). This bridge is a request/response socket with no event
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
        return _call_answer(outcome)
    return {"error": f"unknown op: {op!r}"}


def _call_answer(outcome: ToolOutcome) -> dict[str, Any]:
    """The socket reply for one call: text, error flag and any images.

    Images ride as ``{"media_type", "data"}`` with ``data`` base64, at most
    ``IMAGE_BUDGET_CHARS`` of it per result. An image past the budget is
    dropped and a line in the text says so, so the model knows it is missing
    rather than seeing a result that silently lacks it. A result without
    images keeps the two-key shape.
    """
    resp: dict[str, Any] = {"text": outcome.text, "is_error": outcome.is_error}
    if not outcome.images:
        return resp
    images: list[dict[str, str]] = []
    omitted: list[str] = []
    used = 0
    for image in outcome.images:
        data = image.data
        if isinstance(data, (bytes, bytearray)):
            data = base64.b64encode(data).decode("ascii")
        if not isinstance(data, str) or not data:
            continue
        if used + len(data) > IMAGE_BUDGET_CHARS:
            omitted.append(
                f"[image omitted: {image.media_type}, about "
                f"{len(data) * 3 // 4} bytes, too large to return through the "
                "tool bridge]"
            )
            continue
        used += len(data)
        images.append({"media_type": image.media_type, "data": data})
    if images:
        resp["images"] = images
    if omitted:
        resp["text"] = "\n".join([outcome.text, *omitted]) if outcome.text else "\n".join(omitted)
    return resp


# ─── Config generation ───────────────────────────────────────────────────────


def bundled_server_config(socket_path: str, token: str | None = None) -> dict[str, Any]:
    """The ``.mcp.json`` entry that launches the child bridge for this address.

    ``socket_path`` must be the *resolved* address — for the TCP form that means
    the real bound port, not ``tcp:127.0.0.1:0``. ``token`` rides in the
    entry's ``env`` (not ``args``: a process list is world-readable).

    ``alwaysLoad`` keeps the bridge's tools in every request. Without it a
    claude CLI that has ToolSearch defers every MCP tool behind it (the
    default since 2.1.x, not threshold based), so the model would have to
    search before calling the tools its prompt names. Older CLIs drop the key.
    It stays on even when the host defers some tools: those carry their own
    ``_meta`` ``anthropic/alwaysLoad`` false in the tool list (see
    ``serve_registry_socket``), which 2.1.285+ honours per tool, so a CLI too
    old for the per-tool key still loads everything rather than nothing.
    """
    config: dict[str, Any] = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "fluid.tools.mcp.bundled_server", "--socket", socket_path],
        "alwaysLoad": True,
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
    cli_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Write an ``.mcp.json``-style config and return it.

    Includes the bundled server (only when the registry actually exposes
    inproc/builtin tools that pass ``tool_filter``) plus any ``user_servers``,
    each rewritten from fluid's config shape into the CLI's.
    """
    servers: dict[str, Any] = {}
    specs = [
        s for s in registry.list_tools(loaded_only=True)
        if s.source in ("inproc", "builtin") and _permitted(tool_filter, s.name)
    ]
    if specs:
        servers[server_name] = bundled_server_config(socket_path, token)
    for name, server in (user_servers or {}).items():
        servers[name] = cli_server_entry(server)
    if cli_env is not None:
        servers = {name: isolated_cli_server_entry(server, cli_env)
                   for name, server in servers.items()}
    config = {"mcpServers": servers}
    Path(path).write_text(json.dumps(config, indent=2))
    return config


def isolated_cli_server_entry(server: dict[str, Any], cli_env: dict[str, str]) -> dict[str, Any]:
    """Keep Claude-only credentials out of even CLI-spawned stdio MCPs.

    The CLI overlays MCP env rather than replacing its inherited environment.
    Mask the auth values before the trampoline starts, then remove the empty
    names in the trampoline. No secret is written into the MCP config.
    """
    from fluid.tools.child_env import is_claude_auth, scrub_claude_auth_env

    entry = dict(server)
    overlay = dict(entry.get("env") or {})
    names = {n for n in cli_env.keys() | overlay.keys() if is_claude_auth(n)}
    if names and cli_env.get("ANTHROPIC_API_KEY"):
        names.add("ANTHROPIC_API_KEY")
    if entry.get("type", "stdio") != "stdio" or not names:
        return entry
    entry["env"] = {**scrub_claude_auth_env(overlay), **dict.fromkeys(names, "")}
    entry["args"] = ["-m", "fluid.tools.child_env", entry["command"], *entry.get("args", [])]
    entry["command"] = sys.executable
    return entry


def cli_server_entry(server: dict[str, Any]) -> dict[str, Any]:
    """A fluid ``[mcp.<name>]`` entry in the CLI's ``.mcp.json`` shape.

    fluid names the transport ``transport`` (``stdio``, ``http``,
    ``streamable_http``); the CLI names it ``type`` and calls the streamable
    transport plain ``http``. An entry that already carries ``type`` is left
    as written.
    """
    entry = dict(server)
    transport = entry.pop("transport", None)
    if "type" not in entry:
        if transport in (None, "stdio"):
            entry["type"] = "stdio"
        elif transport == "streamable_http":
            entry["type"] = "http"
        else:
            entry["type"] = str(transport)
    return entry


# ─── Child side: MCP stdio server proxying to the parent socket ──────────────


async def _socket_rpc(
    socket_path: str, req: dict[str, Any], token: str | None = None,
) -> dict[str, Any]:
    """One request/response round-trip against the parent's bridge socket."""
    tcp = _parse_address(socket_path)
    if tcp is not None:
        reader, writer = await asyncio.open_connection(*tcp, limit=STREAM_LINE_LIMIT)
    else:
        reader, writer = await asyncio.open_unix_connection(
            socket_path, limit=STREAM_LINE_LIMIT,
        )
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
    """A ``mcp.types.Tool`` for one ``{"op":"list"}`` entry from the parent.

    The entry's ``meta`` (the deferral marker) becomes the tool's ``_meta``.
    An SDK whose Tool model takes no ``_meta`` gets the tool without it,
    which the CLI then loads like any other.
    """
    key = _field_name(types_mod.Tool, "inputSchema", "input_schema")
    fields: dict[str, Any] = {
        "name": spec["name"],
        "description": spec.get("description", ""),
        key: spec.get("input_schema") or {"type": "object"},
    }
    meta = spec.get("meta")
    if isinstance(meta, dict) and meta:
        try:
            return types_mod.Tool(**fields, _meta=meta)
        except Exception:  # noqa: BLE001 — a model without _meta: list it loaded
            logger.debug("mcp Tool takes no _meta; %s is listed loaded", spec["name"])
    return types_mod.Tool(**fields)


def _reply_result(types_mod: Any, data: dict[str, Any]) -> Any:
    """The ``CallToolResult`` for one ``{"op":"call"}`` reply from the parent."""
    text = data.get("text", "")
    images = data.get("images")
    return _call_result(
        types_mod, text if isinstance(text, str) else str(text),
        bool(data.get("is_error")),
        images if isinstance(images, list) else (),
    )


def _call_result(
    types_mod: Any, text: str, is_error: bool, images: Any = (),
) -> Any:
    """A ``mcp.types.CallToolResult``: one text block, then any images.

    ``images`` are the parent's ``{"media_type", "data"}`` entries, each
    becoming an ``ImageContent`` after the text block. A result with images
    and no text leaves the empty text block out.
    """
    images = [
        img for img in images
        if isinstance(img, dict)
        and isinstance(img.get("data"), str) and img["data"]
    ]
    content: list[Any] = []
    if text or not images:
        content.append(types_mod.TextContent(type="text", text=text))
    if images:
        mime_key = _field_name(types_mod.ImageContent, "mimeType", "mime_type")
        for img in images:
            media_type = img.get("media_type")
            content.append(types_mod.ImageContent(
                type="image", data=img["data"],
                **{mime_key: media_type if isinstance(media_type, str) and media_type
                   else "image/png"},
            ))
    key = _field_name(types_mod.CallToolResult, "isError", "is_error")
    return types_mod.CallToolResult(content=content, **{key: is_error})


def _tool_use_id_from_meta(meta: Any) -> str | None:
    """The CLI's tool_use id from a tools/call request's ``_meta``, if any.

    ``meta`` is the request's meta model (extra fields allowed) or a plain
    mapping; the id is an extra field named ``TOOL_USE_ID_META_KEY``.
    """
    if meta is None:
        return None
    if isinstance(meta, dict):
        value = meta.get(TOOL_USE_ID_META_KEY)
    else:
        extra = getattr(meta, "model_extra", None) or {}
        value = extra.get(TOOL_USE_ID_META_KEY)
        if value is None:
            value = getattr(meta, TOOL_USE_ID_META_KEY, None)
    return value if isinstance(value, str) and value else None


def _call_request(
    name: str, arguments: dict[str, Any] | None, tool_use_id: str | None,
) -> dict[str, Any]:
    """The ``{"op":"call"}`` socket request; ``tool_use_id`` only when known."""
    req: dict[str, Any] = {"op": "call", "name": name, "arguments": arguments or {}}
    if tool_use_id:
        req["tool_use_id"] = tool_use_id
    return req


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
            tool_use_id = _tool_use_id_from_meta(getattr(params, "meta", None))
            if tool_use_id is None:
                tool_use_id = _tool_use_id_from_meta(getattr(ctx, "meta", None))
            data = await _socket_rpc(
                socket_path,
                _call_request(params.name, params.arguments, tool_use_id),
                token,
            )
            # Build the CallToolResult ourselves so the parent's is_error
            # survives instead of being flattened into a success.
            return _reply_result(t, data)

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
        # mcp 1.x hands the handler only name/arguments; the request's
        # ``_meta`` lives on the per-request context, which is unset when the
        # handler is driven outside a session (tests).
        try:
            meta = server.request_context.meta
        except LookupError:
            meta = None
        data = await _socket_rpc(
            socket_path, _call_request(name, arguments, _tool_use_id_from_meta(meta)), token,
        )
        # Returning a CallToolResult (rather than bare content) is what lets
        # the parent's is_error survive: the decorator's own normalization path
        # always reports success.
        return _reply_result(t, data)

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
