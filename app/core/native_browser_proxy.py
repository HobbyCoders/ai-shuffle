"""Native browser tools without one Node discovery/proxy process per session.

Only the app-managed proxy config is eligible. The build exports its metadata
catalog; absent/invalid catalogs fall back to STDIO, so source-only installs do
not lose browser tools. Calls still use the exact token-authenticated RPC route
(the same origin guard, session validation, actor ownership and SSRF policy),
not a direct runtime shortcut. Runtime startup remains demand-driven.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from functools import lru_cache
from pathlib import Path

import httpx
from fluid.tools.inproc import InprocTool, create_inproc_server

MAX_PAYLOAD = 262144
MAX_RESPONSE = 16 * 1024 * 1024
TIMEOUT_SECONDS = 90
_UNAVAILABLE = "Browser runtime unavailable; outcome unknown, not replayed"


def _unavailable(status: int) -> str:
    return f"Browser runtime unavailable (HTTP {int(status)}); outcome unknown, not replayed"


def _error(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


@lru_cache(maxsize=8)
def _catalog(path: str, modified: int, size: int):
    # mtime/size are cache keys, so an in-place development build is not stale.
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tools = data.get("tools")
    if (
        data.get("version") != 1
        or not isinstance(tools, list)
        or not 1 <= len(tools) <= 64
    ):
        raise ValueError("Invalid browser catalog")
    names = set()
    for tool in tools:
        name = tool.get("name")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"browser_[a-z_]+", name)
            or name in names
            or not isinstance(tool.get("description"), str)
            or not isinstance(tool.get("inputSchema"), dict)
            or tool["inputSchema"].get("type") != "object"
        ):
            raise ValueError("Invalid browser tool metadata")
        names.add(name)
    # The server's MCP ``instructions`` (its operating rules: refs, stale
    # screenshots, human control). The STDIO server sends them at initialize;
    # this path has no handshake, so the build exports them here.
    instructions = data.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        instructions = None
    return tools, instructions


async def forward_tool(name: str, arguments: dict, env: dict):
    """Bounded, non-replaying forwarding, equivalent to browser-mcp proxy.ts."""
    try:
        session_id = env["AIS_SESSION_ID"]
        token = env["AIS_PANEL_TOKEN"]
        url = httpx.URL(env["AIS_PANEL_REGISTER_URL"])
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("Invalid browser RPC URL")
        url = url.copy_with(
            path=re.sub(r"/register/?$", "/rpc", url.path), query=None, fragment=None
        )
        origin = str(url.copy_with(path="/", query=None, fragment=None)).rstrip("/")
        body = json.dumps(
            {
                "session_id": session_id,
                "token": token,
                "tool": name,
                "arguments": arguments,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_PAYLOAD:
            return _error("Browser payload too large")
        # No retries or redirects: an admitted browser mutation may have run.
        async with (
            asyncio.timeout(TIMEOUT_SECONDS),
            httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as client,
            client.stream(
                "POST",
                url,
                content=body,
                headers={"content-type": "application/json", "origin": origin},
            ) as response,
        ):
            if not response.is_success:
                # The status alone is safe to show and tells a refused token
                # (403), a gone session (404) and a failed runtime (503) apart.
                return _error(_unavailable(response.status_code))
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > MAX_RESPONSE:
                    return _error(_UNAVAILABLE)
                raw.extend(chunk)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise TypeError("Invalid MCP response")
            return result
    except Exception:  # noqa: BLE001 -- never leak routing/token details or replay a mutation
        # Never expose worker URLs, tokens, or response bodies in an error.
        return _error(_UNAVAILABLE)


def register_native_browser_proxy(registry, config) -> bool:
    """Return True only when the managed STDIO helper has been replaced."""
    from app.core.builtin_mcp_servers import _browser_mcp_entry

    if (
        not isinstance(config, dict)
        or config.get("command") != "node"
        or config.get("type", "stdio") != "stdio"
        or config.get("transport", "stdio") != "stdio"
    ):
        return False
    env = config.get("env") or {}
    args = config.get("args") or []
    if not isinstance(env, dict) or not isinstance(args, list):
        return False
    if (
        env.get("AIS_BROWSER_MODE") != "proxy"
        or len(args) != 1
        or not all(
            env.get(key)
            for key in ("AIS_SESSION_ID", "AIS_PANEL_TOKEN", "AIS_PANEL_REGISTER_URL")
        )
    ):
        return False
    try:
        entry = Path(_browser_mcp_entry()).resolve()
        if Path(args[0]).resolve() != entry:
            return False  # Never replace a user/external MCP server.
        path = entry.with_name("catalog.json")
        stat = path.stat()
        if stat.st_size > MAX_PAYLOAD:
            return False
        catalog, instructions = _catalog(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, TypeError, AttributeError):
        return False

    # Retain only routing fields, not provider credentials from the child env.
    routing = {
        key: env[key]
        for key in ("AIS_SESSION_ID", "AIS_PANEL_TOKEN", "AIS_PANEL_REGISTER_URL")
    }

    def handler(name):
        async def call(arguments):
            from dataclasses import replace

            from fluid.tools.hardening import (
                elision_notice,
                fence_untrusted,
                scrub_error_text,
            )
            from fluid.tools.registry import _normalize

            outcome = _normalize(await forward_tool(name, arguments, routing))
            # Transport is local; page content is not. Preserve dispatch's MCP
            # hardening instead of treating browser output as trusted host text.
            if outcome.is_error:
                return replace(outcome, text=scrub_error_text(outcome.text))
            if outcome.text:
                text = outcome.text
                notice = elision_notice(text)
                if notice:
                    text = f"{text}\n\n{notice}"
                return replace(
                    outcome, text=fence_untrusted(text, f"mcp__browser__{name}")
                )
            return outcome

        return call

    # Match connect_mcp's collision rule: an external server cannot replace an
    # existing builtin or in-process tool and inherit its loaded/safe status.
    reserved = {
        tool.name
        for tool in registry.list_tools(loaded_only=False)
        if tool.source != "mcp"
    }
    tools = [
        InprocTool(
            name=tool["name"],
            description=tool["description"],
            input_schema=copy.deepcopy(tool["inputSchema"]),
            handler=handler(tool["name"]),
            risk="write",
        )
        for tool in catalog
        if f"mcp__browser__{tool['name']}" not in reserved
    ]
    registry.register_inproc(create_inproc_server("browser", tools), defer=True)
    setter = getattr(registry, "set_server_instructions", None)
    if setter is not None:
        setter("browser", instructions)
    return True
